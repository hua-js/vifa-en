"""Deterministic scheduler and bounded manual-job tests."""

from datetime import datetime, timedelta
from threading import Barrier, Event, Thread
import time
import unittest
from uuid import UUID

from m3_worker.errors import M3Error
from m3_worker.scheduler.runner import SchedulerRunner, due_slots
from m3_worker.services.job_service import JobService


T0030 = datetime.fromisoformat("2026-08-25T00:30:00+08:00")
T0102 = datetime.fromisoformat("2026-08-25T01:02:00+08:00")
T0117 = datetime.fromisoformat("2026-08-25T01:17:00+08:00")


class RecordingForecast:
    def __init__(self, failures=None, entered=None, release=None):
        self.calls = []
        self.failures = dict(failures or {})
        self.entered = entered
        self.release = release

    def bootstrap(self, station_id, as_of):
        self.calls.append(("bootstrap", station_id, as_of))

    def select_models(self, station_id):
        self.calls.append(("select", station_id))

    def run_forecast(self, station_id, as_of):
        self.calls.append(("forecast", station_id, as_of))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        failure = self.failures.get(station_id)
        if failure is not None:
            raise failure


class RecordingAcceptance:
    def __init__(self, backfill_failures=None):
        self.calls = []
        self.backfill_failures = dict(backfill_failures or {})

    def run_baseline(self, station_id, as_of):
        self.calls.append(("baseline", station_id, as_of))

    def backfill_actuals(self, station_id, as_of):
        self.calls.append(("backfill", station_id, as_of))
        failure = self.backfill_failures.get(station_id)
        if failure is not None:
            raise failure
        return 0


class RecordingEvaluation:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = dict(failures or {})

    def evaluate_station(self, station_id, as_of):
        self.calls.append(("evaluate", station_id, as_of))
        failure = self.failures.get(station_id)
        if failure is not None:
            raise failure


class RecordingDailyForecast:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = dict(failures or {})

    def run_station(self, station_id, as_of):
        self.calls.append(("daily", station_id, as_of))
        failure = self.failures.get(station_id)
        if failure is not None:
            raise failure
        return 0


class SchedulerTests(unittest.TestCase):
    def test_due_slots_are_exact_and_0102_is_acceptance_only(self):
        """Adding a rolling forecast at 01:02 would run and persist the model twice."""
        self.assertEqual(due_slots(T0030), ("model_selection",))
        self.assertEqual(due_slots(T0102), ("acceptance_baseline",))
        self.assertEqual(due_slots(T0117), ("forecast",))
        self.assertEqual(due_slots(T0117.replace(minute=3)), ())

    def test_due_slots_rejects_naive_or_non_shanghai_time(self):
        """Accepting ambiguous time makes the process schedule against the host timezone."""
        with self.assertRaisesRegex(M3Error, "Asia/Shanghai"):
            due_slots(datetime(2026, 8, 25, 1, 2))
        with self.assertRaisesRegex(M3Error, "Asia/Shanghai"):
            due_slots(datetime.fromisoformat("2026-08-24T17:02:00+00:00"))

    def test_tick_canonicalizes_seconds_and_attempts_slot_once(self):
        """A 15-second polling loop must not duplicate a minute or violate baseline's exact slot."""
        forecast = RecordingForecast()
        acceptance = RecordingAcceptance()
        runner = SchedulerRunner(("station-1",), forecast, acceptance)

        runner.tick(T0102.replace(second=11, microsecond=7))
        runner.tick(T0102.replace(second=48))

        self.assertEqual(acceptance.calls, [("baseline", "station-1", T0102)])
        self.assertEqual(forecast.calls, [])

    def test_acceptance_disabled_skips_baseline_and_backfill_but_keeps_other_stages(self):
        """Phase-one production must not call missing acceptance endpoints."""
        forecast = RecordingForecast()
        acceptance = RecordingAcceptance()
        evaluations = RecordingEvaluation()
        daily = RecordingDailyForecast()
        runner = SchedulerRunner(
            ("station-1",),
            forecast,
            acceptance,
            acceptance_enabled=False,
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        runner.tick(T0102)
        runner.tick(T0117)
        runner.tick(T0030)

        self.assertEqual(acceptance.calls, [])
        self.assertEqual(
            evaluations.calls, [("evaluate", "station-1", T0117)]
        )
        self.assertEqual(daily.calls, [("daily", "station-1", T0117)])
        self.assertEqual(
            forecast.calls,
            [
                ("forecast", "station-1", T0117),
                ("bootstrap", "station-1", T0030),
                ("select", "station-1"),
            ],
        )

    def test_acceptance_failure_alerts_its_stage_and_continues_later_stages(self):
        """A backfill outage must not suppress evaluation or daily forecast submission."""
        forecast = RecordingForecast()
        acceptance = RecordingAcceptance(
            backfill_failures={
                "station-1": M3Error(
                    "sink_http_failed", "NocoBase action failed"
                )
            }
        )
        evaluations = RecordingEvaluation()
        daily = RecordingDailyForecast()
        alerts = []
        runner = SchedulerRunner(
            ("station-1",),
            forecast,
            acceptance,
            alert_sink=lambda *args: alerts.append(args),
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        runner.tick(T0117)

        self.assertEqual(forecast.calls, [("forecast", "station-1", T0117)])
        self.assertEqual(acceptance.calls, [("backfill", "station-1", T0117)])
        self.assertEqual(evaluations.calls, [("evaluate", "station-1", T0117)])
        self.assertEqual(daily.calls, [("daily", "station-1", T0117)])
        self.assertEqual(
            alerts,
            [("station-1", "acceptance_backfill", "sink_http_failed", T0117)],
        )

    def test_forecast_failure_alerts_its_stage_and_continues_other_three_stages(self):
        """A rolling forecast failure must not serialize unrelated scheduled work."""
        forecast = RecordingForecast(
            failures={"station-1": RuntimeError("source-secret=https://internal")}
        )
        acceptance = RecordingAcceptance()
        evaluations = RecordingEvaluation()
        daily = RecordingDailyForecast()
        alerts = []
        runner = SchedulerRunner(
            ("station-1",),
            forecast,
            acceptance,
            alert_sink=lambda *args: alerts.append(args),
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        runner.tick(T0117)
        runner.tick(T0117.replace(second=30))

        self.assertEqual(forecast.calls, [("forecast", "station-1", T0117)])
        self.assertEqual(acceptance.calls, [("backfill", "station-1", T0117)])
        self.assertEqual(evaluations.calls, [("evaluate", "station-1", T0117)])
        self.assertEqual(daily.calls, [("daily", "station-1", T0117)])
        self.assertEqual(
            alerts, [("station-1", "forecast", "internal_error", T0117)]
        )
        self.assertNotIn("source-secret", repr(alerts))

    def test_evaluation_failure_alerts_its_stage_and_continues_daily_submission(self):
        """A custom evaluation outage must not suppress the daily forecast sweep."""
        evaluations = RecordingEvaluation(
            failures={
                "station-1": M3Error(
                    "evaluation_failed", "custom evaluation failed"
                )
            }
        )
        daily = RecordingDailyForecast()
        alerts = []
        runner = SchedulerRunner(
            ("station-1",),
            RecordingForecast(),
            RecordingAcceptance(),
            alert_sink=lambda *args: alerts.append(args),
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        runner.tick(T0117)

        self.assertEqual(daily.calls, [("daily", "station-1", T0117)])
        self.assertEqual(
            alerts,
            [("station-1", "custom_evaluation", "evaluation_failed", T0117)],
        )

    def test_all_station_one_stage_failures_do_not_block_station_two(self):
        """Every isolated failure at one station must leave the next station runnable."""
        forecast = RecordingForecast(
            failures={"station-1": M3Error("forecast_failed", "forecast failed")}
        )
        acceptance = RecordingAcceptance(
            backfill_failures={
                "station-1": M3Error("backfill_failed", "backfill failed")
            }
        )
        evaluations = RecordingEvaluation(
            failures={
                "station-1": M3Error("evaluation_failed", "evaluation failed")
            }
        )
        daily = RecordingDailyForecast(
            failures={"station-1": M3Error("daily_failed", "daily failed")}
        )
        alerts = []
        runner = SchedulerRunner(
            ("station-1", "station-2"),
            forecast,
            acceptance,
            alert_sink=lambda *args: alerts.append(args),
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        runner.tick(T0117)

        self.assertEqual(
            forecast.calls,
            [
                ("forecast", "station-1", T0117),
                ("forecast", "station-2", T0117),
            ],
        )
        self.assertEqual(
            acceptance.calls,
            [
                ("backfill", "station-1", T0117),
                ("backfill", "station-2", T0117),
            ],
        )
        self.assertEqual(
            evaluations.calls,
            [
                ("evaluate", "station-1", T0117),
                ("evaluate", "station-2", T0117),
            ],
        )
        self.assertEqual(
            daily.calls,
            [
                ("daily", "station-1", T0117),
                ("daily", "station-2", T0117),
            ],
        )
        self.assertEqual(
            alerts,
            [
                ("station-1", "forecast", "forecast_failed", T0117),
                ("station-1", "acceptance_backfill", "backfill_failed", T0117),
                ("station-1", "custom_evaluation", "evaluation_failed", T0117),
                ("station-1", "daily_custom_forecast", "daily_failed", T0117),
            ],
        )

    def test_manual_forecast_failure_still_propagates_to_the_job_service(self):
        """Manual jobs need raised failures so their terminal status remains truthful."""
        failure = M3Error("forecast_failed", "manual forecast failed")
        forecast = RecordingForecast(failures={"station-1": failure})
        acceptance = RecordingAcceptance()
        evaluations = RecordingEvaluation()
        daily = RecordingDailyForecast()
        runner = SchedulerRunner(
            ("station-1",),
            forecast,
            acceptance,
            custom_evaluation_service=evaluations,
            daily_custom_forecast_service=daily,
        )

        with self.assertRaises(M3Error) as caught:
            runner.run_manual("station-1", "forecast", T0117)

        self.assertIs(caught.exception, failure)
        self.assertEqual(acceptance.calls, [])
        self.assertEqual(evaluations.calls, [])
        self.assertEqual(daily.calls, [])

    def test_scheduled_and_manual_forecasts_share_one_station_lock(self):
        """Separate manual locking would allow concurrent publication for one station."""
        entered = Event()
        release = Event()
        forecast = RecordingForecast(entered=entered, release=release)
        acceptance = RecordingAcceptance()
        runner = SchedulerRunner(("station-1",), forecast, acceptance)

        scheduled = Thread(target=runner.tick, args=(T0117,))
        manual = Thread(
            target=runner.run_manual,
            args=("station-1", "forecast", T0117 + timedelta(minutes=1)),
        )
        scheduled.start()
        self.assertTrue(entered.wait(1))
        manual.start()
        time.sleep(0.03)
        self.assertEqual(len(forecast.calls), 1)
        release.set()
        scheduled.join(2)
        manual.join(2)

        self.assertEqual(
            [call[0] for call in forecast.calls], ["forecast", "forecast"]
        )
        self.assertEqual(
            [call[0] for call in acceptance.calls], ["backfill", "backfill"]
        )

    def test_manual_validation_precedes_any_service_side_effect(self):
        """Unconfigured stations and task names must never reach outbound services."""
        forecast = RecordingForecast()
        acceptance = RecordingAcceptance()
        runner = SchedulerRunner(("station-1",), forecast, acceptance)

        with self.assertRaisesRegex(M3Error, "configured"):
            runner.run_manual("unknown", "forecast", T0117)
        with self.assertRaisesRegex(M3Error, "task"):
            runner.run_manual("station-1", "acceptance_baseline", T0117)
        with self.assertRaisesRegex(M3Error, "time"):
            runner.run_manual("station-1", "forecast", datetime(2026, 8, 25))

        self.assertEqual(forecast.calls, [])
        self.assertEqual(acceptance.calls, [])

    def test_manual_model_selection_uses_completed_quarter_hour(self):
        """Passing an off-grid manual clock directly to bootstrap violates its source window contract."""
        forecast = RecordingForecast()
        runner = SchedulerRunner(
            ("station-1",), forecast, RecordingAcceptance()
        )
        manual_now = T0117.replace(minute=28, second=49)

        runner.run_manual("station-1", "model_selection", manual_now)

        self.assertEqual(
            forecast.calls,
            [
                ("bootstrap", "station-1", T0117.replace(minute=15)),
                ("select", "station-1"),
            ],
        )

    def test_concurrent_ticks_cannot_reserve_the_same_station_minute_twice(self):
        """A check outside the dedupe lock would let simultaneous pollers duplicate a slot."""
        entered = Event()
        release = Event()
        forecast = RecordingForecast(entered=entered, release=release)
        runner = SchedulerRunner(
            ("station-1",), forecast, RecordingAcceptance()
        )
        first = Thread(target=runner.tick, args=(T0117,))
        second = Thread(target=runner.tick, args=(T0117.replace(second=30),))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        second.join(1)
        release.set()
        first.join(2)

        self.assertEqual(forecast.calls, [("forecast", "station-1", T0117)])

    def test_start_is_single_instance_and_stop_is_bounded_and_idempotent(self):
        """Repeated lifecycle calls must not create duplicate scheduler threads."""
        runner = SchedulerRunner(
            ("station-1",), RecordingForecast(), RecordingAcceptance(), poll_seconds=0.01
        )
        runner.start()
        self.assertTrue(runner.running)
        with self.assertRaisesRegex(RuntimeError, "already"):
            runner.start()
        runner.stop()
        runner.stop()
        self.assertFalse(runner.running)

    def test_default_stop_waits_for_blocked_station_operation_before_returning(self):
        """Shutdown must not close HTTP clients beneath an in-flight station operation."""
        entered = Event()
        release = Event()
        stopped = Event()
        runner = SchedulerRunner(
            ("station-1",),
            RecordingForecast(entered=entered, release=release),
            RecordingAcceptance(),
            clock=lambda: T0117,
            poll_seconds=60,
        )
        runner.start()
        self.assertTrue(entered.wait(1))
        stopper = Thread(target=lambda: (runner.stop(), stopped.set()))
        stopper.start()
        time.sleep(0.03)

        self.assertFalse(stopped.is_set())
        self.assertTrue(runner.running)
        self.assertEqual(runner.lifecycle_state, "stopping")
        release.set()
        stopper.join(2)
        self.assertTrue(stopped.is_set())
        self.assertFalse(runner.running)
        self.assertEqual(runner.lifecycle_state, "stopped")

    def test_finite_stop_timeout_is_safe_and_repeated_stop_completes_after_release(self):
        """A configured test timeout must fail visibly instead of pretending shutdown completed."""
        entered = Event()
        release = Event()
        runner = SchedulerRunner(
            ("station-1",),
            RecordingForecast(entered=entered, release=release),
            RecordingAcceptance(),
            clock=lambda: T0117,
            poll_seconds=60,
            stop_timeout_seconds=0.02,
        )
        runner.start()
        self.assertTrue(entered.wait(1))

        with self.assertRaises(M3Error) as caught:
            runner.stop()
        self.assertEqual(caught.exception.code, "scheduler_stop_timeout")
        self.assertTrue(runner.running)
        self.assertEqual(runner.lifecycle_state, "stopping")
        release.set()
        for _ in range(100):
            if not runner.running:
                break
            time.sleep(0.005)
        self.assertFalse(runner.healthy)
        self.assertEqual(runner.last_error_code, "scheduler_stop_timeout")
        runner.stop()
        runner.stop()
        self.assertFalse(runner.running)

    def test_late_loop_failure_cannot_replace_stop_timeout_during_shutdown(self):
        """A blocked loop failure after stop timeout must not hide shutdown failure."""
        entered = Event()
        release = Event()

        def blocked_clock():
            entered.set()
            release.wait(2)
            raise M3Error("late_loop_failure", "safe loop failure")

        runner = SchedulerRunner(
            ("station-1",),
            RecordingForecast(),
            RecordingAcceptance(),
            clock=blocked_clock,
            poll_seconds=60,
            stop_timeout_seconds=0.02,
        )
        runner.start()
        self.assertTrue(entered.wait(1))

        with self.assertRaises(M3Error) as caught:
            runner.stop()
        self.assertEqual(caught.exception.code, "scheduler_stop_timeout")
        self.assertEqual(runner.lifecycle_state, "stopping")
        release.set()
        for _ in range(100):
            if not runner.running:
                break
            time.sleep(0.005)

        self.assertFalse(runner.running)
        self.assertFalse(runner.healthy)
        self.assertEqual(runner.last_error_code, "scheduler_stop_timeout")
        runner.stop()

    def test_stop_before_start_cannot_be_cleared_by_later_start(self):
        """A concurrent late start must not erase a shutdown request."""
        runner = SchedulerRunner(
            ("station-1",), RecordingForecast(), RecordingAcceptance()
        )
        runner.stop()
        with self.assertRaises(RuntimeError):
            runner.start()
        self.assertEqual(runner.lifecycle_state, "stopped")
        self.assertFalse(runner.running)

    def test_concurrent_start_and_stop_always_converge_to_stopped(self):
        """Whichever lifecycle call wins must leave no live thread after both return."""
        for _ in range(25):
            runner = SchedulerRunner(
                ("station-1",),
                RecordingForecast(),
                RecordingAcceptance(),
                clock=lambda: T0117.replace(minute=18),
                poll_seconds=0.005,
            )
            gate = Barrier(3)
            start_errors = []

            def start():
                gate.wait()
                try:
                    runner.start()
                except RuntimeError as error:
                    start_errors.append(error)

            def stop():
                gate.wait()
                runner.stop()

            starter = Thread(target=start)
            stopper = Thread(target=stop)
            starter.start()
            stopper.start()
            gate.wait()
            starter.join(2)
            stopper.join(2)
            runner.stop()
            self.assertFalse(starter.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertFalse(runner.running)
            self.assertEqual(runner.lifecycle_state, "stopped")
            self.assertLessEqual(len(start_errors), 1)

    def test_loop_failure_marks_unhealthy_alerts_safely_and_success_recovers(self):
        """A live thread with a permanently broken clock is not a healthy scheduler."""
        fail = Event()
        fail.set()
        alerts = []

        def clock():
            if fail.is_set():
                raise RuntimeError("source-secret")
            return T0117.replace(minute=18)

        runner = SchedulerRunner(
            ("station-1",),
            RecordingForecast(),
            RecordingAcceptance(),
            clock=clock,
            alert_sink=lambda *args: alerts.append(args),
            poll_seconds=0.005,
        )
        runner.start()
        for _ in range(100):
            if not runner.healthy:
                break
            time.sleep(0.005)
        self.assertFalse(runner.healthy)
        self.assertEqual(runner.last_error_code, "internal_error")
        self.assertTrue(alerts)
        self.assertEqual(alerts[0][0:3], ("station-1", "scheduler_loop", "internal_error"))
        self.assertEqual(alerts[0][3].utcoffset(), timedelta(hours=8))
        self.assertNotIn("secret", repr(alerts))
        fail.clear()
        for _ in range(100):
            if runner.healthy:
                break
            time.sleep(0.005)
        self.assertTrue(runner.healthy)
        self.assertIsNone(runner.last_error_code)
        self.assertEqual(runner.lifecycle_state, "running")
        runner.stop()


class JobServiceTests(unittest.TestCase):
    def test_job_uses_worker_clock_uuid_and_returns_copies(self):
        """Caller time or mutable returned state must not alter the retained job."""
        calls = []
        jobs = JobService(
            lambda station, task, at: calls.append((station, task, at)),
            now=lambda: T0117,
            station_ids=("station-1",),
            max_workers=1,
            max_pending=2,
            max_retained=2,
        )
        submitted = jobs.submit("station-1", "forecast")
        UUID(submitted.job_id)
        submitted.status = "failed"
        jobs.close()

        retained = jobs.get(submitted.job_id)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.status, "succeeded")
        self.assertEqual(calls, [("station-1", "forecast", T0117)])

    def test_capacity_bounds_queued_plus_running_and_recovers_after_completion(self):
        """ThreadPoolExecutor's unbounded queue must not admit work past capacity."""
        entered = Event()
        release = Event()

        def blocking(*_args):
            entered.set()
            release.wait(2)

        jobs = JobService(
            blocking,
            now=lambda: T0117,
            station_ids=("station-1",),
            max_workers=1,
            max_pending=1,
            max_retained=2,
        )
        first = jobs.submit("station-1", "forecast")
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(M3Error, "capacity") as caught:
            jobs.submit("station-1", "forecast")
        self.assertEqual(caught.exception.code, "job_capacity_exceeded")
        release.set()
        for _ in range(100):
            if jobs.get(first.job_id).status == "succeeded":
                break
            time.sleep(0.005)
        second = jobs.submit("station-1", "forecast")
        jobs.close()
        self.assertEqual(jobs.get(second.job_id).status, "succeeded")

    def test_invalid_submission_and_closed_service_are_rejected_before_execution(self):
        """Job queue validation must enforce the exact production allowlist."""
        calls = []
        jobs = JobService(
            lambda *args: calls.append(args),
            now=lambda: T0117,
            station_ids=("station-1",),
            max_workers=1,
            max_pending=1,
            max_retained=1,
        )
        with self.assertRaises(M3Error) as station_error:
            jobs.submit("unknown", "forecast")
        with self.assertRaises(M3Error) as task_error:
            jobs.submit("station-1", "baseline")
        self.assertEqual(station_error.exception.code, "station_not_configured")
        self.assertEqual(task_error.exception.code, "job_task_invalid")
        jobs.close()
        jobs.close()
        with self.assertRaises(M3Error) as closed_error:
            jobs.submit("station-1", "forecast")
        self.assertEqual(closed_error.exception.code, "job_service_closed")
        self.assertEqual(calls, [])

    def test_terminal_state_retention_is_bounded_without_evicting_active_jobs(self):
        """Long-lived workers must not retain every finished manual job forever."""
        jobs = JobService(
            lambda *_args: None,
            now=lambda: T0117,
            station_ids=("station-1",),
            max_workers=1,
            max_pending=1,
            max_retained=2,
        )
        ids = []
        for _ in range(3):
            job = jobs.submit("station-1", "forecast")
            ids.append(job.job_id)
            for _ in range(100):
                state = jobs.get(job.job_id)
                if state is not None and state.status == "succeeded":
                    break
                time.sleep(0.005)
        jobs.close()

        self.assertIsNone(jobs.get(ids[0]))
        self.assertEqual(jobs.get(ids[1]).status, "succeeded")
        self.assertEqual(jobs.get(ids[2]).status, "succeeded")

    def test_exceptions_become_safe_terminal_codes(self):
        """Job responses must not expose exception messages, tracebacks, or malformed codes."""
        failures = iter(
            [
                M3Error("forecast_failed", "token=source-secret"),
                RuntimeError("https://internal/source?token=secret"),
            ]
        )

        def fail(*_args):
            raise next(failures)

        jobs = JobService(
            fail,
            now=lambda: T0117,
            station_ids=("station-1",),
            max_workers=1,
            max_pending=1,
            max_retained=2,
        )
        first = jobs.submit("station-1", "forecast")
        for _ in range(100):
            if jobs.get(first.job_id).status == "failed":
                break
            time.sleep(0.005)
        second = jobs.submit("station-1", "forecast")
        jobs.close()

        self.assertEqual(jobs.get(first.job_id).error_code, "forecast_failed")
        self.assertEqual(jobs.get(second.job_id).error_code, "internal_error")
        self.assertNotIn("secret", repr((jobs.get(first.job_id), jobs.get(second.job_id))))


if __name__ == "__main__":
    unittest.main()
