"""Process isolation, shutdown and readiness regression boundaries."""

from datetime import datetime
import importlib.util
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest

from m3.worker.main import WorkerResources
from m3.worker.services.forecast_service import ForecastService
from m3.worker.services.station_cache import StationCache


def child_identity(value):
    return os.getpid(), value


def hold_native_gil(seconds, marker):
    import ctypes
    Path(marker).write_text(str(os.getpid()))
    library = ctypes.PyDLL(None)
    library.sleep(seconds)
    return os.getpid()


def read_schedule(day):
    from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE
    calendar = ACTIVE_SCHEDULE.get()
    return calendar.slot_is_working(day, 9 * 3600), calendar.manifest()


def schedule_present():
    from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE
    return ACTIVE_SCHEDULE.get() is not None


def fail_with_domain_error():
    from m3.worker.errors import M3Error
    raise M3Error("insufficient_history", "history missing", {"days": 3})


def exit_child():
    os._exit(7)


def create_isolation_test_app():
    """Offline fixture: real Uvicorn lifecycle, no production clients or writes."""
    from m3.tests.test_m3_worker_api import FakeResources, settings
    from m3.worker.main import create_app
    from m3.worker.services.compute_process import ForecastComputeProcess

    class Resources(FakeResources):
        def __init__(self):
            super().__init__()
            self.compute = ForecastComputeProcess()
            self.tmp = tempfile.TemporaryDirectory()
            self.marker = str(Path(self.tmp.name) / "native-started")
            self.thread = None

        def current_ready(self):
            return self.compute.healthy

        def close(self):
            self.compute.close()
            if self.thread is not None:
                self.thread.join(3)
            self.tmp.cleanup()
            super().close()

        def run(self):
            try:
                self.compute.run(hold_native_gil, 30, self.marker)
            except Exception:
                pass  # Shutdown deliberately interrupts the fixture's native sleep.

    resources = Resources()
    application = create_app(settings=settings(), resources=resources)

    def start():
        if resources.thread is None:
            resources.thread = Thread(target=resources.run)
            resources.thread.start()
        return {"started": True}

    def progress():
        marker = Path(resources.marker)
        return {"parent_pid": os.getpid(), "child_pid": int(marker.read_text()) if marker.exists() else None}

    application.add_api_route("/test/native", start, methods=["POST"], status_code=202)
    application.add_api_route("/test/native", progress, methods=["GET"])
    return application


class ComputeProcessTests(unittest.TestCase):
    def new_compute(self, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec("m3.worker.services.compute_process"))
        from m3.worker.services.compute_process import ForecastComputeProcess
        process = ForecastComputeProcess(**kwargs)
        self.addCleanup(process.close)
        return process

    def test_current_ready_does_not_wait_for_forecast_cache_lock(self):
        cache = StationCache("ES01")
        cache.state.state = "ready"
        forecast = object.__new__(ForecastService)
        forecast._caches = {"ES01": cache}
        resources = object.__new__(WorkerResources)
        resources.recovery_ready = True
        resources.statsforecast_version = "2.1.1"
        resources._startup_statsforecast_version = "2.1.1"
        resources.settings = SimpleNamespace(station_ids=("ES01",))
        resources.forecast_service = forecast
        resources.compute = None
        locked, release, finished = Event(), Event(), Event()
        result = []

        def hold_cache():
            with cache.exclusive():
                locked.set()
                release.wait(3)

        def check():
            result.append(resources.current_ready())
            finished.set()

        holder = Thread(target=hold_cache)
        holder.start()
        self.assertTrue(locked.wait(1))
        reader = Thread(target=check)
        reader.start()
        responsive = finished.wait(0.25)
        release.set()
        holder.join(2)
        reader.join(2)
        self.assertTrue(responsive, "readiness waited for the long forecast lock")
        self.assertEqual(result, [True])

    def test_work_runs_in_a_reused_child_process(self):
        compute = self.new_compute()
        first = compute.run(child_identity, {"values": [1, 2]})
        second = compute.run(child_identity, "next")
        self.assertNotEqual(first[0], os.getpid())
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], {"values": [1, 2]})
        self.assertTrue(compute.healthy)

    def test_schedule_mapping_proxies_cross_process_and_do_not_leak(self):
        from datetime import date
        from m3.worker.domain.production_schedule import ProductionSchedule, use_schedule
        compute = self.new_compute()
        day = date(2026, 10, 21)
        calendar = ProductionSchedule("ES02", "station_2", "113", day, day, day,
                                      {day: "WORK"}, {}, '{"source":"test"}')
        with use_schedule(calendar):
            working, manifest = compute.run(read_schedule, day)
        self.assertTrue(working)
        self.assertEqual(manifest, calendar.manifest())
        self.assertFalse(compute.run(schedule_present))

    def test_domain_error_round_trip_does_not_break_worker(self):
        from m3.worker.errors import M3Error
        compute = self.new_compute()
        with self.assertRaises(M3Error) as error:
            compute.run(fail_with_domain_error)
        self.assertEqual(error.exception.code, "insufficient_history")
        self.assertEqual(error.exception.details, {"days": 3})
        self.assertTrue(compute.healthy)
        self.assertEqual(compute.run(child_identity, "ok")[1], "ok")

    def test_child_crash_fails_closed_without_inline_fallback(self):
        from m3.worker.errors import M3Error
        compute = self.new_compute()
        with self.assertRaises(M3Error):
            compute.run(exit_child)
        self.assertFalse(compute.healthy)
        with self.assertRaises(M3Error):
            compute.run(child_identity, "must not run inline")
        from fastapi.testclient import TestClient
        from m3.tests.test_m3_worker_api import FakeResources, settings
        from m3.worker.main import create_app
        resources = FakeResources()
        resources.current_ready = lambda: compute.healthy
        with TestClient(create_app(settings=settings(), resources=resources)) as client:
            started = time.monotonic()
            self.assertEqual(client.get("/ready").status_code, 503)
            self.assertLess(time.monotonic() - started, 0.75)

    def test_native_gil_hold_does_not_stop_parent_and_close_reaps_child(self):
        compute = self.new_compute()
        result, errors = [], []
        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp) / "started")

            def run():
                try:
                    result.append(compute.run(hold_native_gil, 30, marker))
                except Exception as error:
                    errors.append(error)

            thread = Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 15
            while not Path(marker).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(Path(marker).exists(), "child did not start")
            child = int(Path(marker).read_text())
            started = time.monotonic()
            self.assertTrue(compute.healthy)
            compute.close()
            thread.join(3)
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse(thread.is_alive())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertFalse(compute.healthy)
            self.assertFalse(result)
            self.assertTrue(errors)

    def test_health_and_ready_stay_responsive_during_native_gil_hold(self):
        from fastapi.testclient import TestClient
        from m3.tests.test_m3_worker_api import FakeResources, settings
        from m3.worker.main import create_app
        compute = self.new_compute()
        resources = FakeResources()
        resources.current_ready = lambda: compute.healthy
        errors = []
        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp) / "started")

            def run():
                try:
                    compute.run(hold_native_gil, 30, marker)
                except Exception as error:
                    errors.append(error)

            with TestClient(create_app(settings=settings(), resources=resources)) as client:
                thread = Thread(target=run)
                thread.start()
                deadline = time.monotonic() + 15
                while not Path(marker).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(Path(marker).exists())
                try:
                    for path in ("/health", "/ready", "/health", "/ready"):
                        started = time.monotonic()
                        self.assertEqual(client.get(path).status_code, 200)
                        self.assertLess(time.monotonic() - started, 0.75)
                finally:
                    compute.close()
                    thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_shutdown_event_interrupts_native_computation(self):
        stop = Event()
        compute = self.new_compute(stop_event=stop)
        errors = []
        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp) / "started")

            def run():
                try:
                    compute.run(hold_native_gil, 30, marker)
                except Exception as error:
                    errors.append(error)

            thread = Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 15
            while not Path(marker).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(Path(marker).exists())
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertFalse(compute.healthy)

    def test_real_15minute_load_and_soc_outputs_match_inline(self):
        from dataclasses import asdict, replace
        from datetime import timedelta
        import pandas as pd
        from m3.tests.test_m3_custom_forecasting import (
            load_dataset_with_weeks, make_selection_config, soc_dataset_with_weekly_pattern,
        )
        from m3.worker.domain.custom_forecasting import select_custom_champion, forecast_custom_series
        from m3.worker.domain.production_schedule import ProductionSchedule, use_schedule
        compute = self.new_compute()
        config = make_selection_config(28, interval_seconds=900)
        soc = soc_dataset_with_weekly_pattern()
        grid = pd.date_range(config.history_start, periods=28 * 96, freq="15min")
        frame = soc.frame.set_index("ds").reindex(grid, method="ffill").rename_axis("ds").reset_index()
        soc = replace(soc, frame=frame, interval_seconds=900, points_per_day=96,
                      source_available_points=len(frame), end=grid[-1].to_pydatetime())
        day = config.history_start.date() - timedelta(days=1)
        statuses = {day + timedelta(days=i): "WORK" for i in range(31)}
        calendar = ProductionSchedule("ES02", "station_2", "113", day,
                                      config.forecast_end.date(), config.forecast_start.date(),
                                      statuses, {}, '{"source":"equivalence-test"}')
        with use_schedule(calendar):
            for dataset in (load_dataset_with_weeks(4, interval_seconds=900), soc):
                inline = select_custom_champion(dataset, config)
                child = compute.run(select_custom_champion, dataset, config)
                expected, actual = asdict(inline), asdict(child)
                expected.pop("selected_at")
                actual.pop("selected_at")
                self.assertEqual(actual, expected)
                self.assertEqual(
                    compute.run(forecast_custom_series, dataset, child, config),
                    forecast_custom_series(dataset, inline, config),
                )

    def test_regular_forecast_selection_and_predictions_match_inline(self):
        from dataclasses import asdict
        from datetime import timedelta
        from m3.tests.m3_test_support import make_quarter_hour_points
        from m3.worker.domain.forecasting import select_champion, forecast_one_safe
        from m3.worker.domain.training_data import build_training_dataset
        compute = self.new_compute()
        for unique_id in ("station_total_load", "storage_soc"):
            dataset = build_training_dataset(make_quarter_hour_points(7, unique_id), unique_id)
            inline = select_champion(dataset)
            child = compute.run(select_champion, dataset)
            expected, actual = asdict(inline), asdict(child)
            expected.pop("selected_at")
            actual.pop("selected_at")
            self.assertEqual(actual, expected)
            at = dataset.end + timedelta(minutes=15)
            self.assertEqual(compute.run(forecast_one_safe, dataset, child, at),
                             forecast_one_safe(dataset, inline, at))

    def test_real_uvicorn_remains_responsive_and_reaps_compute_child(self):
        import httpx
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            socket = str(Path(tmp) / "api.sock")
            output = Path(tmp) / "server.log"
            with output.open("w+") as log:
                server = subprocess.Popen([
                    sys.executable, "-m", "uvicorn",
                    "m3.tests.test_m3_compute_process:create_isolation_test_app", "--factory",
                    "--uds", socket, "--no-access-log",
                ], stdout=log, stderr=subprocess.STDOUT)
                child_pid = None
                try:
                    deadline = time.monotonic() + 20
                    while not Path(socket).exists() and time.monotonic() < deadline:
                        if server.poll() is not None:
                            self.fail("offline Uvicorn exited: " + output.read_text())
                        time.sleep(0.05)
                    self.assertTrue(Path(socket).exists(), output.read_text())
                    with httpx.Client(transport=httpx.HTTPTransport(uds=socket),
                                      base_url="http://offline", timeout=1) as client:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            if server.poll() is not None:
                                self.fail("offline Uvicorn exited: " + output.read_text())
                            try:
                                if client.get("/ready").status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            time.sleep(0.05)
                        else:
                            self.fail("offline Uvicorn did not become ready: " + output.read_text())
                        self.assertEqual(client.post("/test/native").status_code, 202)
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            status = client.get("/test/native").json()
                            child_pid = status["child_pid"]
                            if child_pid is not None:
                                break
                            time.sleep(0.05)
                        self.assertIsNotNone(child_pid)
                        self.assertNotEqual(child_pid, server.pid)
                        samples = []
                        for _ in range(5):
                            for path in ("/health", "/ready", "/offline-not-found"):
                                started = time.monotonic()
                                response = client.get(path)
                                elapsed = time.monotonic() - started
                                self.assertEqual(response.status_code, 404 if path == "/offline-not-found" else 200)
                                self.assertLess(elapsed, 0.75)
                                samples.append(elapsed)
                        print(f"offline_uvicorn_compute_child={child_pid} probes={len(samples)} max_seconds={max(samples):.4f}")
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=3)
                        self.fail("offline Uvicorn shutdown did not reap the child promptly")
                self.assertIn(server.returncode, (0, -15), output.read_text())
                self.assertIn("Application shutdown complete", output.read_text())
                if child_pid is not None:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
