import copy
import unittest
from unittest.mock import patch

from m1 import dashboard_energy_api as api


STAMP = "2026-10-22T10:45:01+08:00"


def source_rows():
    return [
        {"f_es_sn": station["code"], "emu_sn": sn, "latest_power": 5,
         "last_time_iso": STAMP}
        for station in api.TARGET_STATIONS for sn in station["cabinet_sns"]
    ] + [
        {"f_es_sn": "ES01", "emu_sn": "emu13", "latest_power": 999,
         "input_ac_solar_power": 400, "latest_solar_power": 380, "last_time_iso": STAMP},
        {"f_es_sn": "ES02", "emu_sn": "emu27", "input_ac_solar_power": 200,
         "latest_solar_power": 190, "last_time_iso": "2026-10-22T10:40:02+08:00"},
    ]


def raw_data():
    return {"status": "ok", "data": {
        "sites": [{"id": api.TARGET_SITE_ID}],
        "es_list": [{"id": "s1", "station_code": "ES01", "fk_site": api.TARGET_SITE_ID},
                    {"id": "s2", "station_code": "ES02", "fk_site": api.TARGET_SITE_ID}],
        "nodes": [
            {"id": "ess", "node_type": "ess", "is_aggregate": True,
             "fk_es": "s1", "fk_site_id": api.TARGET_SITE_ID},
            {"id": "real-pv-id", "sn": "emu13", "name": "1号电站-光伏工控机",
             "node_type": "ess", "parentId": "ess", "fk_es": "s1",
             "fk_site_id": api.TARGET_SITE_ID},
        ],
        "realtime": [{"fk_en": "real-pv-id", "power": 999, "soc": 80,
                      "timestamp": "old", "cabinet_timestamp": "old"}],
        "stats": {"total_nodes": 2},
    }}


class Station1PvTests(unittest.TestCase):
    def collect(self, rows=None):
        with patch.object(api, "fetch_external_rows", return_value=(source_rows() if rows is None else rows, None)):
            sources, errors = api.fetch_station_storage_sources("fixture")
        result = api.build_result(raw_data(), storage_sources=sources, source_errors=errors)["data"]
        meter = next(row for row in result["realtime"] if row["fk_en"] == "real-pv-id")
        return result, meter, sources, errors

    def test_controller_is_pv_with_real_identity_and_own_power_time(self):
        raw = raw_data()
        original = copy.deepcopy(raw)
        result, meter, sources, errors = self.collect()
        self.assertEqual(errors, {})
        self.assertEqual((meter["pv_input_power_kw"], meter["power"], meter["conversion_efficiency"]),
                         (400, 380, 95))
        self.assertEqual(meter["timestamp"], STAMP)
        self.assertIsNone(meter["soc"])
        self.assertNotIn("cabinet_timestamp", meter)
        node = next(node for node in result["nodes"] if node["id"] == "real-pv-id")
        self.assertEqual(node["node_type"], "meter")
        self.assertNotEqual(node["parentId"], "ess")
        self.assertEqual(sources["ES01"]["power"], 10)
        self.assertEqual(sources["ES02"]["power"], 30)
        api.build_result(raw, storage_sources=sources)
        self.assertEqual(raw, original)

    def test_invalid_or_missing_pv_never_uses_old_storage_power(self):
        for field in ("input_ac_solar_power", "latest_solar_power"):
            for value in (None, -1, "NaN", "Infinity", "broken"):
                with self.subTest(field=field, value=value):
                    rows = source_rows()
                    rows[-2][field] = value
                    _, meter, sources, errors = self.collect(rows)
                    self.assertIsNone(meter["power"])
                    self.assertEqual(meter["data_quality"], "missing")
                    self.assertEqual(sources["ES01"]["power"], 10)
                    self.assertIn("pv_meter:ES01", errors)
        _, meter, _, errors = self.collect([r for r in source_rows() if r["emu_sn"] != "emu13"])
        self.assertIsNone(meter["power"])
        self.assertIn("pv_meter:ES01", errors)

    def test_invalid_time_never_falls_back_to_storage_time(self):
        rows = source_rows()
        rows[-2].update(last_time_iso="invalid", timestamp=STAMP)
        _, meter, _, errors = self.collect(rows)
        self.assertEqual(meter["timestamp"], "")
        self.assertIsNone(meter["power"])
        self.assertIn("pv_meter:ES01", errors)

    def test_zero_is_standby_with_no_division_or_efficiency_alarm(self):
        rows = source_rows()
        rows[-2].update(input_ac_solar_power=0, latest_solar_power=0)
        result, meter, _, _ = self.collect(rows)
        self.assertEqual(meter["power"], 0)
        self.assertEqual(meter["run_status"], "standby")
        self.assertIsNone(meter["conversion_efficiency"])
        self.assertFalse(any(a["device_sn"] == "emu13" for a in result["alerts"]))

    def test_low_efficiency_alarm_is_owned_by_station1_real_node(self):
        rows = source_rows()
        rows[-2]["latest_solar_power"] = 300
        result, _, _, _ = self.collect(rows)
        alert = next(a for a in result["alerts"] if a["device_sn"] == "emu13")
        self.assertEqual(alert["fk_en_id"], "real-pv-id")
        self.assertEqual(alert["fk_site_id"], api.TARGET_SITE_ID)
        self.assertEqual(alert["value"], 75)

    def test_other_station_cannot_supply_station1_meter(self):
        rows = source_rows()
        rows[-2]["f_es_sn"] = "ES02"
        _, meter, _, errors = self.collect(rows)
        self.assertIsNone(meter["power"])
        self.assertIn("storage_cabinets", errors)
        data = raw_data()["data"]
        data["nodes"][1]["fk_es"] = "s2"
        before = copy.deepcopy(data)
        self.assertEqual(api.apply_station_pv_meter(data, {}), [])
        self.assertEqual(data, before)

    def test_missing_cabinet_does_not_erase_independent_pv(self):
        result, meter, sources, errors = self.collect([r for r in source_rows() if r["emu_sn"] != "emu11"])
        self.assertEqual(meter["power"], 380)
        self.assertNotIn("power", sources["ES01"])
        self.assertIn("storage_cabinets:ES01", errors)
        self.assertEqual(result["stats"]["total_nodes"], 3)

    def test_existing_pv_group_is_reused(self):
        data = raw_data()["data"]
        data["nodes"].append({"id": "existing-pv", "fk_es": "s1", "node_type": "pv_grid_ac", "is_aggregate": True})
        api.apply_station_pv_meter(data, {})
        self.assertEqual(len(data["nodes"]), 3)
        self.assertEqual(data["nodes"][1]["parentId"], "existing-pv")

    def test_station2_reuses_meter_with_own_controller_values_and_alert(self):
        raw = raw_data()
        raw["data"]["nodes"].extend([
            {"id": "s2-pv", "fk_es": "s2", "node_type": "pv_grid_ac", "is_aggregate": True},
            {"id": "s2-meter", "fk_es": "s2", "node_type": "meter", "parentId": "s2-pv"},
        ])
        rows = source_rows()
        rows[-1]["latest_solar_power"] = 150
        with patch.object(api, "fetch_external_rows", return_value=(rows, None)):
            sources, _ = api.fetch_station_storage_sources("fixture")
        data = api.build_result(raw, growatt_rows=[{"sn": "emu1", "a0": 1, "a1": 100, "a35": 10}],
                                storage_sources=sources)["data"]
        meter = next(row for row in data["realtime"] if row["fk_en"] == "s2-meter")
        self.assertEqual((meter["pv_input_power_kw"], meter["power"], meter["conversion_efficiency"]),
                         (200, 150, 75))
        self.assertEqual(meter["timestamp"], "2026-10-22T10:40:02+08:00")
        self.assertEqual(meter["pv_device_sn"], "emu27")
        self.assertEqual(data["pv_inverters"], [])
        alerts = [alert for alert in data["alerts"] if alert["category"] == "pv_efficiency"]
        self.assertEqual([(alert["device_sn"], alert["fk_en_id"], alert["start_time"]) for alert in alerts],
                         [("emu27", "s2-meter", meter["timestamp"])])


if __name__ == "__main__":
    unittest.main()
