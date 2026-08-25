import importlib
import unittest


TIMESTAMP = "2026-08-25T14:36:00+08:00"
ES01_SNS = ("emu11", "emu12")
ES02_SNS = ("emu21", "emu22", "emu23", "emu24", "emu25", "emu26")


def make_es01_rows():
    return [
        {
            "f_es_sn": "ES01",
            "emu_sn": "emu11",
            "last_time_iso": TIMESTAMP,
            "load_power": 200,
            "latest_grid_power": -100000,
            "latest_power": -20,
            "battery_power": -18,
            "pcs1_power": -10,
            "pcs2_power": -9,
            "input_ac_solar_power": None,
            "latest_solar_power": None,
            "acpv_rated_power": None,
        },
        {
            "f_es_sn": "ES01",
            "emu_sn": "emu12",
            "last_time_iso": TIMESTAMP,
            "load_power": 999,
            "latest_grid_power": 999999,
            "latest_power": 30,
            "battery_power": 28,
            "pcs1_power": 14,
            "pcs2_power": 13,
            "input_ac_solar_power": None,
            "latest_solar_power": None,
            "acpv_rated_power": None,
        },
    ]


def make_es02_rows():
    pcs_values = {
        "emu21": (-5, -4),
        "emu22": (12, 11),
        "emu23": (-8, -7),
        "emu24": (0, 0),
        "emu25": (6, 5),
        "emu26": (14, 13),
    }
    meter_values = {
        "emu21": (-10, -9),
        "emu22": (25, 23),
        "emu23": (-20, -18),
        "emu24": (0, 0),
        "emu25": (15, 14),
        "emu26": (30, 28),
    }
    rows = []
    for cabinet_sn in ES02_SNS:
        latest_power, battery_power = meter_values[cabinet_sn]
        pcs1_power, pcs2_power = pcs_values[cabinet_sn]
        rows.append({
            "f_es_sn": "ES02",
            "emu_sn": cabinet_sn,
            "last_time_iso": TIMESTAMP,
            "load_power": 420 if cabinet_sn == "emu26" else 999,
            "latest_grid_power": 567398.56 if cabinet_sn == "emu26" else 999999,
            "latest_power": latest_power,
            "battery_power": battery_power,
            "pcs1_power": pcs1_power,
            "pcs2_power": pcs2_power,
            "input_ac_solar_power": 936 if cabinet_sn == "emu26" else 999,
            "latest_solar_power": 891 if cabinet_sn == "emu26" else 999,
            "acpv_rated_power": 1000 if cabinet_sn == "emu26" else 999,
        })
    return rows


class StationEnergyDataAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = importlib.import_module("m2.station_energy_data_adapter")

    def test_fetch_es02_uses_only_t_emu_with_embedded_pcs_and_solar_fields(self):
        emu_url = "https://station.example/api/t_emu:list?filter=%7B%7D"
        calls = []

        def request_json(url, token, timeout):
            calls.append((url, token, timeout))
            return {"data": make_es02_rows()}

        record = self.adapter.fetch_station_source_record(
            "ES02",
            {
                "emu_url": emu_url,
                "emu_token": "emu-secret",
                "timeout_seconds": 7,
            },
            request_json=request_json,
        )

        self.assertEqual(calls, [(emu_url, "emu-secret", 7.0)])
        self.assertEqual(record["pv_dc_power"], 936.0)
        self.assertEqual(record["pv_ac_power"], 891.0)
        self.assertEqual(record["pcs_charge_power"], 24.0)
        self.assertEqual(record["pcs_discharge_power"], 61.0)
        self.assertEqual(record["cabinet_charge_power"], 30.0)
        self.assertEqual(record["cabinet_discharge_power"], 70.0)
        self.assertEqual(record["bms_charge_power"], 27.0)
        self.assertEqual(record["bms_discharge_power"], 65.0)
        self.assertAlmostEqual(record["grid_import_power"], 567.39856)
        self.assertEqual(record["load_power"], 420.0)
        self.assertEqual(len(record["source_times"]), 6)
        self.assertNotIn("acpv_rated_power", record)
        self.assertNotIn("emu-secret", repr(record))

    def test_es01_ignores_nullable_pv_fields_and_maps_negative_grid_as_export(self):
        record = self.adapter.build_station_source_record(
            station_id="ES01",
            emu_rows=make_es01_rows() + make_es02_rows(),
        )

        self.assertEqual(record["pv_dc_power"], 0.0)
        self.assertEqual(record["pv_ac_power"], 0.0)
        self.assertEqual(record["grid_import_power"], 0.0)
        self.assertEqual(record["grid_export_power"], 100.0)
        self.assertEqual(record["cabinet_charge_power"], 20.0)
        self.assertEqual(record["cabinet_discharge_power"], 30.0)
        self.assertEqual(record["pcs_charge_power"], 19.0)
        self.assertEqual(record["pcs_discharge_power"], 27.0)

    def test_station_requires_exact_configured_cabinet_set(self):
        rows = [row for row in make_es02_rows() if row["emu_sn"] != "emu25"]

        with self.assertRaisesRegex(
            self.adapter.StationEnergyDataError,
            "缺少.*emu25",
        ):
            self.adapter.build_station_source_record(
                station_id="ES02",
                emu_rows=rows,
            )

    def test_null_pcs_power_is_rejected_instead_of_becoming_zero(self):
        rows = make_es02_rows()
        rows[0]["pcs1_power"] = None

        with self.assertRaisesRegex(
            self.adapter.StationEnergyDataError,
            "emu21.pcs1_power",
        ):
            self.adapter.build_station_source_record(
                station_id="ES02",
                emu_rows=rows,
            )

    def test_non_object_source_row_is_rejected_with_source_context(self):
        with self.assertRaisesRegex(
            self.adapter.StationEnergyDataError,
            "t_emu.*对象",
        ):
            self.adapter.build_station_source_record(
                station_id="ES01",
                emu_rows=[None],
            )

    def test_invalid_source_url_raises_adapter_error(self):
        with self.assertRaisesRegex(
            self.adapter.StationEnergyDataError,
            "请求失败",
        ):
            self.adapter.fetch_station_source_record(
                "ES01",
                {
                    "emu_url": "not-a-url",
                    "emu_token": "emu-secret",
                },
            )

    def test_unknown_station_is_rejected(self):
        with self.assertRaisesRegex(
            self.adapter.StationEnergyDataError,
            "只支持 ES01 或 ES02",
        ):
            self.adapter.build_station_source_record(
                station_id="ES03",
                emu_rows=[],
            )


if __name__ == "__main__":
    unittest.main()
