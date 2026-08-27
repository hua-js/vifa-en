"""Pure station-level coordination for device and chain bottleneck rules."""

import json
from datetime import timedelta

from m2.station_efficiency_history import (
    HistoryError,
    evaluate_battery_temperature_rise,
    evaluate_chain_low_efficiency,
    evaluate_inverter_low_load,
    minute_bucket,
    normalize_rule,
    SHANGHAI_TIMEZONE,
    time_in_zone,
)


CHAIN_META = {
    "pv_storage": ("光→储", "pv_storage_efficiency", ("pv_inverter", "battery_cabinet")),
    "storage_load": ("储→用", "storage_load_efficiency", ("battery_cabinet",)),
    "pv_load": ("光→用", "pv_load_efficiency", ("pv_inverter",)),
}

_DEVICE_EVENT_TYPES = {
    "pv_inverter": "inverter_low_load",
    "battery_cabinet": "battery_temperature_rise",
}
_KNOWN_EVENT_TYPES = frozenset((*_DEVICE_EVENT_TYPES.values(), "chain_low_efficiency"))
_EPHEMERAL_EVENT_FIELDS = frozenset(("id", "created_at", "updated_at", "createdAt", "updatedAt"))


def _station_identifier(value, field):
    if value is None or isinstance(value, bool) or not str(value).strip():
        raise HistoryError("invalid_station_data", f"{field} 不能为空", {"field": field})
    return str(value).strip()


def _validate_station_rows(rows, station_id, row_kind):
    if not isinstance(rows, list):
        raise HistoryError("invalid_station_data", f"{row_kind} 必须是数组")
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryError("invalid_station_data", f"{row_kind} 每条记录必须是对象")
        if _station_identifier(row.get("station_id"), "station_id") != station_id:
            raise HistoryError("invalid_station_data", "输入数据必须属于指定场站", {"field": "station_id"})


def _active_by_identity(active_events, station_id):
    _validate_station_rows(active_events, station_id, "active_events")
    active_by_identity = {}
    for event in active_events:
        event_type = event.get("event_type")
        if event_type not in _KNOWN_EVENT_TYPES:
            raise HistoryError("invalid_active_event", "活动事件类型不支持", {"field": "event_type"})
        device_id = _station_identifier(event.get("device_id"), "device_id")
        if event_type == "chain_low_efficiency" and device_id not in CHAIN_META:
            raise HistoryError("invalid_active_event", "活动链路事件设备不支持", {
                "field": "device_id",
            })
        identity = (event_type, device_id)
        if identity in active_by_identity:
            raise HistoryError("duplicate_active_event", "同一活动事件只能存在一条", {
                "event_type": event_type,
                "device_id": device_id,
            })
        active_by_identity[identity] = event
    return active_by_identity


def _group_device_points(device_points, station_id):
    _validate_station_rows(device_points, station_id, "device_points")
    grouped = {}
    for point in device_points:
        device_type = point.get("device_type")
        if device_type not in _DEVICE_EVENT_TYPES:
            raise HistoryError("invalid_device_point", "device_type 不支持", {"field": "device_type"})
        device_id = _station_identifier(point.get("device_id"), "device_id")
        grouped.setdefault((device_type, device_id), []).append(point)
    return grouped


def _shanghai_minute(value):
    return minute_bucket(value).astimezone(SHANGHAI_TIMEZONE).isoformat()


def _shanghai_time(value, field):
    return time_in_zone(value, "Asia/Shanghai", field)


def _inverter_samples(points):
    return [{
        "device_id": point.get("device_id"),
        "device_name": point.get("device_name"),
        "data_time": _shanghai_minute(point.get("data_time")),
        "active_power_kw": point.get("active_power_kw"),
        "rated_power_kw": point.get("rated_power_kw"),
    } for point in points]


def _battery_samples(points):
    return [{
        "device_id": point.get("device_id"),
        "device_name": point.get("device_name"),
        "data_time": _shanghai_minute(point.get("data_time")),
        "temperature_c": point.get("temperature_c"),
    } for point in points if point.get("temperature_c") is not None]


def _device_events(grouped, active_by_identity, rule):
    events = []
    for device_type, evaluator, sample_builder in (
        ("pv_inverter", evaluate_inverter_low_load, _inverter_samples),
        ("battery_cabinet", evaluate_battery_temperature_rise, _battery_samples),
    ):
        event_type = _DEVICE_EVENT_TYPES[device_type]
        device_ids = {
            device_id for point_type, device_id in grouped if point_type == device_type
        }
        device_ids.update(
            device_id for active_type, device_id in active_by_identity
            if active_type == event_type
        )
        for device_id in sorted(device_ids):
            active_event = active_by_identity.get((event_type, device_id))
            event = evaluator(
                sample_builder(grouped.get((device_type, device_id), [])),
                rule,
                active_event=active_event,
            )
            if event is not None:
                events.append(event)
    return events


def _cause_summary(event):
    return {
        "event_type": event["event_type"],
        "device_id": event["device_id"],
        "device_name": event["device_name"],
    }


def _event_confirmation_minute(event):
    evidence = event.get("evidence") if isinstance(event, dict) else None
    if isinstance(evidence, dict) and evidence.get("confirmation_time") is not None:
        return minute_bucket(evidence["confirmation_time"])
    rule = evidence.get("rule") if isinstance(evidence, dict) else None
    trigger_field = {
        "inverter_low_load": "inverter_trigger_minutes",
        "battery_temperature_rise": "temperature_trigger_minutes",
    }.get(event.get("event_type"))
    trigger_minutes = rule.get(trigger_field) if isinstance(rule, dict) and trigger_field else None
    if isinstance(trigger_minutes, bool) or not isinstance(trigger_minutes, int) or trigger_minutes <= 0:
        raise HistoryError(
            "invalid_active_event", "设备事件缺少确认时间", {"field": "evidence.confirmation_time"},
        )
    return minute_bucket(event.get("start_time")) + timedelta(minutes=trigger_minutes - 1)


def _diagnosed_causes(device_events, confirmation_time=None):
    if confirmation_time is None:
        active_causes = [
            event for event in device_events
            if event["status"] == "active"
        ]
    else:
        confirmation_minute = minute_bucket(confirmation_time)
        active_causes = []
        for event in device_events:
            event_confirmation = _event_confirmation_minute(event)
            end_time = (
                minute_bucket(event["end_time"])
                if event.get("end_time") is not None else None
            )
            if (
                event_confirmation <= confirmation_minute
                and (end_time is None or confirmation_minute < end_time)
            ):
                active_causes.append(event)
    causes_by_chain = {chain_name: [] for chain_name in CHAIN_META}
    for event in sorted(active_causes, key=lambda item: (item["event_type"], item["device_id"])):
        event_type = event["event_type"]
        for chain_name, (chain_label, _column, _types) in CHAIN_META.items():
            if chain_label in event["impact_chain"]:
                causes_by_chain[chain_name].append(_cause_summary(event))
    return causes_by_chain


def _snapshot_row(point, device_type):
    fields = (
        ("device_id", "device_name", "active_power_kw", "rated_power_kw", "load_rate_pct", "source_time", "data_time")
        if device_type == "pv_inverter" else
        ("device_id", "device_name", "subdevice_id", "temperature_c", "source_time", "data_time")
    )
    row = {field: point.get(field) for field in fields}
    if row.get("data_time") is not None:
        row["data_time"] = _shanghai_minute(row["data_time"])
    if row.get("source_time") is not None:
        row["source_time"] = _shanghai_time(row["source_time"], "source_time")
    return row


def _trigger_snapshot(grouped, device_types, confirmation_time):
    confirmation_minute = minute_bucket(confirmation_time)
    snapshot = {}
    for device_type, snapshot_key in (
        ("pv_inverter", "pv_inverters"),
        ("battery_cabinet", "battery_cabinets"),
    ):
        if device_type not in device_types:
            continue
        rows = []
        for (point_type, device_id), points in sorted(grouped.items()):
            if point_type != device_type:
                continue
            rows.extend(
                _snapshot_row(point, device_type)
                for point in points
                if minute_bucket(point.get("data_time")) == confirmation_minute
            )
        snapshot[snapshot_key] = sorted(rows, key=lambda item: item["device_id"])
    return snapshot


def _chain_samples(minute_points, chain_name):
    label, efficiency_column, _device_types = CHAIN_META[chain_name]
    return [{
        "device_id": chain_name,
        "device_name": label,
        "data_time": point.get("data_time"),
        "efficiency_pct": point.get(efficiency_column),
    } for point in minute_points]


def _chain_events(minute_points, grouped, active_by_identity, device_events, rule):
    current_causes_by_chain = _diagnosed_causes(device_events)
    events = []
    for chain_name, (_label, _column, device_types) in CHAIN_META.items():
        active_event = active_by_identity.get(("chain_low_efficiency", chain_name))
        samples = _chain_samples(minute_points, chain_name)
        event = evaluate_chain_low_efficiency(
            samples,
            rule,
            active_event=active_event,
            trigger_device_snapshot={},
            diagnosed_causes=(
                current_causes_by_chain[chain_name] if active_event is not None else []
            ),
        )
        if event is not None and active_event is None:
            confirmation_time = event["evidence"]["confirmation_time"]
            confirmation_causes = _diagnosed_causes(
                device_events, confirmation_time,
            )[chain_name]
            event["evidence"]["trigger_device_snapshot"] = _trigger_snapshot(
                grouped, device_types, confirmation_time,
            )
            event["evidence"]["diagnosed_causes"] = confirmation_causes
            event["evidence"]["cause_status"] = (
                "diagnosed" if confirmation_causes else "pending"
            )
        if event is not None:
            events.append(event)
    return events


def _normalized_event(event):
    copied = json.loads(json.dumps(event, ensure_ascii=False, allow_nan=False))
    for field in _EPHEMERAL_EVENT_FIELDS:
        copied.pop(field, None)
    for field in ("start_time", "last_seen_time", "end_time"):
        if copied.get(field) is not None:
            copied[field] = _shanghai_minute(copied[field])
    evidence = copied.get("evidence")
    if isinstance(evidence, dict):
        for field in ("last_low_load_time", "last_condition_time", "last_low_efficiency_time"):
            if evidence.get(field) is not None:
                evidence[field] = _shanghai_minute(evidence[field])
    return copied


def evaluate_station_bottlenecks(*, station_id, minute_points, device_points, active_events, rule):
    """Return new, changed, or recovered station bottleneck events without I/O."""
    station_id = _station_identifier(station_id, "station_id")
    rule = normalize_rule(rule)
    if rule["station_id"] != station_id:
        raise HistoryError("invalid_station_data", "规则场站与输入场站不匹配", {"field": "rule.station_id"})
    _validate_station_rows(minute_points, station_id, "minute_points")
    active_by_identity = _active_by_identity(active_events, station_id)
    grouped = _group_device_points(device_points, station_id)

    device_events = _device_events(grouped, active_by_identity, rule)
    chain_events = _chain_events(minute_points, grouped, active_by_identity, device_events, rule)
    evaluated = device_events + chain_events

    changed = []
    for event in evaluated:
        active_event = active_by_identity.get((event["event_type"], event["device_id"]))
        if active_event is None or _normalized_event(event) != _normalized_event(active_event):
            changed.append(event)
    return changed
