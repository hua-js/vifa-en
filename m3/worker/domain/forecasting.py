from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib.metadata import version
from types import SimpleNamespace

import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoARIMA, AutoETS, MSTL, SeasonalNaive

from m3.worker.contracts import ForecastPoint, ForecastSeries, is_load_series
from m3.worker.domain.soc_anchoring import (
    anchored_soc_cv_predictions,
    recent_soc_residual_offset,
)
from m3.worker.domain.training_data import TrainingDataset
from m3.worker.domain.work_schedule import schedule_cross_validation, schedule_forecast
from m3.worker.errors import M3Error
from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE
from m3.worker.domain.custom_forecasting import soc_schedule_values


MODEL_NAMES = ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL")


def _candidate_factories():
    return [
        (
            "SeasonalNaive",
            lambda: SeasonalNaive(season_length=96, alias="SeasonalNaive"),
        ),
        ("AutoETS", lambda: AutoETS(season_length=96, alias="AutoETS")),
        ("AutoARIMA", lambda: AutoARIMA(season_length=96, alias="AutoARIMA")),
        (
            "MSTL",
            lambda: MSTL(
                season_length=[96, 672],
                trend_forecaster=AutoARIMA(),
                alias="MSTL",
            ),
        ),
    ]


def candidate_models():
    return [factory() for _, factory in _candidate_factories()]


@dataclass(frozen=True)
class Champion:
    model_name: str
    cv_mape_percent: float | None
    selected_at: pd.Timestamp
    training_start: pd.Timestamp
    training_end: pd.Timestamp
    statsforecast_version: str
    selection_reason: str | None = None


def seasonal_naive_champion(
    dataset: TrainingDataset,
    selection_reason: str | None = None,
) -> Champion:
    return Champion(
        model_name="SeasonalNaive",
        cv_mape_percent=None,
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
        selection_reason=selection_reason,
    )


def _model_mapes(
    cv: pd.DataFrame,
    imputed_keys: frozenset[tuple[str, datetime]],
    fitted: pd.DataFrame | None = None,
) -> dict[str, float]:
    scorable = cv[(cv["y"] != 0) & cv["y"].notna()].copy()
    scorable = scorable[
        ~scorable.apply(
            lambda row: (row["unique_id"], row["ds"].to_pydatetime())
            in imputed_keys,
            axis=1,
        )
    ]
    scores = {}
    for name in MODEL_NAMES:
        if name not in scorable or scorable.empty:
            continue
        predictions = (
            anchored_soc_cv_predictions(scorable, fitted, name, imputed_keys)
            if fitted is not None
            else pd.to_numeric(scorable[name], errors="coerce")
        )
        if predictions.isna().any() or not np.isfinite(predictions).all():
            continue
        score = float(
            (abs(scorable["y"] - predictions) / abs(scorable["y"])).mean()
            * 100
        )
        if np.isfinite(score):
            scores[name] = score
    return scores


def select_champion(dataset: TrainingDataset) -> Champion:
    if dataset.mode == "insufficient":
        raise M3Error(
            "insufficient_history", "fewer than seven complete training days"
        )
    if ACTIVE_SCHEDULE.get() is not None and not is_load_series(dataset.frame["unique_id"].iloc[0]):
        # Share the existing SOC donor selection rather than fitting a second
        # schedule model. No cross-validation score is fabricated for this policy.
        return replace(seasonal_naive_champion(dataset), model_name="SOCScheduleDelta")
    if dataset.mode == "warming_up":
        return seasonal_naive_champion(dataset)

    scores: dict[str, float] = {}
    unique_id = dataset.frame["unique_id"].iloc[0]
    needs_soc_anchor = not is_load_series(unique_id)
    for model_name, factory in _candidate_factories():
        try:
            model = factory()
            engine = StatsForecast(models=[model], freq="15min", n_jobs=1)
            cv = schedule_cross_validation(
                engine, dataset.frame, model_name=model_name,
            ) if not needs_soc_anchor else engine.cross_validation(
                df=dataset.frame,
                h=96,
                step_size=96,
                n_windows=7,
                **({"fitted": True} if needs_soc_anchor else {}),
            )
            fitted = (
                engine.cross_validation_fitted_values()
                if needs_soc_anchor
                else None
            )
            score = _model_mapes(cv, dataset.imputed_keys, fitted).get(model_name)
        except Exception:
            continue
        if score is not None:
            scores[model_name] = score

    if not scores:
        raise M3Error("model_selection_failed", "all StatsForecast candidates failed")
    winner = min(scores, key=lambda name: (scores[name], name))
    return Champion(
        model_name=winner,
        cv_mape_percent=scores[winner],
        selected_at=pd.Timestamp.now(tz="Asia/Shanghai"),
        training_start=pd.Timestamp(dataset.start),
        training_end=pd.Timestamp(dataset.end),
        statsforecast_version=version("statsforecast"),
    )


def clip_value(unique_id: str, raw: float) -> tuple[float, float, bool]:
    value = float(raw)
    published = (
        max(value, 0.0)
        if is_load_series(unique_id)
        else min(max(value, 2.0), 99.0)
    )
    return value, published, value != published


def model_by_name(name: str):
    for model_name, factory in _candidate_factories():
        if model_name == name:
            return factory()
    raise ValueError(f"unsupported StatsForecast model: {name}")


def forecast_frame(
    dataset: TrainingDataset, model_name: str
) -> tuple[pd.DataFrame, pd.DataFrame | None, str, str | None]:
    unique_id = dataset.frame["unique_id"].iloc[0]
    if not is_load_series(unique_id) and ACTIVE_SCHEDULE.get() is not None:
        origin = dataset.end + timedelta(minutes=15)
        # The common SOC routine only needs these three dataset attributes.
        adapter = SimpleNamespace(frame=dataset.frame, imputed_keys=dataset.imputed_keys, interval_seconds=900)
        values = soc_schedule_values(adapter, origin=origin, periods=96)
        frame = pd.DataFrame({"unique_id": unique_id,
            "ds": pd.date_range(origin, periods=96, freq="15min"), "SOCScheduleDelta": values})
        return frame, None, "SOCScheduleDelta", None
    needs_soc_anchor = not is_load_series(unique_id)
    try:
        engine = StatsForecast(
            models=[model_by_name(model_name)], freq="15min", n_jobs=1
        )
        frame = schedule_forecast(
            engine, dataset.frame,
            origin=dataset.end + timedelta(minutes=15),
            periods=96, model_name=model_name,
        ) if not needs_soc_anchor else engine.forecast(
            df=dataset.frame,
            h=96,
            **({"fitted": True} if needs_soc_anchor else {}),
        )
        fitted = engine.forecast_fitted_values() if needs_soc_anchor else None
        return frame, fitted, model_name, None
    except Exception as champion_error:
        if model_name == "SeasonalNaive":
            raise
        fallback = StatsForecast(
            models=[model_by_name("SeasonalNaive")], freq="15min", n_jobs=1
        )
        frame = schedule_forecast(
            fallback, dataset.frame,
            origin=dataset.end + timedelta(minutes=15),
            periods=96, model_name="SeasonalNaive",
        ) if not needs_soc_anchor else fallback.forecast(
            df=dataset.frame,
            h=96,
            **({"fitted": True} if needs_soc_anchor else {}),
        )
        fitted = fallback.forecast_fitted_values() if needs_soc_anchor else None
        return frame, fitted, "SeasonalNaive", type(champion_error).__name__


def forecast_one(
    dataset: TrainingDataset,
    champion: Champion | None,
    as_of: datetime,
) -> ForecastSeries:
    unique_id = dataset.frame["unique_id"].iloc[0]
    unit = "kW" if is_load_series(unique_id) else "%"
    if dataset.mode == "insufficient":
        return ForecastSeries(
            unique_id=unique_id,
            unit=unit,
            model_name="none",
            status="insufficient_history",
            points=[],
            fallback_reason="history_under_7_days",
        )
    if champion is None:
        raise M3Error("model_unavailable", f"no champion for {unique_id}")

    frame, fitted, used_model, fallback_reason = forecast_frame(
        dataset, champion.model_name
    )
    expected_first_data_time = as_of.replace(
        minute=(as_of.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    if (
        len(frame) != 96
        or pd.Timestamp(frame["ds"].iloc[0]).to_pydatetime()
        != expected_first_data_time
    ):
        raise M3Error(
            "forecast_alignment_invalid",
            "forecast horizon is not aligned to the completed bucket",
        )

    soc_offset = (
        recent_soc_residual_offset(fitted, used_model, dataset.imputed_keys)
        if fitted is not None
        else 0.0
    )
    points = []
    for horizon_step, row in enumerate(frame.itertuples(index=False), start=1):
        data_time = pd.Timestamp(row.ds).to_pydatetime()
        raw, published, clipped = clip_value(
            unique_id, float(getattr(row, used_model)) + soc_offset
        )
        points.append(
            ForecastPoint(
                data_time=data_time,
                target_time=data_time + timedelta(minutes=15),
                horizon_step=horizon_step,
                raw_forecast=raw,
                forecast_value=published,
                is_clipped=clipped,
            )
        )

    status = (
        "degraded"
        if fallback_reason or champion.selection_reason
        else ("warming_up" if dataset.mode == "warming_up" else "ok")
    )
    return ForecastSeries(
        unique_id=unique_id,
        unit=unit,
        model_name=used_model,
        status=status,
        points=points,
        fallback_reason=fallback_reason or champion.selection_reason,
    )


def forecast_one_safe(
    dataset: TrainingDataset,
    champion: Champion | None,
    as_of: datetime,
) -> ForecastSeries:
    try:
        return forecast_one(dataset, champion, as_of)
    except Exception:
        unique_id = dataset.frame["unique_id"].iloc[0]
        return ForecastSeries(
            unique_id=unique_id,
            unit="kW" if is_load_series(unique_id) else "%",
            model_name=champion.model_name if champion is not None else "none",
            status="error",
            points=[],
            fallback_reason="forecast_failed",
        )
