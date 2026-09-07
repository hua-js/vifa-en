"""Pure minute-history and bottleneck transformations for the energy dashboard."""

from datetime import datetime, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo


CHAIN_COLUMNS = {
    "pv_storage": (
        "pv_storage_efficiency", "pv_storage_input_kw", "pv_storage_output_kw"
    ),
    "storage_load": (
        "storage_load_efficiency", "storage_load_input_kw", "storage_load_output_kw"
    ),
    "pv_load": (
        "pv_load_efficiency", "pv_load_input_kw", "pv_load_output_kw"
    ),
}
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class HistoryError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def _parse_time(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HistoryError("invalid_time", f"{field} 不是合法 ISO 8601 时间", {"field": field}) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryError("invalid_time", f"{field} 必须包含时区", {"field": field})
    return parsed


def minute_bucket(value):
    return _parse_time(value, "data_time").replace(second=0, microsecond=0)


def _optional_non_negative(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError("invalid_minute_point", f"{field} 必须是数值或 null", {"field": field})
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise HistoryError("invalid_minute_point", f"{field} 必须是有限非负数", {"field": field})
    return number


def _required_identifier(value, field, code):
    if value is None or isinstance(value, bool):
        raise HistoryError(code, f"{field} 不能为空", {"field": field})
    identifier = str(value).strip()
    if not identifier:
        raise HistoryError(code, f"{field} 不能为空", {"field": field})
    return identifier


def build_minute_point(station_id, data_time, chains, formula_version, calculated_at):
    station_id = _required_identifier(station_id, "station_id", "invalid_minute_point")
    if set(chains or {}) != set(CHAIN_COLUMNS):
        raise HistoryError("invalid_minute_point", "chains 必须包含三条链路", {"field": "chains"})
    formula_version = _required_identifier(formula_version, "formula_version", "invalid_minute_point")

    point = {
        "station_id": station_id,
        "data_time": minute_bucket(data_time).isoformat(),
        "formula_version": formula_version,
        "calculated_at": _parse_time(calculated_at, "calculated_at").isoformat(),
    }
    for chain_name, columns in CHAIN_COLUMNS.items():
        chain = chains[chain_name]
        if not isinstance(chain, dict):
            raise HistoryError("invalid_minute_point", "链路结果必须是对象", {"field": chain_name})
        efficiency_column, input_column, output_column = columns
        point[efficiency_column] = _optional_non_negative(chain.get("efficiency"), efficiency_column)
        point[input_column] = _optional_non_negative(chain.get("input_kw"), input_column)
        point[output_column] = _optional_non_negative(chain.get("output_kw"), output_column)
    return point


def build_minute_upsert(point):
    return {
        "collection": "t_efficiency_points",
        "key": {
            "station_id": point["station_id"],
            "data_time": _canonical_minute(point["data_time"]).isoformat(),
        },
        "values": dict(point),
    }


RULE_FIELDS = (
    "station_id", "enabled", "inverter_min_running_power_kw",
    "inverter_low_load_threshold_pct", "inverter_trigger_minutes",
    "inverter_recovery_minutes", "temperature_rise_window_minutes",
    "temperature_rise_threshold_c", "temperature_trigger_minutes",
    "temperature_recovery_minutes", "version", "updated_at",
    "chain_low_efficiency_threshold_pct", "chain_low_efficiency_trigger_minutes",
    "chain_low_efficiency_recovery_minutes",
)
_COMMON_SNAPSHOT_RULE_FIELDS = (
    "station_id", "enabled", "version", "updated_at",
)
_SNAPSHOT_RULE_FIELDS_BY_EVENT_TYPE = {
    "inverter_low_load": _COMMON_SNAPSHOT_RULE_FIELDS + (
        "inverter_min_running_power_kw",
        "inverter_low_load_threshold_pct",
        "inverter_trigger_minutes",
        "inverter_recovery_minutes",
    ),
    "battery_temperature_rise": _COMMON_SNAPSHOT_RULE_FIELDS + (
        "temperature_rise_window_minutes",
        "temperature_rise_threshold_c",
        "temperature_trigger_minutes",
        "temperature_recovery_minutes",
    ),
    "chain_low_efficiency": _COMMON_SNAPSHOT_RULE_FIELDS + (
        "chain_low_efficiency_threshold_pct",
        "chain_low_efficiency_trigger_minutes",
        "chain_low_efficiency_recovery_minutes",
    ),
}
_INTEGER_RULE_FIELDS = (
    "inverter_trigger_minutes", "inverter_recovery_minutes",
    "temperature_rise_window_minutes", "temperature_trigger_minutes",
    "temperature_recovery_minutes", "chain_low_efficiency_trigger_minutes",
    "chain_low_efficiency_recovery_minutes", "version",
)
_NUMBER_RULE_FIELDS = (
    "inverter_min_running_power_kw", "inverter_low_load_threshold_pct",
    "temperature_rise_threshold_c", "chain_low_efficiency_threshold_pct",
)
_PERCENT_RULE_FIELDS = (
    "inverter_low_load_threshold_pct", "chain_low_efficiency_threshold_pct",
)


def _normalize_rule_record(record, required_fields):
    if not isinstance(record, dict):
        raise HistoryError("invalid_rule", "瓶颈规则必须是对象")
    missing = [field for field in required_fields if field not in record]
    if missing:
        raise HistoryError("invalid_rule", "瓶颈规则缺少字段", {"fields": missing})
    normalized = dict(record)
    for field in _INTEGER_RULE_FIELDS:
        if field not in required_fields:
            continue
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HistoryError("invalid_rule", f"{field} 必须是正整数", {"field": field})
    for field in _NUMBER_RULE_FIELDS:
        if field not in required_fields:
            continue
        try:
            number = _optional_non_negative(record[field], field)
        except HistoryError as exc:
            raise HistoryError("invalid_rule", exc.message, exc.details) from exc
        if number is None:
            raise HistoryError("invalid_rule", f"{field} 不能为空", {"field": field})
        normalized[field] = number
    if not isinstance(record["enabled"], bool):
        raise HistoryError("invalid_rule", "enabled 必须是布尔值", {"field": "enabled"})
    for field in _PERCENT_RULE_FIELDS:
        if field not in required_fields:
            continue
        if normalized[field] > 100:
            raise HistoryError("invalid_rule", f"{field} 不能超过 100%", {"field": field})
    normalized["station_id"] = _required_identifier(record["station_id"], "station_id", "invalid_rule")
    normalized["updated_at"] = _parse_time(record["updated_at"], "updated_at").isoformat()
    return normalized


def normalize_rule(record):
    return _normalize_rule_record(record, RULE_FIELDS)


def _normalize_active_rule_snapshot(record, event_type):
    required_fields = _SNAPSHOT_RULE_FIELDS_BY_EVENT_TYPE[event_type]
    return _normalize_rule_record(record, required_fields)


def _ordered_one_device(samples, normalizer):
    if not isinstance(samples, list):
        raise HistoryError("invalid_device_samples", "设备样本必须是数组")
    ordered = sorted((normalizer(sample) for sample in samples), key=lambda item: item["_time"])
    device_ids = {sample["device_id"] for sample in ordered}
    if len(device_ids) > 1:
        raise HistoryError("invalid_device_samples", "一次只能评估一个设备")
    times = [sample["_time"] for sample in ordered]
    if len(times) != len(set(times)):
        raise HistoryError("invalid_device_samples", "设备样本时间不能重复")
    return ordered


def _normalize_inverter_samples(samples):
    def normalize(sample):
        if not isinstance(sample, dict):
            raise HistoryError("invalid_device_samples", "设备样本必须是对象")
        active = _optional_non_negative(sample.get("active_power_kw"), "active_power_kw")
        rated = _optional_non_negative(sample.get("rated_power_kw"), "rated_power_kw")
        if active is None or rated is None or rated <= 0:
            raise HistoryError("invalid_device_samples", "逆变器功率必须完整且额定功率大于零")
        time = minute_bucket(sample.get("data_time"))
        return {
            "device_id": _required_identifier(sample.get("device_id"), "device_id", "invalid_device_samples"),
            "device_name": _required_identifier(sample.get("device_name"), "device_name", "invalid_device_samples"),
            "data_time": time.isoformat(),
            "active_power_kw": active,
            "rated_power_kw": rated,
            "_time": time,
        }
    return _ordered_one_device(samples, normalize)


def _trailing_minute_run(items, predicate, time_getter=lambda item: item["_time"]):
    run = []
    previous_time = None
    for item in items:
        current_time = time_getter(item)
        if previous_time is not None and current_time - previous_time != timedelta(minutes=1):
            run = []
        if predicate(item):
            run.append(item)
        else:
            run = []
        previous_time = current_time
    return run


def _new_event(
    rule, event_type, first, last, observed, threshold, unit, impact, text, evidence,
):
    return {
        "station_id": rule["station_id"],
        "event_type": event_type,
        "device_id": first["device_id"],
        "device_name": first["device_name"],
        "start_time": first["data_time"],
        "end_time": None,
        "last_seen_time": last["data_time"],
        "status": "active",
        "observed_value": observed,
        "threshold_value": threshold,
        "observed_unit": unit,
        "evidence": {
            "display_text": text,
            **dict(evidence),
            "rule": dict(rule),
        },
        "impact_chain": list(impact),
        "rule_version": rule["version"],
    }


def _validate_active_event(
    active_event, expected_station_id, expected_event_type,
    expected_device_id=None, expected_device_name=None,
):
    if not isinstance(active_event, dict):
        raise HistoryError("invalid_active_event", "活动事件必须是对象")
    station_id = _required_identifier(
        active_event.get("station_id"), "station_id", "invalid_active_event",
    )
    if station_id != expected_station_id:
        raise HistoryError("invalid_active_event", "活动事件场站不匹配", {"field": "station_id"})
    if active_event.get("event_type") != expected_event_type:
        raise HistoryError("invalid_active_event", "活动事件类型不匹配", {"field": "event_type"})
    if active_event.get("status") != "active":
        raise HistoryError("invalid_active_event", "活动事件状态必须为 active", {"field": "status"})
    if active_event.get("end_time") is not None:
        raise HistoryError("invalid_active_event", "活动事件结束时间必须为空", {"field": "end_time"})
    device_id = _required_identifier(active_event.get("device_id"), "device_id", "invalid_active_event")
    device_name = _required_identifier(active_event.get("device_name"), "device_name", "invalid_active_event")
    try:
        start_time = _parse_time(active_event.get("start_time"), "start_time")
        last_seen_time = _parse_time(active_event.get("last_seen_time"), "last_seen_time")
    except HistoryError as exc:
        raise HistoryError("invalid_active_event", exc.message, exc.details) from exc
    if last_seen_time < start_time:
        raise HistoryError("invalid_active_event", "活动事件最后时间不能早于开始时间")
    if expected_device_id is not None and device_id != expected_device_id:
        raise HistoryError("invalid_active_event", "活动事件设备不匹配", {"field": "device_id"})
    if expected_device_name is not None and device_name != expected_device_name:
        raise HistoryError("invalid_active_event", "活动事件设备名称不匹配", {"field": "device_name"})

    evidence = active_event.get("evidence")
    if not isinstance(evidence, dict):
        raise HistoryError("invalid_active_event", "活动事件证据必须是对象", {"field": "evidence"})
    try:
        snapshot = _normalize_active_rule_snapshot(
            evidence.get("rule"), expected_event_type,
        )
    except HistoryError as exc:
        raise HistoryError(
            "invalid_active_event", "活动事件规则快照无效", {"field": "evidence.rule"},
        ) from exc
    if snapshot["station_id"] != station_id:
        raise HistoryError("invalid_active_event", "活动事件规则快照场站不匹配", {"field": "evidence.rule.station_id"})
    rule_version = active_event.get("rule_version")
    if isinstance(rule_version, bool) or not isinstance(rule_version, int) or rule_version <= 0:
        raise HistoryError("invalid_active_event", "活动事件规则版本无效", {"field": "rule_version"})
    if rule_version != snapshot["version"]:
        raise HistoryError("invalid_active_event", "活动事件规则版本不匹配", {"field": "rule_version"})

    threshold_field, observed_unit = {
        "inverter_low_load": ("inverter_low_load_threshold_pct", "%"),
        "battery_temperature_rise": ("temperature_rise_threshold_c", "℃"),
        "chain_low_efficiency": ("chain_low_efficiency_threshold_pct", "%"),
    }[expected_event_type]
    threshold = active_event.get("threshold_value")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
        raise HistoryError("invalid_active_event", "活动事件阈值无效", {"field": "threshold_value"})
    if float(threshold) != snapshot[threshold_field]:
        raise HistoryError("invalid_active_event", "活动事件阈值不匹配", {"field": "threshold_value"})
    if active_event.get("observed_unit") != observed_unit:
        raise HistoryError("invalid_active_event", "活动事件观测单位不匹配", {"field": "observed_unit"})
    return snapshot, start_time, last_seen_time


def evaluate_inverter_low_load(samples, rule, active_event=None):
    rule = normalize_rule(rule)
    ordered = _normalize_inverter_samples(samples)
    event_start_time = None
    previous_last_seen_time = None
    snapshot = None
    if active_event is not None:
        expected_device = ordered[0] if ordered else None
        snapshot, event_start_time, previous_last_seen_time = _validate_active_event(
            active_event, rule["station_id"], "inverter_low_load",
            expected_device["device_id"] if expected_device else None,
            expected_device["device_name"] if expected_device else None,
        )
    if not ordered:
        return dict(active_event) if active_event else None
    if not rule["enabled"] and active_event is None:
        return None

    def low_load(sample, threshold, min_power):
        rate = sample["active_power_kw"] / sample["rated_power_kw"] * 100.0
        return sample["active_power_kw"] >= min_power and rate < threshold

    if active_event is None:
        run = _trailing_minute_run(
            ordered,
            lambda sample: low_load(
                sample,
                rule["inverter_low_load_threshold_pct"],
                rule["inverter_min_running_power_kw"],
            ),
        )
        if len(run) < rule["inverter_trigger_minutes"]:
            return None
        rates = [sample["active_power_kw"] / sample["rated_power_kw"] * 100.0 for sample in run]
        first, last = run[0], run[-1]
        return _new_event(
            rule, "inverter_low_load", first, last,
            min(rates), rule["inverter_low_load_threshold_pct"], "%",
            ["光→储", "光→用"],
            f"负载率最低 {min(rates):.2f}%，低于阈值 {rule['inverter_low_load_threshold_pct']:.2f}%",
            {
                "current_load_rate_pct": last["active_power_kw"] / last["rated_power_kw"] * 100.0,
                "minimum_load_rate_pct": min(rates),
                "rated_power_kw": last["rated_power_kw"],
                "low_load_threshold_pct": rule["inverter_low_load_threshold_pct"],
                "continuous_minutes": len(run),
                "last_low_load_time": last["data_time"],
                "confirmation_time": run[
                    rule["inverter_trigger_minutes"] - 1
                ]["data_time"],
            },
        )

    event_history = [sample for sample in ordered if sample["_time"] >= event_start_time]
    ordered = [sample for sample in ordered if sample["_time"] > previous_last_seen_time]
    if not ordered:
        return {**active_event, "evidence": dict(active_event["evidence"])}
    event = {**active_event, "evidence": dict(active_event["evidence"])}
    low_run = _trailing_minute_run(
        ordered,
        lambda sample: low_load(
            sample,
            snapshot["inverter_low_load_threshold_pct"],
            snapshot["inverter_min_running_power_kw"],
        ),
    )
    recovery = _trailing_minute_run(
        event_history,
        lambda sample: not low_load(
            sample,
            snapshot["inverter_low_load_threshold_pct"],
            snapshot["inverter_min_running_power_kw"],
        ),
    )
    valid_rates = [
        sample["active_power_kw"] / sample["rated_power_kw"] * 100.0
        for sample in ordered if sample["active_power_kw"] >= snapshot["inverter_min_running_power_kw"]
    ]
    if valid_rates:
        event["observed_value"] = min(event["observed_value"], min(valid_rates))
    if low_run:
        previous_low_time = _parse_time(
            event["evidence"].get("last_low_load_time", event["last_seen_time"]),
            "last_low_load_time",
        )
        new_low_run = [sample for sample in low_run if sample["_time"] > previous_low_time]
        if new_low_run:
            if new_low_run[0]["_time"] - previous_low_time == timedelta(minutes=1):
                event["evidence"]["continuous_minutes"] += len(new_low_run)
            else:
                event["evidence"]["continuous_minutes"] = len(new_low_run)
            event["evidence"]["last_low_load_time"] = new_low_run[-1]["data_time"]
    latest = ordered[-1]
    event["evidence"]["current_load_rate_pct"] = (
        latest["active_power_kw"] / latest["rated_power_kw"] * 100.0
    )
    event["evidence"]["minimum_load_rate_pct"] = event["observed_value"]
    event["evidence"]["rated_power_kw"] = latest["rated_power_kw"]
    event["evidence"]["low_load_threshold_pct"] = snapshot["inverter_low_load_threshold_pct"]
    event["evidence"]["display_text"] = (
        f"负载率最低 {event['observed_value']:.2f}%，"
        f"低于阈值 {snapshot['inverter_low_load_threshold_pct']:.2f}%"
    )
    event["last_seen_time"] = ordered[-1]["data_time"]
    if (
        len(recovery) >= snapshot["inverter_recovery_minutes"]
        and recovery[-1]["_time"] > previous_last_seen_time
    ):
        event["status"] = "recovered"
        event["end_time"] = recovery[0]["data_time"]
    return event


def _normalize_chain_samples(samples):
    def normalize(sample):
        if not isinstance(sample, dict):
            raise HistoryError("invalid_device_samples", "设备样本必须是对象")
        value = sample.get("efficiency_pct")
        if value is None:
            efficiency = None
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise HistoryError("invalid_device_samples", "efficiency_pct 必须是有限数值或 null")
            efficiency = float(value)
            if efficiency < 0 or efficiency > 100:
                raise HistoryError("invalid_device_samples", "efficiency_pct 必须在 0 到 100 之间")
        input_value = sample.get("input_kw")
        if input_value is None:
            input_kw = None
        else:
            if (
                isinstance(input_value, bool)
                or not isinstance(input_value, (int, float))
                or not math.isfinite(float(input_value))
                or float(input_value) < 0
            ):
                raise HistoryError(
                    "invalid_device_samples",
                    "input_kw 必须是有限非负数或 null",
                )
            input_kw = float(input_value)
        time = minute_bucket(sample.get("data_time")).astimezone(SHANGHAI_TIMEZONE)
        return {
            "device_id": _required_identifier(sample.get("device_id"), "device_id", "invalid_device_samples"),
            "device_name": _required_identifier(sample.get("device_name"), "device_name", "invalid_device_samples"),
            "data_time": time.isoformat(),
            "efficiency_pct": efficiency,
            "input_kw": input_kw,
            "_time": time,
        }
    return _ordered_one_device(samples, normalize)


def _json_safe_copy(value, field):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HistoryError("invalid_event_evidence", f"{field} 必须只包含 JSON 安全值", {"field": field}) from exc


def _event_copy(event):
    return {**event, "evidence": _json_safe_copy(event["evidence"], "evidence")}


def _chain_event_copy(event):
    copied = _event_copy(event)
    for field in ("start_time", "last_seen_time", "end_time"):
        if copied.get(field) is not None:
            copied[field] = _parse_time(copied[field], field).astimezone(SHANGHAI_TIMEZONE).isoformat()
    for field in ("last_low_efficiency_time",):
        if copied["evidence"].get(field) is not None:
            copied["evidence"][field] = _parse_time(
                copied["evidence"][field], field,
            ).astimezone(SHANGHAI_TIMEZONE).isoformat()
    return copied


def _chain_event_evidence(sample, minimum, rule, continuous_minutes, last_low_time, snapshot, causes):
    return {
        "current_efficiency_pct": sample["efficiency_pct"],
        "minimum_efficiency_pct": minimum,
        "low_efficiency_threshold_pct": rule["chain_low_efficiency_threshold_pct"],
        "continuous_minutes": continuous_minutes,
        "last_low_efficiency_time": last_low_time,
        "confirmation_time": None,
        "cause_status": "diagnosed" if causes else "pending",
        "diagnosed_causes": causes,
        "trigger_device_snapshot": snapshot,
    }


def _sync_chain_diagnosed_causes(event, diagnosed_causes):
    if diagnosed_causes is None:
        return event
    causes = _json_safe_copy(diagnosed_causes, "diagnosed_causes")
    if not isinstance(causes, list):
        raise HistoryError("invalid_event_evidence", "diagnosed_causes 必须是数组", {"field": "diagnosed_causes"})
    event["evidence"]["cause_status"] = "diagnosed" if causes else "pending"
    event["evidence"]["diagnosed_causes"] = causes
    return event


def evaluate_chain_low_efficiency(
    samples, rule, active_event=None, trigger_device_snapshot=None, diagnosed_causes=None,
):
    """Evaluate one energy-chain's minute efficiency without side effects."""
    rule = normalize_rule(rule)
    ordered = _normalize_chain_samples(samples)
    event_start_time = None
    previous_last_seen_time = None
    snapshot_rule = None
    if active_event is not None:
        expected_device = ordered[0] if ordered else None
        snapshot_rule, event_start_time, previous_last_seen_time = _validate_active_event(
            active_event, rule["station_id"], "chain_low_efficiency",
            expected_device["device_id"] if expected_device else None,
            expected_device["device_name"] if expected_device else None,
        )
    if not ordered:
        return (
            _sync_chain_diagnosed_causes(_chain_event_copy(active_event), diagnosed_causes)
            if active_event else None
        )
    if not rule["enabled"] and active_event is None:
        return None

    def low_efficiency(sample, threshold):
        return (
            sample["efficiency_pct"] is not None
            and sample["efficiency_pct"] < threshold
        )

    def stopped(sample):
        return sample["efficiency_pct"] is None and sample["input_kw"] == 0

    if active_event is None:
        run = _trailing_minute_run(
            ordered,
            lambda sample: low_efficiency(
                sample, rule["chain_low_efficiency_threshold_pct"],
            ),
        )
        if len(run) < rule["chain_low_efficiency_trigger_minutes"]:
            return None
        first, last = run[0], run[-1]
        confirmation = run[rule["chain_low_efficiency_trigger_minutes"] - 1]
        minimum = min(sample["efficiency_pct"] for sample in run)
        device_snapshot = _json_safe_copy(
            trigger_device_snapshot if trigger_device_snapshot is not None else {},
            "trigger_device_snapshot",
        )
        if not isinstance(device_snapshot, dict):
            raise HistoryError("invalid_event_evidence", "trigger_device_snapshot 必须是对象", {"field": "trigger_device_snapshot"})
        causes = _json_safe_copy(diagnosed_causes or [], "diagnosed_causes")
        if not isinstance(causes, list):
            raise HistoryError("invalid_event_evidence", "diagnosed_causes 必须是数组", {"field": "diagnosed_causes"})
        event = _new_event(
            rule, "chain_low_efficiency", first, last,
            minimum, rule["chain_low_efficiency_threshold_pct"], "%",
            [first["device_name"]],
            f"链路效率最低 {minimum:.2f}%，低于阈值 {rule['chain_low_efficiency_threshold_pct']:.2f}%",
            _chain_event_evidence(
                last, minimum, rule, len(run), last["data_time"], device_snapshot, causes,
            ),
        )
        event["evidence"]["confirmation_time"] = confirmation["data_time"]
        return event

    new_samples = [sample for sample in ordered if sample["_time"] > previous_last_seen_time]
    if not any(
        sample["efficiency_pct"] is not None or stopped(sample)
        for sample in new_samples
    ):
        return _sync_chain_diagnosed_causes(
            _chain_event_copy(active_event), diagnosed_causes,
        )

    event = _chain_event_copy(active_event)
    _sync_chain_diagnosed_causes(event, diagnosed_causes)
    evidence = event["evidence"]
    low_run = _trailing_minute_run(
        new_samples,
        lambda sample: low_efficiency(
            sample, snapshot_rule["chain_low_efficiency_threshold_pct"],
        ),
    )
    if low_run:
        previous_low_time = _parse_time(
            evidence.get("last_low_efficiency_time", event["last_seen_time"]),
            "last_low_efficiency_time",
        )
        new_low_run = [sample for sample in low_run if sample["_time"] > previous_low_time]
        if new_low_run:
            if new_low_run[0]["_time"] - previous_low_time == timedelta(minutes=1):
                evidence["continuous_minutes"] += len(new_low_run)
            else:
                evidence["continuous_minutes"] = len(new_low_run)
            evidence["last_low_efficiency_time"] = new_low_run[-1]["data_time"]

    valid_new_samples = [
        sample for sample in new_samples
        if sample["efficiency_pct"] is not None
    ]
    if valid_new_samples:
        event["observed_value"] = min(
            event["observed_value"],
            min(sample["efficiency_pct"] for sample in valid_new_samples),
        )
    observed_new_samples = [
        sample for sample in new_samples
        if sample["efficiency_pct"] is not None or stopped(sample)
    ]
    latest = observed_new_samples[-1]
    evidence["current_efficiency_pct"] = latest["efficiency_pct"]
    evidence["minimum_efficiency_pct"] = event["observed_value"]
    evidence["low_efficiency_threshold_pct"] = snapshot_rule["chain_low_efficiency_threshold_pct"]
    evidence["display_text"] = (
        f"链路效率最低 {event['observed_value']:.2f}%，"
        f"低于阈值 {snapshot_rule['chain_low_efficiency_threshold_pct']:.2f}%"
    )
    event["last_seen_time"] = latest["data_time"]

    event_history = [sample for sample in ordered if sample["_time"] >= event_start_time]
    efficiency_recovery = _trailing_minute_run(
        event_history,
        lambda sample: (
            sample["efficiency_pct"] is not None
            and sample["efficiency_pct"] >= snapshot_rule["chain_low_efficiency_threshold_pct"]
        ),
    )
    stopped_recovery = _trailing_minute_run(event_history, stopped)
    if (
        len(efficiency_recovery) >= snapshot_rule["chain_low_efficiency_recovery_minutes"]
        and efficiency_recovery[-1]["_time"] > previous_last_seen_time
    ):
        event["status"] = "recovered"
        event["end_time"] = efficiency_recovery[0]["data_time"]
        evidence["recovery_reason"] = "efficiency_recovered"
    elif (
        len(stopped_recovery) >= snapshot_rule["chain_low_efficiency_recovery_minutes"]
        and stopped_recovery[-1]["_time"] > previous_last_seen_time
    ):
        event["status"] = "recovered"
        event["end_time"] = stopped_recovery[0]["data_time"]
        evidence["recovery_reason"] = "chain_stopped"
    return event


def _normalize_temperature_samples(samples):
    def normalize(sample):
        if not isinstance(sample, dict):
            raise HistoryError("invalid_device_samples", "设备样本必须是对象")
        value = sample.get("temperature_c")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HistoryError("invalid_device_samples", "temperature_c 必须是有限数值")
        time = minute_bucket(sample.get("data_time"))
        return {
            "device_id": _required_identifier(sample.get("device_id"), "device_id", "invalid_device_samples"),
            "device_name": _required_identifier(sample.get("device_name"), "device_name", "invalid_device_samples"),
            "data_time": time.isoformat(),
            "temperature_c": float(value),
            "_time": time,
        }
    return _ordered_one_device(samples, normalize)


def _temperature_observations(samples, window_minutes):
    by_time = {sample["_time"]: sample for sample in samples}
    observations = []
    for sample in samples:
        window_start = sample["_time"] - timedelta(minutes=window_minutes)
        required = [
            window_start + timedelta(minutes=offset)
            for offset in range(window_minutes + 1)
        ]
        if not all(point_time in by_time for point_time in required):
            continue
        start_sample = by_time[window_start]
        observations.append({
            "sample": sample,
            "window_start_sample": start_sample,
            "rise_c": sample["temperature_c"] - start_sample["temperature_c"],
        })
    return observations


def _trailing_observation_run(observations, predicate):
    return _trailing_minute_run(
        observations,
        predicate,
        time_getter=lambda item: item["sample"]["_time"],
    )


def _temperature_evidence(
    observation, maximum, rule, continuous_minutes, last_condition_time,
    confirmation_time=None,
):
    return {
        "current_temperature_c": observation["sample"]["temperature_c"],
        "window_start_temperature_c": observation["window_start_sample"]["temperature_c"],
        "window_minutes": rule["temperature_rise_window_minutes"],
        "current_rise_c": observation["rise_c"],
        "maximum_rise_c": maximum,
        "threshold_c": rule["temperature_rise_threshold_c"],
        "continuous_minutes": continuous_minutes,
        "last_condition_time": last_condition_time,
        **({"confirmation_time": confirmation_time} if confirmation_time is not None else {}),
    }


def evaluate_battery_temperature_rise(samples, rule, active_event=None):
    rule = normalize_rule(rule)
    ordered = _normalize_temperature_samples(samples)
    event_start_time = None
    previous_last_seen = None
    snapshot = None
    if active_event is not None:
        expected_device = ordered[0] if ordered else None
        snapshot, event_start_time, previous_last_seen = _validate_active_event(
            active_event, rule["station_id"], "battery_temperature_rise",
            expected_device["device_id"] if expected_device else None,
            expected_device["device_name"] if expected_device else None,
        )
    if not ordered:
        return dict(active_event) if active_event else None
    if not rule["enabled"] and active_event is None:
        return None

    snapshot = snapshot if active_event else rule
    observations = _temperature_observations(
        ordered, snapshot["temperature_rise_window_minutes"],
    )
    if not observations:
        return dict(active_event) if active_event else None

    if active_event is None:
        run = _trailing_observation_run(
            observations,
            lambda item: item["rise_c"] >= rule["temperature_rise_threshold_c"],
        )
        if len(run) < rule["temperature_trigger_minutes"]:
            return None
        maximum = max(item["rise_c"] for item in run)
        first, last = run[0]["sample"], run[-1]["sample"]
        return _new_event(
            rule, "battery_temperature_rise", first, last,
            maximum, rule["temperature_rise_threshold_c"], "℃",
            ["光→储", "储→用"],
            f"{rule['temperature_rise_window_minutes']} 分钟最大温升 {maximum:.2f}℃",
            _temperature_evidence(
                run[-1], maximum, rule, len(run), last["data_time"],
                run[rule["temperature_trigger_minutes"] - 1]["sample"]["data_time"],
            ),
        )

    event = {**active_event, "evidence": dict(active_event["evidence"])}
    evidence = event["evidence"]
    new_observations = [
        item for item in observations if item["sample"]["_time"] > previous_last_seen
    ]
    if new_observations:
        latest = new_observations[-1]
        event_observations = [
            item for item in new_observations
            if item["sample"]["_time"] >= event_start_time
        ]
        if event_observations:
            event["observed_value"] = max(
                event["observed_value"], max(item["rise_c"] for item in event_observations),
            )
        evidence.update(_temperature_evidence(
            latest, event["observed_value"], snapshot,
            evidence["continuous_minutes"], evidence.get("last_condition_time", event["last_seen_time"]),
        ))
        event["last_seen_time"] = latest["sample"]["data_time"]

    condition_run = _trailing_observation_run(
        observations,
        lambda item: item["rise_c"] >= snapshot["temperature_rise_threshold_c"],
    )
    if condition_run:
        previous_condition = _parse_time(
            evidence.get("last_condition_time", active_event["last_seen_time"]),
            "last_condition_time",
        )
        new_conditions = [
            item for item in condition_run if item["sample"]["_time"] > previous_condition
        ]
        if new_conditions:
            if new_conditions[0]["sample"]["_time"] - previous_condition == timedelta(minutes=1):
                evidence["continuous_minutes"] += len(new_conditions)
            else:
                evidence["continuous_minutes"] = len(new_conditions)
            evidence["last_condition_time"] = new_conditions[-1]["sample"]["data_time"]

    evidence["maximum_rise_c"] = event["observed_value"]
    evidence["threshold_c"] = snapshot["temperature_rise_threshold_c"]
    evidence["window_minutes"] = snapshot["temperature_rise_window_minutes"]
    evidence["display_text"] = (
        f"{snapshot['temperature_rise_window_minutes']} 分钟最大温升 "
        f"{event['observed_value']:.2f}℃"
    )
    recovery = _trailing_observation_run(
        observations,
        lambda item: (
            item["sample"]["_time"] >= event_start_time
            and item["rise_c"] < snapshot["temperature_rise_threshold_c"]
        ),
    )
    if (
        len(recovery) >= snapshot["temperature_recovery_minutes"]
        and recovery[-1]["sample"]["_time"] > previous_last_seen
    ):
        event["status"] = "recovered"
        event["end_time"] = recovery[0]["sample"]["data_time"]
    return event


def build_event_upsert(event):
    return {
        "collection": "t_efficiency_bottleneck_events",
        "key": {
            "station_id": event["station_id"],
            "event_type": event["event_type"],
            "device_id": event["device_id"],
            "start_time": event["start_time"],
        },
        "values": dict(event),
    }


EVENT_LABELS = {
    "inverter_low_load": "逆变器低负载",
    "battery_temperature_rise": "电池温升",
    "chain_low_efficiency": "链路低效率",
}


def _canonical_minute(value, field="data_time"):
    return _parse_time(value, field).astimezone(timezone.utc).replace(second=0, microsecond=0)


def _public_minute(value, zone, field="data_time"):
    return _canonical_minute(value, field).astimezone(zone).isoformat()


def _public_time(value, zone, field):
    return _parse_time(value, field).astimezone(zone).isoformat()


def _day_bounds(as_of, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise HistoryError("invalid_timezone", "场站时区无效", {"timezone": timezone_name}) from exc
    local = _parse_time(as_of, "as_of").astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def calendar_day_bounds(as_of, timezone_name):
    """返回指定时刻所在场站自然日的带时区起止时间。"""
    start, end = _day_bounds(as_of, timezone_name)
    return start.isoformat(), end.isoformat()


def time_in_zone(value, timezone_name, field="data_time"):
    """将带时区的时间转换为场站配置时区。"""
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise HistoryError(
            "invalid_timezone",
            "场站时区无效",
            {"timezone": timezone_name},
        ) from exc
    return _parse_time(value, field).astimezone(zone).isoformat()


def _summary_efficiency(points, input_column, output_column):
    pairs = [
        (point.get(input_column), point.get(output_column))
        for point in points
        if point.get(input_column) is not None and point.get(output_column) is not None
    ]
    total_input = sum(pair[0] / 60.0 for pair in pairs)
    total_output = sum(pair[1] / 60.0 for pair in pairs)
    return None if total_input <= 0 else total_output / total_input * 100.0


def summarize_today(points):
    return {
        "pv_storage_efficiency": _summary_efficiency(points, "pv_storage_input_kw", "pv_storage_output_kw"),
        "storage_load_efficiency": _summary_efficiency(points, "storage_load_input_kw", "storage_load_output_kw"),
        "pv_load_efficiency": _summary_efficiency(points, "pv_load_input_kw", "pv_load_output_kw"),
    }


def _event_overlaps(event, start, end):
    event_start = _parse_time(event["start_time"], "start_time")
    event_end = _parse_time(event["end_time"], "end_time") if event.get("end_time") else None
    return event_start < end and (event_end is None or event_end >= start)


def _public_event_device(event):
    if event.get("event_type") != "chain_low_efficiency":
        return event["device_name"]
    evidence = event.get("evidence")
    causes = evidence.get("diagnosed_causes") if isinstance(evidence, dict) else None
    names = []
    for cause in causes if isinstance(causes, list) else []:
        if not isinstance(cause, dict):
            continue
        name = cause.get("device_name") or cause.get("device_id")
        if name is not None and str(name).strip() and str(name).strip() not in names:
            names.append(str(name).strip())
    return "、".join(names) if names else "原因待判断"


def build_event_history(station_id, timezone_name, start_time, end_time, events):
    """按重叠区间读取完整事件生命周期；状态表示当前已保存的最新状态。"""
    zone = ZoneInfo(timezone_name)
    start = _parse_time(start_time, "start_time").astimezone(zone)
    end = _parse_time(end_time, "end_time").astimezone(zone)
    if start >= end:
        raise HistoryError("invalid_range", "结束时间必须晚于开始时间")
    public_events = []
    for event in sorted(events, key=lambda item: _parse_time(item["start_time"], "start_time")):
        if str(event.get("station_id")) != str(station_id) or not _event_overlaps(event, start, end):
            continue
        public_events.append({
            "id": event.get("id"),
            "event_type": event["event_type"],
            "type": EVENT_LABELS[event["event_type"]],
            "device": _public_event_device(event),
            "start": _public_time(event["start_time"], start.tzinfo, "start_time"),
            "end": _public_time(event["end_time"], start.tzinfo, "end_time") if event.get("end_time") else None,
            "evidence": event["evidence"]["display_text"],
            "cause_status": event["evidence"].get("cause_status"),
            "diagnosed_causes": list(event["evidence"].get("diagnosed_causes") or []),
            "trigger_device_snapshot": dict(event["evidence"].get("trigger_device_snapshot") or {}),
            "impact": list(event["impact_chain"]),
            "status": "持续中" if event["status"] == "active" else "已恢复",
        })
    return {
        "operation": "events",
        "station_id": station_id,
        "range": {
            "timezone": timezone_name,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
        "events": public_events,
    }


def build_calendar_day_dashboard(station_id, timezone_name, as_of, realtime, points, events):
    start, end = _day_bounds(as_of, timezone_name)
    cutoff = minute_bucket(as_of).astimezone(start.tzinfo)
    selected = sorted(
        (
            point for point in points
            if str(point.get("station_id")) == str(station_id)
            and start <= _parse_time(point["data_time"], "data_time").astimezone(start.tzinfo) <= cutoff
        ),
        key=lambda point: _parse_time(point["data_time"], "data_time"),
    )
    keys = [(str(point["station_id"]), _canonical_minute(point["data_time"])) for point in selected]
    if len(keys) != len(set(keys)):
        raise HistoryError("duplicate_minute", "同一场站同一分钟只能有一个效率点")
    latest = _public_minute(selected[-1]["data_time"], start.tzinfo) if selected else None
    summary = summarize_today(selected)
    public_events = build_event_history(
        station_id, timezone_name, start.isoformat(), end.isoformat(), events,
    )["events"]
    return {
        "operation": "dashboard",
        "range": {
            "mode": "calendar_day",
            "timezone": timezone_name,
            "start_time": start.isoformat(),
            "latest_time": latest,
            "end_time": end.isoformat(),
            "interval_seconds": 60,
        },
        "realtime": dict(realtime or {}),
        "summary_today": {
            "start_time": start.isoformat(),
            "end_time": latest,
            **summary,
        },
        "trend": [{
            "data_time": _public_minute(point["data_time"], start.tzinfo),
            "pvStorage": point["pv_storage_efficiency"],
            "storageLoad": point["storage_load_efficiency"],
            "pvLoad": point["pv_load_efficiency"],
        } for point in selected],
        "events": public_events,
    }
