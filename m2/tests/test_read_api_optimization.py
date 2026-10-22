"""Regression cases for split reads and read-only routing (not run in this edit)."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from m2.tests.test_energy_efficiency_api import energy_api, ENVIRONMENT
from m2.tests.test_efficiency_history_query import point, NOW

with patch.dict("sys.modules", {"energy_efficiency_local_config": None}):
    from m2 import resident_api


class SplitReadTests(unittest.TestCase):
    def test_curve_reads_no_realtime_events_or_writes(self):
        forbidden = Mock(side_effect=AssertionError("unrelated read or write"))
        result, code = energy_api.execute(
            ["curve", "ES02", "2026-09-06"], ENVIRONMENT, now=NOW,
            fetch_station=forbidden, calculate=forbidden, fetch_events=forbidden,
            process_minute=forbidden, cleanup_history=forbidden,
            fetch_points=lambda *args: [point("2026-09-06T10:00:00+08:00", input_kw=200, output_kw=180)],
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["operation"], "curve")
        self.assertAlmostEqual(result["data"]["summary"]["pv_storage_efficiency"], 90)
        self.assertNotIn("events", result["data"])
        self.assertNotIn("realtime", result["data"])
        forbidden.assert_not_called()

    def test_history_reads_start_together_and_preserve_result(self):
        barrier = threading.Barrier(2, timeout=2)

        def points(*args):
            barrier.wait()
            return [point("2026-09-06T10:00:00+08:00")]

        def events(*args):
            barrier.wait()
            return []

        result, code = energy_api.execute(
            ["history", "ES02", "2026-09-06"], ENVIRONMENT,
            now=NOW, fetch_points=points, fetch_events=events,
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["operation"], "history")
        self.assertEqual(len(result["data"]["trend"]), 1)
        self.assertEqual(result["data"]["events"], [])

    def test_realtime_skips_history_events_and_writes(self):
        forbidden = Mock(side_effect=AssertionError("unrelated read or write"))
        source = {"bus_id": "ES02", "data_time": "2026-09-07T10:30:00+08:00"}
        result, code = energy_api.execute(
            ["realtime", "ES02"], ENVIRONMENT,
            fetch_station=lambda *args: source,
            calculate=lambda record: {"data_time": record["data_time"]},
            fetch_points=forbidden, fetch_events=forbidden,
            process_minute=forbidden, cleanup_history=forbidden,
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["operation"], "realtime")
        self.assertEqual(result["data"]["station_id"], "ES02")
        self.assertEqual(result["data"]["range"]["start_time"], "2026-09-07T00:00:00+08:00")
        self.assertNotIn("trend", result["data"])
        forbidden.assert_not_called()

    def test_read_router_rejects_writes_duplicates_and_extra_parameters(self):
        for query in (
            "operation=minute&station_id=ES02", "operation=cleanup",
            "station_id=ES02&station_id=ES01", "operation=curve&station_id=ES02",
            "operation=events&station_id=ES02&start_date=2026-09-06",
            "operation=realtime&station_id=ES02&date=2026-09-06",
            "operation=realtime&station_id=ES03",
        ):
            with self.subTest(query=query), self.assertRaises((ValueError, resident_api.api.EntrypointError)):
                resident_api.query_arguments(query)

    def test_read_router_preserves_fixed_query_arguments(self):
        self.assertEqual(resident_api.query_arguments("station_id=ES02"), ["dashboard", "ES02"])
        self.assertEqual(
            resident_api.query_arguments("operation=curve&station_id=ES02&date=2026-09-06"),
            ["curve", "ES02", "2026-09-06"],
        )

    def test_pooled_read_errors_keep_source_and_store_error_codes(self):
        for args, expected in ((["realtime", "ES02"], "source_error"), (["curve", "ES02", "2026-09-06"], "store_error")):
            with self.subTest(args=args):
                server = SimpleNamespace(
                    environ=ENVIRONMENT, executor=resident_api.api.execute,
                    transport=SimpleNamespace(get_json=Mock(side_effect=TimeoutError("private transport detail"))),
                )
                payload, code = resident_api.ResidentServer.read(server, args)
                self.assertNotEqual(code, 0)
                self.assertEqual(payload["error"]["code"], expected)
                self.assertNotIn("private transport detail", str(payload))
