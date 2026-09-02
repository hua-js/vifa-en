"""Daily custom forecast template rolling, recovery, and retry coverage."""

from datetime import datetime, timedelta
from threading import Event, Thread
import unittest

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastRequest,
    CustomRunRecord,
    build_custom_forecast_config,
)
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import StoredCustomRun
from m3_worker.services.daily_custom_forecast_service import (
    DAILY_REQUESTED_BY,
    DailyCustomForecastService,
)


DAY = datetime.fromisoformat("2026-09-02T00:00:00+08:00")
NOW = DAY.replace(hour=0, minute=17)


def make_run(
    run_id: str,
    *,
    station_id: str = "ES01",
    interval_seconds: int = 900,
    history_days: int = 28,
    forecast_days: int = 1,
    forecast_start: datetime | None = None,
    status: str = "succeeded",
    idempotency_key: str | None = None,
    requested_by: str | None = "m3_operations_api",
    created_offset: int = 0,
) -> StoredCustomRun:
    start = forecast_start or DAY - timedelta(days=1)
    config = CustomForecastConfig(
        history_start=start - timedelta(days=history_days),
        history_end=start,
        history_days=history_days,
        forecast_start=start,
        forecast_end=start + timedelta(days=forecast_days),
        forecast_days=forecast_days,
        interval_seconds=interval_seconds,
        points_per_day=86_400 // interval_seconds,
        expected_points_per_series=(86_400 // interval_seconds) * forecast_days,
        model_policy=(
            "seasonal_naive_only" if history_days < 28 else "full_selection"
        ),
    )
    created_at = DAY + timedelta(minutes=created_offset)
    return StoredCustomRun(
        record_id=created_offset + 1,
        record=CustomRunRecord(
            run_id=run_id,
            station_id=station_id,
            idempotency_key=idempotency_key or f"{run_id}-key",
            config=config,
            status=status,
            requested_by=requested_by,
            error_code="training_failed" if status == "failed" else None,
        ),
        model_manifest=None,
        source_manifest=None,
        content_hash=None,
        started_at=None,
        completed_at=(
            created_at if status in {"succeeded", "evaluated", "failed"} else None
        ),
        evaluated_at=created_at if status == "evaluated" else None,
        created_at=created_at,
        updated_at=created_at,
    )


def request_for(run: StoredCustomRun) -> CustomForecastRequest:
    return CustomForecastRequest(
        history_start=run.config.history_start,
        history_end=run.config.history_end,
        forecast_days=run.config.forecast_days,
        interval_seconds=run.config.interval_seconds,
        idempotency_key=run.record.idempotency_key,
    )


class InMemoryRepository:
    def __init__(
        self,
        templates: (
            dict[tuple[str, int], StoredCustomRun | BaseException] | None
        ) = None,
        daily_runs: dict[str, list[StoredCustomRun]] | None = None,
    ) -> None:
        self.templates = templates or {}
        self.daily_runs = daily_runs or {}
        self.template_calls: list[tuple[str, int]] = []
        self.daily_calls: list[tuple[str, datetime]] = []

    def latest_completed_template(self, station_id: str, *, interval_seconds: int):
        self.template_calls.append((station_id, interval_seconds))
        result = self.templates.get((station_id, interval_seconds))
        if isinstance(result, BaseException):
            raise result
        return result

    def list_daily_runs(self, station_id: str, *, forecast_start: datetime):
        self.daily_calls.append((station_id, forecast_start))
        return list(self.daily_runs.get(station_id, []))


class RecordingSubmitter:
    def __init__(self, failures: set[tuple[str, int]] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, CustomForecastRequest, str | None]] = []

    def submit(
        self,
        station_id: str,
        request: CustomForecastRequest,
        *,
        requested_by: str | None,
    ) -> StoredCustomRun:
        self.calls.append((station_id, request, requested_by))
        if (station_id, request.interval_seconds) in self.failures:
            raise M3Error("job_capacity_exceeded", "executor is full")
        return make_run(
            f"submitted-{request.interval_seconds}-{len(self.calls)}",
            station_id=station_id,
            interval_seconds=request.interval_seconds,
            history_days=(request.history_end - request.history_start).days,
            forecast_days=request.forecast_days,
            forecast_start=request.history_end,
            status="queued",
            idempotency_key=request.idempotency_key,
            requested_by=requested_by,
        )


class BlockingRepository(InMemoryRepository):
    def __init__(self, templates) -> None:
        super().__init__(templates)
        self.entered = Event()
        self.release = Event()

    def latest_completed_template(self, station_id: str, *, interval_seconds: int):
        if not self.template_calls:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("test did not release repository")
        return super().latest_completed_template(
            station_id, interval_seconds=interval_seconds
        )


class DailyCustomForecastServiceTests(unittest.TestCase):
    def test_rolls_each_latest_template_configuration_independently(self):
        """Using one template's cadence or history for every request corrupts forecasts."""
        template_900 = make_run(
            "template-900", interval_seconds=900, history_days=21, forecast_days=2
        )
        template_3600 = make_run(
            "template-3600",
            interval_seconds=3600,
            history_days=35,
            forecast_days=4,
        )
        repository = InMemoryRepository(
            {
                ("ES01", 900): template_900,
                ("ES01", 3600): template_3600,
            }
        )
        submitter = RecordingSubmitter()

        attempted = DailyCustomForecastService(repository, submitter).run_station(
            "ES01", NOW
        )

        self.assertEqual(attempted, 2)
        self.assertEqual(len(submitter.calls), 2)
        requests = {
            request.interval_seconds: request for _, request, _ in submitter.calls
        }
        for template in (template_900, template_3600):
            request = requests[template.config.interval_seconds]
            self.assertEqual(request.forecast_days, template.config.forecast_days)
            self.assertEqual(request.history_end, DAY)
            self.assertEqual(
                request.history_start,
                DAY - timedelta(days=template.config.history_days),
            )
            self.assertEqual(
                build_custom_forecast_config(request).model_policy,
                template.config.model_policy,
            )
        self.assertEqual(
            {requested_by for _, _, requested_by in submitter.calls},
            {DAILY_REQUESTED_BY},
        )

    def test_before_daily_start_returns_without_repository_or_submit_calls(self):
        """Moving the boundary earlier would create tasks during the 00:02 sweep."""
        repository = InMemoryRepository(
            {("ES01", 900): make_run("template-900")}
        )
        submitter = RecordingSubmitter()

        attempted = DailyCustomForecastService(repository, submitter).run_station(
            "ES01", DAY.replace(minute=2)
        )

        self.assertEqual(attempted, 0)
        self.assertEqual(repository.template_calls, [])
        self.assertEqual(repository.daily_calls, [])
        self.assertEqual(submitter.calls, [])

    def test_now_must_be_an_aware_shanghai_timestamp(self):
        """Accepting a naive or non-Shanghai clock can roll the wrong calendar day."""
        service = DailyCustomForecastService(InMemoryRepository(), RecordingSubmitter())

        for invalid_now in (
            datetime(2026, 9, 2, 0, 17),
            datetime.fromisoformat("2026-09-02T00:17:00+00:00"),
        ):
            with self.subTest(now=invalid_now), self.assertRaises(ValueError):
                service.run_station("ES01", invalid_now)

    def test_same_day_or_future_completed_template_already_covers_interval(self):
        """Ignoring completed coverage would duplicate a manual forecast on its own day."""
        for forecast_start in (DAY, DAY + timedelta(days=1)):
            with self.subTest(forecast_start=forecast_start):
                repository = InMemoryRepository(
                    {
                        ("ES01", 900): make_run(
                            "covered-template", forecast_start=forecast_start
                        )
                    }
                )
                submitter = RecordingSubmitter()
                service = DailyCustomForecastService(repository, submitter)

                self.assertEqual(service.run_station("ES01", NOW), 0)
                self.assertEqual(submitter.calls, [])
                self.assertEqual(repository.daily_calls, [])

    def test_no_completed_template_is_rechecked_later_without_error(self):
        """Caching an empty discovery would miss a user's first completed task that day."""
        repository = InMemoryRepository()
        submitter = RecordingSubmitter()
        service = DailyCustomForecastService(repository, submitter)

        self.assertEqual(service.run_station("ES01", NOW), 0)
        self.assertEqual(service.run_station("ES01", NOW.replace(hour=12)), 0)

        self.assertEqual(len(repository.template_calls), 12)
        self.assertEqual(repository.daily_calls, [])
        self.assertEqual(submitter.calls, [])

    def test_terminal_daily_rows_do_not_submit_duplicates(self):
        """A terminal or running automatic row must suppress another normal attempt."""
        template = make_run("template-900")
        for status in ("running", "succeeded", "evaluated"):
            with self.subTest(status=status):
                daily = make_run(
                    f"daily-{status}",
                    forecast_start=DAY,
                    status=status,
                    requested_by=DAILY_REQUESTED_BY,
                )
                repository = InMemoryRepository(
                    {("ES01", 900): template}, {"ES01": [daily]}
                )
                submitter = RecordingSubmitter()

                attempted = DailyCustomForecastService(
                    repository, submitter
                ).run_station("ES01", NOW)

                self.assertEqual(attempted, 0)
                self.assertEqual(submitter.calls, [])

    def test_queued_daily_row_is_rescheduled_with_its_exact_request_and_key(self):
        """Rolling a queued row again could conflict with its persisted identity."""
        template = make_run("template-900", history_days=28, forecast_days=1)
        queued = make_run(
            "daily-queued",
            history_days=60,
            forecast_days=3,
            forecast_start=DAY,
            status="queued",
            idempotency_key="persisted-queue-key",
            requested_by=DAILY_REQUESTED_BY,
        )
        repository = InMemoryRepository(
            {("ES01", 900): template}, {"ES01": [queued]}
        )
        submitter = RecordingSubmitter()

        attempted = DailyCustomForecastService(repository, submitter).run_station(
            "ES01", NOW
        )

        self.assertEqual(attempted, 1)
        _, submitted, requested_by = submitter.calls[0]
        self.assertEqual(submitted, request_for(queued))
        self.assertEqual(requested_by, DAILY_REQUESTED_BY)

    def test_failed_rows_retry_from_first_persisted_config_with_next_attempt_key(self):
        """A retry must retain attempt one's config even if the latest template changed."""
        template = make_run("new-template", history_days=90, forecast_days=7)
        first = make_run(
            "daily-failed-1",
            history_days=21,
            forecast_days=2,
            forecast_start=DAY,
            status="failed",
            idempotency_key="old-first-key",
            requested_by=DAILY_REQUESTED_BY,
            created_offset=1,
        )
        second = make_run(
            "daily-failed-2",
            history_days=21,
            forecast_days=2,
            forecast_start=DAY,
            status="failed",
            idempotency_key="old-second-key",
            requested_by=DAILY_REQUESTED_BY,
            created_offset=2,
        )
        for failed_rows, expected_attempt in (([first], 2), ([first, second], 3)):
            with self.subTest(expected_attempt=expected_attempt):
                repository = InMemoryRepository(
                    {("ES01", 900): template}, {"ES01": failed_rows}
                )
                submitter = RecordingSubmitter()

                attempted = DailyCustomForecastService(
                    repository, submitter
                ).run_station("ES01", NOW)

                self.assertEqual(attempted, 1)
                request = submitter.calls[0][1]
                self.assertEqual(request.history_start, first.config.history_start)
                self.assertEqual(request.history_end, first.config.history_end)
                self.assertEqual(request.forecast_days, first.config.forecast_days)
                self.assertTrue(request.idempotency_key.endswith(f":{expected_attempt}"))
                self.assertNotIn(first.run_id, request.idempotency_key)
                self.assertLessEqual(len(request.idempotency_key), 128)

    def test_three_failed_rows_raise_stable_error_without_a_fourth_attempt(self):
        """Dropping the retry cap would create an unbounded daily task storm."""
        template = make_run("template-900")
        failures = [
            make_run(
                f"daily-failed-{attempt}",
                forecast_start=DAY,
                status="failed",
                requested_by=DAILY_REQUESTED_BY,
                created_offset=attempt,
            )
            for attempt in (1, 2, 3)
        ]
        submitter = RecordingSubmitter()
        service = DailyCustomForecastService(
            InMemoryRepository(
                {("ES01", 900): template}, {"ES01": failures}
            ),
            submitter,
        )

        with self.assertRaises(M3Error) as raised:
            service.run_station("ES01", NOW)

        self.assertEqual(raised.exception.code, "daily_custom_forecast_failed")
        self.assertEqual(submitter.calls, [])

    def test_one_interval_failure_continues_other_intervals_and_stations(self):
        """Returning at the first error would starve unrelated cadence and station work."""
        repository = InMemoryRepository(
            {
                ("ES01", 30): make_run("es01-template-30", interval_seconds=30),
                ("ES01", 900): make_run("es01-template-900", interval_seconds=900),
                ("ES02", 900): make_run(
                    "es02-template-900", station_id="ES02", interval_seconds=900
                ),
            }
        )
        submitter = RecordingSubmitter({("ES01", 30)})
        service = DailyCustomForecastService(repository, submitter)

        with self.assertRaises(M3Error) as raised:
            service.run_station("ES01", NOW)
        es02_attempted = service.run_station("ES02", NOW)

        self.assertEqual(raised.exception.code, "job_capacity_exceeded")
        self.assertEqual(es02_attempted, 1)
        self.assertEqual(
            [
                (station_id, request.interval_seconds)
                for station_id, request, _ in submitter.calls
            ],
            [("ES01", 30), ("ES01", 900), ("ES02", 900)],
        )

    def test_template_lookup_failure_continues_discovered_intervals(self):
        """A failed cadence lookup must not suppress submission for another template."""
        repository = InMemoryRepository(
            {
                ("ES01", 30): M3Error("sink_http_failed", "template lookup failed"),
                ("ES01", 900): make_run("es01-template-900", interval_seconds=900),
            }
        )
        submitter = RecordingSubmitter()
        service = DailyCustomForecastService(repository, submitter)

        with self.assertRaises(M3Error) as raised:
            service.run_station("ES01", NOW)

        self.assertEqual(raised.exception.code, "sink_http_failed")
        self.assertEqual(
            [
                (station_id, request.interval_seconds)
                for station_id, request, _ in submitter.calls
            ],
            [("ES01", 900)],
        )

    def test_restart_reconstructs_the_same_deterministic_attempt_key(self):
        """A process-local identity would create duplicate rows after restart."""
        long_station_id = "ES01-" + "x" * 120
        template = make_run(
            "long-station-template",
            station_id=long_station_id,
            interval_seconds=3600,
            history_days=35,
            forecast_days=4,
        )
        keys = []
        for _restart in range(2):
            submitter = RecordingSubmitter()
            service = DailyCustomForecastService(
                InMemoryRepository({(long_station_id, 3600): template}), submitter
            )

            self.assertEqual(service.run_station(long_station_id, NOW), 1)
            keys.append(submitter.calls[0][1].idempotency_key)

        self.assertEqual(keys[0], keys[1])
        self.assertNotIn(long_station_id, keys[0])
        self.assertLessEqual(len(keys[0]), 128)

    def test_completed_date_cache_serializes_same_station_sweeps(self):
        """A cache check race would repeat all repository reads for the same station/day."""
        covered = make_run("covered-template", forecast_start=DAY)
        repository = BlockingRepository({("ES01", 900): covered})
        service = DailyCustomForecastService(repository, RecordingSubmitter())
        results: list[int] = []

        first = Thread(target=lambda: results.append(service.run_station("ES01", NOW)))
        second = Thread(target=lambda: results.append(service.run_station("ES01", NOW)))
        first.start()
        self.assertTrue(repository.entered.wait(timeout=5))
        second.start()
        repository.release.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(results), [0, 0])
        self.assertEqual(len(repository.template_calls), 6)


if __name__ == "__main__":
    unittest.main()
