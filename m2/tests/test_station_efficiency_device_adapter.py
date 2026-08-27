import unittest

from m2.tests.test_station_energy_data_adapter import TIMESTAMP, make_es01_rows


INVERTER_SNS = (
    "emu1",
    "emu2",
    "emu3",
    "emu4",
    "emu5",
    "emu21",
    "emu22",
    "emu23",
    "emu24",
)
CONFIG = {
    "timezone": "Asia/Shanghai",
    "device_sample_max_age_minutes": 2,
    "pv_inverter_sns_by_station": {"ES02": INVERTER_SNS},
    "pv_inverter_rated_power_kw": 60,
}


def inverter_row(sn, timestamp, power):
    return {"sn": sn, "timestamp": timestamp, "a35": power}


class StationEfficiencyDeviceAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from m2 import station_efficiency_device_adapter

        cls.adapter = station_efficiency_device_adapter

    def test_builds_nine_inverter_points_and_selects_latest_valid_sample(self):
        rows = [
            inverter_row(sn, "2026-08-27T10:00:30+08:00", 12.0)
            for sn in INVERTER_SNS
        ]
        rows.append(inverter_row("emu1", "2026-08-27T10:00:50+08:00", 9.0))

        points = self.adapter.build_inverter_device_points(
            station_id="ES02",
            growall_rows=rows,
            data_time="2026-08-27T10:01:00+08:00",
            config=CONFIG,
        )

        emu1 = next(point for point in points if point["device_id"] == "emu1")
        self.assertEqual(len(points), 9)
        self.assertEqual(emu1["active_power_kw"], 9.0)
        self.assertEqual(emu1["rated_power_kw"], 60.0)
        self.assertEqual(emu1["load_rate_pct"], 15.0)
        self.assertEqual(emu1["data_time"], "2026-08-27T10:01:00+08:00")
        self.assertEqual(emu1["source_time"], "2026-08-27T10:00:50+08:00")
        self.assertEqual(
            tuple(emu1),
            self.adapter.DEVICE_POINT_FIELDS,
        )

    def test_rejects_future_and_more_than_two_minute_old_inverter_samples(self):
        rows = [
            inverter_row("emu1", "2026-08-27T10:01:01+08:00", 10.0),
            inverter_row("emu2", "2026-08-27T09:58:59+08:00", 10.0),
        ]

        points = self.adapter.build_inverter_device_points(
            station_id="ES02",
            growall_rows=rows,
            data_time="2026-08-27T10:01:00+08:00",
            config=CONFIG,
        )

        self.assertEqual(points, [])

    def test_builds_battery_points_from_t_emu_and_keeps_missing_temperature_null(self):
        rows = make_es01_rows()
        rows[0]["max_cell_temperature"] = 31.5
        rows[0]["hottest_cluster"] = "cluster-2"

        points = self.adapter.build_battery_device_points(
            station_id="ES01",
            emu_rows=rows,
            data_time=TIMESTAMP,
            config={
                **CONFIG,
                "battery_max_temperature_field": "max_cell_temperature",
                "battery_hot_cluster_field": "hottest_cluster",
            },
        )

        self.assertEqual(points[0]["battery_power_kw"], -18.0)
        self.assertEqual(points[0]["temperature_c"], 31.5)
        self.assertEqual(points[0]["subdevice_id"], "cluster-2")
        self.assertIsNone(points[1]["temperature_c"])
        self.assertIsNone(points[0]["active_power_kw"])

    def test_es01_does_not_emit_inverter_points(self):
        points = self.adapter.build_inverter_device_points(
            station_id="ES01",
            growall_rows=[inverter_row("emu1", TIMESTAMP, 12)],
            data_time=TIMESTAMP,
            config=CONFIG,
        )

        self.assertEqual(points, [])

    def test_fetch_growall_rows_validates_response_without_exposing_token(self):
        rows = [inverter_row("emu1", TIMESTAMP, 12)]

        actual = self.adapter.fetch_growall_rows(
            {
                "growall_url": "https://station.example/api/t_growall:list",
                "growall_token": "do-not-expose-me",
            },
            request_json=lambda *args: {"data": rows},
        )

        self.assertEqual(actual, rows)
        with self.assertRaises(self.adapter.StationEfficiencyDeviceDataError) as error:
            self.adapter.fetch_growall_rows(
                {
                    "growall_url": "https://station.example/api/t_growall:list",
                    "growall_token": "do-not-expose-me",
                },
                request_json=lambda *args: {"data": {}},
            )
        self.assertNotIn("do-not-expose-me", str(error.exception))


if __name__ == "__main__":
    unittest.main()
