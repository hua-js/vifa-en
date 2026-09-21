"""Regression cases for production schedule merging; no live services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

import httpx

from m3.worker.clients.production_schedule import ProductionScheduleClient
from m3.worker.domain.production_schedule import use_schedule
from m3.worker.domain.work_schedule import schedule_day, schedule_slot
from m3.worker.errors import M3Error

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 9, 27)  # Sunday; API can report actual working day.


def at(hour):
    return datetime(2026, 9, 27, hour, tzinfo=SHANGHAI)


def fact(day=DAY, status="REST"):
    return {"date": day.isoformat(), "deptCode": "113", "status": status,
            "scheduledHeadcount": 142, "workHeadcount": 0, "restHeadcount": 142,
            "holidayHeadcount": 0, "workRatio": 0, "plannedHours": 0}


def overtime(period, *, stations=None, identifier=1):
    return {"id": identifier, "production_date": DAY.isoformat(),
            "stations": stations or ["ES02"], "period": period, "status": "confirmed",
            "createdAt": "2026-09-21T01:00:00Z", "updatedAt": "2026-09-21T01:00:00Z"}


class ProductionScheduleTests(unittest.TestCase):
    def load(self, rows, overrides=(), *, code=200, forecast_start=DAY):
        calls = []
        def response(request):
            calls.append(request)
            return httpx.Response(200, json={"code": code, "data": {
                "startDate": DAY.isoformat(), "endDate": DAY.isoformat(),
                "scheduleHorizonEnd": DAY.isoformat(), "total": len(rows), "rows": rows,
            }})
        class OvertimeApi:
            def list_records_all(self, *args, **kwargs):
                return list(overrides)
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            provider = ProductionScheduleClient("private-test-key", http, OvertimeApi(),
                [SimpleNamespace(station_id="full-ES02", station_key="station_2")], lambda: at(0))
            calendar = provider.load("full-ES02", DAY, DAY, forecast_start=forecast_start)
        return calendar, calls

    def test_sunday_actual_work_replaces_fixed_weekly_rest(self):
        calendar, calls = self.load([fact(status="WORK")])
        with use_schedule(calendar):
            self.assertTrue(schedule_slot(at(9))[0])
            self.assertFalse(schedule_slot(at(13))[0])
            self.assertTrue(schedule_day(at(0)))
        self.assertEqual(calls[0].url.params["deptCodes"], "113")

    def test_half_day_overtime_does_not_turn_evening_into_work(self):
        calendar, _ = self.load([fact()], [overtime("afternoon")])
        with use_schedule(calendar):
            self.assertFalse(schedule_slot(at(9))[0])
            self.assertTrue(schedule_slot(at(15))[0])
            self.assertFalse(schedule_slot(at(20))[0])
            self.assertTrue(schedule_day(at(0)))

    def test_other_station_exception_does_not_apply(self):
        calendar, _ = self.load([fact()], [overtime("all_day", stations=["ES01"])])
        with use_schedule(calendar):
            self.assertFalse(schedule_slot(at(15))[0])
            self.assertFalse(schedule_day(at(0)))

    def test_all_day_excludes_meals_and_night(self):
        calendar, _ = self.load([], [overtime("all_day")])
        with use_schedule(calendar):
            for hour in (9, 15, 20):
                self.assertTrue(schedule_slot(at(hour))[0])
            for hour in (0, 12, 13, 18, 21, 23):
                self.assertFalse(schedule_slot(at(hour))[0])

    def test_missing_future_is_not_rest(self):
        with self.assertRaises(M3Error) as error:
            self.load([])
        self.assertEqual(error.exception.code, "schedule_unavailable")

    def test_partial_override_does_not_hide_unknown_future(self):
        with self.assertRaises(M3Error) as error:
            self.load([], [overtime("morning")])
        self.assertEqual(error.exception.code, "schedule_unavailable")

    def test_overlapping_all_day_records_are_rejected(self):
        with self.assertRaises(M3Error) as error:
            self.load([fact()], [overtime("all_day"), overtime("morning", identifier=2)])
        self.assertEqual(error.exception.code, "schedule_overtime_conflict")

    def test_business_error_with_http_200_is_not_empty_success(self):
        with self.assertRaises(M3Error) as error:
            self.load([], code=401)
        self.assertEqual(error.exception.code, "schedule_source_rejected")

    def test_historical_absence_has_explicit_provenance(self):
        calendar, _ = self.load([], forecast_start=date(2026, 9, 28))
        self.assertEqual(calendar.manifest()["historical_fallback_dates"], [DAY.isoformat()])
        self.assertNotIn("private-test-key", json.dumps(calendar.manifest()))

    def test_scope_is_reset_after_failure_and_isolated_across_threads(self):
        work, _ = self.load([fact(status="WORK")])
        rest, _ = self.load([fact()])
        def read(calendar):
            with use_schedule(calendar):
                return schedule_slot(at(9))[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(read, [work, rest])), [True, False])
        with self.assertRaises(RuntimeError):
            with use_schedule(work):
                raise RuntimeError("test")
        self.assertFalse(schedule_slot(at(9))[0])

class IntradaySocScheduleTests(unittest.TestCase):
    def test_afternoon_run_ranks_same_time_anchor_instead_of_midnight_soc(self):
        from datetime import timedelta
        import pandas as pd
        from m3.worker.domain.custom_forecasting import soc_schedule_values
        origin = datetime(2026, 9, 21, 14, tzinfo=SHANGHAI)
        rows = []
        # A week ago matches the afternoon anchor; two weeks ago matches only
        # midnight. The latter must not be selected for an afternoon forecast.
        for days, midnight, anchor, delta in ((7, 10, 50, 1), (14, 50, 80, -5)):
            day = origin - timedelta(days=days)
            rows.extend([
                (day.replace(hour=0), midnight),
                (day - timedelta(minutes=15), anchor),
                (day, anchor),
                (day + timedelta(minutes=15), anchor + delta),
            ])
        rows.append((origin - timedelta(minutes=15), 50))
        frame = pd.DataFrame([{"unique_id": "storage_soc", "ds": t, "y": y} for t, y in rows])
        dataset = SimpleNamespace(frame=frame, imputed_keys=frozenset(), interval_seconds=900)
        self.assertEqual(soc_schedule_values(dataset, origin=origin, periods=2), [50, 51])
