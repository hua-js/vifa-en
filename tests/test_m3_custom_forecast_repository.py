"""Regression tests for idempotent custom forecast point persistence."""

from dataclasses import replace
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
    RUN_FIELDS,
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


def make_series(
    unique_id: str, *, model_name: str = "SeasonalNaive"
) -> CustomForecastSeries:
    is_load = unique_id == "station_total_load"
    value = 800.0 if is_load else 55.0
    return CustomForecastSeries(
        unique_id=unique_id,
        unit="kW" if is_load else "%",
        model_name=model_name,
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


def persisted_run_row(
    *,
    run_id: str,
    status: str,
    completed_at: str,
    selection_policy: str,
) -> dict:
    return {
        "id": len(run_id),
        "run_id": run_id,
        "station_id": "ES02",
        "idempotency_key": f"{run_id}-key",
        "history_start": "2026-08-04T00:00:00+08:00",
        "history_end": "2026-09-01T00:00:00+08:00",
        "history_days": 28,
        "forecast_start": "2026-09-01T00:00:00+08:00",
        "forecast_end": "2026-09-02T00:00:00+08:00",
        "forecast_days": 1,
        "interval_seconds": 60,
        "points_per_day": 1440,
        "expected_points_per_series": 1440,
        "model_policy": "full_selection",
        "status": status,
        "model_manifest": {"selection_policy": selection_policy},
        "source_manifest": {},
        "content_hash": "a" * 64,
        "error_code": None,
        "requested_by": "m3_operations_api",
        "started_at": "2026-09-01T00:00:00+08:00",
        "completed_at": completed_at,
        "evaluated_at": completed_at if status == "evaluated" else None,
        "createdAt": completed_at,
        "updatedAt": completed_at,
    }


class LatestRunsApi:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_records(self, collection, *, filter, fields, sort=None):
        if collection != "energy_forecast_manual_runs":
            raise AssertionError(collection)
        if filter != {
            "station_id": "ES02",
            "interval_seconds": 60,
            "forecast_days": 1,
            "status": {"$in": ["succeeded", "evaluated"]},
        }:
            raise AssertionError(filter)
        if fields != RUN_FIELDS:
            raise AssertionError(fields)
        if sort != ["-completed_at", "-createdAt"]:
            raise AssertionError(sort)
        return [dict(row) for row in self.rows]


class CustomForecastRepositoryTests(unittest.TestCase):
    def test_latest_usable_run_skips_newer_incompatible_terminal_runs(self):
        """A stale task format must not hide the newest usable matching forecast."""
        api = LatestRunsApi(
            [
                persisted_run_row(
                    run_id="newer-legacy-run",
                    status="succeeded",
                    completed_at="2026-09-01T12:02:00+08:00",
                    selection_policy="weekly_load_v1",
                ),
                persisted_run_row(
                    run_id="latest-minute-run",
                    status="evaluated",
                    completed_at="2026-09-01T12:01:00+08:00",
                    selection_policy="weekly_load_v2",
                ),
                persisted_run_row(
                    run_id="older-minute-run",
                    status="succeeded",
                    completed_at="2026-09-01T12:00:00+08:00",
                    selection_policy="weekly_load_v2",
                ),
            ]
        )

        run = CustomForecastRepository(api).latest_usable(
            "ES02",
            interval_seconds=60,
            forecast_days=1,
            selection_policy="weekly_load_v2",
        )

        self.assertIsNotNone(run)
        self.assertEqual(run.run_id, "latest-minute-run")

    def test_persists_realized_point_model_when_selection_evidence_differs(self):
        """Point rows remain authoritative for a selected-winner final fallback."""
        run = replace(
            make_run(),
            model_manifest={
                "selection_policy": "weekly_load_v1",
                "series": {
                    "station_total_load": {
                        "model_name": "WeeklyWeighted2",
                        "realized_model_name": "WeeklyNaive",
                        "realized_status": "degraded",
                        "realized_fallback_reason": "RuntimeError",
                    }
                },
            },
        )
        series = [
            make_series("station_total_load", model_name="WeeklyNaive"),
            make_series("storage_soc", model_name="SeasonalNaive"),
        ]
        baselines = [
            make_series("station_total_load", model_name="WeeklyNaive"),
            make_series("storage_soc", model_name="SeasonalNaive"),
        ]

        values = CustomForecastRepository._point_values(run, series, baselines)

        load_rows = [
            value for value in values if value["unique_id"] == "station_total_load"
        ]
        self.assertEqual(len(load_rows), 24)
        self.assertEqual({value["model_name"] for value in load_rows}, {"WeeklyNaive"})

    def test_requires_weekly_naive_load_baseline_and_seasonal_naive_soc_baseline(self):
        """Changing either fixed baseline model must reject the persistence payload."""
        run = make_run()
        series = [
            make_series("station_total_load"),
            make_series("storage_soc"),
        ]
        baselines = [
            make_series("station_total_load", model_name="WeeklyNaive"),
            make_series("storage_soc", model_name="SeasonalNaive"),
        ]

        values = CustomForecastRepository._point_values(run, series, baselines)

        self.assertEqual(len(values), 48)
        for unique_id, model_name in (
            ("station_total_load", "SeasonalNaive"),
            ("storage_soc", "WeeklyNaive"),
        ):
            wrong_baselines = [
                make_series("station_total_load", model_name="WeeklyNaive"),
                make_series("storage_soc", model_name="SeasonalNaive"),
            ]
            wrong_baselines[0 if unique_id == "station_total_load" else 1] = (
                make_series(unique_id, model_name=model_name)
            )
            with self.subTest(unique_id=unique_id), self.assertRaises(M3Error):
                CustomForecastRepository._point_values(run, series, wrong_baselines)

    def test_accepts_a_failed_create_when_the_exact_batch_was_committed(self):
        api = AmbiguousCommitApi()
        repository = CustomForecastRepository(api)
        run = make_run()
        series = [
            make_series("station_total_load"),
            make_series("storage_soc"),
        ]
        baselines = [
            make_series("station_total_load", model_name="WeeklyNaive"),
            make_series("storage_soc"),
        ]

        digest = repository.store_points(run, series, baselines)

        self.assertEqual(api.create_calls, 1)
        self.assertEqual(len(api.rows), 48)
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
