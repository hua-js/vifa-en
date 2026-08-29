import importlib.util
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from m2.station_efficiency_device_adapter import build_battery_device_points


ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "energy-efficiency-api.py"
SPEC = importlib.util.spec_from_file_location("energy_efficiency_api", ENTRYPOINT_PATH)
energy_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(energy_api)

ENVIRONMENT = {
    "VIFA_EMU_URL": "https://station.example/api/t_emu:list?filter=%7B%7D",
    "VIFA_EMU_TOKEN": "source-secret",
    "M2_NOCOBASE_BASE_URL": "https://station.example",
    "M2_NOCOBASE_TOKEN": "store-secret",
    "M2_TIMEZONE": "Asia/Shanghai",
    "M2_REQUEST_TIMEOUT_SECONDS": "10",
}


class EnergyEfficiencyApiTests(unittest.TestCase):
    def test_default_runtime_config_maps_t_emu_max_temp_safely(self):
        clean_spec = importlib.util.spec_from_file_location(
            "energy_efficiency_api_without_local_config",
            ENTRYPOINT_PATH,
        )
        clean_api = importlib.util.module_from_spec(clean_spec)
        with patch.dict(sys.modules, {"energy_efficiency_local_config": None}):
            clean_spec.loader.exec_module(clean_api)

        points = build_battery_device_points(
            station_id="ES01",
            emu_rows=[
                {
                    "f_es_sn": "ES01",
                    "emu_sn": "emu11",
                    "last_time_iso": "2026-08-29T10:00:00+08:00",
                    "battery_power": -18000,
                    "max_temp": 31.5,
                },
                {
                    "f_es_sn": "ES01",
                    "emu_sn": "emu12",
                    "last_time_iso": "2026-08-29T10:00:00+08:00",
                    "battery_power": -17000,
                    "max_temp": "not-a-temperature",
                },
            ],
            minute_bucket_time="2026-08-29T10:00:00+08:00",
            calculation_time="2026-08-29T10:00:00+08:00",
            config=clean_api.load_runtime_config(ENVIRONMENT),
        )

        self.assertEqual(points[0]["temperature_c"], 31.5)
        self.assertIsNone(points[0]["subdevice_id"])
        self.assertIsNone(points[1]["temperature_c"])

    def test_parse_request_accepts_cleanup_only_without_station(self):
        self.assertEqual(energy_api.parse_request(["cleanup"]), ("cleanup", None))
        for invalid in (["cleanup", "ES02"], ["minute"], ["dashboard", "ES03"]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(energy_api.EntrypointError):
                    energy_api.parse_request(invalid)

    def test_runtime_and_station_rule_configs_are_isolated_between_calls(self):
        original_rules = copy.deepcopy(energy_api.LOCAL_BOTTLENECK_RULES)
        self.addCleanup(
            setattr,
            energy_api,
            "LOCAL_BOTTLENECK_RULES",
            original_rules,
        )

        first_runtime = energy_api.load_runtime_config(ENVIRONMENT)
        first_station = energy_api._config_for_station(first_runtime, "ES02")
        first_station["bottleneck_rule"]["chain_low_efficiency_threshold_pct"] = 1
        second_runtime = energy_api.load_runtime_config(ENVIRONMENT)
        second_station = energy_api._config_for_station(second_runtime, "ES02")

        self.assertEqual(
            first_runtime["bottleneck_rules"]["ES02"]["chain_low_efficiency_threshold_pct"],
            85,
        )
        self.assertEqual(
            second_runtime["bottleneck_rules"]["ES02"]["chain_low_efficiency_threshold_pct"],
            85,
        )
        self.assertEqual(
            second_station["bottleneck_rule"]["chain_low_efficiency_threshold_pct"],
            85,
        )

    def test_runtime_config_rejects_non_shanghai_timezone(self):
        with self.assertRaises(energy_api.EntrypointError):
            energy_api.load_runtime_config({**ENVIRONMENT, "M2_TIMEZONE": "UTC"})

    def test_runtime_config_accepts_only_fixed_60_kw_inverter_rating(self):
        self.assertEqual(
            energy_api.load_runtime_config({
                **ENVIRONMENT,
                "M2_PV_INVERTER_RATED_POWER_KW": "60.0",
            })["pv_inverter_rated_power_kw"],
            60.0,
        )
        for invalid in ("59.9", "61", "not-a-number"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(energy_api.EntrypointError) as caught:
                    energy_api.load_runtime_config({
                        **ENVIRONMENT,
                        "M2_PV_INVERTER_RATED_POWER_KW": invalid,
                    })
                self.assertEqual(caught.exception.code, "missing_config")
                self.assertNotIn(invalid, str(caught.exception))

    def test_partial_minute_sanitizes_untrusted_warning_details(self):
        secret = "growall-token-should-not-escape"
        endpoint = "https://secret.example/api/t_growall:list"
        payload, exit_code = energy_api.execute(
            ["minute", "ES02"],
            ENVIRONMENT,
            process_minute=lambda station_id, config: {
                "status": "partial",
                "warnings": [{
                    "stage": f"upstream:{endpoint}",
                    "code": f"upstream:{secret}",
                    "message": f"request {endpoint} used {secret}",
                    "exception": RuntimeError(secret),
                }],
                "minute_point": {"data_time": "2026-08-27T10:00:00+08:00"},
                "saved_record": {"id": 1}, "device_points_saved": 6,
                "event_updates": [],
                "event_persistence": {
                    "attempted": 3, "saved": 2, "failed": 1,
                    "outbox_pending": 1,
                },
            },
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["data"]["warnings"], [{
            "stage": "minute_job",
            "code": "unknown_warning",
            "message": "分钟任务部分处理失败",
        }])
        self.assertNotIn(secret, repr(payload))
        self.assertNotIn(endpoint, repr(payload))

    def test_minute_uses_environment_config_and_official_default_rule(self):
        received_config = {}
        growall_token = "test-growall-token"

        def process_minute(station_id, config):
            received_config.update(config)
            return {
                "status": "ok", "warnings": [],
                "minute_point": {"data_time": "2026-08-27T10:00:00+08:00"},
                "saved_record": {"id": 81}, "device_points_saved": 0,
                "event_updates": [],
            }

        payload, exit_code = energy_api.execute(
            ["minute", "ES02"],
            {
                **ENVIRONMENT,
                "VIFA_GROWALL_URL": "https://station.example/api/t_growall:list",
                "VIFA_GROWALL_TOKEN": growall_token,
                "M2_DEVICE_POINT_RETENTION_DAYS": "31",
            },
            process_minute=process_minute,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            received_config["growall_url"],
            "https://station.example/api/t_growall:list",
        )
        self.assertEqual(received_config["device_point_retention_days"], 31)
        self.assertEqual(
            received_config["event_outbox_path"],
            energy_api.DEFAULT_EVENT_OUTBOX_PATH,
        )
        self.assertEqual(
            Path(received_config["event_outbox_path"]).parent,
            ENTRYPOINT_PATH.parent,
        )
        self.assertEqual(received_config["bottleneck_rule"], {
            "station_id": "ES02", "enabled": True,
            "chain_low_efficiency_threshold_pct": 85,
            "chain_low_efficiency_trigger_minutes": 2,
            "chain_low_efficiency_recovery_minutes": 2,
            "inverter_min_running_power_kw": 5,
            "inverter_low_load_threshold_pct": 20,
            "inverter_trigger_minutes": 3,
            "inverter_recovery_minutes": 2,
            "temperature_rise_window_minutes": 5,
            "temperature_rise_threshold_c": 3,
            "temperature_trigger_minutes": 2,
            "temperature_recovery_minutes": 2,
            "version": 1,
            "updated_at": "2026-08-27T00:00:00+08:00",
        })
        self.assertNotIn(ENVIRONMENT["VIFA_EMU_TOKEN"], repr(payload))
        self.assertNotIn(ENVIRONMENT["M2_NOCOBASE_TOKEN"], repr(payload))
        self.assertNotIn(growall_token, repr(payload))

    def test_partial_minute_returns_exit_zero_and_warning_counts(self):
        payload, exit_code = energy_api.execute(
            ["minute", "ES02"], ENVIRONMENT,
            process_minute=lambda station_id, config: {
                "status": "partial", "warnings": [{"code": "growall_unavailable"}],
                "minute_point": {"data_time": "2026-08-27T10:00:00+08:00"},
                "saved_record": {"id": 1}, "device_points_saved": 6,
                "event_updates": [],
                "event_persistence": {
                    "attempted": 3, "saved": 2, "failed": 1,
                    "outbox_pending": 1,
                },
            },
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["data"]["device_points_saved"], 6)
        self.assertEqual(payload["data"]["warning_count"], 1)
        self.assertEqual(payload["data"]["event_update_count"], 0)
        self.assertEqual(payload["data"]["event_persistence"], {
            "attempted": 3, "saved": 2, "failed": 1, "outbox_pending": 1,
        })

    def test_cleanup_returns_deleted_count(self):
        payload, exit_code = energy_api.execute(
            ["cleanup"], ENVIRONMENT,
            cleanup_history=lambda config: {
                "cutoff": "2026-07-28T10:00:00+08:00", "deleted_count": 12,
            },
        )

        self.assertEqual((payload["status"], exit_code), ("ok", 0))
        self.assertEqual(payload["data"]["operation"], "cleanup")
        self.assertEqual(payload["data"]["deleted_count"], 12)

    def test_minute_command_saves_one_station_and_returns_public_json_result(self):
        def process_minute(station_id, config):
            return {
                "status": "ok",
                "warnings": [],
                "minute_point": {
                    "station_id": station_id,
                    "data_time": "2026-08-26T09:30:00+08:00",
                },
                "saved_record": {"id": 81},
                "device_points_saved": 2,
                "event_updates": [{"event_type": "chain_low_efficiency"}],
                "event_persistence": {
                    "attempted": 1, "saved": 1, "failed": 0,
                    "outbox_pending": 0,
                },
            }

        payload, exit_code = energy_api.execute(
            ["minute", "ES01"],
            ENVIRONMENT,
            process_minute=process_minute,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload,
            {
                "status": "ok",
                "data": {
                    "operation": "minute",
                    "station_id": "ES01",
                    "data_time": "2026-08-26T09:30:00+08:00",
                    "minute_point": {
                        "station_id": "ES01",
                        "data_time": "2026-08-26T09:30:00+08:00",
                    },
                    "saved_id": 81,
                    "device_points_saved": 2,
                    "event_update_count": 1,
                    "event_persistence": {
                        "attempted": 1,
                        "saved": 1,
                        "failed": 0,
                        "outbox_pending": 0,
                    },
                    "warning_count": 0,
                    "warnings": [],
                },
            },
        )

    def test_dashboard_reads_today_points_and_returns_beijing_time_curve(self):
        point = {
            "station_id": "ES02",
            "data_time": "2026-08-26T09:30:00+08:00",
            "pv_storage_efficiency": 88.5,
            "storage_load_efficiency": None,
            "pv_load_efficiency": 100.0,
            "pv_storage_input_kw": 100.0,
            "pv_storage_output_kw": 88.5,
            "storage_load_input_kw": None,
            "storage_load_output_kw": None,
            "pv_load_input_kw": 80.0,
            "pv_load_output_kw": 80.0,
            "formula_version": "energy-chain-v1",
            "calculated_at": "2026-08-26T09:30:03+08:00",
        }

        def fetch_station(station_id, config):
            return {
                "bus_id": station_id,
                "data_time": "2026-08-26T01:30:27Z",
                "source_times": {
                    "emu:emu21": "2026-08-26T01:30:26Z",
                },
            }

        def calculate(source):
            return {
                "bus_id": source["bus_id"],
                "data_time": source["data_time"],
                "calculated_at": "2026-08-26T01:30:29Z",
                "calculation_mode": "estimated",
            }

        def fetch_points(station_id, start_time, end_time, config):
            if (
                station_id,
                start_time,
                end_time,
            ) == (
                "ES02",
                "2026-08-26T00:00:00+08:00",
                "2026-08-27T00:00:00+08:00",
            ):
                return [point]
            return []

        active_event = {
            "id": 101, "station_id": "ES02",
            "event_type": "chain_low_efficiency", "device_id": "pv_storage",
            "device_name": "光→储", "start_time": "2026-08-25T23:58:00+08:00",
            "end_time": None, "last_seen_time": "2026-08-26T09:30:00+08:00",
            "status": "active", "observed_value": 80, "threshold_value": 85,
            "observed_unit": "%", "impact_chain": ["光→储"],
            "evidence": {
                "display_text": "光→储效率最低 80.00%，低于阈值 85.00%",
                "cause_status": "pending", "diagnosed_causes": [],
                "trigger_device_snapshot": {"pv_inverters": []},
            },
        }
        recovered_event = {
            **active_event, "id": 102, "event_type": "battery_temperature_rise",
            "device_id": "emu21", "device_name": "电池柜 emu21",
            "start_time": "2026-08-26T08:20:00+08:00",
            "end_time": "2026-08-26T09:20:00+08:00", "status": "recovered",
            "impact_chain": ["光→储", "储→用"],
            "evidence": {
                "display_text": "5 分钟最大温升 4.00℃",
                "trigger_device_snapshot": {"battery_cabinets": [{"device_id": "emu21", "temperature_c": None}]},
            },
        }
        fetch_event_calls = []

        def fetch_events(station_id, start_time, end_time, config):
            fetch_event_calls.append((station_id, start_time, end_time))
            return [active_event, recovered_event]

        payload, exit_code = energy_api.execute(
            ["dashboard", "ES02"],
            ENVIRONMENT,
            fetch_station=fetch_station,
            calculate=calculate,
            fetch_points=fetch_points,
            fetch_events=fetch_events,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["range"]["timezone"], "Asia/Shanghai")
        self.assertEqual(
            payload["data"]["realtime"]["inputs"]["data_time"],
            "2026-08-26T09:30:27+08:00",
        )
        self.assertEqual(
            payload["data"]["realtime"]["inputs"]["source_times"],
            {"emu:emu21": "2026-08-26T09:30:26+08:00"},
        )
        self.assertEqual(
            payload["data"]["realtime"]["result"]["calculated_at"],
            "2026-08-26T09:30:29+08:00",
        )
        self.assertEqual(
            payload["data"]["trend"],
            [{
                "data_time": "2026-08-26T09:30:00+08:00",
                "pvStorage": 88.5,
                "storageLoad": None,
                "pvLoad": 100.0,
            }],
        )
        self.assertEqual(fetch_event_calls, [(
            "ES02", "2026-08-26T00:00:00+08:00", "2026-08-27T00:00:00+08:00",
        )])
        self.assertEqual([event["id"] for event in payload["data"]["events"]], [101, 102])
        self.assertEqual(payload["data"]["events"][0]["cause_status"], "pending")
        self.assertEqual(
            payload["data"]["events"][1]["trigger_device_snapshot"],
            {"battery_cabinets": [{"device_id": "emu21", "temperature_c": None}]},
        )


if __name__ == "__main__":
    unittest.main()
