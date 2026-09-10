"""Small, deterministic load candidates for weekly custom forecasts.

The profile candidates deliberately use exact timestamp lookups.  This keeps
holdout evaluation honest: a caller supplies the origin of the forecast and
no source observation at or after that origin can be used.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from statistics import median

import numpy as np
import pandas as pd

from m3.worker.domain.custom_training_data import CustomTrainingDataset


LOAD_SELECTION_POLICY = "weekly_load_v2"
MODEL_ORDER = (
    "WeeklyNaive",
    "WeeklyWeighted2",
    "WeeklyRegimeAdjusted",
    "WeeklyMedian3",
    "AutoARIMA",
    "MSTL",
)


@dataclass(frozen=True)
class LoadCandidateScore:
    """Comparable holdout metrics for one load candidate."""

    model_name: str
    wape_percent: float | None
    mae: float | None
    mape_percent: float | None
    scorable_point_count: int
    skip_reason: str | None

    @property
    def comparison_key(self) -> tuple[object, ...]:
        return load_candidate_comparison_key(self)


def eligible_load_models(
    usable_week_count: int, interval_seconds: int
) -> tuple[str, ...]:
    """Return candidates permitted by consecutive usable-week evidence."""

    names = ["WeeklyNaive"] if usable_week_count >= 1 else []
    if usable_week_count >= 3:
        names.extend(("WeeklyWeighted2", "WeeklyRegimeAdjusted"))
    if usable_week_count >= 4:
        names.append("WeeklyMedian3")
        if interval_seconds >= 900:
            names.extend(("AutoARIMA", "MSTL"))
    return tuple(names)


def _as_datetime(value: object) -> datetime | None:
    """Normalize pandas timestamps while preserving their timezone."""

    if value is None or value is pd.NaT:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def _profile_lags(model_name: str) -> tuple[int, ...]:
    if model_name == "WeeklyNaive":
        return (1,)
    if model_name in {"WeeklyWeighted2", "WeeklyRegimeAdjusted"}:
        return (1, 2)
    if model_name == "WeeklyMedian3":
        return (1, 2, 3)
    raise ValueError(f"unsupported weekly load profile: {model_name}")


def weekly_profile_values(
    dataset: CustomTrainingDataset,
    model_name: str,
    *,
    origin: datetime,
    periods: int,
) -> list[float]:
    """Forecast ``periods`` points from exact weekly lag observations.

    Weekly profile inputs may be imputed values in the cleaned training frame;
    imputation is only excluded when scoring actual holdout targets.  Every
    lookup is exact and source timestamps must be strictly older than origin.
    """

    if type(periods) is not int or periods < 0:
        raise ValueError("periods must be a non-negative integer")
    lags = _profile_lags(model_name)
    if type(origin) is not datetime:
        raise ValueError("origin must be a datetime")

    source_values: dict[datetime, float] = {}
    for row in dataset.frame.itertuples(index=False):
        timestamp = _as_datetime(getattr(row, "ds"))
        if timestamp is None or timestamp >= origin:
            continue
        try:
            value = float(getattr(row, "y"))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            source_values[timestamp] = value

    interval = timedelta(seconds=dataset.interval_seconds)
    values: list[float] = []
    for period in range(periods):
        target = origin + period * interval
        lag_values: list[float] = []
        for lag in lags:
            source_time = target - timedelta(days=7 * lag)
            if source_time >= origin:
                raise ValueError("weekly profile source must be before origin")
            try:
                lag_values.append(source_values[source_time])
            except KeyError as exc:
                raise ValueError(
                    f"missing weekly profile source for {target.isoformat()}"
                ) from exc
        if model_name == "WeeklyNaive":
            values.append(lag_values[0])
        elif model_name in {"WeeklyWeighted2", "WeeklyRegimeAdjusted"}:
            values.append(2 / 3 * lag_values[0] + 1 / 3 * lag_values[1])
        else:
            values.append(float(median(lag_values)))
    if model_name == "WeeklyRegimeAdjusted" and values:
        lower, low_ceiling, upper = np.percentile(values, (10, 40, 90))
        separated = upper > 0 and (lower <= 0 or upper >= lower * 3)
        recent_values = [
            value
            for timestamp, value in source_values.items()
            if origin - timedelta(days=3) <= timestamp < origin
        ]
        low_values = [value for value in values if value <= low_ceiling]
        if separated and recent_values and low_values:
            recent_ceiling = float(np.percentile(recent_values, 25))
            recent_low_values = [
                value for value in recent_values if value <= recent_ceiling
            ]
            base_standby = float(median(low_values))
            recent_standby = float(median(recent_low_values))
            if base_standby > 0:
                ratio = min(1.5, max(0.5, recent_standby / base_standby))
                values = [
                    value * ratio if value <= low_ceiling else value
                    for value in values
                ]
    return values


def _excluded_datetimes(excluded_times: frozenset[datetime]) -> set[datetime]:
    normalized: set[datetime] = set()
    for value in excluded_times:
        timestamp = _as_datetime(value)
        if timestamp is not None:
            normalized.add(timestamp)
    return normalized


def load_candidate_score(
    model_name: str,
    actual: pd.DataFrame,
    predicted: list[float],
    excluded_times: frozenset[datetime],
) -> LoadCandidateScore:
    """Score all candidates on one common finite, non-imputed actual set."""

    if len(actual) != len(predicted):
        raise ValueError("actual and predicted must have equal length")
    excluded = _excluded_datetimes(excluded_times)
    actual_values: list[float] = []
    raw_predictions: list[object] = []
    for row, forecast in zip(actual.itertuples(index=False), predicted, strict=True):
        timestamp = _as_datetime(getattr(row, "ds"))
        if timestamp is None or timestamp in excluded:
            continue
        try:
            actual_value = float(getattr(row, "y"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(actual_value):
            continue
        actual_values.append(actual_value)
        raw_predictions.append(forecast)

    scorable_count = len(actual_values)
    if not actual_values:
        return LoadCandidateScore(
            model_name,
            None,
            None,
            None,
            0,
            "no_scorable_points",
        )

    try:
        prediction_values = [float(value) for value in raw_predictions]
    except (TypeError, ValueError, OverflowError):
        prediction_values = []
    if len(prediction_values) != scorable_count or not all(
        math.isfinite(value) for value in prediction_values
    ):
        return LoadCandidateScore(
            model_name,
            None,
            None,
            None,
            scorable_count,
            "non_finite_prediction",
        )

    errors = [abs(actual_value - forecast) for actual_value, forecast in zip(actual_values, prediction_values, strict=True)]
    actual_sum = sum(abs(value) for value in actual_values)
    wape = 100 * sum(errors) / actual_sum if actual_sum else None
    mae = float(np.mean(errors)) if errors else None
    nonzero_apes = [
        100 * error / abs(actual_value)
        for actual_value, error in zip(actual_values, errors, strict=True)
        if actual_value != 0
    ]
    mape = float(np.mean(nonzero_apes)) if nonzero_apes else None
    return LoadCandidateScore(
        model_name,
        wape,
        mae,
        mape,
        scorable_count,
        None,
    )


def load_candidate_comparison_key(score: LoadCandidateScore) -> tuple[object, ...]:
    """Sort scores by WAPE, falling back to MAE when WAPE is unavailable."""

    try:
        model_index = MODEL_ORDER.index(score.model_name)
    except ValueError as exc:
        raise ValueError(f"unsupported load candidate: {score.model_name}") from exc
    if score.wape_percent is None:
        return (1, score.mae if score.mae is not None else math.inf, model_index)
    return (
        0,
        score.wape_percent,
        score.mae if score.mae is not None else math.inf,
        model_index,
    )


load_candidate_sort_key = load_candidate_comparison_key
candidate_comparison_key = load_candidate_comparison_key
