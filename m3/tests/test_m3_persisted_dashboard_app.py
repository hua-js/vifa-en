"""HTTP contract for the read-only persisted M3 Dashboard container."""

import anyio
from fastapi.testclient import TestClient
import unittest

from m3.worker.config import StationBinding
from m3.worker.errors import M3Error
from m3.worker.persisted_dashboard_app import create_persisted_dashboard_app
from m3.worker.services.live_dashboard_service import DashboardCache


NOW = __import__("datetime").datetime.fromisoformat("2026-08-27T10:05:00+08:00")
STATIONS = (
    StationBinding("plant-alpha-ES01", "station_1", "1# 电站"),
    StationBinding("plant-beta-ES02", "station_2", "2# 电站"),
)


def envelope():
    return DashboardCache(STATIONS).snapshot(NOW)


class PersistedDashboardAppTests(unittest.TestCase):
    def test_exposes_only_fixed_health_and_dashboard_routes(self):
        settings = object()
        app = create_persisted_dashboard_app(
            lambda: settings,
            lambda received: envelope() if received is settings else None,
        )

        with TestClient(app) as client:
            health = client.get("/health")
            dashboard = client.get("/dashboard")
            missing = [client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["data"]["operation"], "forecast_dashboard")
        self.assertTrue(all(response.status_code == 404 for response in missing))

    def test_settings_are_parsed_once_per_process_lifespan(self):
        calls = []
        settings = object()

        def settings_factory():
            calls.append("settings")
            return settings

        app = create_persisted_dashboard_app(settings_factory, lambda _: envelope())
        with TestClient(app) as client:
            self.assertEqual(client.get("/dashboard").status_code, 200)
            self.assertEqual(client.get("/dashboard").status_code, 200)

        self.assertEqual(calls, ["settings"])

    def test_dashboard_rejects_query_and_body_before_reading_external_data(self):
        calls = []

        def builder(_settings):
            calls.append("builder")
            return envelope()

        app = create_persisted_dashboard_app(lambda: object(), builder)
        with TestClient(app) as client:
            query = client.get("/dashboard?station_id=plant-alpha-ES01")
            body = client.request("GET", "/dashboard", content=b"{}")

        self.assertEqual(query.status_code, 400)
        self.assertEqual(body.status_code, 400)
        self.assertEqual(query.json()["error"]["code"], "invalid_request")
        self.assertEqual(body.json()["error"]["code"], "invalid_request")
        self.assertEqual(calls, [])

    def test_blocking_dashboard_builder_runs_in_an_anyio_worker_thread(self):
        def builder(_settings):
            anyio.from_thread.check_cancelled()
            return envelope()

        app = create_persisted_dashboard_app(lambda: object(), builder)
        with TestClient(app) as client:
            response = client.get("/dashboard")

        self.assertEqual(response.status_code, 200)

    def test_error_taxonomy_is_bounded_and_never_leaks_exception_details(self):
        cases = (
            (M3Error("source_http_failed", "Bearer raw-secret"), 502, "source_error"),
            (M3Error("sink_http_failed", "Bearer dashboard-secret"), 502, "nocobase_error"),
            (M3Error("dashboard_contract_invalid", "plant-alpha-ES01"), 502, "contract_error"),
            (M3Error("unclassified", "unsafe-internal"), 500, "internal_error"),
            (RuntimeError("Bearer unexpected-secret"), 500, "internal_error"),
        )
        for error, expected_status, expected_code in cases:
            def builder(_settings, error=error):
                raise error

            app = create_persisted_dashboard_app(lambda: object(), builder)
            with self.subTest(error=error), TestClient(app) as client:
                response = client.get("/dashboard")

            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.json()["error"]["code"], expected_code)
            self.assertEqual(set(response.json()), {"status", "error"})
            self.assertEqual(set(response.json()["error"]), {"code", "message"})
            for unsafe in ("secret", "plant-alpha-ES01", "unsafe-internal"):
                self.assertNotIn(unsafe, response.text)

    def test_malformed_builder_output_is_a_safe_contract_error(self):
        app = create_persisted_dashboard_app(
            lambda: object(),
            lambda _settings: {"status": "ok", "data": {"token": "secret"}},
        )

        with TestClient(app) as client:
            response = client.get("/dashboard")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "contract_error")
        self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
