import unittest

from m2.station_efficiency_job import process_station_minute


TIMESTAMP = "2026-08-26T01:30:27Z"
ES02_SNS = ("emu21", "emu22", "emu23", "emu24", "emu25", "emu26")


def make_balanced_es02_rows():
    cabinet_power = (-20, -20, -20, -20, -10, -10)
    battery_power = (-18000, -18000, -18000, -18000, -9000, -9000)
    pcs2_power = (-8, -8, -8, -8, -8, -7)
    rows = []
    for index, emu_sn in enumerate(ES02_SNS):
        is_master = emu_sn == "emu26"
        rows.append({
            "f_es_sn": "ES02",
            "emu_sn": emu_sn,
            "last_time_iso": TIMESTAMP,
            "latest_power": cabinet_power[index],
            "battery_power": battery_power[index],
            "pcs1_power": -8,
            "pcs2_power": pcs2_power[index],
            "load_power": None,
            "latest_grid_power": -10000 if is_master else 0,
            "input_ac_solar_power": None,
            "latest_solar_power": None,
            "acpv_rated_power": None,
        })
    rows.append({
        "f_es_sn": "ES02",
        "emu_sn": "emu27",
        "last_time_iso": TIMESTAMP,
        "latest_power": None,
        "battery_power": None,
        "pcs1_power": None,
        "pcs2_power": None,
        "load_power": None,
        "latest_grid_power": None,
        "input_ac_solar_power": 200,
        "latest_solar_power": 190,
        "acpv_rated_power": 1000,
    })
    return rows


CONFIG = {
    "emu_url": "https://station.example/api/t_emu:list?filter=%7B%7D",
    "emu_token": "source-secret",
    "nocobase_base_url": "https://vifa.hlszh.com",
    "nocobase_token": "store-secret",
    "timeout_seconds": 7,
    "timezone": "Asia/Shanghai",
}


class StationEfficiencyJobTests(unittest.TestCase):
    def test_processes_one_station_minute_and_saves_one_point(self):
        source_calls = []
        store_calls = []

        def source_request(url, token, timeout):
            source_calls.append((url, token, timeout))
            return {"data": make_balanced_es02_rows()}

        def store_request(url, token, timeout, body):
            store_calls.append((url, token, timeout, body))
            return {"data": {"id": 31, **body}}

        output = process_station_minute(
            "ES02",
            CONFIG,
            source_request_json=source_request,
            store_request_json=store_request,
        )

        self.assertEqual(len(source_calls), 1)
        self.assertEqual(len(store_calls), 1)
        self.assertEqual(
            store_calls[0][0],
            "https://vifa.hlszh.com/api/t_efficiency_points:updateOrCreate"
            "?filterKeys%5B%5D=station_id&filterKeys%5B%5D=data_time",
        )
        self.assertEqual(store_calls[0][3]["station_id"], "ES02")

        point = output["minute_point"]
        self.assertEqual(store_calls[0][3], point)
        self.assertEqual(point["station_id"], "ES02")
        self.assertEqual(point["data_time"], "2026-08-26T09:30:00+08:00")
        self.assertEqual(point["formula_version"], "energy-chain-v1")
        self.assertAlmostEqual(point["pv_storage_input_kw"], 200 * 100 / 190)
        self.assertEqual(point["pv_storage_output_kw"], 90.0)
        self.assertAlmostEqual(point["pv_storage_efficiency"], 85.5)
        self.assertIsNone(point["storage_load_efficiency"])
        self.assertIsNone(point["storage_load_input_kw"])
        self.assertIsNone(point["storage_load_output_kw"])
        self.assertEqual(point["pv_load_input_kw"], 80.0)
        self.assertEqual(point["pv_load_output_kw"], 80.0)
        self.assertEqual(point["pv_load_efficiency"], 100.0)
        self.assertEqual(output["saved_record"]["id"], 31)
        self.assertNotIn("source-secret", repr(output))
        self.assertNotIn("store-secret", repr(output))

    def test_invalid_source_data_is_not_saved(self):
        rows = make_balanced_es02_rows()
        rows[0]["pcs1_power"] = None
        store_call_count = 0

        def source_request(url, token, timeout):
            return {"data": rows}

        def store_request(url, token, timeout, body):
            nonlocal store_call_count
            store_call_count += 1
            return {"data": {"id": 1}}

        with self.assertRaisesRegex(ValueError, "pcs1_power"):
            process_station_minute(
                "ES02",
                CONFIG,
                source_request_json=source_request,
                store_request_json=store_request,
            )

        self.assertEqual(store_call_count, 0)


if __name__ == "__main__":
    unittest.main()
