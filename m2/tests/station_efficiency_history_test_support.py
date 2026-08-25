from datetime import datetime, timedelta

from m2.station_efficiency_history import build_calendar_day_dashboard, build_minute_point
from m2.tests.station_energy_test_support import build_dashboard_response


def _chains(index):
    return {
        "pv_storage": {"efficiency": 88.0 + index * 0.02, "input_kw": 100.0, "output_kw": 88.0 + index * 0.02},
        "storage_load": {"efficiency": None if index == 1 else 90.0, "input_kw": None if index == 1 else 100.0, "output_kw": None if index == 1 else 90.0},
        "pv_load": {"efficiency": 95.0, "input_kw": 100.0, "output_kw": 95.0},
    }


def build_history_dashboard_response():
    legacy = build_dashboard_response()
    realtime = dict(legacy["realtime"])
    realtime["inputs"] = dict(realtime["inputs"])
    realtime["result"] = dict(realtime["result"])
    realtime["inputs"]["data_time"] = "2026-08-25T14:36:00+08:00"
    realtime["result"]["data_time"] = "2026-08-25T14:36:00+08:00"
    start = datetime.fromisoformat("2026-08-25T14:27:00+08:00")
    minutes = [0, 1, 2, 6, 7, 8, 9]
    points = [
        build_minute_point(
            "station-1",
            (start + timedelta(minutes=offset)).isoformat(),
            _chains(index),
            "energy-chain-v1",
            (start + timedelta(minutes=offset, seconds=2)).isoformat(),
        )
        for index, offset in enumerate(minutes)
    ]
    events = [
        {
            "id": 1, "station_id": "station-1", "event_type": "inverter_low_load",
            "device_id": "inv-1", "device_name": "1#逆变器",
            "start_time": "2026-08-25T14:30:00+08:00", "end_time": None,
            "last_seen_time": "2026-08-25T14:36:00+08:00", "status": "active",
            "observed_value": 12.4, "threshold_value": 20.0, "observed_unit": "%",
            "evidence": {"display_text": "负载率最低 12.40%"},
            "impact_chain": ["光→储", "光→用"], "rule_version": 4,
        },
        {
            "id": 2, "station_id": "station-1", "event_type": "battery_temperature_rise",
            "device_id": "battery-1", "device_name": "1#电池簇",
            "start_time": "2026-08-24T23:50:00+08:00", "end_time": "2026-08-25T00:10:00+08:00",
            "last_seen_time": "2026-08-25T00:11:00+08:00", "status": "recovered",
            "observed_value": 3.5, "threshold_value": 3.0, "observed_unit": "℃",
            "evidence": {"display_text": "5 分钟最大温升 3.50℃"},
            "impact_chain": ["光→储", "储→用"], "rule_version": 4,
        },
    ]
    return build_calendar_day_dashboard(
        "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
        realtime, points, events,
    )
