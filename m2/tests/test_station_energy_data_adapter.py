import importlib
import unittest


PV_SNS = (
    "emu1", "emu2", "emu3", "emu4", "emu5",
    "emu21", "emu22", "emu23", "emu24",
)


class StationEnergyDataAdapterTests(unittest.TestCase):
    def test_invalid_source_url_raises_adapter_error(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        with self.assertRaisesRegex(
            adapter.StationEnergyDataError,
            "请求失败",
        ):
            adapter.fetch_station_source_record(
                "ES01",
                {
                    "station_tree_url": "not-a-url",
                    "station_tree_token": "tree-secret",
                    "emu_url": "https://station.example/api/t_emu:list",
                    "emu_token": "emu-secret",
                    "pcs_token": "pcs-secret",
                },
            )

    def test_non_object_source_row_is_rejected_with_source_context(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        with self.assertRaisesRegex(
            adapter.StationEnergyDataError,
            "t_emu.*对象",
        ):
            adapter.build_station_source_record(
                station_id="ES01",
                master_emu_sn="emu11",
                cabinet_sns=("emu11",),
                emu_rows=[None],
                growall_rows=[],
                pcs_records_by_cabinet={},
            )

    def test_non_object_pcs_row_is_rejected_with_cabinet_context(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        timestamp = "2026-08-25T14:36:00+08:00"
        with self.assertRaisesRegex(
            adapter.StationEnergyDataError,
            "emu11 PCS.*对象",
        ):
            adapter.build_station_source_record(
                station_id="ES01",
                master_emu_sn="emu11",
                cabinet_sns=("emu11",),
                emu_rows=[{
                    "f_es_sn": "ES01",
                    "emu_sn": "emu11",
                    "last_time_iso": timestamp,
                    "load_power": 100,
                    "latest_grid_power": 100000,
                    "latest_power": 10,
                    "battery_power": 9,
                }],
                growall_rows=[],
                pcs_records_by_cabinet={"emu11": [None, {}]},
            )

    def test_fixed_station_pv_topology_cannot_be_overridden(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        timestamp = "2026-08-25T14:36:00+08:00"
        with self.assertRaises(TypeError):
            adapter.build_station_source_record(
                station_id="ES02",
                master_emu_sn="emu26",
                cabinet_sns=("emu26",),
                emu_rows=[{
                    "f_es_sn": "ES02",
                    "emu_sn": "emu26",
                    "last_time_iso": timestamp,
                    "load_power": 100,
                    "latest_grid_power": 100000,
                    "latest_power": 10,
                    "battery_power": 9,
                }],
                growall_rows=[],
                pcs_records_by_cabinet={
                    "emu26": [
                        {
                            "fk_emu_sn": "emu26", "sn": "pcs261", "a6039": "5",
                            "timestamp": timestamp,
                        },
                        {
                            "fk_emu_sn": "emu26", "sn": "pcs262", "a6039": "5",
                            "timestamp": timestamp,
                        },
                    ],
                },
                pv_inverter_sns=(),
            )

    def test_pcs_record_from_another_cabinet_is_rejected(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        timestamp = "2026-08-25T14:36:00+08:00"
        emu_rows = [{
            "f_es_sn": "ES01",
            "emu_sn": "emu11",
            "last_time_iso": timestamp,
            "load_power": 100,
            "latest_grid_power": 100000,
            "latest_power": 10,
            "battery_power": 9,
        }]
        pcs_records = {
            "emu11": [
                {
                    "fk_emu_sn": "emu26", "sn": "pcs261", "a6039": "5",
                    "timestamp": timestamp,
                },
                {
                    "fk_emu_sn": "emu11", "sn": "pcs112", "a6039": "5",
                    "timestamp": timestamp,
                },
            ],
        }

        with self.assertRaisesRegex(adapter.StationEnergyDataError, "柜号不匹配"):
            adapter.build_station_source_record(
                station_id="ES01",
                master_emu_sn="emu11",
                cabinet_sns=("emu11",),
                emu_rows=emu_rows,
                growall_rows=[],
                pcs_records_by_cabinet=pcs_records,
            )

    def test_duplicate_pcs_sn_is_rejected_instead_of_double_counted(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        timestamp = "2026-08-25T14:36:00+08:00"
        emu_rows = [{
            "f_es_sn": "ES01",
            "emu_sn": "emu11",
            "last_time_iso": timestamp,
            "load_power": 100,
            "latest_grid_power": 100000,
            "latest_power": 10,
            "battery_power": 9,
        }]
        duplicate = {
            "fk_emu_sn": "emu11", "sn": "pcs111", "a6039": "5",
            "timestamp": timestamp,
        }

        with self.assertRaisesRegex(adapter.StationEnergyDataError, "PCS sn 重复"):
            adapter.build_station_source_record(
                station_id="ES01",
                master_emu_sn="emu11",
                cabinet_sns=("emu11",),
                emu_rows=emu_rows,
                growall_rows=[],
                pcs_records_by_cabinet={"emu11": [duplicate, dict(duplicate)]},
            )

    def test_fetch_es02_reads_growall_and_sums_nine_inverters(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        tree_url = "https://ems.example/api/en:list"
        emu_url = "https://station.example/api/t_emu:list"
        growall_url = "http://cabinet.example:3510/api/t_growall:list?filter=%7B%7D"
        base_url = "http://cabinet.example:2250"
        timestamp = "2026-08-25T14:36:00+08:00"
        responses = {
            tree_url: {
                "data": [{
                    "es": {"sn": "ES02"},
                    "node_type": "ess",
                    "is_aggregate": True,
                    "children": [{
                        "es": {"sn": "ES02"},
                        "node_type": "ess",
                        "is_aggregate": False,
                        "sn": "emu26",
                        "local_url": base_url,
                        "is_master": True,
                    }],
                }],
            },
            emu_url: {
                "data": [{
                    "f_es_sn": "ES02",
                    "emu_sn": "emu26",
                    "last_time_iso": timestamp,
                    "load_power": 400,
                    "latest_grid_power": 200000,
                    "latest_power": 30,
                    "battery_power": 28,
                }],
            },
            growall_url: {
                "data": [
                    {"sn": sn, "timestamp": timestamp, "a1": "100", "a35": "95"}
                    for sn in PV_SNS
                ],
            },
            f"{base_url}/api/t_pcs_1:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu26", "sn": "pcs261", "a6039": "14",
                    "timestamp": timestamp,
                },
            },
            f"{base_url}/api/t_pcs_2:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu26", "sn": "pcs262", "a6039": "13",
                    "timestamp": timestamp,
                },
            },
        }

        def request_json(url, token, timeout):
            return responses[url]

        record = adapter.fetch_station_source_record(
            "ES02",
            {
                "station_tree_url": tree_url,
                "station_tree_token": "tree-secret",
                "emu_url": emu_url,
                "emu_token": "emu-secret",
                "pcs_token": "pcs-secret",
                "growall_url": growall_url,
                "growall_token": "growall-secret",
            },
            request_json=request_json,
        )

        self.assertEqual(record["pv_dc_power"], 900.0)
        self.assertEqual(record["pv_ac_power"], 855.0)
        self.assertEqual(record["bus_id"], "ES02")

    def test_fetch_station_source_record_reads_layout_emu_and_two_pcs_per_cabinet(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        tree_url = "https://ems.example/api/en:list"
        emu_url = "https://station.example/api/t_emu:list"
        responses = {
            tree_url: {
                "data": [{
                    "es": {"sn": "ES01"},
                    "node_type": "ess",
                    "is_aggregate": True,
                    "children": [
                        {
                            "es": {"sn": "ES01"},
                            "node_type": "ess",
                            "is_aggregate": False,
                            "sn": "emu11",
                            "local_url": "http://cabinet.example:2200",
                            "is_master": True,
                        },
                        {
                            "es": {"sn": "ES01"},
                            "node_type": "ess",
                            "is_aggregate": False,
                            "sn": "emu12",
                            "local_url": "http://cabinet.example:2210",
                            "is_master": False,
                        },
                    ],
                }],
            },
            emu_url: {
                "data": [
                    {
                        "f_es_sn": "ES01",
                        "emu_sn": "emu11",
                        "last_time_iso": "2026-08-25T14:36:00+08:00",
                        "load_power": 150,
                        "latest_grid_power": 100000,
                        "latest_power": -20,
                        "battery_power": -18,
                    },
                    {
                        "f_es_sn": "ES01",
                        "emu_sn": "emu12",
                        "last_time_iso": "2026-08-25T14:36:00+08:00",
                        "load_power": 999,
                        "latest_grid_power": 999999,
                        "latest_power": 30,
                        "battery_power": 28,
                    },
                ],
            },
            "http://cabinet.example:2200/api/t_pcs_1:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu11", "sn": "pcs111", "a6039": "-10",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                },
            },
            "http://cabinet.example:2200/api/t_pcs_2:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu11", "sn": "pcs112", "a6039": "-9",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                },
            },
            "http://cabinet.example:2210/api/t_pcs_1:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu12", "sn": "pcs121", "a6039": "14",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                },
            },
            "http://cabinet.example:2210/api/t_pcs_2:get?filter=%7B%7D": {
                "data": {
                    "fk_emu_sn": "emu12", "sn": "pcs122", "a6039": "13",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                },
            },
        }
        calls = []

        def request_json(url, token, timeout):
            calls.append((url, token, timeout))
            return responses[url]

        record = adapter.fetch_station_source_record(
            "ES01",
            {
                "station_tree_url": tree_url,
                "station_tree_token": "tree-secret",
                "emu_url": emu_url,
                "emu_token": "emu-secret",
                "pcs_token": "pcs-secret",
                "timeout_seconds": 7,
            },
            request_json=request_json,
        )

        self.assertEqual(record["grid_import_power"], 100.0)
        self.assertEqual(record["cabinet_charge_power"], 20.0)
        self.assertEqual(record["cabinet_discharge_power"], 30.0)
        self.assertEqual(record["pcs_charge_power"], 19.0)
        self.assertEqual(record["pcs_discharge_power"], 27.0)
        self.assertEqual(len(calls), 6)
        self.assertNotIn("tree-secret", repr(record))
        self.assertNotIn("emu-secret", repr(record))
        self.assertNotIn("pcs-secret", repr(record))

    def test_station_tree_discovers_es02_cabinets_and_master(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        payload = {
            "data": [
                {
                    "es": {"sn": "ES02"},
                    "node_type": "ess",
                    "is_aggregate": True,
                    "children": [
                        {
                            "es": {"sn": "ES02"},
                            "node_type": "ess",
                            "is_aggregate": False,
                            "sn": "emu21",
                            "local_url": "http://e606pro.hlszh.com:1080",
                            "is_master": False,
                        },
                        {
                            "es": {"sn": "ES02"},
                            "node_type": "ess",
                            "is_aggregate": False,
                            "sn": "emu26",
                            "local_url": "http://e606pro.hlszh.com:2250",
                            "is_master": True,
                        },
                    ],
                },
                {
                    "es": {"sn": "ES02"},
                    "node_type": "grid",
                    "is_aggregate": True,
                    "children": [],
                },
            ]
        }

        layout = adapter.extract_station_layout(payload, "ES02")

        self.assertEqual(layout, {
            "station_id": "ES02",
            "master_emu_sn": "emu26",
            "cabinets": [
                {
                    "sn": "emu21",
                    "local_url": "http://e606pro.hlszh.com:1080",
                },
                {
                    "sn": "emu26",
                    "local_url": "http://e606pro.hlszh.com:2250",
                },
            ],
        })

    def test_es02_sources_map_to_complete_three_chain_record(self):
        try:
            adapter = importlib.import_module("m2.station_energy_data_adapter")
        except ModuleNotFoundError:
            self.fail("m2.station_energy_data_adapter 尚未实现")

        emu_rows = []
        cabinet_values = {
            "emu21": (-10, -9),
            "emu22": (25, 23),
            "emu23": (-20, -18),
            "emu24": (0, 0),
            "emu25": (15, 14),
            "emu26": (30, 28),
        }
        for cabinet_sn, (meter_power, battery_power) in cabinet_values.items():
            emu_rows.append({
                "f_es_sn": "ES02",
                "emu_sn": cabinet_sn,
                "last_time_iso": "2026-08-25T14:36:00+08:00",
                "load_power": 420 if cabinet_sn == "emu26" else 999,
                "latest_grid_power": 567398.56 if cabinet_sn == "emu26" else 999999,
                "latest_power": meter_power,
                "battery_power": battery_power,
                "latest_solar_power": 999,
            })

        growall_rows = [
            {
                "sn": sn,
                "timestamp": "2026-08-25T14:36:00+08:00",
                "a1": str(100 + index),
                "a35": str(95 + index),
            }
            for index, sn in enumerate(PV_SNS)
        ]

        pcs_values = {
            "emu21": (-5, -4),
            "emu22": (12, 11),
            "emu23": (-8, -7),
            "emu24": (0, 0),
            "emu25": (6, 5),
            "emu26": (14, 13),
        }
        pcs_records_by_cabinet = {
            cabinet_sn: [
                {
                    "fk_emu_sn": cabinet_sn,
                    "sn": f"pcs{cabinet_sn[3:]}{index}",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                    "a6039": str(power),
                }
                for index, power in enumerate(values, start=1)
            ]
            for cabinet_sn, values in pcs_values.items()
        }

        record = adapter.build_station_source_record(
            station_id="ES02",
            master_emu_sn="emu26",
            cabinet_sns=tuple(cabinet_values),
            emu_rows=emu_rows,
            growall_rows=growall_rows,
            pcs_records_by_cabinet=pcs_records_by_cabinet,
        )

        self.assertEqual(record["bus_id"], "ES02")
        self.assertEqual(record["data_time"], "2026-08-25T14:36:00+08:00")
        self.assertEqual(record["pv_dc_power"], 936.0)
        self.assertEqual(record["pv_ac_power"], 891.0)
        self.assertEqual(record["load_power"], 420.0)
        self.assertAlmostEqual(record["grid_import_power"], 567.39856)
        self.assertEqual(record["grid_export_power"], 0.0)
        self.assertEqual(record["cabinet_charge_power"], 30.0)
        self.assertEqual(record["cabinet_discharge_power"], 70.0)
        self.assertEqual(record["bms_charge_power"], 27.0)
        self.assertEqual(record["bms_discharge_power"], 65.0)
        self.assertEqual(record["pcs_charge_power"], 24.0)
        self.assertEqual(record["pcs_discharge_power"], 61.0)
        self.assertEqual(record["storage_aux_power"], 0.0)
        self.assertEqual(record["device_status"], {})
        self.assertEqual(len(record["source_times"]), 27)

    def test_es01_has_no_pv_and_negative_grid_power_is_export(self):
        adapter = importlib.import_module("m2.station_energy_data_adapter")
        emu_rows = [
            {
                "f_es_sn": "ES01",
                "emu_sn": "emu11",
                "last_time_iso": "2026-08-25T14:36:00+08:00",
                "load_power": 200,
                "latest_grid_power": -100000,
                "latest_power": -20,
                "battery_power": -18,
            },
            {
                "f_es_sn": "ES01",
                "emu_sn": "emu12",
                "last_time_iso": "2026-08-25T14:36:00+08:00",
                "load_power": 999,
                "latest_grid_power": 999999,
                "latest_power": 30,
                "battery_power": 28,
            },
        ]
        pcs_records_by_cabinet = {
            cabinet_sn: [
                {
                    "fk_emu_sn": cabinet_sn,
                    "sn": f"pcs{cabinet_sn[3:]}{index}",
                    "timestamp": "2026-08-25T14:36:00+08:00",
                    "a6039": str(power),
                }
                for index, power in enumerate(values, start=1)
            ]
            for cabinet_sn, values in {
                "emu11": (-10, -9),
                "emu12": (14, 13),
            }.items()
        }

        record = adapter.build_station_source_record(
            station_id="ES01",
            master_emu_sn="emu11",
            cabinet_sns=("emu11", "emu12"),
            emu_rows=emu_rows,
            growall_rows=[],
            pcs_records_by_cabinet=pcs_records_by_cabinet,
        )

        self.assertEqual(record["pv_dc_power"], 0.0)
        self.assertEqual(record["pv_ac_power"], 0.0)
        self.assertEqual(record["grid_import_power"], 0.0)
        self.assertEqual(record["grid_export_power"], 100.0)
        self.assertEqual(record["cabinet_charge_power"], 20.0)
        self.assertEqual(record["cabinet_discharge_power"], 30.0)


if __name__ == "__main__":
    unittest.main()
