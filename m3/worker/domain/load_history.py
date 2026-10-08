"""Bounded real-observation recovery shared by both load forecast paths."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

import pandas as pd

from m3.worker.domain.work_schedule import schedule_slot
from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE

MAX_LOAD_FALLBACK_DAYS = 28
MAX_WEEK_IMPUTATION_RATIO = 0.05
POST_HOLIDAY_LOAD_MODEL = "PostHolidayWorkdayReference"
POST_HOLIDAY_LOAD_REASON = "post_holiday_workday_history"
POST_HOLIDAY_LOAD_POLICY = "pre-holiday-complete-day-median-v1"


@dataclass(frozen=True)
class PostHolidayLoadReference:
    origin: datetime
    interval: timedelta
    values: tuple[float, ...]
    donors: tuple[tuple[datetime, tuple[datetime, ...]], ...]

    def manifest(self) -> dict:
        # Compact contiguous mappings so seven days at 30-second granularity do
        # not exceed the sink's response budget. Every source is reconstructible.
        segments = []
        previous_target = None
        previous_sources = ()
        for target, sources in self.donors:
            contiguous = previous_target is not None and target == previous_target + self.interval
            matching_sources = len(sources) == len(previous_sources) and all(
                source == previous + self.interval
                for source, previous in zip(sources, previous_sources)
            )
            if segments and contiguous and matching_sources:
                segments[-1]["point_count"] += 1
            else:
                segments.append({"target_start": target.isoformat(), "point_count": 1,
                                 "source_starts": [source.isoformat() for source in sources]})
            previous_target, previous_sources = target, sources
        return {
            "policy": POST_HOLIDAY_LOAD_POLICY,
            "interval_seconds": int(self.interval.total_seconds()),
            "segments": segments,
        }


def post_holiday_load_reference(
    observed: pd.Series, *, history_end: datetime, origin: datetime,
    interval: timedelta, periods: int,
) -> PostHolidayLoadReference | None:
    """Use complete real days only during the first week after a confirmed holiday.

    This is a forecast reference, never a repair to training or raw data. Every
    target must have pre-holiday donors with the same whole-day and slot schedule.
    """
    calendar = ACTIVE_SCHEDULE.get()
    if calendar is None or calendar.statuses.get(origin.date()) != "WORK":
        return None
    # Look back through at most six resumption days. Unknown calendar entries,
    # shutdowns and overtime do not qualify as a confirmed holiday interval.
    day = origin.date() - timedelta(days=1)
    for _ in range(7):
        if calendar.statuses.get(day) == "HOLIDAY" and not calendar.overtime.get(day):
            break
        if calendar.statuses.get(day) not in {"WORK", "REST"}:
            return None
        day -= timedelta(days=1)
    else:
        return None
    holiday_end = day
    while calendar.statuses.get(day) == "HOLIDAY" and not calendar.overtime.get(day):
        day -= timedelta(days=1)
    holiday_start = day + timedelta(days=1)
    if (holiday_end - holiday_start).days + 1 < 3:
        return None

    valid = observed[observed.notna() & (observed >= 0) & (observed < float("inf"))].copy()
    valid = valid[valid.index < history_end]
    valid = valid[valid.index >= history_end - timedelta(days=MAX_LOAD_FALLBACK_DAYS)]
    points_per_day = int(timedelta(days=1) / interval)
    complete_days = []
    for donor_day in sorted(set(valid.index.date), reverse=True):
        if donor_day >= holiday_start or donor_day not in calendar.statuses:
            continue
        midnight = pd.Timestamp(origin).normalize() + timedelta(days=(donor_day - origin.date()).days)
        slots = pd.date_range(midnight, periods=points_per_day, freq=interval)
        if not slots.isin(valid.index).all():
            continue
        signature = tuple(schedule_slot(slot)[0] for slot in slots)
        complete_days.append((donor_day, calendar.day_is_working(donor_day), signature))

    values, evidence = [], []
    matches_by_day = {}
    for step in range(periods):
        target = origin + step * interval
        target_day = target.date()
        if target_day not in matches_by_day:
            midnight = pd.Timestamp(target).normalize()
            signature = tuple(schedule_slot(slot)[0] for slot in
                              pd.date_range(midnight, periods=points_per_day, freq=interval))
            working = calendar.day_is_working(target_day)
            matches_by_day[target_day] = [
                day for day, donor_working, donor_signature in complete_days
                if donor_working == working and donor_signature == signature
            ][:3]
        sources = tuple(target - timedelta(days=(target_day - day).days)
                        for day in matches_by_day[target_day])
        if not sources:
            return None
        values.append(float(median(float(valid.loc[source]) for source in sources)))
        evidence.append((target, sources))
    return PostHolidayLoadReference(origin, interval, tuple(values), tuple(evidence))


def fill_load_schedule_gaps(
    frame: pd.DataFrame,
    observed_load: pd.Series,
    imputed_times: set[datetime],
    *,
    history_end: datetime,
    interval: timedelta,
    points_per_day: int,
    history_days: int,
) -> list[tuple[datetime, datetime]]:
    """Fill a training copy from earlier real samples; preserve imputation evidence."""
    def week_key(timestamp):
        return (history_end - timestamp - interval) // timedelta(days=7)

    weekly_imputed = Counter(week_key(t) for t in imputed_times)
    budget = int(points_per_day * 7 * MAX_WEEK_IMPUTATION_RATIO)
    donors = []
    for value in frame.index[frame["y"].isna()]:
        week = week_key(value)
        if weekly_imputed[week] >= budget:
            continue
        working = schedule_slot(value)[0]
        for days in range(1, min(MAX_LOAD_FALLBACK_DAYS, history_days) + 1):
            donor = value - timedelta(days=days)
            if donor not in observed_load.index:
                continue
            observed = observed_load.loc[donor]
            if pd.isna(observed) or observed < 0:
                continue
            if schedule_slot(donor)[0] != working:
                continue
            frame.loc[value, "y"] = observed
            imputed_times.add(value.to_pydatetime())
            weekly_imputed[week] += 1
            donors.append((value.to_pydatetime(), donor.to_pydatetime()))
            break
    return donors
