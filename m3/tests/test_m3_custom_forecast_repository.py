"""Regression tests for idempotent custom forecast point persistence."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from m3.worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastPoint,
    CustomForecastSeries,
    CustomRunRecord,
)
from m3.worker.errors import M3Error
from m3.worker.services.custom_forecast_repository import (
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
    station_id: str = "ES02",
    interval_seconds: int = 60,
    forecast_start: str = "2026-09-01T00:00:00+08:00",
    requested_by: str = "m3_operations_api",
) -> dict:
    forecast_start_at = datetime.fromisoformat(forecast_start)
    return {
        "id": len(run_id),
        "run_id": run_id,
        "station_id": station_id,
        "idempotency_key": f"{run_id}-key",
        "history_start": (forecast_start_at - timedelta(days=28)).isoformat(),
        "history_end": forecast_start,
        "history_days": 28,
        "forecast_start": forecast_start,
        "forecast_end": (forecast_start_at + timedelta(days=1)).isoformat(),
        "forecast_days": 1,
        "interval_seconds": interval_seconds,
        "points_per_day": 86_400 // interval_seconds,
        "expected_points_per_series": 86_400 // interval_seconds,
        "model_policy": "full_selection",
        "status": status,
        "model_manifest": {"selection_policy": selection_policy},
        "source_manifest": {},
        "content_hash": "a" * 64,
        "error_code": None,
        "requested_by": requested_by,
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
            "forecast_start": {"$lte": "2026-09-01T00:00:00+08:00"},
            "forecast_end": {"$gt": "2026-09-01T00:00:00+08:00"},
        }:
            raise AssertionError(filter)
        if fields != RUN_FIELDS:
            raise AssertionError(fields)
        if sort != ["-completed_at", "-createdAt"]:
            raise AssertionError(sort)
        return [dict(row) for row in self.rows]


class TemplateRunsApi:
    def __init__(self, rows: list[dict], *, interval_seconds: int = 900) -> None:
        self.rows = rows
        self.interval_seconds = interval_seconds
        self.calls = 0

    def list_first_record(self, collection, *, filter, fields, sort):
        self.calls += 1
        if collection != "energy_forecast_manual_runs":
            raise AssertionError(collection)
        if filter != {
            "station_id": "ES01",
            "interval_seconds": self.interval_seconds,
            "status": {"$in": ["succeeded", "evaluated"]},
        }:
            raise AssertionError(filter)
        if fields != RUN_FIELDS:
            raise AssertionError(fields)
        if sort != ["-completed_at", "-createdAt"]:
            raise AssertionError(sort)
        return None if not self.rows else dict(self.rows[0])


class DailyRunsApi:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls = 0

    def list_records_all(self, collection, *, filter, fields, sort=None):
        self.calls += 1
        if collection != "energy_forecast_manual_runs":
            raise AssertionError(collection)
        if filter != {
            "station_id": "ES01",
            "forecast_start": "2026-09-02T00:00:00+08:00",
            "interval_seconds": 900,
            "requested_by": "m3_daily_scheduler",
        }:
            raise AssertionError(filter)
        if fields != RUN_FIELDS:
            raise AssertionError(fields)
        if sort != ["createdAt"]:
            raise AssertionError(sort)
        return [dict(row) for row in self.rows]


class CustomForecastRepositoryTests(unittest.TestCase):
    def test_latest_completed_template_returns_the_newest_terminal_interval_run(self):
        """Removing the interval filter could return a template at the wrong cadence."""
        api = TemplateRunsApi(
            [
                persisted_run_row(
                    run_id="newest-quarter-hour-run",
                    status="evaluated",
                    completed_at="2026-09-02T12:01:00+08:00",
                    selection_policy="weekly_load_v2",
                    station_id="ES01",
                    interval_seconds=900,
                ),
                persisted_run_row(
                    run_id="older-quarter-hour-run",
                    status="succeeded",
                    completed_at="2026-09-02T12:00:00+08:00",
                    selection_policy="weekly_load_v2",
                    station_id="ES01",
                    interval_seconds=900,
                ),
            ]
        )

        run = CustomForecastRepository(api).latest_completed_template(
            "ES01", interval_seconds=900
        )

        self.assertIsNotNone(run)
        self.assertEqual(run.run_id, "newest-quarter-hour-run")
        self.assertEqual(api.calls, 1)

    def test_latest_completed_template_returns_none_when_no_terminal_run_exists(self):
        """An empty bounded query must not manufacture a template."""
        api = TemplateRunsApi([])

        self.assertIsNone(
            CustomForecastRepository(api).latest_completed_template(
                "ES01", interval_seconds=900
            )
        )
        self.assertEqual(api.calls, 1)

    def test_latest_completed_template_rejects_a_malformed_newest_row(self):
        """Skipping a corrupt newest row would silently select a stale template."""
        malformed = persisted_run_row(
            run_id="malformed-quarter-hour-run",
            status="evaluated",
            completed_at="2026-09-02T12:01:00+08:00",
            selection_policy="weekly_load_v2",
            station_id="ES01",
            interval_seconds=900,
        )
        del malformed["run_id"]
        api = TemplateRunsApi([malformed])

        with self.assertRaises(M3Error) as raised:
            CustomForecastRepository(api).latest_completed_template(
                "ES01", interval_seconds=900
            )

        self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_latest_completed_template_rejects_response_outside_query_identity(self):
        """A filtered first row must still prove station, cadence, state, and completion."""
        missing_completion = persisted_run_row(
            run_id="missing-completion",
            status="succeeded",
            completed_at="2026-09-02T12:01:00+08:00",
            selection_policy="weekly_load_v2",
            station_id="ES01",
            interval_seconds=900,
        )
        missing_completion["completed_at"] = None
        invalid_rows = (
            persisted_run_row(
                run_id="wrong-station",
                status="succeeded",
                completed_at="2026-09-02T12:01:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES02",
                interval_seconds=900,
            ),
            persisted_run_row(
                run_id="wrong-interval",
                status="succeeded",
                completed_at="2026-09-02T12:01:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES01",
                interval_seconds=60,
            ),
            persisted_run_row(
                run_id="wrong-status",
                status="queued",
                completed_at="2026-09-02T12:01:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES01",
                interval_seconds=900,
            ),
            missing_completion,
        )
        for invalid_row in invalid_rows:
            with self.subTest(run_id=invalid_row["run_id"]):
                api = TemplateRunsApi([invalid_row])

                with self.assertRaises(M3Error) as raised:
                    CustomForecastRepository(api).latest_completed_template(
                        "ES01", interval_seconds=900
                    )

                self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_latest_completed_template_rejects_an_unallowed_interval_without_querying(self):
        """An unsupported cadence must not broaden the repository query."""
        api = TemplateRunsApi([], interval_seconds=17)

        with self.assertRaises(ValueError):
            CustomForecastRepository(api).latest_completed_template(
                "ES01", interval_seconds=17
            )

        self.assertEqual(api.calls, 0)

    def test_list_daily_runs_returns_only_the_requested_daily_attempts(self):
        """A mismatched scheduler identity or forecast day is a persistence contract error."""
        daily_start = datetime.fromisoformat("2026-09-02T00:00:00+08:00")
        api = DailyRunsApi(
            [
                persisted_run_row(
                    run_id="first-daily-attempt",
                    status="succeeded",
                    completed_at="2026-09-02T00:05:00+08:00",
                    selection_policy="weekly_load_v2",
                    station_id="ES01",
                    interval_seconds=900,
                    forecast_start=daily_start.isoformat(),
                    requested_by="m3_daily_scheduler",
                ),
                persisted_run_row(
                    run_id="second-daily-attempt",
                    status="succeeded",
                    completed_at="2026-09-02T00:10:00+08:00",
                    selection_policy="weekly_load_v2",
                    station_id="ES01",
                    interval_seconds=900,
                    forecast_start=daily_start.isoformat(),
                    requested_by="m3_daily_scheduler",
                ),
            ]
        )

        runs = CustomForecastRepository(api).list_daily_runs(
            "ES01", forecast_start=daily_start, interval_seconds=900
        )

        self.assertEqual([run.run_id for run in runs], ["first-daily-attempt", "second-daily-attempt"])
        self.assertEqual(api.calls, 1)

    def test_list_daily_runs_rejects_invalid_forecast_start_without_querying(self):
        """Daily lookup must be a Shanghai-midnight query before reaching NocoBase."""
        invalid_starts = (
            datetime(2026, 9, 2),
            datetime.fromisoformat("2026-09-02T00:00:00+00:00"),
            datetime.fromisoformat("2026-09-02T00:15:00+08:00"),
        )
        for forecast_start in invalid_starts:
            with self.subTest(forecast_start=forecast_start):
                api = DailyRunsApi([])

                with self.assertRaises(ValueError):
                    CustomForecastRepository(api).list_daily_runs(
                        "ES01", forecast_start=forecast_start, interval_seconds=900
                    )

                self.assertEqual(api.calls, 0)

    def test_list_daily_runs_rejects_an_unallowed_interval_without_querying(self):
        """An unsupported cadence must not broaden the daily-state query."""
        api = DailyRunsApi([])

        with self.assertRaises(ValueError):
            CustomForecastRepository(api).list_daily_runs(
                "ES01",
                forecast_start=datetime.fromisoformat(
                    "2026-09-02T00:00:00+08:00"
                ),
                interval_seconds=17,
            )

        self.assertEqual(api.calls, 0)

    def test_list_daily_runs_rejects_mismatched_persisted_identity(self):
        """A server response outside the daily query identity must not be trusted."""
        invalid_rows = (
            persisted_run_row(
                run_id="wrong-station",
                status="succeeded",
                completed_at="2026-09-02T00:05:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES02",
                interval_seconds=900,
                forecast_start="2026-09-02T00:00:00+08:00",
                requested_by="m3_daily_scheduler",
            ),
            persisted_run_row(
                run_id="wrong-requester",
                status="succeeded",
                completed_at="2026-09-02T00:05:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES01",
                interval_seconds=900,
                forecast_start="2026-09-02T00:00:00+08:00",
                requested_by="m3_operations_api",
            ),
            persisted_run_row(
                run_id="wrong-forecast-day",
                status="succeeded",
                completed_at="2026-09-02T00:05:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES01",
                interval_seconds=900,
                forecast_start="2026-09-03T00:00:00+08:00",
                requested_by="m3_daily_scheduler",
            ),
            persisted_run_row(
                run_id="wrong-interval",
                status="succeeded",
                completed_at="2026-09-02T00:05:00+08:00",
                selection_policy="weekly_load_v2",
                station_id="ES01",
                interval_seconds=60,
                forecast_start="2026-09-02T00:00:00+08:00",
                requested_by="m3_daily_scheduler",
            ),
        )
        for invalid_row in invalid_rows:
            with self.subTest(run_id=invalid_row["run_id"]):
                api = DailyRunsApi([invalid_row])

                with self.assertRaises(M3Error) as raised:
                    CustomForecastRepository(api).list_daily_runs(
                        "ES01",
                        forecast_start=datetime.fromisoformat(
                            "2026-09-02T00:00:00+08:00"
                        ),
                        interval_seconds=900,
                    )

                self.assertEqual(raised.exception.code, "sink_contract_invalid")

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
            covering_at=datetime.fromisoformat("2026-09-01T12:03:00+08:00"),
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
