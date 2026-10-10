"""Fixed Shanghai work schedule shared by load forecast entry points."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import numpy as np
import pandas as pd

from m3.worker.errors import M3Error
from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE, PRODUCTION_SCHEDULE_POLICY


WORK_SCHEDULE_POLICY = "mon-sat-work-sun-rest-v1"

# schedule_slot/schedule_day are pure for a given active calendar: within a
# selection run the calendar never changes while the same timestamp is queried
# hundreds of thousands of times across candidates and validation folds.
# Memoize per calendar identity (strong references prevent id() reuse) and
# bound growth so long-lived workers do not accumulate run history.
_MEMO_CALENDAR_LIMIT = 4
_MEMO_ENTRY_LIMIT = 500_000
_MEMO_MISS = object()
_slot_memos: dict[int, tuple[object, dict]] = {}
_day_memos: dict[int, tuple[object, dict]] = {}


def _memo(store: dict, calendar: object) -> dict:
    key = id(calendar)
    entry = store.get(key)
    if entry is None or entry[0] is not calendar:
        if len(store) >= _MEMO_CALENDAR_LIMIT:
            store.clear()
        entry = (calendar, {})
        store[key] = entry
    return entry[1]


def _memo_lookup(memo: dict, value: object) -> object:
    try:
        return memo.get(value, _MEMO_MISS)
    except TypeError:  # unhashable input: fall through to the raising path
        return _MEMO_MISS


def schedule_slot(value: object) -> tuple[bool, int]:
    """Return (is_workday, seconds since midnight) in Asia/Shanghai."""
    calendar = ACTIVE_SCHEDULE.get()
    memo = _memo(_slot_memos, calendar)
    cached = _memo_lookup(memo, value)
    if cached is not _MEMO_MISS:
        return cast(tuple[bool, int], cached)
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("work schedule requires a timezone-aware timestamp")
    local = timestamp.tz_convert("Asia/Shanghai")
    second = local.hour * 3600 + local.minute * 60 + local.second
    working = calendar.slot_is_working(local.date(), second) if calendar else local.weekday() < 6
    result = (working, second)
    if len(memo) < _MEMO_ENTRY_LIMIT:
        memo[timestamp] = result
    return result


def schedule_day(value: object) -> bool:
    """Whole-day context for SOC, distinct from whether a shift is active now."""
    calendar = ACTIVE_SCHEDULE.get()
    memo = _memo(_day_memos, calendar)
    cached = _memo_lookup(memo, value)
    if cached is not _MEMO_MISS:
        return cast(bool, cached)
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("work schedule requires a timezone-aware timestamp")
    local = timestamp.tz_convert("Asia/Shanghai")
    result = calendar.day_is_working(local.date()) if calendar else local.weekday() < 6
    if len(memo) < _MEMO_ENTRY_LIMIT:
        memo[timestamp] = result
    return result


def schedule_policy() -> str:
    return PRODUCTION_SCHEDULE_POLICY if ACTIVE_SCHEDULE.get() else WORK_SCHEDULE_POLICY


def schedule_manifest() -> dict | None:
    calendar = ACTIVE_SCHEDULE.get()
    return calendar.manifest() if calendar else None


def schedule_interpolate(values: pd.Series, *, limit: int) -> pd.Series:
    """Interpolate short gaps without crossing a work/rest boundary."""
    workdays = pd.Series([schedule_slot(value)[0] for value in values.index], index=values.index)
    segments = workdays.ne(workdays.shift()).cumsum()
    return values.groupby(segments).transform(
        lambda group: group.interpolate(method="time", limit=limit, limit_area="inside")
    )


@dataclass(frozen=True)
class LoadScheduleProfile:
    values: dict[tuple[bool, int], float]

    @classmethod
    def fit(cls, frame: pd.DataFrame, *, origin: datetime) -> "LoadScheduleProfile":
        # Filter before inspecting values: validation targets are never inputs.
        history = frame.loc[frame["ds"] < origin, ["ds", "y"]].copy()
        history["y"] = pd.to_numeric(history["y"], errors="coerce")
        history = history.loc[np.isfinite(history["y"]) & (history["y"] >= 0)]
        if history.empty:
            raise M3Error("insufficient_history", "No usable schedule history")
        slots = [schedule_slot(value) for value in history["ds"]]
        history["workday"] = [slot[0] for slot in slots]
        history["second"] = [slot[1] for slot in slots]
        return cls(history.groupby(["workday", "second"])["y"].median().to_dict())

    def offsets(self, timestamps) -> np.ndarray:
        try:
            return np.asarray(
                [self.values[schedule_slot(value)] for value in timestamps],
                dtype=float,
            )
        except KeyError:
            # Never substitute working-day history for a missing rest-day slot.
            raise M3Error(
                "insufficient_history", "Missing matching work schedule history"
            ) from None


def schedule_forecast(
    engine,
    frame: pd.DataFrame,
    *,
    origin: datetime,
    periods: int,
    model_name: str,
) -> pd.DataFrame:
    """Fit residuals and restore the target day's own load profile."""
    profile = LoadScheduleProfile.fit(frame, origin=origin)
    training = frame.loc[frame["ds"] < origin].copy()
    training["y"] = training["y"].to_numpy(dtype=float) - profile.offsets(training["ds"])
    predicted = engine.forecast(df=training, h=periods).copy()
    try:
        residuals = pd.to_numeric(predicted[model_name], errors="raise").to_numpy(dtype=float)
        if len(predicted) != periods or not np.isfinite(residuals).all():
            raise ValueError("invalid residual forecast")
        predicted[model_name] = residuals + profile.offsets(predicted["ds"])
    except (KeyError, TypeError, ValueError):
        raise M3Error("forecast_values_invalid", "Schedule forecast is invalid") from None
    return predicted


def schedule_cross_validation(
    engine, frame: pd.DataFrame, *, model_name: str, horizon: int = 96, windows: int = 7
) -> pd.DataFrame:
    """Refit the calendar baseline inside each rolling validation window."""
    results = []
    for window in range(windows, 0, -1):
        split = len(frame) - window * horizon
        if split <= 0:
            raise M3Error("insufficient_history", "No schedule validation history")
        holdout = frame.iloc[split : split + horizon]
        origin = pd.Timestamp(holdout["ds"].iloc[0]).to_pydatetime()
        prediction = schedule_forecast(
            engine, frame.iloc[:split], origin=origin,
            periods=horizon, model_name=model_name,
        )
        if list(prediction["ds"]) != list(holdout["ds"]):
            raise M3Error("forecast_alignment_invalid", "Schedule validation is misaligned")
        result = holdout.copy()
        result[model_name] = prediction[model_name].to_numpy()
        result["cutoff"] = frame["ds"].iloc[split - 1]
        results.append(result)
    return pd.concat(results, ignore_index=True)
