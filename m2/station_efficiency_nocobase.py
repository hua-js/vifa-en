"""将分钟效率点和瓶颈事件幂等保存到 NocoBase。"""

from datetime import datetime, timezone
import json
import math
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
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
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
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
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


def _list_query(filter_value, *, sort=None, page_size=None):
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
    return urlencode(pairs)


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
    return _read_records(payload, "NocoBase 未返回合法设备分钟数据")


def fetch_active_events(station_id, config, request_json=None):
    """读取场站当前仍处于活动状态的瓶颈事件。"""
    filter_value = {
        "$and": [
            {"station_id": {"$eq": _station_id(station_id)}},
            {"status": {"$eq": "active"}},
        ]
    }
    request_json = request_json or _request_get_json
    payload = request_json(
        f"{_base_url(config)}/api/t_efficiency_bottleneck_events:list?"
        f"{_list_query(filter_value)}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
    )
    return _read_records(payload, "NocoBase 未返回合法活动事件")


def fetch_dashboard_events(
    station_id,
    start_time,
    end_time,
    config,
    request_json=None,
):
    """读取当天活动中的事件，以及当天恢复的事件。"""
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
                    {"end_time": {"$gte": start_utc, "$lt": end_utc}},
                ]},
            ]},
        ]
    }
    request_json = request_json or _request_get_json
    payload = request_json(
        f"{_base_url(config)}/api/t_efficiency_bottleneck_events:list?"
        f"{_list_query(filter_value, sort='start_time')}",
        _config_text(config, "nocobase_token"),
        _timeout(config),
    )
    return _read_records(payload, "NocoBase 未返回合法看板事件")


def _saved_record(payload):
    if not isinstance(payload, dict):
        raise StationEfficiencyStoreError("NocoBase 未返回合法结果")
    if payload.get("errors"):
        raise StationEfficiencyStoreError("NocoBase 拒绝写入")
    record = payload.get("data")
    if not isinstance(record, dict):
        raise StationEfficiencyStoreError("NocoBase 未返回保存后的记录")
    return record


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
    try:
        key = {
            field: point[field]
            for field in ("station_id", "device_type", "device_id", "data_time")
        }
    except KeyError as exc:
        raise StationEfficiencyStoreError("设备分钟数据缺少唯一标识") from exc
    return _save_upsert(
        {
            "collection": "t_efficiency_device_points",
            "key": key,
            "values": dict(point),
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
