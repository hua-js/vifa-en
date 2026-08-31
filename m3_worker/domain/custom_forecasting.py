"""StatsForecast selection and prediction for configurable M3 intervals."""

from dataclasses import dataclass, replace
from importlib.metadata import version

import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoARIMA, AutoETS, MSTL, SeasonalNaive

from m3_worker.contracts import is_load_series
from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastPoint,
    CustomForecastSeries,
    validate_custom_series_for_config,
)
from m3_worker.domain.custom_load_profiles import (
    LoadCandidateScore,
    eligible_load_models,
    load_candidate_score,
    weekly_profile_values,
)
from m3_worker.domain.custom_training_data import CustomTrainingDataset
from m3_worker.errors import M3Error


MODEL_NAMES = ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL")


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
    return CustomChampion(
        model_name="SeasonalNaive",
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
    return CustomChampion(
        model_name="WeeklyNaive",
        cv_mape_percent=None,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        selection_reason=selection_reason,
        selection_status=selection_status,
    )


def _model_mape(
    cv: pd.DataFrame,
    model_name: str,
    imputed_keys: frozenset[tuple[str, object]],
) -> float | None:
    scorable = cv[(cv["y"] != 0) & cv["y"].notna()].copy()
    scorable = scorable[
        ~scorable.apply(
            lambda row: (row["unique_id"], row["ds"].to_pydatetime())
            in imputed_keys,
            axis=1,
        )
    ]
    if model_name not in scorable or scorable.empty:
        return None
    predictions = pd.to_numeric(scorable[model_name], errors="coerce")
    if predictions.isna().any() or not np.isfinite(predictions).all():
        return None
    score = float(
        (abs(scorable["y"] - predictions) / abs(scorable["y"])).mean() * 100
    )
    return score if np.isfinite(score) else None


def select_load_champion(
    dataset: CustomTrainingDataset, config: CustomForecastConfig
) -> CustomChampion:
    usable_week_count = len(dataset.usable_weeks)
    if usable_week_count == 0:
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
                forecast = engine.forecast(df=training.frame, h=len(holdout))
                predictions = forecast[model_name].tolist()
            scores.append(
                load_candidate_score(
                    model_name, holdout, predictions, excluded_times
                )
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

    viable_scores = [score for score in scores if score.mae is not None]
    if not viable_scores:
        raise M3Error("model_selection_failed", "all load candidates failed")
    winner = min(viable_scores, key=lambda score: score.comparison_key)
    return CustomChampion(
        model_name=winner.model_name,
        cv_mape_percent=winner.mape_percent,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        candidate_scores=tuple(scores),
        selection_metric="wape_percent",
    )


def _load_automatic_model(name: str, config: CustomForecastConfig):
    if name == "AutoARIMA":
        return AutoARIMA(
            season_length=config.weekly_season_length,
            alias="AutoARIMA",
        )
    if name == "MSTL":
        return MSTL(
            season_length=[config.daily_season_length, config.weekly_season_length],
            trend_forecaster=AutoARIMA(),
            alias="MSTL",
        )
    raise ValueError(f"unsupported load automatic model: {name}")


def _select_soc_champion(
    dataset: CustomTrainingDataset, config: CustomForecastConfig
) -> CustomChampion:
    if dataset.interval_seconds != config.interval_seconds:
        raise M3Error("training_interval_invalid", "Training interval does not match")
    if dataset.mode == "insufficient":
        raise M3Error(
            "insufficient_history", "fewer than seven complete training days"
        )
    if config.model_policy == "seasonal_naive_only" or dataset.mode == "warming_up":
        return seasonal_naive_champion(dataset)

    scores: dict[str, float] = {}
    for model_name, factory in _candidate_factories(config):
        try:
            engine = StatsForecast(
                models=[factory()], freq=config.pandas_frequency, n_jobs=1
            )
            cv = engine.cross_validation(
                df=dataset.frame,
                h=config.points_per_day,
                step_size=config.points_per_day,
                n_windows=7,
            )
            score = _model_mape(cv, model_name, dataset.imputed_keys)
        except Exception:
            continue
        if score is not None:
            scores[model_name] = score
    if not scores:
        raise M3Error("model_selection_failed", "all StatsForecast candidates failed")
    winner = min(scores, key=lambda name: (scores[name], name))
    return CustomChampion(
        model_name=winner,
        cv_mape_percent=scores[winner],
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
    )


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
        else min(max(value, 0.0), 100.0)
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
        model = _model_by_name(champion.model_name, config)
        engine = StatsForecast(
            models=[model], freq=config.pandas_frequency, n_jobs=1
        )
        return (
            engine.forecast(df=dataset.frame, h=config.expected_points_per_series),
            champion.model_name,
            None,
        )
    except Exception as champion_error:
        if champion.model_name == "SeasonalNaive":
            raise
        fallback = StatsForecast(
            models=[_model_by_name("SeasonalNaive", config)],
            freq=config.pandas_frequency,
            n_jobs=1,
        )
        return (
            fallback.forecast(
                df=dataset.frame, h=config.expected_points_per_series
            ),
            "SeasonalNaive",
            type(champion_error).__name__,
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
        engine.forecast(df=dataset.frame, h=config.expected_points_per_series),
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
    if not is_load_series(unique_id):
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
        public_fallback_reason = None
    elif champion.selection_reason:
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
