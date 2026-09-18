"""将分钟效率点和瓶颈事件幂等保存到 NocoBase。"""

from datetime import datetime, timezone
import json
import math
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from m2.station_efficiency_history import (
    build_event_upsert,
    build_minute_upsert,
)


ALLOWED_COLLECTIONS = {
    "t_efficiency_points",
    "t_efficiency_bottleneck_events",
    "t_efficiency_device_points",
}

_DEVICE_NUMERIC_FIELDS = (
    "active_power_kw", "rated_power_kw", "load_rate_pct",
    "battery_power_kw", "temperature_c",
)
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _normalize_device_record(record):
    """Decode NocoBase decimal strings at the storage boundary, not in rules."""
    normalized = dict(record)
    for field in _DEVICE_NUMERIC_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        if isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value.strip()):
            value = float(value)
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise StationEfficiencyStoreError(f"设备分钟数据 {field} 必须是有限数值或 null")
        normalized[field] = value
    return normalized


class StationEfficiencyStoreError(ValueError):
    """分钟效率或瓶颈事件无法安全写入 NocoBase。"""


def _config_text(config, field):
    value = config.get(field) if isinstance(config, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise StationEfficiencyStoreError(f"配置缺少 {field}")
    return value.strip()


def _base_url(config):
    value = _config_text(config, "nocobase_base_url").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise StationEfficiencyStoreError("nocobase_base_url 必须是 HTTP(S) 地址")
    return value


def _timeout(config):
    value = config.get("timeout_seconds", 10) if isinstance(config, dict) else 10
    if isinstance(value, bool):
        raise StationEfficiencyStoreError("timeout_seconds 必须是大于 0 的数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StationEfficiencyStoreError(
            "timeout_seconds 必须是大于 0 的数值"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise StationEfficiencyStoreError("timeout_seconds 必须是大于 0 的数值")
    return number


def _request_json(url, token, timeout, body):
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return _transport_json(request, timeout)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise StationEfficiencyStoreError("NocoBase 写入未授权") from exc
        raise StationEfficiencyStoreError(
            f"NocoBase 写入失败，HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, TypeError, ValueError) as exc:
        raise StationEfficiencyStoreError("NocoBase 写入失败") from exc


def _request_get_json(url, token, timeout):
    try:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        return _transport_json(request, timeout)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise StationEfficiencyStoreError("NocoBase 读取未授权") from exc
        raise StationEfficiencyStoreError(
            f"NocoBase 读取失败，HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, TypeError, ValueError) as exc:
        raise StationEfficiencyStoreError("NocoBase 读取失败") from exc


def _request_post_json(url, token, timeout):
    """执行不需要业务请求体的固定 NocoBase POST 操作。"""
    return _request_json(url, token, timeout, {})


def _transport_json(request, timeout):
    """Report allowlisted transport metadata immediately, without request secrets."""
    started = time.monotonic()

    def report(kind, status=None):
        resource = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        collection, _, operation = resource.partition(":")
        diagnostic = {
            "source": "m2_store_diagnostic",
            "collection": collection if collection in ALLOWED_COLLECTIONS else "unknown",
            "operation": operation if operation in {"list", "updateOrCreate", "destroy"} else "unknown",
            "error_type": kind,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
        }
        try:
            print(json.dumps(diagnostic, separators=(",", ":")), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass  # Diagnostic output must not change persistence behavior.

    status = None
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            payload = json.load(response)
    except HTTPError as exc:
        report("http_error", exc.code)
        raise
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        report("timeout" if isinstance(reason, TimeoutError) else "network_error")
        raise
    except (TypeError, ValueError):
        report("invalid_json", status)
        raise
    if not isinstance(payload, dict) or payload.get("errors"):
        report("response_error", status)
    else:
        operation = urlsplit(request.full_url).path.rsplit(":", 1)[-1]
        data = payload.get("data")
        valid = (
            _is_single_record(data) if operation == "updateOrCreate" else
            isinstance(data, list) if operation == "list" else
            type(data) is int and data >= 0 if operation == "destroy" else
            isinstance(data, (dict, list))
        )
        if not valid:
            report("invalid_response", status)
    return payload


def _canonical_time(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StationEfficiencyStoreError(f"{field} 不是合法时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StationEfficiencyStoreError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def fetch_minute_points(
    station_id,
    start_time,
    end_time,
    config,
    request_json=None,
):
    """读取一个场站在指定自然日内的分钟效率点。"""
    station_text = str(station_id).strip()
    if not station_text:
        raise StationEfficiencyStoreError("station_id 不能为空")
    start_utc = _canonical_time(start_time, "start_time")
    end_utc = _canonical_time(end_time, "end_time")
    if datetime.fromisoformat(end_utc) <= datetime.fromisoformat(start_utc):
        raise StationEfficiencyStoreError("end_time 必须晚于 start_time")

    filter_value = {
        "$and": [
            {"station_id": {"$eq": station_text}},
            {"data_time": {"$gte": start_utc, "$lt": end_utc}},
        ]
    }
    query = urlencode([
        (
            "filter",
            json.dumps(
                filter_value,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
        ("sort[]", "data_time"),
        ("pageSize", "1440"),
    ])
    request_json = request_json or _request_get_json
    payload = request_json(
        f"{_base_url(config)}/api/t_efficiency_points:list?{query}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
    )
    if not isinstance(payload, dict) or payload.get("errors"):
        raise StationEfficiencyStoreError("NocoBase 拒绝读取")
    records = payload.get("data")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        raise StationEfficiencyStoreError("NocoBase 未返回合法分钟数据")
    return records


def _read_records(payload, message):
    if not isinstance(payload, dict) or payload.get("errors"):
        raise StationEfficiencyStoreError("NocoBase 拒绝读取")
    records = payload.get("data")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        raise StationEfficiencyStoreError(message)
    return records


def _station_id(station_id):
    station_text = str(station_id).strip()
    if not station_text:
        raise StationEfficiencyStoreError("station_id 不能为空")
    return station_text


def _time_range(station_id, start_time, end_time):
    station_text = _station_id(station_id)
    start_utc = _canonical_time(start_time, "start_time")
    end_utc = _canonical_time(end_time, "end_time")
    if datetime.fromisoformat(end_utc) <= datetime.fromisoformat(start_utc):
        raise StationEfficiencyStoreError("end_time 必须晚于 start_time")
    return station_text, start_utc, end_utc


def _list_query(filter_value, *, sort=None, page_size=None, page=None):
    pairs = [
        (
            "filter",
            json.dumps(filter_value, ensure_ascii=False, separators=(",", ":")),
        ),
    ]
    if sort is not None:
        pairs.append(("sort[]", sort))
    if page_size is not None:
        pairs.append(("pageSize", str(page_size)))
    if page is not None:
        pairs.append(("page", str(page)))
    return urlencode(pairs)


def _fetch_event_pages(filter_value, config, request_json, message, sort=None):
    """读取所有 NocoBase 事件页面，避免默认分页遗漏活动事件。"""
    page = 1
    records = []
    while True:
        payload = request_json(
            f"{_base_url(config)}/api/t_efficiency_bottleneck_events:list?"
            f"{_list_query(filter_value, sort=sort, page_size=100, page=page)}",
            _config_text(config, "nocobase_token"),
            _timeout(config),
        )
        page_records = _read_records(payload, message)
        records.extend(page_records)
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            raise StationEfficiencyStoreError("NocoBase 未返回合法分页信息")
        total_pages = meta.get("totalPage")
        if type(total_pages) is not int:
            raise StationEfficiencyStoreError("NocoBase 未返回合法分页信息")
        if total_pages == 0:
            if page == 1 and not page_records:
                return records
            raise StationEfficiencyStoreError("NocoBase 未返回合法分页信息")
        if total_pages < page:
            raise StationEfficiencyStoreError("NocoBase 未返回合法分页信息")
        if page == total_pages:
            return records
        page += 1


def fetch_device_points(
    station_id,
    start_time,
    end_time,
    config,
    request_json=None,
):
    """读取一个场站指定时间范围内的设备分钟点。"""
    station_text, start_utc, end_utc = _time_range(
        station_id, start_time, end_time,
    )
    filter_value = {
        "$and": [
            {"station_id": {"$eq": station_text}},
            {"data_time": {"$gte": start_utc, "$lt": end_utc}},
        ]
    }
    request_json = request_json or _request_get_json
    payload = request_json(
        f"{_base_url(config)}/api/t_efficiency_device_points:list?"
        f"{_list_query(filter_value, sort='data_time', page_size=1440)}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
    )
    return [
        _normalize_device_record(record)
        for record in _read_records(payload, "NocoBase 未返回合法设备分钟数据")
    ]


def fetch_active_events(station_id, config, request_json=None):
    """读取场站当前仍处于活动状态的瓶颈事件。"""
    filter_value = {
        "$and": [
            {"station_id": {"$eq": _station_id(station_id)}},
            {"status": {"$eq": "active"}},
        ]
    }
    request_json = request_json or _request_get_json
    return _fetch_event_pages(
        filter_value,
        config,
        request_json,
        "NocoBase 未返回合法活动事件",
    )


def fetch_dashboard_events(
    station_id,
    start_time,
    end_time,
    config,
    request_json=None,
):
    """读取与查询区间重叠的活动或已恢复事件，包含跨日后恢复的记录。"""
    station_text, start_utc, end_utc = _time_range(
        station_id, start_time, end_time,
    )
    filter_value = {
        "$and": [
            {"station_id": {"$eq": station_text}},
            {"$or": [
                {"status": {"$eq": "active"}},
                {"$and": [
                    {"status": {"$eq": "recovered"}},
                    {"end_time": {"$gte": start_utc}},
                ]},
            ]},
            {"start_time": {"$lt": end_utc}},
        ]
    }
    request_json = request_json or _request_get_json
    return _fetch_event_pages(
        filter_value,
        config,
        request_json,
        "NocoBase 未返回合法看板事件",
        sort="start_time",
    )


def _is_single_record(data):
    # NocoBase may return the update branch as a one-record array.
    return isinstance(data, dict) or (
        isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict)
    )


def _saved_record(payload):
    if not isinstance(payload, dict):
        raise StationEfficiencyStoreError("NocoBase 未返回合法结果")
    if payload.get("errors"):
        raise StationEfficiencyStoreError("NocoBase 拒绝写入")
    record = payload.get("data")
    if not _is_single_record(record):
        raise StationEfficiencyStoreError("NocoBase 未返回保存后的记录")
    return record[0] if isinstance(record, list) else record


def _save_upsert(envelope, config, request_json=None):
    if not isinstance(envelope, dict) or set(envelope) != {
        "collection",
        "key",
        "values",
    }:
        raise StationEfficiencyStoreError("保存数据结构不正确")
    collection = envelope["collection"]
    key = envelope["key"]
    values = envelope["values"]
    if collection not in ALLOWED_COLLECTIONS:
        raise StationEfficiencyStoreError("不允许写入该数据表")
    if not isinstance(key, dict) or not key:
        raise StationEfficiencyStoreError("保存条件不能为空")
    if not isinstance(values, dict) or not values:
        raise StationEfficiencyStoreError("保存内容不能为空")

    request_json = request_json or _request_json
    query = urlencode([
        ("filterKeys[]", field)
        for field in key
    ])
    payload = request_json(
        f"{_base_url(config)}/api/{collection}:updateOrCreate?{query}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
        dict(values),
    )
    return _saved_record(payload)


def save_minute_point(point, config, request_json=None):
    """同场站同一分钟有则更新、无则新增。"""
    return _save_upsert(
        build_minute_upsert(point),
        config,
        request_json=request_json,
    )


def save_bottleneck_event(event, config, request_json=None):
    """同设备同起点事件有则更新、无则新增。"""
    return _save_upsert(
        build_event_upsert(event),
        config,
        request_json=request_json,
    )


def save_device_point(point, config, request_json=None):
    """按场站、设备类型、设备和时间幂等保存设备分钟点。"""
    if not isinstance(point, dict):
        raise StationEfficiencyStoreError("设备分钟数据必须是对象")
    values = dict(point)
    try:
        values["data_time"] = _canonical_time(values["data_time"], "data_time")
        key = {
            field: values[field]
            for field in ("station_id", "device_type", "device_id", "data_time")
        }
    except KeyError as exc:
        raise StationEfficiencyStoreError("设备分钟数据缺少唯一标识") from exc
    return _save_upsert(
        {
            "collection": "t_efficiency_device_points",
            "key": key,
            "values": values,
        },
        config,
        request_json=request_json,
    )


def delete_device_points_before(cutoff, config, request_json=None):
    """删除保留期之前的设备分钟点，返回 NocoBase 报告的删除数。"""
    cutoff_utc = _canonical_time(cutoff, "cutoff")
    filter_value = {"data_time": {"$lt": cutoff_utc}}
    query = _list_query(filter_value)
    request_json = request_json or _request_post_json
    payload = request_json(
        f"{_base_url(config)}/api/t_efficiency_device_points:destroy?{query}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
    )
    if not isinstance(payload, dict) or payload.get("errors"):
        raise StationEfficiencyStoreError("NocoBase 拒绝删除")
    deleted = payload.get("data")
    if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
        raise StationEfficiencyStoreError("NocoBase 未返回合法删除数量")
    return deleted
