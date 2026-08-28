"""FastAPI operations surface, lifecycle, and resource assembly tests."""

from datetime import datetime
import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

from fastapi.testclient import TestClient
from pydantic import HttpUrl, SecretStr, TypeAdapter

from m3_worker.config import Settings, StationBinding
from m3_worker.contracts import JobState
from m3_worker.errors import M3Error
from m3_worker.main import WorkerResources, build_resources, create_app


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


class WorkerApiTests(unittest.TestCase):
    def test_import_without_production_environment_is_safe(self):
        """Importing the uvicorn target must not eagerly read missing secrets."""
        names = [name for name in os.environ if name.startswith("M3_")]
        old = {name: os.environ.pop(name) for name in names}
        try:
            module = importlib.reload(importlib.import_module("m3_worker.main"))
            self.assertEqual(module.app.title, "VIFA M3 Forecast Worker")
        finally:
            os.environ.update(old)

    def test_openapi_has_only_five_paths_and_manual_schema_forbids_properties(self):
        """An accidental inbound data/URL endpoint would violate the active-pull boundary."""
        app = create_app(settings(), FakeResources(), start_scheduler=False)
        schema = app.openapi()

        self.assertEqual(
            sorted(schema["paths"]),
            [
                "/health",
                "/v1/jobs/{job_id}",
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
            health = client.get("/health").json()
            self.assertEqual(health["status"], "ok")
            self.assertEqual(resources.events, ["recover", "start"])
        self.assertEqual(resources.events, ["recover", "start", "stop", "close"])

    def test_recovery_failure_cannot_report_ready(self):
        """A running thread cannot make failed dependency recovery healthy."""
        resources = FakeResources(recovery_ready=False)
        app = create_app(settings(), resources)
        with TestClient(app) as client:
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
            health = client.get("/health").json()
        self.assertEqual(health["status"], "initializing")
        self.assertEqual(health["scheduler"], "unhealthy")
        self.assertEqual(health["dependencies"], "ready")

    def test_live_healthy_but_stopping_scheduler_cannot_report_ready(self):
        """A stop-in-progress thread must not be advertised as a running scheduler."""
        resources = FakeResources()
        app = create_app(settings(), resources)
        with TestClient(app) as client:
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
        """A recovery exception before yield must not leak the pool or HTTP clients."""
        resources = FakeResources()

        def fail_recovery():
            resources.events.append("recover")
            raise RuntimeError("source-secret")

        resources.recover = fail_recovery
        app = create_app(settings(), resources)

        with self.assertRaises(RuntimeError):
            with TestClient(app):
                pass
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
            with TestClient(app):
                pass
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
            with TestClient(app):
                pass
        self.assertEqual(caught.exception.code, "scheduler_stop_timeout")
        self.assertEqual(resources.events, ["recover", "start", "stop"])


class ResourceTests(unittest.TestCase):
    def test_dynamic_readiness_rechecks_runtime_version_and_station_state(self):
        """A startup version string cannot keep health ready after runtime/state drift."""
        states = {"station-1": SimpleNamespace(state="ready")}
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=("station-1",)),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=SimpleNamespace(state=lambda station: states[station]),
            acceptance_service=SimpleNamespace(),
            scheduler=SimpleNamespace(running=True, healthy=True),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW,
            alert_sink=lambda *_args: None,
            statsforecast_version="2.1.1",
            recovery_ready=True,
        )
        with patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"):
            self.assertTrue(resources.current_ready())
        with patch("m3_worker.main.verify_statsforecast_runtime", return_value="9.9.9"):
            self.assertFalse(resources.current_ready())
        resources.statsforecast_version = "9.9.9"
        with patch("m3_worker.main.verify_statsforecast_runtime", return_value="9.9.9"):
            self.assertFalse(resources.current_ready())
        resources.statsforecast_version = "2.1.1"
        states["station-1"] = SimpleNamespace(state="initializing")
        with patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"):
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
            reconcile_writing_batches=lambda: calls.append(("reconcile",))
        )
        alerts = []
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=("station-1", "station-2")),
            source_http=SimpleNamespace(close=lambda: None),
            nocobase_http=SimpleNamespace(close=lambda: None),
            forecast_service=Forecast(),
            acceptance_service=acceptance,
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
                ("reconcile",),
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
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: None),
            clock=lambda: NOW.replace(minute=7, second=19),
            alert_sink=lambda *_items: None,
            statsforecast_version="2.1.1",
        )

        with patch(
            "m3_worker.main.verify_statsforecast_runtime",
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

    def test_resource_close_order_is_jobs_then_source_then_nocobase_and_idempotent(self):
        """Closing clients before jobs can break still-running outbound work."""
        events = []
        resources = WorkerResources(
            settings=SimpleNamespace(station_ids=()),
            source_http=SimpleNamespace(close=lambda: events.append("source")),
            nocobase_http=SimpleNamespace(close=lambda: events.append("nocobase")),
            forecast_service=SimpleNamespace(),
            acceptance_service=SimpleNamespace(),
            scheduler=SimpleNamespace(running=False),
            jobs=SimpleNamespace(close=lambda: events.append("jobs")),
            clock=lambda: NOW,
            alert_sink=lambda *_args: None,
            statsforecast_version="2.1.1",
        )
        resources.close()
        resources.close()
        self.assertEqual(events, ["jobs", "source", "nocobase"])

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
            patch("m3_worker.main.httpx.Client", Client),
            patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3_worker.main.ForecastService", return_value=SimpleNamespace()),
            patch("m3_worker.main.AcceptanceService", return_value=SimpleNamespace()),
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

    def test_build_resources_splits_raw_context_nocobase_and_alert_dependencies(self):
        """Model/actual reads, acceptance context, persistence, and alerts keep fixed credentials."""
        created = []

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created.append(self)

            def close(self):
                pass

        configured = settings()
        raw_source = object()
        context_source = object()
        nocobase_api = object()
        forecast_service = object()
        acceptance_service = object()
        alert_client = object()
        with (
            patch("m3_worker.main.httpx.Client", Client),
            patch(
                "m3_worker.main.RawEnergySourceClient",
                return_value=raw_source,
            ) as raw_constructor,
            patch(
                "m3_worker.main.SourceApiClient",
                return_value=context_source,
            ) as context_constructor,
            patch(
                "m3_worker.main.NocoBaseApiClient",
                return_value=nocobase_api,
            ) as nocobase_constructor,
            patch(
                "m3_worker.main.NodeRedAlertClient",
                return_value=alert_client,
            ) as alert_constructor,
            patch(
                "m3_worker.main.ForecastService",
                return_value=forecast_service,
            ) as forecast_constructor,
            patch(
                "m3_worker.main.AcceptanceService",
                return_value=acceptance_service,
            ) as acceptance_constructor,
            patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
        ):
            resources = build_resources(configured, clock=lambda: NOW)

        raw_constructor.assert_called_once_with(
            str(configured.raw_source_url),
            "raw-secret",
            created[0],
            allowed_station_ids=(ES01, ES02),
        )
        context_constructor.assert_called_once_with(
            str(configured.source_base_url),
            "source-secret",
            created[0],
            ANY,
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
        acceptance_kwargs = acceptance_constructor.call_args.kwargs
        self.assertIs(acceptance_kwargs["context_source"], context_source)
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
            patch("m3_worker.main.httpx.Client", Client),
            patch("m3_worker.main.NodeRedAlertClient", return_value=alert_client),
            patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3_worker.main.ForecastService", return_value=SimpleNamespace()),
            patch("m3_worker.main.AcceptanceService", return_value=SimpleNamespace()),
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
        with self.assertLogs("m3_worker", level="WARNING") as logged:
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
            patch("m3_worker.main.httpx.Client", Client),
            patch("m3_worker.main.verify_statsforecast_runtime", return_value="2.1.1"),
            patch("m3_worker.main.ForecastService", side_effect=construction),
        ):
            with self.assertRaises(ValueError) as caught:
                build_resources(settings(), clock=lambda: NOW)
        self.assertIs(caught.exception, construction)
        self.assertEqual(events, ["create-0", "create-1", "close-0", "close-1"])


if __name__ == "__main__":
    unittest.main()
