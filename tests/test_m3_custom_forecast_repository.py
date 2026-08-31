"""Regression tests for idempotent custom forecast point persistence."""

from datetime import datetime, timedelta
import unittest

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastPoint,
    CustomForecastSeries,
    CustomRunRecord,
)
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)


FORECAST_START = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_run() -> StoredCustomRun:
    config = CustomForecastConfig(
        history_start=FORECAST_START - timedelta(days=28),
        history_end=FORECAST_START,
        history_days=28,
        forecast_start=FORECAST_START,
        forecast_end=FORECAST_START + timedelta(days=1),
        forecast_days=1,
        interval_seconds=3600,
        points_per_day=24,
        expected_points_per_series=24,
        model_policy="full_selection",
    )
    record = CustomRunRecord(
        run_id="ambiguous-write-run",
        station_id="ES01",
        idempotency_key="ambiguous-write-key",
        config=config,
        status="running",
    )
    return StoredCustomRun(
        record_id=14,
        record=record,
        model_manifest=None,
        source_manifest=None,
        content_hash=None,
        started_at=FORECAST_START,
        completed_at=None,
        evaluated_at=None,
        created_at=FORECAST_START,
        updated_at=FORECAST_START,
    )


def make_series(unique_id: str) -> CustomForecastSeries:
    is_load = unique_id == "station_total_load"
    value = 800.0 if is_load else 55.0
    return CustomForecastSeries(
        unique_id=unique_id,
        unit="kW" if is_load else "%",
        model_name="SeasonalNaive",
        status="ok",
        points=[
            CustomForecastPoint(
                target_time=FORECAST_START + timedelta(hours=index),
                horizon_step=index + 1,
                raw_forecast=value,
                forecast_value=value,
                is_clipped=False,
            )
            for index in range(24)
        ],
    )


class AmbiguousCommitApi:
    """Persist a bulk create, then emulate the duplicate HTTP 400 seen in production."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.create_calls = 0

    def list_records_all(self, collection, *, filter, fields, sort=None):
        self.assert_collection(collection)
        return [dict(row) for row in self.rows]

    def create_records(self, collection, values):
        self.assert_collection(collection)
        self.create_calls += 1
        self.rows = [
            {
                "id": index,
                **value,
                "actual_value": None,
                "actual_quality": None,
                "actual_source_revision": None,
                "actual_recorded_at": None,
                "evaluated_at": None,
                "absolute_percentage_error": None,
            }
            for index, value in enumerate(values, start=1)
        ]
        raise M3Error(
            "sink_http_failed",
            "NocoBase action failed",
            {"collection": collection, "action": "create", "status_code": 400},
        )

    @staticmethod
    def assert_collection(collection):
        if collection != "energy_forecast_manual_points":
            raise AssertionError(collection)


class CustomForecastRepositoryTests(unittest.TestCase):
    def test_accepts_a_failed_create_when_the_exact_batch_was_committed(self):
        api = AmbiguousCommitApi()
        repository = CustomForecastRepository(api)
        run = make_run()
        series = [
            make_series("station_total_load"),
            make_series("storage_soc"),
        ]

        digest = repository.store_points(run, series, series)

        self.assertEqual(api.create_calls, 1)
        self.assertEqual(len(api.rows), 48)
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
