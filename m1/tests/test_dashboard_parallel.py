import threading
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from m1 import dashboard_energy_api as api


class ParallelDashboardTests(unittest.TestCase):
    def test_independent_sources_start_together_after_authenticated_topology(self):
        started = set()
        lock = threading.Lock()
        all_started = threading.Event()
        release = threading.Event()
        raw = {"data": {"es_list": []}}
        result = {"status": "ok", "data": {}}
        outcome = []

        def source(name, value):
            def read(*args):
                with lock:
                    started.add(name)
                    if len(started) == 3:
                        all_started.set()
                if not release.wait(3):
                    raise AssertionError("source release timed out")
                return value
            return read

        def run():
            outcome.append(api.collect_dashboard(
                "user-fixture", "energy-fixture", growatt_token="growatt-fixture",
                history_db="history-fixture",
            ))

        with patch.object(api, "fetch_raw_data", return_value=raw) as topology, \
             patch.object(api, "fetch_growatt_rows", side_effect=source("growatt", ([], None))), \
             patch.object(api, "fetch_station_load_sources", side_effect=source("load", ({}, {}))), \
             patch.object(api, "fetch_station_storage_sources", side_effect=source("storage", ({}, {}))), \
             patch.object(api, "build_result", return_value=result), \
             patch.object(api, "attach_alert_history") as history:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(all_started.wait(2), "independent sources must overlap, not run serially")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [result])
            topology.assert_called_once_with("user-fixture")
            history.assert_called_once_with(result, "history-fixture")

    def test_source_errors_and_authoritative_values_are_preserved(self):
        raw = {"data": {"es_list": []}}
        result = {"status": "ok", "data": {}}
        with patch.object(api, "fetch_raw_data", return_value=raw), \
             patch.object(api, "fetch_growatt_rows", return_value=([{"sn": "fixture"}], "growatt error")), \
             patch.object(api, "fetch_station_load_sources", return_value=({"ES01": {"power": 20}}, {"station_load:ES02": "load error"})), \
             patch.object(api, "fetch_station_storage_sources", return_value=({"ES01": {"power": 10}}, {"storage_cabinets:ES02": "storage error"})), \
             patch.object(api, "build_result", return_value=result) as build, \
             patch.object(api, "attach_alert_history"):
            self.assertIs(api.collect_dashboard("user-fixture", "energy-fixture"), result)
            self.assertEqual(build.call_args.kwargs["source_errors"], {
                "growatt": "growatt error", "station_load:ES02": "load error",
                "storage_cabinets:ES02": "storage error",
            })
            self.assertEqual(build.call_args.kwargs["load_sources"]["ES01"]["power"], 20)

    def test_failed_topology_does_not_read_privileged_energy_or_write_history(self):
        with patch.object(api, "fetch_raw_data", side_effect=RuntimeError("denied")), \
             patch.object(api, "fetch_growatt_rows") as growatt, \
             patch.object(api, "fetch_station_load_sources") as load, \
             patch.object(api, "fetch_station_storage_sources") as storage, \
             patch.object(api, "attach_alert_history") as history:
            with self.assertRaises(RuntimeError):
                api.collect_dashboard("invalid-user-fixture", "energy-fixture")
            for operation in (growatt, load, storage, history):
                operation.assert_not_called()

    def test_real_topology_http_auth_rejections_block_energy_and_history(self):
        for status in (401, 403):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://ems.example.invalid/api", status, "denied", Message(), None)
                with patch.object(api.urllib.request, "urlopen", side_effect=error), \
                     patch.object(api, "fetch_growatt_rows") as growatt, \
                     patch.object(api, "fetch_station_load_sources") as load, \
                     patch.object(api, "fetch_station_storage_sources") as storage, \
                     patch.object(api, "attach_alert_history") as history:
                    with self.assertRaises(api.SourceAuthorizationError):
                        api.collect_dashboard("user-fixture", "energy-fixture")
                    for operation in (growatt, load, storage, history):
                        operation.assert_not_called()
