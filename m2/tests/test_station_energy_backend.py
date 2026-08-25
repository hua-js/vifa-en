"""Tests for station_energy_backend."""

from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
import unittest

from m2.station_energy_backend import (
    BackendError,
    DEFAULT_CONFIG,
    POWER_FIELDS,
    _dashboard_events,
    aggregate_samples,
    build_dashboard_payload,
    calculate_bus,
    calculate_request,
    dashboard_request,
    dispatch_request,
    normalize_sample,
    normalize_source_record,
    parse_data_time,
)
from m2.tests.station_energy_test_support import build_dashboard_response, make_sample

BACKEND_PATH = Path(__file__).resolve().parents[1] / "station_energy_backend.py"


class StationEnergyBackendTests(unittest.TestCase):
    def make_source_history(
        self, start="2026-08-24T12:00:00+08:00", hours=25
    ):
        base = datetime.fromisoformat(start)
        return [
            make_sample(
                data_time=(base + timedelta(hours=hour)).isoformat(),
                pv_dc_power=190,
                pv_ac_power=183,
                load_power=80,
                cabinet_charge_power=103,
                pcs_charge_power=100,
                bms_charge_power=95,
            )
            for hour in range(hours)
        ]

    def test_build_dashboard_payload_sorts_and_selects_latest_24_hours(self):
        records = self.make_source_history(hours=27)
        payload = build_dashboard_payload(list(reversed(records)))
        self.assertEqual(payload["operation"], "dashboard")
        self.assertEqual(payload["current"]["data_time"], records[-1]["data_time"])
        self.assertEqual(len(payload["trend_samples"]), 25)
        self.assertEqual(
            payload["trend_samples"][0]["data_time"], records[2]["data_time"]
        )
        self.assertEqual(
            payload["trend_samples"][-1]["data_time"], records[-1]["data_time"]
        )
        response = dashboard_request(payload)
        self.assertEqual(response["operation"], "dashboard")

    def test_build_dashboard_payload_rejects_insufficient_history(self):
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(self.make_source_history(hours=24))
        self.assertEqual(caught.exception.code, "insufficient_history")

    def test_build_dashboard_payload_rejects_duplicate_times(self):
        records = self.make_source_history()
        records.append(dict(records[-1]))
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(records)
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_build_dashboard_payload_does_not_fall_back_from_invalid_latest_record(
        self,
    ):
        records = self.make_source_history()
        del records[-1]["pv_dc_power"]
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(records)
        self.assertEqual(caught.exception.code, "source_mapping_error")

    def test_build_dashboard_payload_rejects_mixed_bus_outside_selected_window(self):
        records = self.make_source_history(hours=27)
        records[0]["bus_id"] = "bus-2"
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(records)
        self.assertEqual(caught.exception.code, "invalid_time_series")
        self.assertEqual(caught.exception.details, {"field": "bus_id", "index": 0})

    def test_build_dashboard_payload_rejects_mixed_timezone_outside_selected_window(
        self,
    ):
        records = self.make_source_history(hours=27)
        records[0]["data_time"] = "2026-08-24T12:00:00+09:00"
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(records)
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_build_dashboard_payload_rejects_gap_outside_selected_window(self):
        records = self.make_source_history(hours=27)
        records[0]["data_time"] = "2026-08-24T10:00:00+08:00"
        with self.assertRaises(BackendError) as caught:
            build_dashboard_payload(records)
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_normalize_source_record_requires_all_power_fields(self):
        record = make_sample()
        del record["grid_export_power"]
        with self.assertRaises(BackendError) as caught:
            normalize_source_record(record)
        self.assertEqual(caught.exception.code, "source_mapping_error")
        self.assertEqual(caught.exception.details["fields"], ["grid_export_power"])

    def test_normalize_source_record_preserves_real_zero_values(self):
        record = make_sample(**{field: 0.0 for field in POWER_FIELDS})
        normalized = normalize_source_record(record)
        self.assertTrue(all(normalized[field] == 0.0 for field in POWER_FIELDS))

    def test_normalize_source_record_rejects_invalid_power_values(self):
        invalid_values = (None, True, -1, float("nan"), float("inf"), 10**400)
        for value in invalid_values:
            with self.subTest(value=value):
                record = make_sample(pv_dc_power=value)
                with self.assertRaises(BackendError) as caught:
                    normalize_source_record(record)
                self.assertEqual(caught.exception.code, "source_mapping_error")
                self.assertEqual(caught.exception.details["field"], "pv_dc_power")

    def test_normalize_sample_fills_missing_power_with_zero(self):
        sample = normalize_sample(
            {"data_time": "2026-08-24T12:00:00+08:00", "pv_ac_power": 96},
            DEFAULT_CONFIG,
        )
        self.assertEqual(sample["pv_ac_power"], 96.0)
        self.assertEqual(sample["grid_export_power"], 0.0)

    def test_normalize_sample_rejects_negative_boolean_and_non_finite_power(self):
        for bad_value in (-1, True, float("nan"), float("inf")):
            with self.subTest(value=bad_value):
                with self.assertRaises(BackendError) as caught:
                    normalize_sample(
                        {
                            "data_time": "2026-08-24T12:00:00+08:00",
                            "pv_ac_power": bad_value,
                        },
                        DEFAULT_CONFIG,
                    )
                self.assertEqual(caught.exception.code, "invalid_input")

    def test_data_time_requires_an_explicit_timezone(self):
        with self.assertRaises(BackendError) as caught:
            parse_data_time("2026-08-24T12:00:00")
        self.assertEqual(caught.exception.code, "invalid_time")

    def test_example_a_pv_to_storage(self):
        result = calculate_bus(
            make_sample(
                pv_ac_power=183,
                load_power=80,
                cabinet_charge_power=103,
                pcs_charge_power=100,
                bms_charge_power=95,
            )
        )
        self.assertEqual(result["pv_to_storage_power"], 103.0)
        self.assertEqual(result["grid_to_storage_power"], 0.0)
        self.assertAlmostEqual(result["pcs_charge_efficiency"], 95.0, places=6)
        self.assertAlmostEqual(
            result["pv_storage_efficiency"], 95 / 103 * 100, places=6
        )

    def test_example_b_storage_to_load_includes_full_path_loss(self):
        result = calculate_bus(
            make_sample(
                load_power=90,
                cabinet_discharge_power=92,
                pcs_discharge_power=95,
                bms_discharge_power=100,
            )
        )
        self.assertEqual(result["storage_to_load_power"], 90.0)
        self.assertAlmostEqual(result["pcs_discharge_efficiency"], 95.0, places=6)
        self.assertAlmostEqual(
            result["cabinet_discharge_efficiency"], 92.0, places=6
        )
        self.assertAlmostEqual(result["storage_load_efficiency"], 90.0, places=6)

    def test_example_c_pv_to_load_tracks_ac_and_dc_input(self):
        result = calculate_bus(
            make_sample(pv_dc_power=100, pv_ac_power=96, load_power=94)
        )
        self.assertAlmostEqual(result["pv_inverter_efficiency"], 96.0, places=6)
        self.assertAlmostEqual(
            result["pv_load_efficiency"], 94 / 96 * 100, places=6
        )
        self.assertAlmostEqual(result["pv_load_dc_efficiency"], 94.0, places=6)

    def test_example_d_allocates_three_load_sources(self):
        result = calculate_bus(
            make_sample(
                pv_ac_power=80,
                load_power=150,
                cabinet_discharge_power=50,
                grid_import_power=20,
            )
        )
        self.assertEqual(result["pv_to_load_power"], 80.0)
        self.assertEqual(result["storage_to_load_power"], 50.0)
        self.assertEqual(result["grid_to_load_power"], 20.0)
        self.assertEqual(result["calculation_mode"], "estimated")

    def test_zero_and_low_denominators_return_null(self):
        zero = calculate_bus(make_sample())
        self.assertIsNone(zero["pv_inverter_efficiency"])
        self.assertIn(
            "zero_denominator",
            zero["quality_by_metric"]["pv_inverter_efficiency"],
        )
        low = calculate_bus(
            make_sample(pv_dc_power=0.5, pv_ac_power=0.45, load_power=0.45)
        )
        self.assertIsNone(low["pv_inverter_efficiency"])
        self.assertIn(
            "low_power", low["quality_by_metric"]["pv_inverter_efficiency"]
        )

    def test_zero_min_power_config_handles_idle_sample(self):
        result = calculate_bus(make_sample(), {"min_power_kw": 0})
        self.assertEqual(result["power_balance_error"], 0.0)
        self.assertIsNone(result["pv_load_efficiency"])

    def test_zero_min_power_config_handles_idle_multi_bus(self):
        response = calculate_request(
            {
                "operation": "calculate",
                "buses": [make_sample(bus_id="a"), make_sample(bus_id="b")],
                "config": {"min_power_kw": 0},
            }
        )
        self.assertEqual(response["station"]["power_balance_error"], 0.0)

    def test_power_balance_error_invalidates_chain_efficiency(self):
        result = calculate_bus(
            make_sample(pv_dc_power=100, pv_ac_power=100, load_power=10)
        )
        self.assertIn("power_balance_error", result["quality_codes"])
        self.assertIsNone(result["pv_load_efficiency"])
        self.assertAlmostEqual(result["pv_inverter_efficiency"], 100.0, places=6)

    def test_time_misalignment_invalidates_all_chain_efficiencies(self):
        result = calculate_bus(
            make_sample(
                pv_dc_power=100,
                pv_ac_power=96,
                load_power=94,
                source_times={
                    "pv": "2026-08-24T12:00:00+08:00",
                    "load": "2026-08-24T12:00:31+08:00",
                },
            )
        )
        self.assertIn("time_misaligned", result["quality_codes"])
        self.assertIsNone(result["pv_load_efficiency"])

    def test_storage_direction_conflict_does_not_hide_pv_inverter_efficiency(self):
        result = calculate_bus(
            make_sample(
                pv_dc_power=100,
                pv_ac_power=96,
                load_power=96,
                pcs_charge_power=10,
                bms_discharge_power=10,
            )
        )
        self.assertIn("direction_conflict", result["quality_codes"])
        self.assertAlmostEqual(result["pv_inverter_efficiency"], 96.0, places=6)
        self.assertIsNone(result["storage_load_efficiency"])

    def test_abnormal_status_is_detected_recursively(self):
        result = calculate_bus(
            make_sample(
                pv_dc_power=100,
                pv_ac_power=96,
                load_power=96,
                device_status={"pcs": {"status": "offline"}},
            )
        )
        self.assertIn("device_abnormal", result["quality_codes"])
        self.assertAlmostEqual(result["pv_inverter_efficiency"], 96.0)
        self.assertAlmostEqual(result["pv_load_efficiency"], 100.0)

    def test_pv_inverter_abnormality_only_masks_pv_related_metrics(self):
        result = calculate_bus(
            make_sample(
                pv_dc_power=100,
                pv_ac_power=96,
                load_power=146,
                cabinet_discharge_power=50,
                pcs_discharge_power=55,
                bms_discharge_power=60,
                device_status={"pv_inverter": {"status": "offline"}},
            )
        )
        self.assertIsNone(result["pv_inverter_efficiency"])
        self.assertIsNone(result["pv_load_efficiency"])
        self.assertAlmostEqual(result["storage_load_efficiency"], 50 / 60 * 100)
        self.assertEqual(result["abnormal_devices"], ["pv_inverter.status"])

    def test_list_device_status_preserves_device_identity(self):
        result = calculate_bus(
            make_sample(
                pv_dc_power=100,
                pv_ac_power=96,
                load_power=96,
                device_status=[
                    {"device": "pv_inverter", "status": "offline"}
                ],
            )
        )
        self.assertEqual(result["abnormal_devices"], ["pv_inverter.status"])
        self.assertIsNone(result["pv_inverter_efficiency"])

    def test_multi_bus_efficiency_uses_summed_numerator_and_denominator(self):
        response = calculate_request(
            {
                "operation": "calculate",
                "buses": [
                    make_sample(
                        bus_id="a",
                        pv_dc_power=100,
                        pv_ac_power=100,
                        load_power=100,
                    ),
                    make_sample(
                        bus_id="b",
                        pv_dc_power=300,
                        pv_ac_power=150,
                        load_power=150,
                    ),
                ],
            }
        )
        self.assertEqual(len(response["buses"]), 2)
        self.assertAlmostEqual(
            response["station"]["pv_load_dc_efficiency"], 62.5, places=6
        )

    def test_time_series_integrates_energy_instead_of_averaging_efficiency(self):
        samples = [
            make_sample(
                data_time="2026-08-24T00:00:00+08:00",
                pv_dc_power=100,
                pv_ac_power=100,
                load_power=100,
            ),
            make_sample(
                data_time="2026-08-24T01:00:00+08:00",
                pv_dc_power=100,
                pv_ac_power=50,
                load_power=50,
            ),
            make_sample(
                data_time="2026-08-24T03:00:00+08:00",
                pv_dc_power=100,
                pv_ac_power=50,
                load_power=50,
            ),
        ]
        result = aggregate_samples(samples)
        self.assertAlmostEqual(result["pv_load_dc_input_energy"], 300.0, places=6)
        self.assertAlmostEqual(result["pv_to_load_energy"], 200.0, places=6)
        self.assertAlmostEqual(
            result["pv_load_dc_efficiency"], 200 / 300 * 100, places=6
        )

    def test_time_series_rejects_non_increasing_timestamps(self):
        repeated = [
            make_sample(data_time="2026-08-24T00:00:00+08:00"),
            make_sample(data_time="2026-08-24T00:00:00+08:00"),
        ]
        with self.assertRaises(BackendError) as caught:
            aggregate_samples(repeated)
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_pv_to_storage_energy_is_integrated_ac_input(self):
        samples = [
            make_sample(
                data_time="2026-08-24T00:00:00+08:00",
                pv_ac_power=183,
                load_power=80,
                cabinet_charge_power=103,
                pcs_charge_power=100,
                bms_charge_power=95,
            ),
            make_sample(
                data_time="2026-08-24T01:00:00+08:00",
                pv_ac_power=183,
                load_power=80,
                cabinet_charge_power=103,
                pcs_charge_power=100,
                bms_charge_power=95,
            ),
        ]
        result = aggregate_samples(samples)
        self.assertEqual(result["pv_to_storage_energy"], 103.0)
        self.assertEqual(result["bms_pv_charge_energy"], 95.0)

    def test_aggregate_preserves_estimated_mode_and_quality(self):
        samples = [
            make_sample(
                data_time="2026-08-24T00:00:00+08:00",
                pv_ac_power=80,
                load_power=150,
                cabinet_discharge_power=50,
                grid_import_power=20,
            ),
            make_sample(
                data_time="2026-08-24T01:00:00+08:00",
                pv_ac_power=80,
                load_power=150,
                cabinet_discharge_power=50,
                grid_import_power=20,
            ),
        ]
        result = aggregate_samples(samples)
        self.assertEqual(result["calculation_mode"], "estimated")
        self.assertIn("estimated", result["quality_codes"])

    def test_dashboard_operation_returns_frontend_ready_contract(self):
        start = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
        current = make_sample(
            data_time="2026-08-25T00:00:00+08:00",
            pv_dc_power=100,
            pv_ac_power=96,
            load_power=94,
        )
        response = dispatch_request(
            {
                "operation": "dashboard",
                "current": current,
                "trend_samples": [
                    make_sample(
                        data_time=(start + timedelta(hours=hour)).isoformat(),
                        pv_dc_power=100,
                        pv_ac_power=96,
                        load_power=94,
                    )
                    for hour in range(25)
                ],
            }
        )
        self.assertEqual(response["operation"], "dashboard")
        self.assertEqual(response["realtime"]["inputs"]["pv_dc_power"], 100.0)
        self.assertEqual(
            response["realtime"]["result"]["pv_load_dc_efficiency"], 94.0
        )
        self.assertIsNone(
            response["realtime"]["result"]["storage_load_efficiency"]
        )
        self.assertEqual(len(response["trend"]), 25)
        self.assertAlmostEqual(response["trend"][0]["pvLoad"], 94 / 96 * 100)
        self.assertAlmostEqual(
            response["summary_24h"]["pv_load_dc_efficiency"], 94.0
        )
        self.assertIsInstance(response["events"], list)

    def test_dashboard_pv_storage_trend_uses_dc_input_boundary(self):
        response = build_dashboard_response()
        self.assertAlmostEqual(
            response["trend"][0]["pvStorage"],
            88.83495145631069,
            places=6,
        )

    def test_dashboard_rejects_history_that_is_not_exactly_24_hours(self):
        with self.assertRaises(BackendError) as caught:
            dashboard_request(
                {
                    "current": make_sample(),
                    "trend_samples": [
                        make_sample(data_time="2026-08-24T00:00:00+08:00"),
                        make_sample(data_time="2026-08-24T02:00:00+08:00"),
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_dashboard_rejects_mixed_bus_history(self):
        with self.assertRaises(BackendError) as caught:
            dashboard_request(
                {
                    "current": make_sample(),
                    "trend_samples": [
                        make_sample(
                            bus_id="a", data_time="2026-08-24T00:00:00+08:00"
                        ),
                        make_sample(
                            bus_id="b", data_time="2026-08-25T00:00:00+08:00"
                        ),
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_dashboard_rejects_current_with_different_timezone(self):
        start = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
        with self.assertRaises(BackendError) as caught:
            dashboard_request(
                {
                    "current": make_sample(
                        data_time="2026-08-25T01:00:00+09:00"
                    ),
                    "trend_samples": [
                        make_sample(
                            data_time=(start + timedelta(hours=hour)).isoformat()
                        )
                        for hour in range(25)
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_dashboard_rejects_history_with_gap_over_one_hour(self):
        hours = [0] + list(range(2, 25))
        with self.assertRaises(BackendError) as caught:
            dashboard_request(
                {
                    "current": make_sample(
                        data_time="2026-08-25T00:00:00+08:00"
                    ),
                    "trend_samples": [
                        make_sample(
                            data_time=(
                                datetime.fromisoformat(
                                    "2026-08-24T00:00:00+08:00"
                                )
                                + timedelta(hours=hour)
                            ).isoformat()
                        )
                        for hour in hours
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "invalid_time_series")

    def test_dashboard_events_coalesce_continuous_quality_intervals(self):
        results = [
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T00:00:00+08:00",
                    pv_dc_power=100,
                    pv_ac_power=100,
                    load_power=10,
                )
            ),
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T01:00:00+08:00",
                    pv_dc_power=100,
                    pv_ac_power=100,
                    load_power=10,
                )
            ),
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T02:00:00+08:00",
                    pv_dc_power=100,
                    pv_ac_power=100,
                    load_power=100,
                )
            ),
        ]
        events = [
            event
            for event in _dashboard_events(results)
            if event["quality_code"] == "power_balance_error"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["start"], results[0]["data_time"])
        self.assertEqual(events[0]["end"], results[2]["data_time"])
        self.assertEqual(events[0]["status"], "已恢复")

    def test_dashboard_events_keep_different_devices_separate(self):
        base_values = {
            "pv_dc_power": 100,
            "pv_ac_power": 96,
            "load_power": 96,
        }
        results = [
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T00:00:00+08:00",
                    device_status={"pv_inverter": {"status": "offline"}},
                    **base_values,
                )
            ),
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T01:00:00+08:00",
                    device_status={"pcs": {"status": "offline"}},
                    **base_values,
                )
            ),
            calculate_bus(
                make_sample(
                    data_time="2026-08-24T02:00:00+08:00",
                    **base_values,
                )
            ),
        ]
        events = [
            event
            for event in _dashboard_events(results)
            if event["quality_code"] == "device_abnormal"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event["device"] for event in events},
            {"pv_inverter.status", "pcs.status"},
        )

    def test_executing_pure_module_has_no_cli_side_effects(self):
        completed = subprocess.run(
            [sys.executable, str(BACKEND_PATH)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
