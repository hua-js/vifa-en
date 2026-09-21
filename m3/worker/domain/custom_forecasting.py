"""StatsForecast selection and prediction for configurable M3 intervals."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib.metadata import version
import logging
from statistics import median
from time import monotonic

import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoARIMA, AutoETS, MSTL, SeasonalNaive

from m3.worker.contracts import is_load_series
from m3.worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastPoint,
    CustomForecastSeries,
    validate_custom_series_for_config,
)
from m3.worker.domain.custom_load_profiles import (
    LoadCandidateScore,
    eligible_load_models,
    load_candidate_score,
    weekly_profile_values,
)
from m3.worker.domain.custom_training_data import CustomTrainingDataset
from m3.worker.domain.work_schedule import schedule_forecast, schedule_slot, schedule_day
from m3.worker.errors import M3Error


logger = logging.getLogger(__name__)


SOC_WEEKLY_DELTA_MODEL = "SOCWeeklyDelta"
SOC_SCHEDULE_DELTA_MODEL = "SOCScheduleDelta"
SOC_SCHEDULE_POLICY = "soc-schedule-delta-v2"
SOC_FIVE_MINUTE_LINEAR_SUFFIX = "5mLinear"
SOC_MODEL_ORDER = (
    SOC_WEEKLY_DELTA_MODEL,
    SOC_SCHEDULE_DELTA_MODEL,
    "SeasonalNaive",
    "AutoETS",
    "AutoARIMA",
    "MSTL",
)


def _candidate_factories(config: CustomForecastConfig):
    daily = config.daily_season_length
    weekly = config.weekly_season_length
    return [
        (
            "SeasonalNaive",
            lambda: SeasonalNaive(season_length=daily, alias="SeasonalNaive"),
        ),
        ("AutoETS", lambda: AutoETS(season_length=daily, alias="AutoETS")),
        ("AutoARIMA", lambda: AutoARIMA(season_length=daily, alias="AutoARIMA")),
        (
            "MSTL",
            lambda: MSTL(
                season_length=[daily, weekly],
                trend_forecaster=AutoARIMA(),
                alias="MSTL",
            ),
        ),
    ]


@dataclass(frozen=True)
class CustomChampion:
    model_name: str
    cv_mape_percent: float | None
    selected_at: pd.Timestamp
    training_start: pd.Timestamp
    training_end: pd.Timestamp
    statsforecast_version: str
    selection_reason: str | None = None
    candidate_scores: tuple[LoadCandidateScore, ...] = ()
    selection_metric: str | None = None
    selection_status: str | None = None


def seasonal_naive_champion(
    dataset: CustomTrainingDataset, selection_reason: str | None = None
) -> CustomChampion:
    """Baseline factory; SOC uses matched-day changes instead of daily copying."""
    return CustomChampion(
        model_name=("SeasonalNaive" if is_load_series(dataset.frame["unique_id"].iloc[0])
                    else SOC_SCHEDULE_DELTA_MODEL),
        cv_mape_percent=None,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        selection_reason=selection_reason,
    )


def weekly_naive_champion(
    dataset: CustomTrainingDataset,
    selection_reason: str | None = None,
    selection_status: str | None = None,
) -> CustomChampion:
    resolved_status = selection_status
    if resolved_status is None and 0 < len(dataset.usable_weeks) < 3:
        resolved_status = "warming_up"
    return CustomChampion(
        model_name="WeeklyNaive",
        cv_mape_percent=None,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        selection_reason=selection_reason,
        selection_status=resolved_status,
    )


def select_load_champion(
    dataset: CustomTrainingDataset, config: CustomForecastConfig
) -> CustomChampion:
    usable_week_count = len(dataset.usable_weeks)
    if usable_week_count == 0:
        latest_week_start = (
            config.history_end - config.weekly_season_length * config.interval
        )
        latest_week = dataset.frame[
            (dataset.frame["ds"] >= latest_week_start)
            & (dataset.frame["ds"] < config.history_end)
        ]
        expected_times = list(
            pd.date_range(
                latest_week_start,
                periods=config.weekly_season_length,
                freq=config.pandas_frequency,
            )
        )
        observed_times = [pd.Timestamp(value) for value in latest_week["ds"]]
        if (
            len(latest_week) == config.weekly_season_length
            and observed_times == expected_times
            and not latest_week["y"].isna().any()
        ):
            return weekly_naive_champion(
                dataset,
                selection_reason="latest_week_high_imputation",
                selection_status="degraded",
            )
        raise M3Error(
            "insufficient_history", "insufficient_history: no complete usable load weeks"
        )
    if usable_week_count < 3:
        return weekly_naive_champion(
            dataset,
            selection_reason="fewer_than_three_usable_weeks",
            selection_status="warming_up",
        )

    latest_week = dataset.usable_weeks[-1]
    holdout = dataset.frame[
        (dataset.frame["ds"] >= latest_week.start)
        & (dataset.frame["ds"] < latest_week.end)
    ]
    training = replace(
        dataset,
        frame=dataset.frame[dataset.frame["ds"] < latest_week.start].copy(),
    )
    unique_id = dataset.frame["unique_id"].iloc[0]
    excluded_times = frozenset(
        timestamp
        for key_unique_id, timestamp in dataset.imputed_keys
        if key_unique_id == unique_id
    )
    scores: list[LoadCandidateScore] = []
    for model_name in eligible_load_models(usable_week_count, config.interval_seconds):
        started = monotonic()
        logger.info(
            "m3_custom_forecast_candidate_started "
            "series=station_total_load model=%s",
            model_name,
        )
        try:
            if model_name.startswith("Weekly"):
                predictions = weekly_profile_values(
                    training,
                    model_name,
                    origin=latest_week.start,
                    periods=len(holdout),
                )
            else:
                engine = StatsForecast(
                    models=[_load_automatic_model(model_name, config)],
                    freq=config.pandas_frequency,
                    n_jobs=1,
                )
                forecast = schedule_forecast(
                    engine, training.frame, origin=latest_week.start,
                    periods=len(holdout), model_name=model_name,
                )
                if list(forecast["ds"]) != list(holdout["ds"]):
                    raise M3Error("forecast_alignment_invalid", "Load holdout is misaligned")
                predictions = forecast[model_name].tolist()
            score = load_candidate_score(
                model_name, holdout, predictions, excluded_times
            )
            scores.append(score)
            logger.info(
                "m3_custom_forecast_candidate_finished "
                "series=station_total_load "
                "model=%s status=ok elapsed_ms=%d",
                model_name,
                int((monotonic() - started) * 1000),
            )
        except Exception as error:
            scores.append(
                LoadCandidateScore(
                    model_name,
                    None,
                    None,
                    None,
                    0,
                    type(error).__name__,
                )
            )
            logger.warning(
                "m3_custom_forecast_candidate_finished "
                "series=station_total_load "
                "model=%s status=failed error_type=%s elapsed_ms=%d",
                model_name,
                type(error).__name__,
                int((monotonic() - started) * 1000),
            )

    viable_scores = [score for score in scores if score.mae is not None]
    if not viable_scores:
        raise M3Error("model_selection_failed", "all load candidates failed")
    winner = min(viable_scores, key=lambda score: score.comparison_key)
    selection_metric = (
        "wape_percent" if winner.wape_percent is not None else "mae"
    )
    return CustomChampion(
        model_name=winner.model_name,
        cv_mape_percent=winner.mape_percent,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        selection_reason=(
            None if winner.wape_percent is not None else "wape_unavailable"
        ),
        candidate_scores=tuple(scores),
        selection_metric=selection_metric,
    )


def _load_automatic_model(name: str, config: CustomForecastConfig):
    if name == "AutoARIMA":
        return _bounded_autoarima(
            season_length=config.daily_season_length,
            alias="AutoARIMA",
        )
    if name == "MSTL":
        return MSTL(
            season_length=[config.daily_season_length, config.weekly_season_length],
            trend_forecaster=_bounded_autoarima(),
            alias="MSTL",
        )
    raise ValueError(f"unsupported load automatic model: {name}")


def _bounded_autoarima(
    *, season_length: int = 1, alias: str = "AutoARIMA"
) -> AutoARIMA:
    return AutoARIMA(
        season_length=season_length,
        approximation=True,
        nmodels=20,
        max_p=3,
        max_q=3,
        max_P=1,
        max_Q=1,
        alias=alias,
    )


def _select_soc_champion(
    dataset: CustomTrainingDataset, config: CustomForecastConfig
) -> CustomChampion:
    if dataset.interval_seconds != config.interval_seconds:
        raise M3Error("training_interval_invalid", "Training interval does not match")
    if dataset.mode == "insufficient":
        raise M3Error(
            "insufficient_history", "fewer than seven complete training days"
        )
    if config.model_policy == "seasonal_naive_only":
        return seasonal_naive_champion(dataset)

    holdout_start = config.history_end - timedelta(days=7)
    holdout = dataset.frame[
        (dataset.frame["ds"] >= holdout_start)
        & (dataset.frame["ds"] < config.history_end)
    ].copy()
    expected_times = list(
        pd.date_range(
            holdout_start,
            periods=config.weekly_season_length,
            freq=config.pandas_frequency,
        )
    )
    observed_times = [pd.Timestamp(value) for value in holdout["ds"]]
    if (
        len(holdout) != config.weekly_season_length
        or observed_times != expected_times
        or holdout["y"].isna().any()
    ):
        raise M3Error(
            "model_selection_failed", "SOC holdout week is incomplete"
        )
    training_frame = dataset.frame[dataset.frame["ds"] < holdout_start].copy()
    if training_frame.empty:
        raise M3Error("model_selection_failed", "SOC holdout training is empty")
    training = replace(
        dataset,
        frame=training_frame,
        end=pd.Timestamp(training_frame["ds"].iloc[-1]).to_pydatetime(),
    )
    unique_id = dataset.frame["unique_id"].iloc[0]
    excluded_times = frozenset(
        timestamp
        for key_unique_id, timestamp in dataset.imputed_keys
        if key_unique_id == unique_id
    )

    # Evaluate the weekdays that this request will actually predict. A Sunday
    # holdout must not choose the winner for a Monday-only request.
    target_weekdays = {
        (config.forecast_start + timedelta(days=day)).weekday()
        for day in range(config.forecast_days)
    }
    matching = holdout["ds"].dt.weekday.isin(target_weekdays).to_numpy()
    scored_holdout = holdout.loc[matching]

    scores: list[LoadCandidateScore] = []
    for model_name, predictor in (
        (SOC_WEEKLY_DELTA_MODEL, _soc_weekly_delta_values),
        (SOC_SCHEDULE_DELTA_MODEL, _soc_schedule_delta_values),
    ):
        try:
            values = predictor(training, origin=holdout_start, periods=len(holdout))
            scores.append(load_candidate_score(
                model_name, scored_holdout,
                [_clip(unique_id, value)[1] for value, keep in zip(values, matching, strict=True) if keep], excluded_times,
            ))
        except Exception as error:
            scores.append(LoadCandidateScore(
                model_name, None, None, None, 0, type(error).__name__,
            ))

    viable_scores = [score for score in scores if score.mae is not None]
    if not viable_scores:
        raise M3Error("model_selection_failed", "all StatsForecast candidates failed")
    winner = min(
        viable_scores,
        key=lambda score: (score.mae, SOC_MODEL_ORDER.index(score.model_name)),
    )
    return CustomChampion(
        model_name=winner.model_name,
        cv_mape_percent=winner.mape_percent,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        candidate_scores=tuple(scores),
        selection_metric="mae",
    )


def _soc_weekly_delta_values(dataset, *, origin: datetime, periods: int) -> list[float]:
    return _soc_delta_values(dataset, origin=origin, periods=periods, weekly=True)


def soc_schedule_values(dataset, *, origin: datetime, periods: int) -> list[float]:
    """Shared schedule-aware SOC entry for custom and regular forecasts."""
    return _soc_schedule_delta_values(dataset, origin=origin, periods=periods)


def _soc_schedule_delta_values(dataset, *, origin: datetime, periods: int) -> list[float]:
    return _soc_delta_values(dataset, origin=origin, periods=periods, weekly=False)


def _soc_day_context(timestamp: datetime) -> tuple[bool, bool]:
    return schedule_day(timestamp), schedule_day(timestamp - timedelta(days=1))


def _recent_soc_plateau(source_values, origin, interval):
    """Highest stable real plateau in the last completed day, or no evidence.

    Require at least one hour and two consecutive observations within 0.5
    percentage points. This is a forecast reference, not a device SOC limit.
    """
    groups = []
    group = []
    previous = None
    for timestamp, value in sorted(source_values.items()):
        if not origin - timedelta(days=1) <= timestamp < origin:
            continue
        if (previous is None or timestamp - previous != interval
                or max([value, *group]) - min([value, *group]) > 0.5):
            if len(group) >= 2 and len(group) * interval >= timedelta(hours=1):
                groups.append(float(median(group)))
            group = []
        group.append(value)
        previous = timestamp
    if len(group) >= 2 and len(group) * interval >= timedelta(hours=1):
        groups.append(float(median(group)))
    return max(groups) if groups else None


def _soc_delta_values(
    dataset: CustomTrainingDataset, *, origin: datetime, periods: int, weekly: bool,
) -> list[float]:
    """Integrate past real SOC changes without carrying saturation overshoot."""
    if type(origin) is not datetime or type(periods) is not int or periods < 0:
        raise ValueError("invalid SOC forecast origin or horizon")
    if not periods:
        return []
    unique_id = dataset.frame["unique_id"].iloc[0]
    imputed = {t for uid, t in dataset.imputed_keys if uid == unique_id}
    source_values = {}
    for row in dataset.frame.itertuples(index=False):
        timestamp = pd.Timestamp(row.ds).to_pydatetime()
        if timestamp >= origin or timestamp in imputed:
            continue
        value = float(row.y)
        if np.isfinite(value):
            source_values[timestamp] = value
    if not source_values:
        raise M3Error("training_data_invalid", "No real SOC history")
    interval = timedelta(seconds=dataset.interval_seconds)
    anchor_time = max(source_values)
    current = source_values[anchor_time]
    values = [current]
    current = _clip(unique_id, current)[1]
    # A rest-day plateau is evidence for the following workday, not a
    # persistent charging target for ordinary workdays after discharging.
    origin_day_start = origin.replace(hour=0, minute=0, second=0, microsecond=0)
    first_day_end = origin_day_start + timedelta(days=1)
    recent_plateau = (
        _recent_soc_plateau(source_values, origin_day_start, interval)
        if _soc_day_context(origin) == (True, False) else None
    )
    day_starts = {}
    day_peaks = {}
    for timestamp, value in sorted(source_values.items()):
        date = timestamp.date()
        if timestamp.hour == timestamp.minute == timestamp.second == 0:
            day_starts[date] = value
        day_peaks[date] = max(day_peaks.get(date, value), value)
    forecast_day_start = current
    previous_day = origin.date()
    for period in range(1, periods):
        target = origin + period * interval
        if target.date() != previous_day:
            forecast_day_start = current
            previous_day = target.date()
        deltas = []
        ranked_deltas = []
        # Weekly candidate preserves same-weekday behavior. The baseline uses
        # closest matching day context and starting SOC, never predicted donors.
        for days in ((7, 14, 21) if weekly else range(1, 29)):
            source_time = target - timedelta(days=days)
            previous_time = source_time - interval
            if source_time not in source_values or previous_time not in source_values:
                continue
            if (_soc_day_context(source_time) != _soc_day_context(target)
                    or schedule_slot(source_time)[0] != schedule_slot(target)[0]
                    or schedule_slot(previous_time)[0] != schedule_slot(target - interval)[0]):
                continue
            delta = source_values[source_time] - source_values[previous_time]
            if weekly:
                deltas.append(delta)
            else:
                if target.date() == origin.date() and origin != origin_day_start:
                    # A live run may start in the afternoon: compare historical
                    # SOC at the same real anchor time, never with midnight SOC.
                    reference = source_values.get(anchor_time - timedelta(days=days))
                else:
                    reference = day_starts.get(source_time.date())
                if reference is None:
                    continue
                distance = abs(reference - forecast_day_start)
                if recent_plateau is not None and target < first_day_end:
                    distance += abs(day_peaks[source_time.date()] - recent_plateau)
                ranked_deltas.append((distance, days, delta))
        if not weekly and ranked_deltas:
            deltas = [min(ranked_deltas)[2]]
        if not deltas:
            raise M3Error("insufficient_history", "Missing matching SOC change history")
        raw = current + float(median(deltas))
        if (not weekly and recent_plateau is not None
                and target < first_day_end and raw > current):
            raw = min(raw, max(current, recent_plateau))
        values.append(raw)
        current = _clip(unique_id, raw)[1]
    return values


def select_custom_champion(
    dataset: CustomTrainingDataset, config: CustomForecastConfig
) -> CustomChampion:
    if dataset.interval_seconds != config.interval_seconds:
        raise M3Error("training_interval_invalid", "Training interval does not match")
    unique_id = dataset.frame["unique_id"].iloc[0]
    if is_load_series(unique_id):
        return select_load_champion(dataset, config)
    return _select_soc_champion(dataset, config)


def _model_by_name(name: str, config: CustomForecastConfig):
    for model_name, factory in _candidate_factories(config):
        if model_name == name:
            return factory()
    raise ValueError(f"unsupported StatsForecast model: {name}")


def _clip(unique_id: str, raw: float) -> tuple[float, float, bool]:
    value = float(raw)
    published = (
        max(value, 0.0)
        if is_load_series(unique_id)
        else min(max(value, 2.0), 99.0)
    )
    return value, published, value != published


def _last_real_value(dataset: CustomTrainingDataset, unique_id: str) -> float:
    for row in dataset.frame.iloc[::-1].itertuples(index=False):
        timestamp = pd.Timestamp(row.ds).to_pydatetime()
        if (unique_id, timestamp) not in dataset.imputed_keys:
            return float(row.y)
    raise M3Error("training_data_invalid", f"no real observations for {unique_id}")


def _forecast_frame(
    dataset: CustomTrainingDataset,
    champion: CustomChampion,
    config: CustomForecastConfig,
) -> tuple[pd.DataFrame, str, str | None]:
    unique_id = dataset.frame["unique_id"].iloc[0]
    if is_load_series(unique_id):
        return _forecast_load_frame(dataset, champion, config)
    try:
        if champion.model_name == SOC_WEEKLY_DELTA_MODEL:
            predictor = _soc_weekly_delta_values
        elif champion.model_name == SOC_SCHEDULE_DELTA_MODEL:
            predictor = _soc_schedule_delta_values
        else:
            raise ValueError("retired SOC model requires schedule-aware fallback")
        values = predictor(dataset, origin=config.forecast_start,
                           periods=config.expected_points_per_series)
        return _load_frame(config, unique_id, champion.model_name, values), champion.model_name, None
    except Exception as champion_error:
        if champion.model_name == SOC_SCHEDULE_DELTA_MODEL:
            raise
        values = _soc_schedule_delta_values(
            dataset, origin=config.forecast_start, periods=config.expected_points_per_series,
        )
        return (
            _load_frame(config, unique_id, SOC_SCHEDULE_DELTA_MODEL, values),
            SOC_SCHEDULE_DELTA_MODEL, type(champion_error).__name__,
        )


def _forecast_load_frame(
    dataset: CustomTrainingDataset,
    champion: CustomChampion,
    config: CustomForecastConfig,
) -> tuple[pd.DataFrame, str, str | None]:
    unique_id = dataset.frame["unique_id"].iloc[0]
    try:
        frame, model_name, _ = _forecast_load_model(dataset, champion, config)
        return (
            _validated_load_forecast_frame(frame, model_name, config),
            model_name,
            None,
        )
    except Exception as champion_error:
        if champion.model_name == "WeeklyNaive":
            raise M3Error(
                "weekly_naive_failed", "weekly naive final forecast failed"
            ) from None
        try:
            values = weekly_profile_values(
                dataset,
                "WeeklyNaive",
                origin=config.forecast_start,
                periods=config.expected_points_per_series,
            )
            fallback_frame = _load_frame(config, unique_id, "WeeklyNaive", values)
            return (
                _validated_load_forecast_frame(
                    fallback_frame, "WeeklyNaive", config
                ),
                "WeeklyNaive",
                type(champion_error).__name__,
            )
        except Exception:
            raise M3Error(
                "weekly_naive_failed", "weekly naive final forecast failed"
            ) from None


def _forecast_load_model(
    dataset: CustomTrainingDataset,
    champion: CustomChampion,
    config: CustomForecastConfig,
) -> tuple[pd.DataFrame, str, str | None]:
    unique_id = dataset.frame["unique_id"].iloc[0]
    if champion.model_name.startswith("Weekly"):
        values = weekly_profile_values(
            dataset,
            champion.model_name,
            origin=config.forecast_start,
            periods=config.expected_points_per_series,
        )
        return (
            _load_frame(config, unique_id, champion.model_name, values),
            champion.model_name,
            None,
        )
    model = _load_automatic_model(champion.model_name, config)
    engine = StatsForecast(models=[model], freq=config.pandas_frequency, n_jobs=1)
    return (
        schedule_forecast(
            engine, dataset.frame, origin=config.forecast_start,
            periods=config.expected_points_per_series, model_name=champion.model_name,
        ),
        champion.model_name,
        None,
    )


def _load_frame(
    config: CustomForecastConfig,
    unique_id: str,
    model_name: str,
    values: list[float],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "unique_id": [unique_id] * len(values),
            "ds": pd.date_range(
                config.forecast_start,
                periods=len(values),
                freq=config.pandas_frequency,
            ),
            model_name: values,
        }
    )


def _validated_load_forecast_frame(
    frame: pd.DataFrame,
    model_name: str,
    config: CustomForecastConfig,
) -> pd.DataFrame:
    if len(frame) != config.expected_points_per_series:
        raise M3Error("forecast_alignment_invalid", "Forecast horizon is incomplete")
    try:
        timestamps = [
            pd.Timestamp(value).to_pydatetime() for value in frame["ds"]
        ]
    except Exception:
        raise M3Error(
            "forecast_alignment_invalid", "Forecast timestamps are invalid"
        ) from None
    expected_times = [
        config.forecast_start + index * config.interval
        for index in range(config.expected_points_per_series)
    ]
    if timestamps != expected_times:
        raise M3Error("forecast_alignment_invalid", "Forecast timestamps are misaligned")
    try:
        numeric_values = np.asarray(
            pd.to_numeric(frame[model_name], errors="raise"), dtype=float
        )
    except Exception:
        raise M3Error(
            "forecast_values_invalid", "Forecast values are invalid"
        ) from None
    if not np.isfinite(numeric_values).all():
        raise M3Error("forecast_values_invalid", "Forecast values are invalid")
    normalized = frame.copy()
    normalized["ds"] = timestamps
    normalized[model_name] = numeric_values
    return normalized


def forecast_custom_series(
    dataset: CustomTrainingDataset,
    champion: CustomChampion | None,
    config: CustomForecastConfig,
) -> CustomForecastSeries:
    unique_id = dataset.frame["unique_id"].iloc[0]
    unit = "kW" if is_load_series(unique_id) else "%"
    if dataset.mode == "insufficient" and not (
        is_load_series(unique_id)
        and champion is not None
        and champion.selection_status is not None
    ):
        return CustomForecastSeries(
            unique_id=unique_id,
            unit=unit,
            model_name="none",
            status="insufficient_history",
            points=[],
            fallback_reason="history_under_7_days",
        )
    if champion is None:
        raise M3Error("model_unavailable", f"no champion for {unique_id}")

    frame, used_model, fallback_reason = _forecast_frame(dataset, champion, config)
    if len(frame) != config.expected_points_per_series:
        raise M3Error("forecast_alignment_invalid", "Forecast horizon is incomplete")
    first_time = pd.Timestamp(frame["ds"].iloc[0]).to_pydatetime()
    if first_time != config.forecast_start:
        raise M3Error("forecast_alignment_invalid", "Forecast start is misaligned")

    soc_offset = 0.0
    if not is_load_series(unique_id) and used_model not in {SOC_WEEKLY_DELTA_MODEL, SOC_SCHEDULE_DELTA_MODEL}:
        first_raw = float(frame[used_model].iloc[0])
        soc_offset = _last_real_value(dataset, unique_id) - first_raw

    points = []
    for horizon_step, row in enumerate(frame.itertuples(index=False), start=1):
        target_time = pd.Timestamp(row.ds).to_pydatetime()
        model_value = float(getattr(row, used_model))
        raw, published, clipped = _clip(unique_id, model_value + soc_offset)
        points.append(
            CustomForecastPoint(
                target_time=target_time,
                horizon_step=horizon_step,
                raw_forecast=raw,
                forecast_value=published,
                is_clipped=clipped,
            )
        )
    if fallback_reason:
        status = "degraded"
        public_fallback_reason = fallback_reason
    elif champion.selection_status:
        status = champion.selection_status
        public_fallback_reason = (
            champion.selection_reason if status == "degraded" else None
        )
    elif champion.selection_reason and champion.selection_reason != "wape_unavailable":
        status = "degraded"
        public_fallback_reason = champion.selection_reason
    else:
        status = "warming_up" if dataset.mode == "warming_up" else "ok"
        public_fallback_reason = None
    series = CustomForecastSeries(
        unique_id=unique_id,
        unit=unit,
        model_name=used_model,
        status=status,
        points=points,
        fallback_reason=public_fallback_reason,
    )
    validate_custom_series_for_config(series, config)
    return series


def interpolate_soc_forecast(
    series: CustomForecastSeries,
    base_config: CustomForecastConfig,
    output_config: CustomForecastConfig,
) -> CustomForecastSeries:
    """Expand five-minute SOC anchors onto a one-minute output grid."""

    if series.unique_id != "storage_soc":
        raise ValueError("SOC interpolation requires the storage SOC series")
    if base_config.interval_seconds != 300 or output_config.interval_seconds != 60:
        raise ValueError("SOC interpolation requires five-minute to one-minute grids")
    if (
        base_config.history_start != output_config.history_start
        or base_config.history_end != output_config.history_end
        or base_config.forecast_start != output_config.forecast_start
        or base_config.forecast_end != output_config.forecast_end
        or base_config.forecast_days != output_config.forecast_days
        or base_config.model_policy != output_config.model_policy
    ):
        raise ValueError("SOC interpolation configurations do not share one window")
    validate_custom_series_for_config(series, base_config)
    if not series.points:
        return series.model_copy(
            update={"model_name": f"{series.model_name}{SOC_FIVE_MINUTE_LINEAR_SUFFIX}"}
        )

    ratio = base_config.interval_seconds // output_config.interval_seconds
    points: list[CustomForecastPoint] = []
    for index in range(output_config.expected_points_per_series):
        left_index = min(index // ratio, len(series.points) - 1)
        right_index = min(left_index + 1, len(series.points) - 1)
        fraction = (index % ratio) / ratio if right_index != left_index else 0.0
        left = series.points[left_index]
        right = series.points[right_index]
        raw_value = left.raw_forecast + fraction * (
            right.raw_forecast - left.raw_forecast
        )
        raw, published, clipped = _clip("storage_soc", raw_value)
        points.append(
            CustomForecastPoint(
                target_time=output_config.forecast_start
                + index * output_config.interval,
                horizon_step=index + 1,
                raw_forecast=raw,
                forecast_value=published,
                is_clipped=clipped,
            )
        )
    output = series.model_copy(
        update={
            "model_name": f"{series.model_name}{SOC_FIVE_MINUTE_LINEAR_SUFFIX}",
            "points": points,
        }
    )
    validate_custom_series_for_config(output, output_config)
    return output
