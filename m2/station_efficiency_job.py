"""Execute one station efficiency minute and its best-effort bottleneck work."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from m2.station_efficiency_bottlenecks import evaluate_station_bottlenecks
from m2.station_efficiency_device_adapter import (
    StationEfficiencyDeviceDataError,
    build_battery_device_points,
    build_inverter_device_points,
    fetch_growall_rows,
)
from m2.station_efficiency_history import (
    HistoryError,
    build_minute_point,
    minute_bucket,
    normalize_rule,
)
from m2.station_efficiency_nocobase import (
    StationEfficiencyStoreError,
    delete_device_points_before,
    fetch_active_events,
    fetch_device_points,
    fetch_minute_points,
    save_bottleneck_event,
    save_device_point,
    save_minute_point,
)
from m2.station_energy_backend import calculate_bus
from m2.station_energy_data_adapter import (
    build_station_source_record,
    fetch_emu_rows,
    get_station_config,
)


FORMULA_VERSION = "energy-chain-v1"
_RULE_WINDOW_FIELDS = (
    "inverter_trigger_minutes",
    "inverter_recovery_minutes",
    "temperature_rise_window_minutes",
    "temperature_trigger_minutes",
    "temperature_recovery_minutes",
    "chain_low_efficiency_trigger_minutes",
    "chain_low_efficiency_recovery_minutes",
)
_BOTTLENECK_RULE_WARNING = {
    "stage": "bottleneck_rule",
    "code": "bottleneck_rule_unavailable",
    "message": "瓶颈规则不可用，已跳过事件评估",
}


def _beijing_time(value, config, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是合法时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    timezone_name = (
        config.get("timezone", "Asia/Shanghai")
        if isinstance(config, dict)
        else "Asia/Shanghai"
    )
    try:
        zone = ZoneInfo(str(timezone_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone 配置不正确") from exc
    return parsed.astimezone(zone).isoformat()


def _chain_from_result(result, metric):
    efficiency = result[metric]
    if efficiency is None:
        return {"efficiency": None, "input_kw": None, "output_kw": None}
    intermediate = result["intermediate"][metric]
    return {
        "efficiency": efficiency,
        "input_kw": intermediate["denominator_kw"],
        "output_kw": intermediate["numerator_kw"],
    }


def _minute_chains(result):
    return {
        "pv_storage": _chain_from_result(result, "pv_storage_dc_efficiency"),
        "storage_load": _chain_from_result(result, "storage_load_efficiency"),
        "pv_load": _chain_from_result(result, "pv_load_efficiency"),
    }


def _warning(stage):
    """Return a JSON-safe warning without propagating transport details or tokens."""
    return {"stage": stage, "code": "best_effort_failed"}


def _validated_bottleneck_rule(config, warnings):
    rule = config.get("bottleneck_rule") if isinstance(config, dict) else None
    try:
        return normalize_rule(rule)
    except HistoryError:
        warnings.append(dict(_BOTTLENECK_RULE_WARNING))
        return None


def _device_history_key(point):
    return (
        point.get("station_id"),
        point.get("device_type"),
        point.get("device_id"),
        minute_bucket(point.get("data_time")).astimezone(ZoneInfo("UTC")).isoformat(),
    )


def _minute_history_key(point):
    return (
        point.get("station_id"),
        minute_bucket(point.get("data_time")).astimezone(ZoneInfo("UTC")).isoformat(),
    )


def _merge_points(history, current, key):
    """Prefer the just-calculated point without relying on read-after-write consistency."""
    merged = {}
    for point in history:
        merged[key(point)] = point
    for point in current:
        merged[key(point)] = point
    return list(merged.values())


def _history_start(minute_point, rule):
    if not isinstance(rule, dict):
        return None
    values = []
    for field in _RULE_WINDOW_FIELDS:
        value = rule.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        values.append(value)
    latest = minute_bucket(minute_point["data_time"])
    return latest - timedelta(minutes=max(values) + 1)


def _save_device_points(points, config, request_json, warnings):
    saved = 0
    for point in points:
        try:
            save_device_point(point, config, request_json=request_json)
        except StationEfficiencyStoreError:
            warnings.append(_warning("device_point_save"))
        else:
            saved += 1
    return saved


def _evaluate_and_save_events(
    station_id,
    minute_point,
    current_device_points,
    rule,
    config,
    query_request_json,
    store_request_json,
    warnings,
):
    history_start = _history_start(minute_point, rule)
    if history_start is None:
        return []
    end_time = minute_bucket(minute_point["data_time"]) + timedelta(minutes=1)
    try:
        minute_history = fetch_minute_points(
            station_id, history_start.isoformat(), end_time.isoformat(), config,
            request_json=query_request_json,
        )
        device_history = fetch_device_points(
            station_id, history_start.isoformat(), end_time.isoformat(), config,
            request_json=query_request_json,
        )
        active_events = fetch_active_events(
            station_id, config, request_json=query_request_json,
        )
    except StationEfficiencyStoreError:
        warnings.append(_warning("bottleneck_history"))
        return []

    updates = evaluate_station_bottlenecks(
        station_id=station_id,
        minute_points=_merge_points(minute_history, [minute_point], _minute_history_key),
        device_points=_merge_points(device_history, current_device_points, _device_history_key),
        active_events=active_events,
        rule=rule,
    )
    for event in updates:
        try:
            save_bottleneck_event(event, config, request_json=store_request_json)
        except StationEfficiencyStoreError:
            warnings.append(_warning("bottleneck_event_save"))
    return updates


def process_station_minute(
    station_id,
    config,
    source_request_json=None,
    growall_request_json=None,
    query_request_json=None,
    store_request_json=None,
):
    """Save the station minute first, then do device and event work independently."""
    emu_rows = fetch_emu_rows(config, request_json=source_request_json)
    source = build_station_source_record(station_id=station_id, emu_rows=emu_rows)
    calculation_config = (
        config.get("calculation_config") if isinstance(config, dict) else None
    )
    calculation = calculate_bus(source, calculation_config)
    minute_point = build_minute_point(
        station_id=station_id,
        data_time=_beijing_time(calculation["data_time"], config, "data_time"),
        chains=_minute_chains(calculation),
        formula_version=FORMULA_VERSION,
        calculated_at=_beijing_time(
            calculation["calculated_at"], config, "calculated_at",
        ),
    )
    saved_record = save_minute_point(
        minute_point, config, request_json=store_request_json,
    )

    warnings = []
    device_points = []
    try:
        device_points.extend(build_battery_device_points(
            station_id=station_id,
            emu_rows=emu_rows,
            data_time=minute_point["data_time"],
            config=config,
        ))
    except StationEfficiencyDeviceDataError:
        warnings.append(_warning("battery_point_build"))

    if get_station_config(station_id)["has_pv"]:
        try:
            growall_rows = fetch_growall_rows(
                config, request_json=growall_request_json,
            )
            device_points.extend(build_inverter_device_points(
                station_id=station_id,
                growall_rows=growall_rows,
                data_time=minute_point["data_time"],
                config=config,
            ))
        except StationEfficiencyDeviceDataError:
            warnings.append(_warning("inverter_point_build"))

    device_points_saved = _save_device_points(
        device_points, config, store_request_json, warnings,
    )
    rule = _validated_bottleneck_rule(config, warnings)
    event_updates = _evaluate_and_save_events(
        station_id, minute_point, device_points, rule, config,
        query_request_json, store_request_json, warnings,
    )
    return {
        "status": "partial" if warnings else "ok",
        "warnings": warnings,
        "minute_point": minute_point,
        "saved_record": saved_record,
        "device_points_saved": device_points_saved,
        "event_updates": event_updates,
    }


def cleanup_device_history(config, now=None, request_json=None):
    """Delete expired device points using the business timezone and configured retention."""
    timezone_name = config.get("timezone", "Asia/Shanghai")
    try:
        zone = ZoneInfo(str(timezone_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone 配置不正确") from exc
    days = config.get("device_point_retention_days", 30)
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("device_point_retention_days 必须是正整数")
    current = now or datetime.now(zone)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now 必须包含时区")
    cutoff = current.astimezone(zone) - timedelta(days=days)
    deleted = delete_device_points_before(
        cutoff.isoformat(), config, request_json=request_json,
    )
    return {"cutoff": cutoff.isoformat(), "deleted_count": deleted}
