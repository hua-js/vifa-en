"""将逐设备原始采样转换为可持久化的分钟设备点。"""

import json
import math
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from m2.station_energy_data_adapter import (
    StationEnergyDataError,
    get_station_config,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
INVERTER_SNS = (
    "emu1",
    "emu2",
    "emu3",
    "emu4",
    "emu5",
    "emu21",
    "emu22",
    "emu23",
    "emu24",
)
INVERTER_SNS_BY_STATION = {"ES02": INVERTER_SNS}
INVERTER_RATED_POWER_KW = 60.0
DEVICE_SAMPLE_MAX_AGE_MINUTES = 2.0
DEVICE_POINT_FIELDS = (
    "station_id",
    "device_type",
    "device_id",
    "device_name",
    "subdevice_id",
    "data_time",
    "source_time",
    "active_power_kw",
    "rated_power_kw",
    "load_rate_pct",
    "battery_power_kw",
    "temperature_c",
)


class StationEfficiencyDeviceDataError(ValueError):
    """逐设备数据源或采样无法安全转换为分钟点。"""


def _request_json(url, token, timeout):
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
            payload = json.load(response)
    except HTTPError as exc:
        raise StationEfficiencyDeviceDataError(
            f"逐设备数据源请求失败，HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, TypeError, ValueError) as exc:
        raise StationEfficiencyDeviceDataError(
            "逐设备数据源请求失败或未返回合法 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise StationEfficiencyDeviceDataError("逐设备数据源未返回 JSON 对象")
    errors = payload.get("errors")
    if errors:
        code = (
            errors[0].get("code")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict)
            else None
        )
        suffix = f"：{code}" if code else ""
        raise StationEfficiencyDeviceDataError(f"逐设备数据源返回错误{suffix}")
    return payload


def _config_text(config, field):
    value = config.get(field) if isinstance(config, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise StationEfficiencyDeviceDataError(f"配置缺少 {field}")
    return value.strip()


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timeout(config):
    value = config.get("timeout_seconds", 10) if isinstance(config, dict) else 10
    timeout = _number(value)
    if timeout is None:
        raise StationEfficiencyDeviceDataError("timeout_seconds 必须是有限数值")
    if timeout <= 0:
        raise StationEfficiencyDeviceDataError("timeout_seconds 必须大于 0")
    return timeout


def _payload_list(payload, source):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise StationEfficiencyDeviceDataError(f"{source} 未返回记录数组")
    if any(not isinstance(row, dict) for row in rows):
        raise StationEfficiencyDeviceDataError(f"{source} 数组中的每条记录必须是对象")
    return rows


def fetch_growall_rows(config, request_json=None):
    """读取并验证一次 t_growall:list 的原始逆变器记录。"""
    request_json = request_json or _request_json
    payload = request_json(
        _config_text(config, "growall_url"),
        _config_text(config, "growall_token"),
        _timeout(config),
    )
    return _payload_list(payload, "t_growall:list")


def _shanghai_time(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StationEfficiencyDeviceDataError(f"{field} 不是合法时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StationEfficiencyDeviceDataError(f"{field} 必须包含时区")
    return parsed.astimezone(SHANGHAI_TIMEZONE)


def _max_sample_age(config):
    value = (
        config.get("device_sample_max_age_minutes", DEVICE_SAMPLE_MAX_AGE_MINUTES)
        if isinstance(config, dict)
        else DEVICE_SAMPLE_MAX_AGE_MINUTES
    )
    minutes = _number(value)
    if minutes is None or minutes < 0 or minutes > DEVICE_SAMPLE_MAX_AGE_MINUTES:
        raise StationEfficiencyDeviceDataError(
            "device_sample_max_age_minutes 必须在 0 到 2 之间"
        )
    return timedelta(minutes=minutes)


def _valid_source_time(value, calculation_time, max_age):
    try:
        source_time = _shanghai_time(value, "source_time")
    except StationEfficiencyDeviceDataError:
        return None
    age = calculation_time - source_time
    if age < timedelta(0) or age > max_age:
        return None
    return source_time


def _station_config(station_id):
    try:
        return get_station_config(station_id)
    except StationEnergyDataError as exc:
        raise StationEfficiencyDeviceDataError(str(exc)) from exc


def _configured_inverter_sns(station_id, config):
    if station_id == "ES01":
        return ()
    configured = (
        config.get("pv_inverter_sns_by_station", INVERTER_SNS_BY_STATION)
        if isinstance(config, dict)
        else INVERTER_SNS_BY_STATION
    )
    values = configured.get("ES02") if isinstance(configured, dict) else None
    if not isinstance(values, (list, tuple)):
        raise StationEfficiencyDeviceDataError("ES02 逆变器配置必须是固定九台设备")
    normalized = tuple(str(value).strip() for value in values)
    if normalized != INVERTER_SNS:
        raise StationEfficiencyDeviceDataError("ES02 逆变器配置必须是固定九台设备")
    return normalized


def _empty_device_point():
    return {field: None for field in DEVICE_POINT_FIELDS}


def _fixed_inverter_rated_power(config):
    configured = (
        config.get("pv_inverter_rated_power_kw", INVERTER_RATED_POWER_KW)
        if isinstance(config, dict) else INVERTER_RATED_POWER_KW
    )
    rated_power_kw = _number(configured)
    if rated_power_kw != INVERTER_RATED_POWER_KW:
        raise StationEfficiencyDeviceDataError(
            "pv_inverter_rated_power_kw 必须固定为 60"
        )
    return rated_power_kw


def _inverter_point(
    station_id, device_id, minute_bucket_time, source_time, active_power_kw,
    rated_power_kw,
):
    point = _empty_device_point()
    point.update({
        "station_id": station_id,
        "device_type": "pv_inverter",
        "device_id": device_id,
        "device_name": f"光伏逆变器 {device_id}",
        "data_time": minute_bucket_time.isoformat(),
        "source_time": source_time.isoformat(),
        "active_power_kw": active_power_kw,
        "rated_power_kw": rated_power_kw,
        "load_rate_pct": active_power_kw / rated_power_kw * 100.0,
    })
    return point


def _battery_point(
    station_id,
    device_id,
    minute_bucket_time,
    source_time,
    battery_power_kw,
    temperature_c,
    subdevice_id,
):
    point = _empty_device_point()
    point.update({
        "station_id": station_id,
        "device_type": "battery_cabinet",
        "device_id": device_id,
        "device_name": f"电池柜 {device_id}",
        "subdevice_id": subdevice_id,
        "data_time": minute_bucket_time.isoformat(),
        "source_time": source_time.isoformat(),
        "battery_power_kw": battery_power_kw,
        "temperature_c": temperature_c,
    })
    return point


def build_inverter_device_points(
    *, station_id, growall_rows, minute_bucket_time, calculation_time, config,
):
    """为 ES02 的九台光伏逆变器选择当前分钟的最新有效采样。"""
    station = _station_config(station_id)
    station_id = station["station_id"]
    inverter_sns = _configured_inverter_sns(station_id, config)
    if not inverter_sns:
        return []
    if not isinstance(growall_rows, list):
        raise StationEfficiencyDeviceDataError("t_growall 记录必须是数组")
    bucket_time = _shanghai_time(minute_bucket_time, "minute_bucket_time")
    exact_calculation_time = _shanghai_time(calculation_time, "calculation_time")
    max_age = _max_sample_age(config)
    rated_power_kw = _fixed_inverter_rated_power(config)
    selected = {}
    for row in growall_rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("sn", "")).strip()
        if device_id not in inverter_sns:
            continue
        source_time = _valid_source_time(
            row.get("timestamp"), exact_calculation_time, max_age,
        )
        active_power_kw = _number(row.get("a35"))
        if source_time is None or active_power_kw is None or active_power_kw < 0:
            continue
        previous = selected.get(device_id)
        if previous is None or source_time > previous[0]:
            selected[device_id] = (source_time, active_power_kw)
    return [
        _inverter_point(
            station_id,
            device_id,
            bucket_time,
            selected[device_id][0],
            selected[device_id][1],
            rated_power_kw,
        )
        for device_id in inverter_sns
        if device_id in selected
    ]


def _optional_temperature(row, field):
    if not field:
        return None
    return _number(row.get(field))


def _optional_subdevice_id(row, field):
    if not field:
        return None
    value = row.get(field)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def build_battery_device_points(
    *, station_id, emu_rows, minute_bucket_time, calculation_time, config,
):
    """从 t_emu 的已配置电池柜读取当前分钟的有效功率与可选温度。"""
    station = _station_config(station_id)
    station_id = station["station_id"]
    if not isinstance(emu_rows, list):
        raise StationEfficiencyDeviceDataError("t_emu 记录必须是数组")
    bucket_time = _shanghai_time(minute_bucket_time, "minute_bucket_time")
    exact_calculation_time = _shanghai_time(calculation_time, "calculation_time")
    max_age = _max_sample_age(config)
    temperature_field = (
        config.get("battery_max_temperature_field", "")
        if isinstance(config, dict)
        else ""
    )
    cluster_field = (
        config.get("battery_hot_cluster_field", "")
        if isinstance(config, dict)
        else ""
    )
    temperature_field = temperature_field.strip() if isinstance(temperature_field, str) else ""
    cluster_field = cluster_field.strip() if isinstance(cluster_field, str) else ""
    selected = {}
    cabinets = station["cabinet_sns"]
    for row in emu_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("f_es_sn", "")).strip() != station_id:
            continue
        device_id = str(row.get("emu_sn", "")).strip()
        if device_id not in cabinets:
            continue
        source_time = _valid_source_time(
            row.get("last_time_iso"), exact_calculation_time, max_age,
        )
        battery_power_w = _number(row.get("battery_power"))
        if source_time is None or battery_power_w is None:
            continue
        previous = selected.get(device_id)
        if previous is None or source_time > previous[0]:
            selected[device_id] = (
                source_time,
                battery_power_w / 1000.0,
                _optional_temperature(row, temperature_field),
                _optional_subdevice_id(row, cluster_field),
            )
    return [
        _battery_point(
            station_id,
            device_id,
            bucket_time,
            selected[device_id][0],
            selected[device_id][1],
            selected[device_id][2],
            selected[device_id][3],
        )
        for device_id in cabinets
        if device_id in selected
    ]
