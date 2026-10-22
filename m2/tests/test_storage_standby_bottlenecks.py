"""Exercise standby classification through source mapping, minutes and events."""

import unittest

from m2.station_efficiency_bottlenecks import evaluate_station_bottlenecks
from m2.station_efficiency_history import build_minute_point
from m2.station_efficiency_job import FORMULA_VERSION, _minute_chains
from m2.station_energy_backend import calculate_bus
from m2.station_energy_data_adapter import (
    StationEnergyDataError,
    build_station_source_record,
    get_station_config,
)
from m2.tests.test_station_efficiency_bottlenecks import RULE
from m2.tests.test_station_energy_data_adapter import make_es01_rows, make_es02_rows


def storage_minute(station, minute, *, overrides=None, grid_w=-50000):
    rows = make_es01_rows(include_pv=True) if station == "ES01" else make_es02_rows()
    config = get_station_config(station)
    time = f"2026-10-22T12:{minute:02d}:00+08:00"
    for row in rows:
        row["last_time_iso"] = time
        if row["emu_sn"] in config["cabinet_sns"]:
            row.update(latest_power=-1, battery_power=0, pcs1_power=0, pcs2_power=0)
        if row["emu_sn"] == config["master_emu_sn"]:
            row["latest_grid_power"] = grid_w
        if row["emu_sn"] == config["pv_emu_sn"]:
            row.update(input_ac_solar_power=200, latest_solar_power=190)
        row.update((overrides or {}).get(row["emu_sn"], {}))
    source = build_station_source_record(station_id=station, emu_rows=rows)
    result = calculate_bus(source)
    point = build_minute_point(
        station, time, _minute_chains(result), FORMULA_VERSION, time,
    )
    return point, source, result


def events(station, points, active=()):
    return evaluate_station_bottlenecks(
        station_id=station, minute_points=points, device_points=[],
        active_events=list(active), rule={**RULE, "station_id": station},
    )


class StorageStandbyBottleneckTests(unittest.TestCase):
    def test_standby_keeps_consumption_and_balance_without_low_efficiency_event(self):
        for station, cabinet_count in (("ES01", 2), ("ES02", 6)):
            for grid_w in (-50000, 50000):
                with self.subTest(station=station, grid_w=grid_w):
                    points = []
                    for minute in (0, 1):
                        point, source, result = storage_minute(station, minute, grid_w=grid_w)
                        points.append(point)
                        self.assertEqual(source["storage_aux_power"], cabinet_count)
                        self.assertEqual(source["load_power"], 190 + grid_w / 1000 - cabinet_count)
                        self.assertEqual(source["cabinet_charge_power"], 0)
                        self.assertEqual(point["pv_storage_input_kw"], 0)
                        self.assertEqual(point["pv_storage_output_kw"], 0)
                        self.assertIsNone(point["pv_storage_efficiency"])
                        self.assertAlmostEqual(result["power_balance_error"], 0)
                    self.assertEqual(events(station, points), [])

    def test_standby_cabinets_do_not_reduce_another_cabinets_charging_efficiency(self):
        points = []
        for minute in (0, 1):
            point, source, result = storage_minute("ES02", minute, overrides={
                "emu21": {"latest_power": -2, "battery_power": -1900, "pcs1_power": -1.95},
            })
            points.append(point)
            self.assertEqual(source["storage_aux_power"], 5)
            self.assertEqual(source["cabinet_charge_power"], 2)
            self.assertAlmostEqual(point["pv_storage_efficiency"], 90.25)
            self.assertAlmostEqual(result["power_balance_error"], 0)
        self.assertEqual(events("ES02", points), [])

    def test_export_does_not_hide_actual_low_efficiency_charging(self):
        for battery_w, pcs_kw in ((-50000, -80), (0, -80), (-50000, 0)):
            with self.subTest(battery_w=battery_w, pcs_kw=pcs_kw):
                points = []
                for minute in (0, 1):
                    point, source, result = storage_minute("ES02", minute, overrides={
                        "emu21": {
                            "latest_power": -100, "battery_power": battery_w,
                            "pcs1_power": pcs_kw,
                        },
                    })
                    points.append(point)
                    self.assertEqual(source["grid_export_power"], 50)
                    self.assertEqual(source["storage_aux_power"], 5)
                    self.assertGreater(point["pv_storage_input_kw"], 0)
                    self.assertAlmostEqual(result["power_balance_error"], 0)
                opened = events("ES02", points)
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0]["device_id"], "pv_storage")
                self.assertEqual(opened[0]["status"], "active")
                self.assertLess(opened[0]["observed_value"], 85)

    def test_returning_to_standby_recovers_active_charging_event(self):
        low = [storage_minute("ES02", minute, overrides={
            "emu21": {"latest_power": -100, "battery_power": 0, "pcs1_power": -80},
        })[0] for minute in (0, 1)]
        active = events("ES02", low)
        standby = [storage_minute("ES02", minute)[0] for minute in (2, 3)]

        recovered = events("ES02", low + standby, active)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "recovered")
        self.assertEqual(recovered[0]["end_time"], standby[0]["data_time"])
        self.assertEqual(recovered[0]["evidence"]["recovery_reason"], "chain_stopped")

    def test_standby_breaks_low_efficiency_trigger_continuity(self):
        def low(minute):
            return storage_minute("ES02", minute, overrides={
                "emu21": {"latest_power": -100, "battery_power": 0, "pcs1_power": -80},
            })[0]

        points = [low(0), storage_minute("ES02", 1)[0], low(2)]
        self.assertEqual(events("ES02", points), [])
        self.assertEqual(len(events("ES02", points + [low(3)])), 1)

    def test_opposing_pcs_powers_are_not_treated_as_standby(self):
        _point, source, _result = storage_minute("ES02", 0, overrides={
            "emu21": {"pcs1_power": -2, "pcs2_power": 2},
        })
        self.assertEqual(source["storage_aux_power"], 5)
        self.assertEqual(source["cabinet_charge_power"], 1)

    def test_unknown_battery_or_pcs_power_is_not_treated_as_standby(self):
        for field in ("battery_power", "pcs1_power", "pcs2_power"):
            with self.subTest(field=field), self.assertRaises(StationEnergyDataError):
                storage_minute("ES02", 0, overrides={"emu21": {field: None}})


if __name__ == "__main__":
    unittest.main()
