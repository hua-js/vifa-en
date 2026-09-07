"""Prevent future or expired tasks from replacing today's dashboard result."""

from datetime import datetime, timedelta
import unittest

from m3_worker.custom_forecast_contracts import CustomForecastRequest
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import CustomForecastRepository
from m3_worker.services.custom_forecast_service import CustomForecastService
from tests.test_m3_custom_forecast_repository import persisted_run_row


class RunApi:
    def __init__(self, rows):
        self.rows = rows

    def first_or_create(self, *args, **kwargs):
        raise AssertionError("future history reached persistence")

    def list_records(self, collection, *, filter, fields, sort=None):
        def matches(row):
            for key, value in filter.items():
                if isinstance(value, dict):
                    for operator, target in value.items():
                        if operator == "$in" and row[key] not in target:
                            return False
                        if operator == "$lte" and row[key] > target:
                            return False
                        if operator == "$gt" and row[key] <= target:
                            return False
                elif row[key] != value:
                    return False
            return True
        return sorted(
            [row for row in self.rows if matches(row)],
            key=lambda row: row["completed_at"], reverse=True,
        )


def row(day, completed, *, station="ES01", interval=900, days=1):
    value = persisted_run_row(
        run_id=f"run-{day}-{completed}", status="succeeded",
        completed_at=f"2026-09-07T{completed}:00:00+08:00",
        selection_policy="weekly_load_v2", station_id=station,
        interval_seconds=interval, forecast_start=f"2026-09-{day}T00:00:00+08:00",
    )
    value["forecast_days"] = days
    value["forecast_end"] = (
        datetime.fromisoformat(value["forecast_start"]) + timedelta(days=days)
    ).isoformat()
    value["expected_points_per_series"] *= days
    return value


class CurrentDayForecastTests(unittest.TestCase):
    def service(self, rows, now="2026-09-07T14:00:00+08:00"):
        service = CustomForecastService(
            CustomForecastRepository(RunApi(rows)), object(),
            now=lambda: datetime.fromisoformat(now), station_ids=("ES01", "ES02"),
        )
        self.addCleanup(service.close)
        return service

    def test_today_wins_over_later_completed_future_and_historical_runs(self):
        for station in ("ES01", "ES02"):
            for interval in (30, 60, 300, 900, 1800, 3600):
                with self.subTest(station=station, interval=interval):
                    rows = [row(day, hour, station=station, interval=interval)
                            for day, hour in (("08", "12"), ("06", "11"), ("07", "01"))]
                    result = self.service(rows).latest(station, interval_seconds=interval, forecast_days=1)
                    self.assertEqual(result.run_id, "run-07-01")

    def test_no_today_returns_none_instead_of_future_or_expired_run(self):
        service = self.service([row("08", "12"), row("06", "11")])
        self.assertIsNone(service.latest("ES01", interval_seconds=900, forecast_days=1))

    def test_multi_day_run_can_cover_today_and_end_is_exclusive(self):
        rows = [row("06", "01", days=2)]
        result = self.service(rows).latest("ES01", interval_seconds=900, forecast_days=2)
        self.assertEqual(result.run_id, "run-06-01")
        expired = self.service(rows, now="2026-09-08T00:00:00+08:00")
        self.assertIsNone(expired.latest("ES01", interval_seconds=900, forecast_days=2))

    def test_midnight_rolls_selection_using_shanghai_date_even_with_utc_clock(self):
        rows = [row("07", "12"), row("08", "01")]
        before = self.service(rows, now="2026-09-07T15:59:59+00:00")
        after = self.service(rows, now="2026-09-07T16:00:00+00:00")
        self.assertEqual(before.latest("ES01", interval_seconds=900, forecast_days=1).run_id, "run-07-12")
        self.assertEqual(after.latest("ES01", interval_seconds=900, forecast_days=1).run_id, "run-08-01")

    def test_future_history_is_rejected_before_persistence(self):
        service = self.service([])
        request = CustomForecastRequest(
            history_start="2026-08-21T00:00:00+08:00",
            history_end="2026-09-08T00:00:00+08:00",
            forecast_days=1, interval_seconds=900, idempotency_key="future-history",
        )
        with self.assertRaises(M3Error) as raised:
            service.submit("ES01", request, requested_by=None)
        self.assertEqual(raised.exception.code, "request_invalid")

    def test_completed_history_is_accepted_at_shanghai_midnight(self):
        saved = row("07", "00")
        api = RunApi([saved])
        api.first_or_create = lambda *args, **kwargs: saved
        service = CustomForecastService(
            CustomForecastRepository(api), object(),
            now=lambda: datetime.fromisoformat("2026-09-06T16:00:00+00:00"),
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        request = CustomForecastRequest(
            history_start=saved["history_start"], history_end=saved["history_end"],
            forecast_days=1, interval_seconds=900,
            idempotency_key=saved["idempotency_key"],
        )
        result = service.submit("ES01", request, requested_by=saved["requested_by"])
        self.assertEqual(result.config.forecast_start.isoformat(), "2026-09-07T00:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
