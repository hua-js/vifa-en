"""Immutable, task-scoped production calendar shared by load and SOC."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from m3.worker.errors import M3Error

PRODUCTION_SCHEDULE_POLICY = "production-api-overtime-v1"
# Per-capita planned hours separating a half-day Saturday (~4h) from a full
# makeup workday Saturday (~8h) for station 2. Calibrated 2026-10-10 from
# dept 113 rows: half-day 572/142=4.0h, full-day 1144/144=7.9h; midpoint 6h.
STATION2_SATURDAY_FULL_DAY_MIN_HOURS = 6.0
PERIODS = {
    "morning": ((8 * 3600, 12 * 3600),),
    "afternoon": ((14 * 3600, 18 * 3600),),
    "evening": ((19 * 3600, 21 * 3600),),
    "all_day": ((8 * 3600, 12 * 3600), (14 * 3600, 18 * 3600), (19 * 3600, 21 * 3600)),
}
ACTIVE_SCHEDULE: ContextVar["ProductionSchedule | None"] = ContextVar("m3_production_schedule", default=None)


@dataclass(frozen=True)
class ProductionSchedule:
    station_id: str
    station_key: str
    department: str
    start: date
    end: date
    forecast_start: date
    statuses: Mapping[date, str]
    overtime: Mapping[date, tuple[str, ...]]
    provenance_json: str
    # Per-capita planned hours (plannedHours / scheduledHeadcount) for days the
    # API reported; absent for zero-headcount days and historical fallback days.
    planned_hours: Mapping[date, float] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "statuses", MappingProxyType(dict(self.statuses)))
        object.__setattr__(self, "overtime", MappingProxyType(dict(self.overtime)))
        object.__setattr__(self, "planned_hours", MappingProxyType(dict(self.planned_hours)))

    def day_is_working(self, day: date) -> bool:
        if not self.start <= day <= self.end:
            raise M3Error("schedule_out_of_range", "排班查询范围不足")
        if self.overtime.get(day):
            return True
        status = self.statuses.get(day)
        if status is not None:
            return status == "WORK"
        if day >= self.forecast_start:
            raise M3Error("schedule_unavailable", "预测日期尚无生产排班")
        return day.weekday() < 6

    def slot_is_working(self, day: date, second: int) -> bool:
        if not self.start <= day <= self.end:
            raise M3Error("schedule_out_of_range", "排班查询范围不足")
        periods = self.overtime.get(day, ())
        if any(start <= second < end for period in periods for start, end in PERIODS[period]):
            return True
        status = self.statuses.get(day)
        if status is None:
            if day >= self.forecast_start:
                # Only a confirmed whole-day exception defines a complete unknown day.
                if "all_day" in periods:
                    return False
                raise M3Error("schedule_unavailable", "预测日期尚无生产排班")
            status = "WORK" if day.weekday() < 6 else "REST"
        # API provides day status only. Time windows are the agreed local policy.
        # Station 2 Saturdays default to the morning-only shift; a makeup
        # full-workday Saturday is recognised by per-capita planned hours
        # (half-day ~4h vs full-day ~8h) without inferring arbitrary windows.
        base_period = "all_day"
        if self.station_key == "station_2" and day.weekday() == 5:
            hours = self.planned_hours.get(day)
            if hours is None or hours < STATION2_SATURDAY_FULL_DAY_MIN_HOURS:
                base_period = "morning"
        return status == "WORK" and any(start <= second < end for start, end in PERIODS[base_period])

    def manifest(self) -> dict:
        value = json.loads(self.provenance_json)
        value["content_hash"] = sha256(self.provenance_json.encode()).hexdigest()
        return value


@contextmanager
def use_schedule(schedule: ProductionSchedule | None):
    token = ACTIVE_SCHEDULE.set(schedule)
    try:
        yield schedule
    finally:
        ACTIVE_SCHEDULE.reset(token)
