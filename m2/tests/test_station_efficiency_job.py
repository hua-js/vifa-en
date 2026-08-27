import json
import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from m2.station_efficiency_job import cleanup_device_history, process_station_minute
from m2.station_efficiency_device_adapter import StationEfficiencyDeviceDataError
from m2.station_efficiency_nocobase import StationEfficiencyStoreError
from m2.station_energy_data_adapter import StationEnergyDataError


TIMESTAMP = "2026-08-26T01:30:00Z"
ES02_SNS = ("emu21", "emu22", "emu23", "emu24", "emu25", "emu26")
INVERTER_SNS = (
    "emu1", "emu2", "emu3", "emu4", "emu5", "emu21", "emu22", "emu23",
    "emu24",
)


def make_balanced_es02_rows(temperature=None):
    cabinet_power = (-20, -20, -20, -20, -10, -10)
    battery_power = (-18000, -18000, -18000, -18000, -9000, -9000)
    pcs2_power = (-8, -8, -8, -8, -8, -7)
    rows = []
    for index, emu_sn in enumerate(ES02_SNS):
        is_master = emu_sn == "emu26"
        rows.append({
            "f_es_sn": "ES02", "emu_sn": emu_sn,
            "last_time_iso": TIMESTAMP, "latest_power": cabinet_power[index],
            "battery_power": battery_power[index], "pcs1_power": -8,
            "pcs2_power": pcs2_power[index], "load_power": None,
            "latest_grid_power": -10000 if is_master else 0,
            "input_ac_solar_power": None, "latest_solar_power": None,
            "acpv_rated_power": None,
            "max_cell_temperature": temperature,
        })
    rows.append({
        "f_es_sn": "ES02", "emu_sn": "emu27", "last_time_iso": TIMESTAMP,
        "latest_power": None, "battery_power": None, "pcs1_power": None,
        "pcs2_power": None, "load_power": None, "latest_grid_power": None,
        "input_ac_solar_power": 200, "latest_solar_power": 190,
        "acpv_rated_power": 1000,
    })
    return rows


def make_growall_rows():
    return [
        {"sn": serial, "timestamp": "2026-08-26T09:30:00+08:00", "a35": 10}
        for serial in INVERTER_SNS
    ]


RULE = {
    "station_id": "ES02", "enabled": True,
    "inverter_min_running_power_kw": 5,
    "inverter_low_load_threshold_pct": 20,
    "inverter_trigger_minutes": 3, "inverter_recovery_minutes": 2,
    "temperature_rise_window_minutes": 5,
    "temperature_rise_threshold_c": 3,
    "temperature_trigger_minutes": 2, "temperature_recovery_minutes": 2,
    "chain_low_efficiency_threshold_pct": 85,
    "chain_low_efficiency_trigger_minutes": 2,
    "chain_low_efficiency_recovery_minutes": 2,
    "version": 1, "updated_at": "2026-08-26T09:00:00+08:00",
}

CONFIG = {
    "emu_url": "https://station.example/api/t_emu:list?filter=%7B%7D",
    "emu_token": "source-secret",
    "growall_url": "https://station.example/api/t_growall:list?filter=%7B%7D",
    "growall_token": "growall-secret",
    "nocobase_base_url": "https://vifa.hlszh.com",
    "nocobase_token": "store-secret",
    "timeout_seconds": 7, "timezone": "Asia/Shanghai",
    "pv_inverter_sns_by_station": {"ES02": INVERTER_SNS},
    "battery_max_temperature_field": "max_cell_temperature",
    "bottleneck_rule": RULE,
}


def historical_minute():
    return {
        "station_id": "ES02", "data_time": "2026-08-26T09:29:00+08:00",
        "pv_storage_efficiency": 80, "storage_load_efficiency": None,
        "pv_load_efficiency": 100,
    }


def timestamp(minute):
    return f"2026-08-26T09:{minute:02d}:00+08:00"


def formal_history_minutes():
    return [
        {
            "station_id": "ES02", "data_time": timestamp(minute),
            "pv_storage_efficiency": 90, "storage_load_efficiency": None,
            "pv_load_efficiency": 100,
        }
        for minute in range(24, 30)
    ]


def formal_history_devices():
    inverter = [
        {
            "station_id": "ES02", "device_type": "pv_inverter",
            "device_id": "emu1", "device_name": "光伏逆变器 emu1",
            "data_time": timestamp(minute), "source_time": timestamp(minute),
            "active_power_kw": 10, "rated_power_kw": 60,
            "load_rate_pct": 10 / 60 * 100, "battery_power_kw": None,
            "temperature_c": None,
        }
        for minute in (28, 29)
    ]
    battery = [
        {
            "station_id": "ES02", "device_type": "battery_cabinet",
            "device_id": "emu21", "device_name": "电池柜 emu21",
            "subdevice_id": None, "data_time": timestamp(minute),
            "source_time": timestamp(minute), "active_power_kw": None,
            "rated_power_kw": None, "load_rate_pct": None,
            "battery_power_kw": -18, "temperature_c": minute - 4,
        }
        for minute in range(24, 30)
    ]
    return inverter + battery


class StationEfficiencyJobTests(unittest.TestCase):
    def make_query_request(
        self, *, minute_rows=None, device_rows=None, active_events=None, calls=None,
    ):
        minute_rows = formal_history_minutes() if minute_rows is None else minute_rows
        device_rows = formal_history_devices() if device_rows is None else device_rows
        active_events = [] if active_events is None else active_events

        def query_request(url, token, timeout):
            if calls is not None:
                calls.append(url)
            if "/t_efficiency_points:list?" in url:
                return {"data": minute_rows}
            if "/t_efficiency_device_points:list?" in url:
                return {"data": device_rows}
            if "/t_efficiency_bottleneck_events:list?" in url:
                return {"data": active_events, "meta": {"totalPage": 1}}
            self.fail(f"unexpected query URL: {url}")

        return query_request

    def test_processes_single_emu_fetch_through_device_and_event_flow(self):
        emu_calls, growall_calls, store_calls = [], [], []
        query_calls = []

        def emu_request(url, token, timeout):
            emu_calls.append((url, token, timeout))
            return {"data": make_balanced_es02_rows(temperature=26)}

        def growall_request(url, token, timeout):
            growall_calls.append((url, token, timeout))
            return {"data": make_growall_rows()}

        def store_request(url, token, timeout, body):
            store_calls.append((url, token, timeout, body))
            return {"data": {"id": len(store_calls), **body}}

        output = process_station_minute(
            "ES02", CONFIG, source_request_json=emu_request,
            growall_request_json=growall_request,
            query_request_json=self.make_query_request(calls=query_calls),
            store_request_json=store_request,
        )

        self.assertEqual(len(emu_calls), 1)
        self.assertEqual(len(growall_calls), 1)
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["device_points_saved"], 15)
        self.assertEqual(output["warnings"], [])
        updates = {(event["event_type"], event["device_id"]): event for event in output["event_updates"]}
        self.assertEqual(updates[("inverter_low_load", "emu1")]["start_time"], timestamp(28))
        self.assertEqual(updates[("battery_temperature_rise", "emu21")]["start_time"], timestamp(29))
        self.assertEqual(output["minute_point"]["station_id"], "ES02")
        self.assertEqual(output["saved_record"]["id"], 1)
        self.assertEqual(len(store_calls), 1 + 15 + len(output["event_updates"]))
        self.assertNotIn("source-secret", repr(output))
        self.assertNotIn("growall-secret", repr(output))
        self.assertNotIn("store-secret", repr(output))
        minute_filter = json.loads(parse_qs(urlsplit(query_calls[0]).query)["filter"][0])
        self.assertEqual(
            minute_filter["$and"][1]["data_time"]["$gte"],
            "2026-08-26T01:24:00+00:00",
        )

    def test_missing_bottleneck_rule_is_a_sanitized_partial_after_minute_save(self):
        store_calls = []
        output = process_station_minute(
            "ES02", {key: value for key, value in CONFIG.items() if key != "bottleneck_rule"},
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: {"data": make_growall_rows()},
            query_request_json=lambda *args: self.fail("无规则时不应读取事件历史"),
            store_request_json=lambda *args: store_calls.append(args) or {"data": {"id": 1}},
        )

        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["warnings"], [{
            "stage": "bottleneck_rule", "code": "bottleneck_rule_unavailable",
            "message": "瓶颈规则不可用，已跳过事件评估",
        }])
        self.assertEqual(len(store_calls), 16)

    def test_invalid_bottleneck_rule_is_a_sanitized_partial_after_minute_save(self):
        invalid_config = {**CONFIG, "bottleneck_rule": {
            "station_id": "ES02", "enabled": True, "secret": "must-not-escape",
        }}
        output = process_station_minute(
            "ES02", invalid_config,
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: {"data": make_growall_rows()},
            query_request_json=lambda *args: self.fail("非法规则时不应读取事件历史"),
            store_request_json=lambda *args: {"data": {"id": 1}},
        )

        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["warnings"], [{
            "stage": "bottleneck_rule", "code": "bottleneck_rule_unavailable",
            "message": "瓶颈规则不可用，已跳过事件评估",
        }])
        self.assertNotIn("must-not-escape", repr(output))

    def test_growall_failure_keeps_station_and_battery_points(self):
        saved_bodies = []

        def store_request(url, token, timeout, body):
            saved_bodies.append(body)
            return {"data": {"id": len(saved_bodies), **body}}

        output = process_station_minute(
            "ES02", CONFIG,
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: (_ for _ in ()).throw(
                StationEfficiencyDeviceDataError("growall-secret must not escape"),
            ),
            query_request_json=self.make_query_request(),
            store_request_json=store_request,
        )

        saved_devices = [body for body in saved_bodies if "device_type" in body]
        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["device_points_saved"], 6)
        self.assertEqual({body["device_type"] for body in saved_devices}, {"battery_cabinet"})
        self.assertTrue(output["warnings"])
        self.assertNotIn("growall-secret", repr(output["warnings"]))

    def test_device_upsert_failure_keeps_other_device_writes(self):
        saved_device_ids = []

        def store_request(url, token, timeout, body):
            if "device_type" in body:
                if body["device_id"] == "emu1":
                    raise StationEfficiencyStoreError("device write failed")
                saved_device_ids.append(body["device_id"])
            return {"data": {"id": len(saved_device_ids) + 1, **body}}

        output = process_station_minute(
            "ES02", CONFIG,
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: {"data": make_growall_rows()},
            query_request_json=self.make_query_request(),
            store_request_json=store_request,
        )

        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["device_points_saved"], 14)
        self.assertNotIn("emu1", saved_device_ids)
        self.assertEqual(len(saved_device_ids), 14)
        self.assertIsNotNone(output["saved_record"])

    def test_event_upsert_failure_keeps_station_and_device_writes(self):
        saved_device_count = 0
        saved_minute_count = 0

        def store_request(url, token, timeout, body):
            nonlocal saved_device_count, saved_minute_count
            if "event_type" in body:
                raise StationEfficiencyStoreError("event write failed")
            if "device_type" in body:
                saved_device_count += 1
            else:
                saved_minute_count += 1
            return {"data": {"id": saved_device_count + saved_minute_count, **body}}

        output = process_station_minute(
            "ES02", CONFIG,
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: {"data": make_growall_rows()},
            query_request_json=self.make_query_request(),
            store_request_json=store_request,
        )

        self.assertEqual(output["status"], "partial")
        self.assertEqual(saved_minute_count, 1)
        self.assertEqual(saved_device_count, 15)
        self.assertEqual(output["device_points_saved"], 15)
        self.assertGreaterEqual(len(output["event_updates"]), 1)

    def test_empty_battery_temperatures_are_not_configuration_errors(self):
        output = process_station_minute(
            "ES02", CONFIG,
            source_request_json=lambda *args: {"data": make_balanced_es02_rows()},
            growall_request_json=lambda *args: {"data": make_growall_rows()},
            query_request_json=self.make_query_request(),
            store_request_json=lambda url, token, timeout, body: {"data": {"id": 1, **body}},
        )

        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["warnings"], [])

    def test_invalid_emu_source_raises_before_any_write(self):
        rows = make_balanced_es02_rows()
        rows[0]["pcs1_power"] = None
        writes = []

        with self.assertRaisesRegex(StationEnergyDataError, "pcs1_power"):
            process_station_minute(
                "ES02", CONFIG,
                source_request_json=lambda *args: {"data": rows},
                store_request_json=lambda *args: writes.append(args) or {"data": {"id": 1}},
            )

        self.assertEqual(writes, [])

    def test_cleanup_uses_configured_retention_days_in_business_timezone(self):
        calls = []
        result = cleanup_device_history(
            {**CONFIG, "device_point_retention_days": 31},
            now=datetime(2026, 8, 27, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
            request_json=lambda *args: calls.append(args) or {"data": 7},
        )

        self.assertEqual(result, {
            "cutoff": "2026-07-27T10:15:00+08:00", "deleted_count": 7,
        })
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
