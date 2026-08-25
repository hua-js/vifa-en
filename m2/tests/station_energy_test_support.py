"""Test-only sample builders for station energy calculations."""

from datetime import datetime, timedelta

from m2.station_energy_backend import POWER_FIELDS, dashboard_request


def make_sample(**overrides):
    sample = {field: 0.0 for field in POWER_FIELDS}
    sample.update(
        {
            "bus_id": "bus-1",
            "data_time": "2026-08-24T12:00:00+08:00",
            "device_status": {},
            "source_times": {},
        }
    )
    sample.update(overrides)
    return sample


def build_dashboard_request():
    base = datetime.fromisoformat("2026-08-23T12:00:00+08:00")
    trend_samples = []
    for hour in range(25):
        sample_time = base + timedelta(hours=hour)
        data_time = sample_time.isoformat()
        if sample_time.hour < 7 or sample_time.hour >= 19:
            sample = make_sample(
                data_time=data_time,
                load_power=90,
                cabinet_discharge_power=92,
                pcs_discharge_power=95,
                bms_discharge_power=100,
            )
        else:
            sample = make_sample(
                data_time=data_time,
                pv_dc_power=190,
                pv_ac_power=183,
                load_power=80,
                cabinet_charge_power=103,
                pcs_charge_power=100,
                bms_charge_power=95,
            )
        trend_samples.append(sample)
    return {
        "operation": "dashboard",
        "current": trend_samples[-1],
        "trend_samples": trend_samples,
    }


def build_dashboard_response():
    return dashboard_request(build_dashboard_request())
