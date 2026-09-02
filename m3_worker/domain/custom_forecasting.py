"""StatsForecast selection and prediction for configurable M3 intervals."""

from dataclasses import dataclass
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
from m3_worker.domain.custom_training_data import CustomTrainingDataset
from m3_worker.domain.soc_anchoring import (
    anchored_soc_cv_predictions,
    recent_soc_residual_offset,
)
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


def _model_mape(
    cv: pd.DataFrame,
    model_name: str,
    imputed_keys: frozenset[tuple[str, object]],
    fitted: pd.DataFrame | None = None,
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
    predictions = (
        anchored_soc_cv_predictions(scorable, fitted, model_name, imputed_keys)
        if fitted is not None
        else pd.to_numeric(scorable[model_name], errors="coerce")
    )
    if predictions.isna().any() or not np.isfinite(predictions).all():
        return None
    score = float(
        (abs(scorable["y"] - predictions) / abs(scorable["y"])).mean() * 100
    )
    return score if np.isfinite(score) else None


def select_custom_champion(
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
    unique_id = dataset.frame["unique_id"].iloc[0]
    needs_soc_anchor = not is_load_series(unique_id)
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
                **({"fitted": True} if needs_soc_anchor else {}),
            )
            fitted = (
                engine.cross_validation_fitted_values()
                if needs_soc_anchor
                else None
            )
            score = _model_mape(
                cv, model_name, dataset.imputed_keys, fitted
            )
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


def _forecast_frame(
    dataset: CustomTrainingDataset,
    champion: CustomChampion,
    config: CustomForecastConfig,
) -> tuple[pd.DataFrame, pd.DataFrame | None, str, str | None]:
    unique_id = dataset.frame["unique_id"].iloc[0]
    needs_soc_anchor = not is_load_series(unique_id)
    try:
        model = _model_by_name(champion.model_name, config)
        engine = StatsForecast(
            models=[model], freq=config.pandas_frequency, n_jobs=1
        )
        frame = engine.forecast(
            df=dataset.frame,
            h=config.expected_points_per_series,
            **({"fitted": True} if needs_soc_anchor else {}),
        )
        fitted = engine.forecast_fitted_values() if needs_soc_anchor else None
        return frame, fitted, champion.model_name, None
    except Exception as champion_error:
        if champion.model_name == "SeasonalNaive":
            raise
        fallback = StatsForecast(
            models=[_model_by_name("SeasonalNaive", config)],
            freq=config.pandas_frequency,
            n_jobs=1,
        )
        frame = fallback.forecast(
            df=dataset.frame,
            h=config.expected_points_per_series,
            **({"fitted": True} if needs_soc_anchor else {}),
        )
        fitted = fallback.forecast_fitted_values() if needs_soc_anchor else None
        return frame, fitted, "SeasonalNaive", type(champion_error).__name__


def forecast_custom_series(
    dataset: CustomTrainingDataset,
    champion: CustomChampion | None,
    config: CustomForecastConfig,
) -> CustomForecastSeries:
    unique_id = dataset.frame["unique_id"].iloc[0]
    unit = "kW" if is_load_series(unique_id) else "%"
    if dataset.mode == "insufficient":
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

    frame, fitted, used_model, fallback_reason = _forecast_frame(
        dataset, champion, config
    )
    if len(frame) != config.expected_points_per_series:
        raise M3Error("forecast_alignment_invalid", "Forecast horizon is incomplete")
    first_time = pd.Timestamp(frame["ds"].iloc[0]).to_pydatetime()
    if first_time != config.forecast_start:
        raise M3Error("forecast_alignment_invalid", "Forecast start is misaligned")

    soc_offset = (
        recent_soc_residual_offset(fitted, used_model, dataset.imputed_keys)
        if fitted is not None
        else 0.0
    )

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
    series = CustomForecastSeries(
        unique_id=unique_id,
        unit=unit,
        model_name=used_model,
        status=(
            "degraded"
            if fallback_reason or champion.selection_reason
            else ("warming_up" if dataset.mode == "warming_up" else "ok")
        ),
        points=points,
        fallback_reason=fallback_reason or champion.selection_reason,
    )
    validate_custom_series_for_config(series, config)
    return series
