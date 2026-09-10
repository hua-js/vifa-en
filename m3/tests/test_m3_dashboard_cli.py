"""One-shot dashboard CLI contract used by the fixed Node-RED Exec node."""

from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from m3.worker.config import StationBinding
from m3.worker.dashboard_cli import build_dashboard, execute, main
from m3.worker.errors import M3Error
from m3.worker.services.live_dashboard_service import DashboardCache


ROOT = Path(__file__).resolve().parents[2]
STATIONS = (
    StationBinding("plant-alpha-ES01", "station_1", "1# 电站"),
    StationBinding("plant-beta-ES02", "station_2", "2# 电站"),
)
NOW = __import__("datetime").datetime.fromisoformat("2026-08-26T10:05:00+08:00")


def environment() -> dict[str, str]:
    return {
        "M3_STATIONS_JSON": json.dumps([
            {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
            {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
        ]),
        "M3_RAW_SOURCE_URL": "https://source.example.test/api/t_es_data:list",
        "M3_RAW_SOURCE_API_TOKEN": "raw-source-secret",
        "M3_NOCOBASE_BASE_URL": "https://nocobase.example.test",
        "M3_DASHBOARD_NOCOBASE_API_KEY": "dashboard-read-secret",
    }


def envelope():
    return DashboardCache(STATIONS).snapshot(NOW)


class DashboardCliTests(unittest.TestCase):
    def test_accepts_only_the_single_fixed_dashboard_argument(self):
        called = False

        def builder(_settings):
            nonlocal called
            called = True
            return envelope()

        for argv in ([], ["dashboard", "ES01"], ["forecast"], ["dashboard", "--x"]):
            with self.subTest(argv=argv):
                payload, exit_code = execute(argv, environment(), builder=builder)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertFalse(called)

    def test_success_returns_the_strict_dashboard_envelope(self):
        seen = []

        def builder(settings):
            seen.append(settings)
            return envelope()

        payload, exit_code = execute(["dashboard"], environment(), builder=builder)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["operation"], "forecast_dashboard")
        self.assertEqual(len(seen), 1)
        self.assertFalse(hasattr(seen[0], "admin_api_token"))

    def test_configuration_errors_use_exit_three_without_secret_details(self):
        payload, exit_code = execute(["dashboard"], {}, builder=lambda _: envelope())

        self.assertEqual(exit_code, 3)
        self.assertEqual(payload, {
            "status": "error",
            "error": {"code": "missing_config", "message": "服务器缺少运行配置"},
        })

    def test_error_taxonomy_is_stable_and_public_messages_are_safe(self):
        cases = (
            (M3Error("source_http_failed", "Bearer raw-secret"), 4, "source_error"),
            (M3Error("sink_http_failed", "Bearer dashboard-secret"), 5, "nocobase_error"),
            (M3Error("dashboard_contract_invalid", "plant-alpha-ES01"), 6, "contract_error"),
            (M3Error("unclassified", "unsafe"), 1, "internal_error"),
            (RuntimeError("Bearer unexpected-secret"), 1, "internal_error"),
        )
        for error, expected_exit, expected_code in cases:
            def builder(_settings, error=error):
                raise error

            with self.subTest(error=error):
                payload, exit_code = execute(
                    ["dashboard"], environment(), builder=builder
                )
                rendered = json.dumps(payload, ensure_ascii=False)
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload["error"]["code"], expected_code)
                self.assertNotIn("secret", rendered)
                self.assertNotIn("plant-alpha-ES01", rendered)
                self.assertEqual(set(payload), {"status", "error"})
                self.assertEqual(set(payload["error"]), {"code", "message"})

    def test_main_writes_exactly_one_compact_json_line(self):
        output = StringIO()

        exit_code = main(
            argv=["dashboard"],
            environ=environment(),
            stdout=output,
            builder=lambda _: envelope(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertTrue(output.getvalue().endswith("\n"))
        self.assertNotIn(": ", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")

    def test_runtime_closes_both_http_clients_even_when_service_fails(self):
        clients = []

        class FakeHttp:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        def client_factory(**_kwargs):
            client = FakeHttp()
            clients.append(client)
            return client

        class FailingService:
            def build(self):
                raise M3Error("sink_http_failed", "unavailable")

        def service_factory(**_kwargs):
            return FailingService()

        settings = SimpleNamespace(
            raw_source_url="https://source.example.test/api/t_es_data:list",
            raw_source_api_token=SimpleNamespace(get_secret_value=lambda: "raw"),
            station_ids=("plant-alpha-ES01", "plant-beta-ES02"),
            nocobase_base_url="https://nocobase.example.test",
            nocobase_api_key=SimpleNamespace(get_secret_value=lambda: "read"),
            stations=STATIONS,
        )

        with self.assertRaises(M3Error):
            build_dashboard(
                settings,
                client_factory=client_factory,
                service_factory=service_factory,
                clock=lambda: NOW,
            )

        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.closed for client in clients))

    def test_root_script_is_a_thin_fixed_entrypoint(self):
        content = (ROOT / "m3/forecast_api.py").read_text(encoding="utf-8")
        self.assertIn("from m3.worker.dashboard_cli import main", content)
        self.assertNotIn("M3_RAW_SOURCE_API_TOKEN", content)
        self.assertNotIn("httpx", content)


if __name__ == "__main__":
    unittest.main()
