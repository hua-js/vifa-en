import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from m1 import dashboard_energy_api as api


def alert(timestamp="2026-09-07T08:00:00+08:00", **extra):
    return {
        "id": "derived:load_spike:load1", "category": "load_spike",
        "fk_site_id": api.TARGET_SITE_ID, "fk_en_id": "load1",
        "start_time": timestamp, "value": 1200, "threshold": 1000,
        "device_sn": "ES01", "txt": "负载功率突增", **extra,
    }


class AlertHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "history.sqlite3"

    def test_duplicate_samples_and_empty_current_survive_reopening(self):
        api.record_alert_history({"alerts": [alert()]}, self.path)
        api.record_alert_history({"alerts": [alert("2026-09-07T00:00:00Z", value=1400)]}, self.path)
        history = api.record_alert_history({"alerts": []}, self.path)
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["records"][0]["value"], 1200)
        self.assertIn("recorded_at", history["records"][0])

    def test_new_samples_sorted_and_limited_without_deleting_archive(self):
        rows = [alert(f"2026-09-0{day}T08:00:00+08:00") for day in (7, 5, 6)]
        with patch.object(api, "ALERT_HISTORY_LIMIT", 2):
            history = api.record_alert_history({"alerts": rows}, self.path)
        self.assertEqual(history["total"], 3)
        self.assertEqual([r["start_time"][8:10] for r in history["records"]], ["07", "06"])
        self.assertEqual(api.record_alert_history({}, self.path)["total"], 3)

    def test_invalid_samples_and_other_sites_are_excluded(self):
        history = api.record_alert_history({"alerts": [
            alert("invalid"), alert(fk_site_id="other"), alert(category="unknown"),
            alert(id=""), alert(),
        ]}, self.path)
        self.assertEqual(history["total"], 1)
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO alert_samples VALUES (?, ?, ?, ?, ?)",
                               ("other", "x", "2099", "2099", json.dumps(alert(fk_site_id="other"))))
        self.assertEqual(api.record_alert_history({}, self.path)["total"], 1)

    def test_storage_failure_does_not_break_realtime(self):
        result = {"status": "ok", "data": {"alerts": [alert()]}}
        api.attach_alert_history(result, self.temp.name)  # Directory is not a database file.
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["data"]["alerts"]), 1)
        self.assertEqual(result["data"]["alert_history"]["status"], "error")

    def test_cli_wires_persistence_without_network(self):
        result = {"status": "ok", "data": {"alerts": [alert()]}}
        output = io.StringIO()
        with patch.object(api, "fetch_raw_data", return_value={}), \
             patch.object(api, "fetch_growatt_rows", return_value=([], None)), \
             patch.object(api, "fetch_station_load_sources", return_value=({}, {})), \
             patch.object(api, "fetch_station_storage_sources", return_value=({}, {})), \
             patch.object(api, "build_result", return_value=result), contextlib.redirect_stdout(output):
            self.assertEqual(api.main(["--history-db", str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["data"]["alert_history"]["total"], 1)


class CabinetTimestampTests(unittest.TestCase):
    def test_each_cabinet_uses_its_own_source_time(self):
        rows = [
            {"f_es_sn": station["code"], "emu_sn": sn, "latest_power": 5,
             "max_temp": 30, "last_time_iso": f"2026-09-07T08:00:{index:02d}+08:00"}
            for station in api.TARGET_STATIONS
            for index, sn in enumerate(station["cabinet_sns"])
        ]
        with patch.object(api, "fetch_external_rows", return_value=(rows, None)):
            sources, errors = api.fetch_station_storage_sources("test-token")
        self.assertEqual(errors, {"pv_meter:ES02": "光伏计量表 emu27 时间缺失或无效"})
        data = {
            "es_list": [{"id": "s1", "station_code": "ES01"}],
            "nodes": [{"id": sn, "sn": sn, "node_type": "ess", "fk_es": "s1"} for sn in ("emu11", "emu12")],
            "realtime": [{"fk_en": "emu11", "timestamp": "original", "power": 9}],
        }
        api.apply_authoritative_power_sources(data, {}, sources)
        realtime = {r["fk_en"]: r for r in data["realtime"]}
        self.assertTrue(realtime["emu11"]["cabinet_timestamp"].endswith("00+08:00"))
        self.assertTrue(realtime["emu12"]["cabinet_timestamp"].endswith("01+08:00"))
        self.assertEqual(realtime["emu11"]["timestamp"], "original")
        self.assertEqual(realtime["emu11"]["power"], 9)

    def test_missing_sibling_preserves_available_time_but_not_aggregate_power(self):
        rows = [{"f_es_sn": "ES01", "emu_sn": "emu11", "last_time_iso": "2026-09-07 08:00:01", "latest_power": 5}]
        with patch.object(api, "fetch_external_rows", return_value=(rows, None)):
            sources, errors = api.fetch_station_storage_sources("test-token")
        self.assertIn("storage_cabinets:ES01", errors)
        self.assertEqual(sources["ES01"]["cabinet_timestamps"]["emu11"], "2026-09-07T08:00:01+08:00")
        data = {"es_list": [{"id": "s1", "station_code": "ES01"}],
                "nodes": [{"id": "agg", "node_type": "ess", "is_aggregate": True, "fk_es": "s1"}],
                "realtime": [{"fk_en": "agg", "power": 123}]}
        api.apply_authoritative_power_sources(data, {}, sources)
        self.assertIsNone(data["realtime"][0]["power"])



class PvMeterTimestampTests(unittest.TestCase):
    def test_meter_timestamp_uses_emu27_and_is_excluded_from_storage_sum(self):
        rows = [
            {"f_es_sn": station["code"], "emu_sn": sn, "latest_power": 5,
             "last_time_iso": "2026-09-07T08:00:00+08:00"}
            for station in api.TARGET_STATIONS for sn in station["cabinet_sns"]
        ]
        rows.append({"f_es_sn": "ES02", "emu_sn": "emu27", "latest_power": 999,
                     "last_time_iso": "2026-09-07T00:15:03Z"})
        with patch.object(api, "fetch_external_rows", return_value=(rows, None)) as fetch:
            sources, errors = api.fetch_station_storage_sources("test-token")
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertIn("emu27", query["filter"][0])
        self.assertEqual(query["pageSize"], ["9"])
        self.assertEqual(errors, {})
        self.assertEqual(sources["ES02"]["power"], 30)
        self.assertNotIn("emu27", sources["ES02"]["cabinet_timestamps"])
        self.assertEqual(sources["ES02"]["pv_meter_timestamp"], "2026-09-07T08:15:03+08:00")
        data = {"es_list": [{"id": "s2", "station_code": "ES02"}],
                "nodes": [{"id": "pv", "node_type": "pv_grid_ac", "is_aggregate": True, "fk_es": "s2"},
                          {"id": "meter", "node_type": "meter", "parentId": "pv", "fk_es": "s2"}],
                "realtime": [{"fk_en": "meter", "timestamp": "older", "power": 42}]}
        result = api.build_result({"data": data}, storage_sources=sources)["data"]
        meter = next(r for r in result["realtime"] if r["fk_en"] == "meter")
        self.assertEqual(meter["pv_meter_timestamp"], "2026-09-07T08:15:03+08:00")
        self.assertEqual(meter["timestamp"], "older")
        self.assertEqual(meter["power"], 42)

    def test_missing_invalid_or_unavailable_meter_has_no_fallback(self):
        for rows, error in [([], None),
                            ([{"f_es_sn": "ES02", "emu_sn": "emu27", "last_time_iso": "invalid", "timestamp": "2026-09-07T08:00:00+08:00"}], None),
                            ([], "unavailable")]:
            with self.subTest(rows=rows, error=error):
                with patch.object(api, "fetch_external_rows", return_value=(rows, error)):
                    sources, errors = api.fetch_station_storage_sources("test-token")
                self.assertTrue(errors)
                self.assertFalse(sources.get("ES02", {}).get("pv_meter_timestamp"))
                data = {"es_list": [{"id": "s2", "station_code": "ES02"}],
                        "nodes": [{"id": "pv", "node_type": "pv_grid_ac", "is_aggregate": True, "fk_es": "s2"},
                                  {"id": "meter", "node_type": "meter", "parentId": "pv", "fk_es": "s2"}]}
                result = api.build_result({"data": data}, storage_sources=sources)["data"]
                meter = next(r for r in result["realtime"] if r["fk_en"] == "meter")
                self.assertEqual(meter["pv_meter_timestamp"], "")

if __name__ == "__main__":
    unittest.main()
