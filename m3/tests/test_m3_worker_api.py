"""FastAPI operations surface, lifecycle, and resource assembly tests."""

from datetime import datetime, timedelta
import importlib
import asyncio
import threading
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from fastapi.testclient import TestClient
from pydantic import HttpUrl, SecretStr, TypeAdapter

from m3.worker.config import Settings, StationBinding
from m3.worker.contracts import JobState
from m3.worker.errors import M3Error
import m3.worker.main as worker_main
from m3.worker.main import WorkerResources, build_resources, create_app


NOW = datetime.fromisoformat("2026-08-25T03:00:00+08:00")
ES01 = "plant-alpha-ES01"
ES02 = "plant-beta-ES02"


def settings() -> Settings:
    url = TypeAdapter(HttpUrl)
    return Settings(
        stations=(
            StationBinding(ES01, "station_1", "Alpha"),
            StationBinding(ES02, "station_2", "Beta"),
        ),
        raw_source_url=url.validate_python("http://raw.internal"),
        raw_source_api_token=SecretStr("raw-secret"),
        source_base_url=url.validate_python("http://source.internal"),
        source_api_token=SecretStr("source-secret"),
        nocobase_base_url=url.validate_python("http://nocobase.internal"),
        nocobase_api_key=SecretStr("sink-secret"),
        admin_api_token=SecretStr("admin-secret"),
    )


class FakeJobs:
    def __init__(self):
        self.submissions = []
        self.jobs = {}

    def submit(self, station_id, task):
        self.submissions.append((station_id, task))
        job = JobState(
            job_id="df574fb6-bd3a-4b01-9004-0ef4da44747a",
            station_id=station_id,
            task=task,
            status="queued",
        )
        self.jobs[job.job_id] = job
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)


class FakeScheduler:
    def __init__(self, events):
        self.events = events
        self.running = False
        self.healthy = True
        self.last_error_code = None
        self.lifecycle_state = "new"

    def start(self):
        self.events.append("start")
        self.running = True
        self.lifecycle_state = "running"

    def stop(self):
        self.events.append("stop")
        self.running = False
        self.lifecycle_state = "stopped"


class FakeResources:
    def __init__(self, *, recovery_ready=True):
        self.events = []
        self.jobs = FakeJobs()
        self.scheduler = FakeScheduler(self.events)
        self.recovery_ready = recovery_ready
        self.statsforecast_version = "2.1.1"
        champion = SimpleNamespace(
            model_name="AutoETS",
            cv_mape_percent=8.5,
            selected_at=NOW,
            training_start=NOW,
            training_end=NOW,
            statsforecast_version="2.1.1",
            selection_reason=None,
        )
        self._state = SimpleNamespace(
            state="ready",
            champions={
                "station_total_load": champion,
                "storage_soc": champion,
            },
            last_source_at=NOW,
            last_published_at=NOW,
            last_error_code=None,
        )
        self.forecast_service = SimpleNamespace(state=lambda _station: self._state)

    def recover(self):
        self.events.append("recover")
        return self.recovery_ready

    def current_ready(self):
        return self.recovery_ready

    def close(self):
        self.events.append("close")


AUTH = {"Authorization": "Bearer admin-secret"}


def wait_recovery(client, app):
    async def wait():
        await asyncio.shield(app.state.recovery_task)
    client.portal.call(wait)


class WorkerApiTests(unittest.TestCase):
    def test_rolling_soc_get_is_authenticated_read_only(self):
        resources = FakeResources()
        resources.clock = lambda: NOW
        resources.rolling_soc = Mock()
        resources.rolling_soc.snapshot.return_value = {
            'station_id': ES02, 'status': 'pending', 'points': []}
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app) as client:
            url = f'/v1/stations/{ES02}/rolling-soc'
            self.assertEqual(client.get(url).status_code, 401)
            response = client.get(url, headers={'Authorization': 'Bearer admin-secret'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['status'], 'pending')
            self.assertEqual(client.get('/v1/stations/unknown/rolling-soc',
                headers={'Authorization': 'Bearer admin-secret'}).status_code, 404)
        resources.rolling_soc.update.assert_not_called()

    def test_latest_custom_forecast_is_selected_by_station_interval_and_horizon(self):
        """A browser without local run state can recover the newest matching result."""
        resources = FakeResources()
        config = SimpleNamespace(
            history_start=NOW - timedelta(days=28),
            history_end=NOW,
            history_days=28,
            forecast_start=NOW,
            forecast_end=NOW + timedelta(days=1),
            forecast_days=1,
            interval_seconds=60,
            points_per_day=1440,
            expected_points_per_series=1440,
            model_policy="full_selection",
        )
        run = SimpleNamespace(
            run_id="latest-minute-run",
            station_id=ES02,
            status="evaluated",
            config=config,
            model_manifest={"selection_policy": "weekly_load_v2"},
            source_manifest={},
            record=SimpleNamespace(error_code=None),
            started_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            evaluated_at=NOW + timedelta(minutes=2),
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=2),
        )
        calls = []

        def latest(station_id, *, interval_seconds, forecast_days):
            calls.append((station_id, interval_seconds, forecast_days))
            return run

        resources.custom_forecasts = SimpleNamespace(latest=latest)
        app = create_app(settings(), resources, start_scheduler=False)

        with TestClient(app) as client:
            response = client.get(
                f"/v1/stations/{ES02}/custom-forecast-runs/latest",
                params={"interval_seconds": 60, "forecast_days": 1},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "latest-minute-run")
        self.assertEqual(response.json()["expected_points_per_series"], 1440)
        self.assertEqual(calls, [(ES02, 60, 1)])

    def test_worker_info_logging_uses_the_uvicorn_handler(self):
        m3_logger = Mock(handlers=[], propagate=True)
        uvicorn_handler = logging.NullHandler()
        uvicorn_logger = Mock(handlers=[uvicorn_handler])

        with patch.object(
            worker_main.logging,
            "getLogger",
            side_effect=lambda name: {
                "m3.worker": m3_logger,
                "uvicorn": uvicorn_logger,
            }[name],
        ):
            worker_main._configure_worker_logging()

        m3_logger.setLevel.assert_called_once_with(logging.INFO)
        m3_logger.addHandler.assert_called_once_with(uvicorn_handler)
        self.assertFalse(m3_logger.propagate)

    def test_import_without_production_environment_is_safe(self):
        """Importing the uvicorn target must not eagerly read missing secrets."""
        names = [name for name in os.environ if name.startswith("M3_")]
        old = {name: os.environ.pop(name) for name in names}
        try:
            module = importlib.reload(importlib.import_module("m3.worker.main"))
            self.assertEqual(module.app.title, "VIFA M3 Forecast Worker")
        finally:
            os.environ.update(old)

    def test_openapi_has_only_approved_paths_and_manual_schema_forbids_properties(self):
        """An accidental inbound data/URL endpoint would violate the active-pull boundary."""
        app = create_app(settings(), FakeResources(), start_scheduler=False)
        schema = app.openapi()

        self.assertEqual(
            sorted(schema["paths"]),
            [
                "/health",
                "/ready",
                "/v1/custom-forecast-runs/{run_id}",
                "/v1/custom-forecast-runs/{run_id}/result",
                "/v1/jobs/{job_id}",
                "/v1/stations/{station_id}/custom-forecast-performance",
                "/v1/stations/{station_id}/custom-forecast-runs/latest",
                "/v1/stations/{station_id}/rolling-soc",
                "/v1/stations/{station_id}/runs/custom-forecast",
                "/v1/stations/{station_id}/runs/forecast",
                "/v1/stations/{station_id}/runs/model-selection",
                "/v1/stations/{station_id}/state",
            ],
        )
        body = schema["components"]["schemas"]["ManualRunRequest"]
        self.assertEqual(body.get("properties"), {})
        self.assertEqual(body.get("additionalProperties"), False)

    def test_runtime_registers_no_docs_or_openapi_http_endpoints(self):
        """Default framework documentation routes would expand the approved inbound surface."""
        app = create_app(settings(), FakeResources(), start_scheduler=False)

        with TestClient(app) as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                self.assertEqual(client.get(path).status_code, 404)

    def test_router_auth_runs_before_station_lookup_and_rejects_malformed_schemes(self):
        """Unauthenticated callers must not enumerate configured station IDs."""
        resources = FakeResources()
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/stations/unknown/state").status_code, 401)
            for value in ("Basic admin-secret", "bearer admin-secret", "Bearer", "Bearer  admin-secret"):
                self.assertEqual(
                    client.get(
                        f"/v1/stations/{ES01}/state",
                        headers={"Authorization": value},
                    ).status_code,
                    401,
                )
            self.assertEqual(
                client.get("/v1/stations/unknown/state", headers=AUTH).status_code,
                404,
            )

    def test_manual_posts_accept_only_empty_json_and_use_exact_allowed_tasks(self):
        """Callers must not submit observations, clocks, credentials, or outbound URLs."""
        resources = FakeResources()
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app) as client:
            for path in (
                f"/v1/stations/{ES01}/runs/forecast",
                f"/v1/stations/{ES01}/runs/model-selection",
            ):
                unsafe = client.post(
                    path,
                    headers=AUTH,
                    json={
                        "as_of": NOW.isoformat(),
                        "source_url": "https://attacker.invalid",
                        "token": "mine",
                        "observations": [],
                    },
                )
                self.assertEqual(unsafe.status_code, 422)
                self.assertEqual(unsafe.json(), {"code": "request_invalid"})
                self.assertNotIn("attacker.invalid", unsafe.text)
                self.assertNotIn("mine", unsafe.text)
                accepted = client.post(path, headers=AUTH, json={})
                self.assertEqual(accepted.status_code, 202)
            self.assertEqual(
                resources.jobs.submissions,
                [(ES01, "forecast"), (ES01, "model_selection")],
            )

    def test_state_and_job_responses_are_typed_and_secret_safe(self):
        """Dataclass internals, URLs, tokens, cache points, and exception text must stay private."""
        resources = FakeResources()
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app) as client:
            state = client.get(f"/v1/stations/{ES01}/state", headers=AUTH)
            submitted = client.post(
                f"/v1/stations/{ES01}/runs/forecast", headers=AUTH, json={}
            )
            job = client.get(
                f"/v1/jobs/{submitted.json()['job_id']}", headers=AUTH
            )

        self.assertEqual(state.status_code, 200)
        self.assertEqual(
            set(state.json()),
            {
                "state",
                "champions",
                "last_source_at",
                "last_published_at",
                "last_error_code",
                "stale",
            },
        )
        self.assertEqual(
            set(state.json()["champions"]["station_total_load"]),
            {
                "model_name",
                "cv_mape_percent",
                "selected_at",
                "training_start",
                "training_end",
                "statsforecast_version",
                "selection_reason",
            },
        )
        self.assertEqual(
            list(state.json()["champions"]),
            ["station_total_load", "storage_soc"],
        )
        self.assertEqual(job.status_code, 200)
        rendered = state.text + job.text
        for secret in ("source-secret", "sink-secret", "admin-secret", "source.internal"):
            self.assertNotIn(secret, rendered)

    def test_health_is_aggregate_unready_before_recovery_and_contains_no_business_data(self):
        """Health must not expose station IDs or claim readiness without startup recovery."""
        resources = FakeResources()
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app) as client:
            payload = client.get("/health").json()

        self.assertEqual(
            payload,
            {
                "status": "initializing",
                "process": "running",
                "scheduler": "disabled",
                "dependencies": "initializing",
                "statsforecast_version": "2.1.1",
            },
        )
        self.assertNotIn(ES01, repr(payload))

    def test_lifespan_recovers_then_starts_and_stops_then_closes_once(self):
        """Starting the scheduler before recovery or leaking owned clients causes races."""
        resources = FakeResources()
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            health = client.get("/health").json()
            self.assertEqual(health["status"], "ok")
            self.assertEqual(resources.events, ["recover", "start"])
        self.assertEqual(resources.events, ["recover", "start", "stop", "close"])

    def test_recovery_failure_cannot_report_ready(self):
        """A running thread cannot make failed dependency recovery healthy."""
        resources = FakeResources(recovery_ready=False)
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            health = client.get("/health").json()
            self.assertEqual(health["status"], "initializing")
            self.assertEqual(health["dependencies"], "failed")

    def test_live_but_unhealthy_scheduler_cannot_report_ready(self):
        """Thread liveness alone must not hide repeated clock/tick failures."""
        resources = FakeResources()
        resources.scheduler.healthy = False
        resources.scheduler.last_error_code = "internal_error"
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            health = client.get("/health").json()
        self.assertEqual(health["status"], "initializing")
        self.assertEqual(health["scheduler"], "unhealthy")
        self.assertEqual(health["dependencies"], "ready")

    def test_live_healthy_but_stopping_scheduler_cannot_report_ready(self):
        """A stop-in-progress thread must not be advertised as a running scheduler."""
        resources = FakeResources()
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            resources.scheduler.running = True
            resources.scheduler.healthy = True
            resources.scheduler.lifecycle_state = "stopping"
            health = client.get("/health").json()
        self.assertEqual(health["status"], "initializing")
        self.assertEqual(health["scheduler"], "stopping")
        self.assertEqual(health["dependencies"], "ready")

    def test_health_rechecks_current_dependency_readiness_on_every_request(self):
        """A successful startup snapshot cannot mask later version metadata drift."""
        resources = FakeResources()
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            self.assertEqual(client.get("/health").json()["status"], "ok")
            resources.current_ready = lambda: False
            resources.statsforecast_version = "9.9.9"
            drifted = client.get("/health").json()
        self.assertEqual(drifted["status"], "initializing")
        self.assertEqual(drifted["dependencies"], "failed")

    def test_test_lifespan_skips_recovery_scheduler_but_still_closes(self):
        """The test mode contract must not accidentally launch background work."""
        resources = FakeResources()
        app = create_app(settings(), resources, start_scheduler=False)
        with TestClient(app):
            self.assertEqual(resources.events, [])
        self.assertEqual(resources.events, ["close"])

    def test_startup_failure_still_stops_and_closes_owned_resources(self):
        """Background recovery failures stay observable and do not leak resources."""
        resources = FakeResources()

        def fail_recovery():
            resources.events.append("recover")
            raise RuntimeError("source-secret")

        resources.recover = fail_recovery
        app = create_app(settings(), resources)

        with TestClient(app) as client:
            wait_recovery(client, app)
            self.assertEqual(client.get("/ready").status_code, 503)
            self.assertEqual(client.get("/health").json()["dependencies"], "failed")
        self.assertEqual(resources.events, ["recover", "stop", "close"])

    def test_raising_stop_still_attempts_close_and_preserves_stop_error(self):
        """A non-live scheduler cleanup error must not skip owned client cleanup."""
        resources = FakeResources()

        def fail_stop():
            resources.events.append("stop")
            resources.scheduler.running = False
            raise M3Error("scheduler_stop_failed", "source-secret")

        def fail_close():
            resources.events.append("close")
            raise RuntimeError("sink-secret")

        resources.scheduler.stop = fail_stop
        resources.close = fail_close
        app = create_app(settings(), resources)
        with self.assertRaises(M3Error) as caught:
            with TestClient(app) as client:
                wait_recovery(client, app)
        self.assertEqual(caught.exception.code, "scheduler_stop_failed")
        self.assertEqual(resources.events, ["recover", "start", "stop", "close"])

    def test_live_scheduler_stop_timeout_skips_dependent_resource_close(self):
        """A live worker must keep its HTTP clients open after stop timeout."""
        resources = FakeResources()

        def timeout_stop():
            resources.events.append("stop")
            resources.scheduler.running = True
            raise M3Error("scheduler_stop_timeout", "still running")

        resources.scheduler.stop = timeout_stop
        app = create_app(settings(), resources)
        with self.assertRaises(M3Error) as caught:
            with TestClient(app) as client:
                wait_recovery(client, app)
        self.assertEqual(caught.exception.code, "scheduler_stop_timeout")
        self.assertEqual(resources.events, ["recover", "start", "stop"])


class BackgroundRecoveryTests(unittest.TestCase):
    def test_reads_available_while_all_prediction_posts_are_blocked(self):
        entered, release = threading.Event(), threading.Event()
        resources = FakeResources()
        resources.custom_forecasts = SimpleNamespace(get=lambda _: None, submit=Mock())
        def recover():
            entered.set()
            if not release.wait(5):
                return False
            return True
        resources.recover = recover
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                self.assertEqual(client.get("/health").json()["dependencies"], "initializing")
                self.assertEqual(client.get("/ready").status_code, 503)
                self.assertEqual(client.get("/v1/custom-forecast-runs/missing", headers=AUTH).status_code, 404)
                for kind in ("forecast", "model-selection", "custom-forecast"):
                    result = client.post(f"/v1/stations/{ES01}/runs/{kind}", headers=AUTH, json={})
                    self.assertEqual(result.status_code, 503, kind)
                self.assertEqual(resources.jobs.submissions, [])
                resources.custom_forecasts.submit.assert_not_called()
                self.assertNotIn("start", resources.events)
            finally:
                release.set()
            wait_recovery(client, app)
            self.assertEqual(client.get("/ready").status_code, 200)
            self.assertEqual(client.post(f"/v1/stations/{ES01}/runs/forecast", headers=AUTH, json={}).status_code, 202)
            resources.current_ready = lambda: False
            self.assertEqual(client.post(f"/v1/stations/{ES01}/runs/forecast", headers=AUTH, json={}).status_code, 503)

    def test_recovery_failure_keeps_scheduler_and_submission_disabled(self):
        resources = FakeResources(recovery_ready=False)
        app = create_app(settings(), resources)
        with TestClient(app) as client:
            wait_recovery(client, app)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/ready").status_code, 503)
            self.assertEqual(client.post(f"/v1/stations/{ES01}/runs/forecast", headers=AUTH, json={}).status_code, 503)
            self.assertNotIn("start", resources.events)
            self.assertEqual(resources.jobs.submissions, [])


class RecoveryShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_during_recovery_never_starts_scheduler_or_closes_live_clients(self):
        entered, release = threading.Event(), threading.Event()
        resources = FakeResources()
        def recover():
            entered.set()
            release.wait(5)
            resources.events.append("recovery_finished")
            return True
        resources.recover = recover
        app = create_app(settings(), resources)
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0)
        try:
            self.assertTrue(app.state.shutting_down)
            self.assertNotIn("close", resources.events)
        finally:
            release.set()
        await closing
        self.assertEqual(resources.events, ["recovery_finished", "stop", "close"])


class ResourceTests(unittest.TestCase):
    def test_startup_recovery_prefers_persisted_models_without_reselection(self):
        events = []

        class Forecast:
            def bootstrap(self, station, at):
                events.append(("bootstrap", station, at))

            def restore_models(self, station):
                events.append(("restore", station))
                return True

            def select_models(self, _station):
                raise AssertionError("restored champions must skip startup selection")

            def state(self, _station):
                return SimpleNamespace(state="ready")

        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1",), acceptance_enabled=False
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=Forecast(),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(recover=lambda: None, close=lambda: None),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(run_station=lambda *_args: 0),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW,
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertTrue(resources.recover())
        self.assertEqual(
            events,
            [
                ("bootstrap", "station-1", NOW),
                ("restore", "station-1"),
            ],
        )

    def test_startup_recovery_reselects_when_persisted_models_are_unusable(self):
        events = []

        class Forecast:
            def bootstrap(self, station, at):
                events.append(("bootstrap", station, at))

            def restore_models(self, station):
                events.append(("restore", station))
                return False

            def select_models(self, station):
                events.append(("select", station))

            def state(self, _station):
                return SimpleNamespace(state="ready")

        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1",), acceptance_enabled=False
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=Forecast(),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(recover=lambda: None, close=lambda: None),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(run_station=lambda *_args: 0),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW,
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertTrue(resources.recover())
        self.assertEqual(
            events,
            [
                ("bootstrap", "station-1", NOW),
                ("restore", "station-1"),
                ("select", "station-1"),
            ],
        )

    def test_build_resources_shares_one_daily_coordinator_with_scheduler(self):
        """Splitting repository/service instances would desynchronize startup and scheduled runs."""
        custom_repository = object()
        custom_forecasts = SimpleNamespace(close=lambda: None)
        daily_custom_forecasts = object()
        scheduler = SimpleNamespace(run_manual=lambda *_args: None)

        class Client:
            def __init__(self, **_kwargs):
                pass

            def close(self):
                pass

        with (
            patch("m3.worker.main.httpx.Client", Client),
            patch(
                "m3.worker.main.CustomForecastRepository",
                return_value=custom_repository,
            ),
            patch(
                "m3.worker.main.CustomForecastService",
                return_value=custom_forecasts,
            ),
            patch(
                "m3.worker.main.DailyCustomForecastService",
                return_value=daily_custom_forecasts,
            ) as daily_constructor,
            patch(
                "m3.worker.main.SchedulerRunner",
                return_value=scheduler,
            ) as scheduler_constructor,
            patch(
                "m3.worker.main.verify_statsforecast_runtime",
                return_value="2.1.1",
            ),
            patch("m3.worker.main.ForecastService", return_value=SimpleNamespace()),
            patch("m3.worker.main.AcceptanceService", return_value=SimpleNamespace()),
        ):
            resources = build_resources(settings(), clock=lambda: NOW)

        daily_constructor.assert_called_once_with(
            custom_repository,
            custom_forecasts,
        )
        self.assertIs(
            scheduler_constructor.call_args.kwargs["daily_custom_forecast_service"],
            daily_custom_forecasts,
        )
        self.assertIs(resources.daily_custom_forecasts, daily_custom_forecasts)
        self.assertIs(resources.scheduler, scheduler)
        resources.close()

    def test_dynamic_readiness_rechecks_runtime_version_and_station_state(self):
        """A startup version string cannot keep health ready after runtime/state drift."""
        states = {"station-1": SimpleNamespace(state="ready")}
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=("station-1",)),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=SimpleNamespace(state=lambda station: states[station]),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(close=lambda: None),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(),
            scheduler=SimpleNamespace(running=True, healthy=True),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW,
            alert_sink=lambda *_args: None,
            statsforecast_version="2.1.1",
            recovery_ready=True,
        )
        with patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"):
            self.assertTrue(resources.current_ready())
        with patch("m3.worker.main.verify_statsforecast_runtime", return_value="9.9.9"):
            self.assertFalse(resources.current_ready())
        resources.statsforecast_version = "9.9.9"
        with patch("m3.worker.main.verify_statsforecast_runtime", return_value="9.9.9"):
            self.assertFalse(resources.current_ready())
        resources.statsforecast_version = "2.1.1"
        states["station-1"] = SimpleNamespace(state="initializing")
        with patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"):
            self.assertFalse(resources.current_ready())

    def test_recover_isolates_stations_and_reconciles_global_batches_once(self):
        """One station failure must not prevent later stations or duplicate global reconciliation."""
        calls = []

        class Forecast:
            def bootstrap(self, station, at):
                calls.append(("bootstrap", station, at))
                if station == "station-1":
                    raise M3Error("source_http_failed", "secret")

            def select_models(self, station):
                calls.append(("select", station))

            def state(self, station):
                return SimpleNamespace(state="ready" if station == "station-2" else "initializing")

        acceptance = SimpleNamespace(
            reconcile_writing_batches=lambda station: calls.append(
                ("reconcile_batches", station)
            ),
            reconcile_run_summary=lambda station: calls.append(
                ("reconcile_summary", station)
            ),
        )
        custom_forecasts = SimpleNamespace(
            recover=lambda: calls.append(("custom_recover",)),
            close=lambda: None,
        )
        daily_custom_forecasts = SimpleNamespace(
            run_station=lambda station, at: calls.append(("daily", station, at))
        )
        alerts = []
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=("station-1", "station-2")),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=Forecast(),
            acceptance_service=acceptance,
            custom_forecasts=custom_forecasts,
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=daily_custom_forecasts,
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW.replace(minute=7, second=19),
            alert_sink=lambda *items: alerts.append(items),
            statsforecast_version="2.1.1",
        )

        self.assertFalse(resources.recover())
        self.assertEqual(
            calls,
            [
                ("bootstrap", "station-1", NOW.replace(minute=0, second=0)),
                ("bootstrap", "station-2", NOW.replace(minute=0, second=0)),
                ("select", "station-2"),
                ("reconcile_batches", "station-1"),
                ("reconcile_summary", "station-1"),
                ("reconcile_batches", "station-2"),
                ("reconcile_summary", "station-2"),
            ],
        )
        self.assertEqual(
            alerts,
            [("station-1", "startup_recovery", "source_http_failed", NOW.replace(minute=0, second=0))],
        )

    def test_recover_skips_acceptance_reconciliation_when_phase_one_is_disabled(self):
        calls = []

        class Forecast:
            def bootstrap(self, station, at):
                calls.append(("bootstrap", station, at))

            def select_models(self, station):
                calls.append(("select", station))

            def state(self, _station):
                return SimpleNamespace(state="ready")

        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1",), acceptance_enabled=False
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=Forecast(),
            acceptance_service=SimpleNamespace(
                reconcile_writing_batches=lambda: (_ for _ in ()).throw(
                    AssertionError("acceptance reconciliation must be disabled")
                )
            ),
            custom_forecasts=SimpleNamespace(
                recover=lambda: None,
                close=lambda: None,
            ),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(run_station=lambda *_args: 0),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW.replace(minute=7, second=19),
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertTrue(resources.recover())
        self.assertEqual(
            calls,
            [
                ("bootstrap", "station-1", NOW.replace(minute=0, second=0)),
                ("select", "station-1"),
            ],
        )

    def test_recover_runs_daily_catch_up_after_custom_recovery_for_every_station(self):
        """Skipping startup catch-up would wait until the next quarter-hour scheduler tick."""
        events = []
        now = NOW.replace(hour=0, minute=18, second=23)
        forecast = SimpleNamespace(
            bootstrap=lambda *_args: None,
            select_models=lambda *_args: None,
            state=lambda _station: SimpleNamespace(state="ready"),
        )
        acceptance = SimpleNamespace(
            reconcile_writing_batches=lambda *_args: None,
            reconcile_run_summary=lambda *_args: None,
        )
        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1", "station-2"),
                acceptance_enabled=True,
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=forecast,
            acceptance_service=acceptance,
            custom_forecasts=SimpleNamespace(
                recover=lambda: events.append(("custom_recover",)),
                close=lambda: None,
            ),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(
                run_station=lambda station, at: events.append(
                    ("daily", station, at)
                )
            ),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: now,
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertTrue(resources.recover())
        self.assertEqual(
            events,
            [
                ("custom_recover",),
                ("daily", "station-1", now),
                ("daily", "station-2", now),
            ],
        )

    def test_recover_daily_failure_alerts_and_continues_without_losing_recovery(self):
        """One failed station must not suppress catch-up or undo custom recovery for another."""
        events = []
        alerts = []
        now = NOW.replace(hour=0, minute=18, second=23)

        def run_daily(station, at):
            events.append(("daily", station, at))
            if station == "station-1":
                raise M3Error(
                    "daily_custom_forecast_failed",
                    "secret",
                )

        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1", "station-2"),
                acceptance_enabled=False,
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=SimpleNamespace(
                bootstrap=lambda *_args: None,
                select_models=lambda *_args: None,
                state=lambda _station: SimpleNamespace(state="ready"),
            ),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(
                recover=lambda: events.append(("custom_recover",)),
                close=lambda: None,
            ),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(run_station=run_daily),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: now,
            alert_sink=lambda *items: alerts.append(items),
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertFalse(resources.recover())
        self.assertEqual(
            events,
            [
                ("custom_recover",),
                ("daily", "station-1", now),
                ("daily", "station-2", now),
            ],
        )
        self.assertEqual(
            alerts,
            [
                (
                    "station-1",
                    "daily_custom_forecast",
                    "daily_custom_forecast_failed",
                    now.replace(minute=15, second=0),
                )
            ],
        )
        self.assertFalse(resources.recovery_ready)

    def test_recover_before_daily_start_calls_coordinator_without_creating_work(self):
        """Startup before 00:17 must preserve the coordinator's no-op boundary."""
        events = []
        now = NOW.replace(hour=0, minute=16, second=59)
        coordinator = worker_main.DailyCustomForecastService(
            SimpleNamespace(
                latest_completed_template=lambda *_args, **_kwargs: (
                    _ for _ in ()
                ).throw(
                    AssertionError(
                        "repository must not be queried before 00:17"
                    )
                ),
            ),
            SimpleNamespace(
                submit=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError(
                        "daily task must not be created before 00:17"
                    )
                ),
            ),
        )

        def run_daily(station, at):
            result = coordinator.run_station(station, at)
            events.append(("daily", station, at, result))
            return result

        resources = WorkerResources(
            settings=SimpleNamespace(
                station_ids=("station-1",),
                acceptance_enabled=False,
            ),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=SimpleNamespace(
                bootstrap=lambda *_args: None,
                select_models=lambda *_args: None,
                state=lambda _station: SimpleNamespace(state="ready"),
            ),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(
                recover=lambda: events.append(("custom_recover",)),
                close=lambda: None,
            ),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(run_station=run_daily),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: now,
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3.worker.main.verify_statsforecast_runtime",
            return_value="2.1.1",
        ):
            self.assertTrue(resources.recover())
        self.assertEqual(
            events,
            [("custom_recover",), ("daily", "station-1", now, 0)],
        )

    def test_resource_close_excludes_stateless_daily_coordinator(self):
        """Closing clients before stateful work can break outbound operations."""
        events = []
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=()),
            source_http=SimpleNamespace(close=lambda: events.append("source")),
            nocobase_http=SimpleNamespace(close=lambda: events.append("nocobase")),
            forecast_service=SimpleNamespace(),
            acceptance_service=SimpleNamespace(),
            custom_forecasts=SimpleNamespace(
                close=lambda: events.append("custom")
            ),
            custom_evaluations=SimpleNamespace(),
            daily_custom_forecasts=SimpleNamespace(),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: events.append("jobs")),
            clock=lambda: NOW,
            alert_sink=lambda *_args: None,
            statsforecast_version="2.1.1",
        )
        resources.close()
        resources.close()
        self.assertEqual(events, ["jobs", "custom", "source", "nocobase"])

    def test_build_resources_uses_two_separate_fixed_bounded_clients(self):
        """Sharing credentials or allowing redirects crosses Source and sink trust boundaries."""
        created = []

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        with (
            patch("m3.worker.main.httpx.Client", Client),
            patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3.worker.main.ForecastService", return_value=SimpleNamespace()),
            patch("m3.worker.main.AcceptanceService", return_value=SimpleNamespace()),
        ):
            resources = build_resources(settings(), clock=lambda: NOW)

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        for client in created:
            self.assertFalse(client.kwargs["follow_redirects"])
            self.assertEqual(client.kwargs["timeout"].connect, 2.0)
            self.assertEqual(client.kwargs["timeout"].read, 15.0)
            self.assertEqual(client.kwargs["timeout"].write, 15.0)
            self.assertEqual(client.kwargs["timeout"].pool, 2.0)
            self.assertEqual(client.kwargs["limits"].max_connections, 12)
            self.assertEqual(client.kwargs["limits"].max_keepalive_connections, 6)
        resources.close()

    def test_build_resources_splits_raw_nocobase_and_alert_dependencies(self):
        """Model/actual reads, persistence, and alerts keep fixed credentials."""
        created = []

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created.append(self)

            def close(self):
                pass

        configured = settings()
        raw_source = object()
        nocobase_api = object()
        acceptance_runs = object()
        forecast_service = object()
        acceptance_service = object()
        alert_client = object()
        with (
            patch("m3.worker.main.httpx.Client", Client),
            patch(
                "m3.worker.main.RawEnergySourceClient",
                return_value=raw_source,
            ) as raw_constructor,
            patch(
                "m3.worker.main.NocoBaseApiClient",
                return_value=nocobase_api,
            ) as nocobase_constructor,
            patch(
                "m3.worker.main.AcceptanceRunService",
                return_value=acceptance_runs,
            ) as run_constructor,
            patch(
                "m3.worker.main.NodeRedAlertClient",
                return_value=alert_client,
            ) as alert_constructor,
            patch(
                "m3.worker.main.ForecastService",
                return_value=forecast_service,
            ) as forecast_constructor,
            patch(
                "m3.worker.main.AcceptanceService",
                return_value=acceptance_service,
            ) as acceptance_constructor,
            patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
        ):
            resources = build_resources(configured, clock=lambda: NOW)

        raw_constructor.assert_called_once_with(
            str(configured.raw_source_url),
            "raw-secret",
            created[0],
            allowed_station_ids=(ES01, ES02),
        )
        nocobase_constructor.assert_called_once_with(
            str(configured.nocobase_base_url),
            "sink-secret",
            created[1],
            ANY,
        )
        alert_constructor.assert_called_once_with(
            str(configured.source_base_url),
            "source-secret",
            created[0],
            ANY,
        )
        forecast_args = forecast_constructor.call_args.args
        self.assertIs(forecast_args[0], raw_source)
        self.assertEqual(list(forecast_args[2]), [ES01, ES02])
        run_constructor.assert_called_once_with(nocobase_api)
        acceptance_kwargs = acceptance_constructor.call_args.kwargs
        self.assertIs(acceptance_kwargs["run_service"], acceptance_runs)
        self.assertIs(acceptance_kwargs["observation_source"], raw_source)
        self.assertIs(acceptance_kwargs["api"], nocobase_api)
        self.assertIs(resources.forecast_service, forecast_service)
        self.assertIs(resources.acceptance_service, acceptance_service)
        resources.close()

    def test_standard_resource_wiring_delivers_alerts_through_fixed_http_client(self):
        """Production must not silently fall back to logging-only alert delivery."""
        delivered = []
        alert_client = SimpleNamespace(
            send=lambda *args: delivered.append(args)
        )

        class Client:
            def __init__(self, **_kwargs):
                pass

            def close(self):
                pass

        with (
            patch("m3.worker.main.httpx.Client", Client),
            patch("m3.worker.main.NodeRedAlertClient", return_value=alert_client),
            patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3.worker.main.ForecastService", return_value=SimpleNamespace()),
            patch("m3.worker.main.AcceptanceService", return_value=SimpleNamespace()),
        ):
            resources = build_resources(settings(), clock=lambda: NOW)

        resources.alert_sink(
            "station-1", "forecast", "forecast_failed", NOW
        )
        self.assertIs(resources.alert_client, alert_client)
        self.assertEqual(
            delivered,
            [("station-1", "forecast", "forecast_failed", NOW)],
        )
        alert_client.send = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("sink-secret")
        )
        with self.assertLogs("m3.worker", level="WARNING") as logged:
            resources.alert_sink(
                "station-1\nsink-secret",
                "https://attacker.invalid",
                "token=secret",
                datetime(2026, 8, 25),
            )
        rendered_log = " ".join(logged.output)
        self.assertNotIn("sink-secret", rendered_log)
        self.assertNotIn("attacker.invalid", rendered_log)
        self.assertNotIn("token=secret", rendered_log)
        resources.close()

    def test_partial_build_attempts_all_client_closes_and_preserves_construction_error(self):
        """Cleanup failures must neither skip later resources nor replace the build cause."""
        events = []

        class Client:
            def __init__(self, **_kwargs):
                self.number = len(events)
                events.append(f"create-{self.number}")

            def close(self):
                events.append(f"close-{self.number}")
                if self.number == 0:
                    raise RuntimeError("source close failed")

        construction = ValueError("forecast construction failed")
        with (
            patch("m3.worker.main.httpx.Client", Client),
            patch("m3.worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3.worker.main.ForecastService", side_effect=construction),
        ):
            with self.assertRaises(ValueError) as caught:
                build_resources(settings(), clock=lambda: NOW)
        self.assertIs(caught.exception, construction)
        self.assertEqual(events, ["create-0", "create-1", "close-0", "close-1"])


if __name__ == "__main__":
    unittest.main()
