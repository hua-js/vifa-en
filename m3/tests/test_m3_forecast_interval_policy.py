"""Paused granularities must never consume executor slots or create daily runs."""

import os
import unittest
from unittest.mock import Mock, patch

from m3.tests.test_m3_custom_forecast_service import (
    NOW, RecoveryRepository, make_run,
)
from m3.tests.test_m3_daily_custom_forecast_service import (
    NOW as DAILY_NOW, InMemoryRepository, RecordingSubmitter,
    make_run as make_daily_run,
)
from m3.worker.custom_forecast_contracts import ALLOWED_INTERVAL_SECONDS, CustomForecastRequest
from m3.worker.errors import M3Error
from m3.worker.services.custom_forecast_policy import enabled_forecast_intervals
from m3.worker.services.custom_forecast_service import CustomForecastService
from m3.worker.services.daily_custom_forecast_service import DailyCustomForecastService


class ForecastIntervalPolicyTests(unittest.TestCase):
    def test_unset_keeps_all_existing_intervals(self):
        with patch.dict(os.environ):
            os.environ.pop("M3_CUSTOM_FORECAST_INTERVALS", None)
            self.assertEqual(enabled_forecast_intervals(), ALLOWED_INTERVAL_SECONDS)

    def test_invalid_configuration_fails_closed(self):
        for value in ("", "900,", "900,900", "901", "900,abc"):
            with self.subTest(value=value), patch.dict(os.environ, M3_CUSTOM_FORECAST_INTERVALS=value):
                with self.assertRaises(ValueError):
                    enabled_forecast_intervals()

    @patch.dict(os.environ, M3_CUSTOM_FORECAST_INTERVALS="900")
    def test_daily_only_reads_and_creates_enabled_interval_for_each_station(self):
        repository = InMemoryRepository({
            (station, interval): make_daily_run(
                f"template-{station}-{interval}", station_id=station, interval_seconds=interval,
            )
            for station in ("ES01", "ES02") for interval in ALLOWED_INTERVAL_SECONDS
        })
        submitter = RecordingSubmitter()
        service = DailyCustomForecastService(repository, submitter)
        for station in ("ES01", "ES02"):
            self.assertEqual(service.run_station(station, DAILY_NOW), 1)
        self.assertEqual(repository.template_calls, [("ES01", 900), ("ES02", 900)])
        self.assertEqual([(station, request.interval_seconds) for station, request, _ in submitter.calls],
                         [("ES01", 900), ("ES02", 900)])

    @patch.dict(os.environ, M3_CUSTOM_FORECAST_INTERVALS="900")
    def test_recovery_holds_other_queued_intervals_without_changing_records(self):
        runs = [make_run(run_id=f"queued-{interval}", interval_seconds=interval)
                for interval in ALLOWED_INTERVAL_SECONDS]
        repository = RecoveryRepository(runs)
        service = CustomForecastService(repository, Mock(), lambda: NOW, station_ids=("ES01",))
        self.addCleanup(service.close)
        with patch.object(service._pool, "submit") as submit:
            service.recover()
        self.assertEqual([call.args[1] for call in submit.call_args_list], ["queued-900"])
        self.assertEqual(repository.transitions, [])
        self.assertTrue(all(run.status == "queued" for run in runs))

    @patch.dict(os.environ, M3_CUSTOM_FORECAST_INTERVALS="900")
    def test_disabled_submission_is_rejected_before_database_write(self):
        repository = Mock()
        service = CustomForecastService(repository, Mock(), lambda: NOW, station_ids=("ES01",))
        self.addCleanup(service.close)
        for interval in ALLOWED_INTERVAL_SECONDS:
            if interval == 900:
                continue
            run = make_run(interval_seconds=interval)
            request = CustomForecastRequest(
                history_start=run.config.history_start, history_end=run.config.history_end,
                forecast_days=1, interval_seconds=interval, idempotency_key=f"paused-{interval}",
            )
            with self.subTest(interval=interval), self.assertRaises(M3Error) as caught:
                service.submit("ES01", request, requested_by="m3_operations_api")
            self.assertEqual(caught.exception.code, "request_invalid")
        repository.create_or_get.assert_not_called()
