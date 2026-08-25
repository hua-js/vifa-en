"""从 t_emu:list 构造场站三条能效链路的标准输入。"""

import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from m2.station_energy_backend import normalize_source_record


STATION_CONFIGS = {
    "ES01": {
        "master_emu_sn": "emu11",
        "cabinet_sns": ("emu11", "emu12"),
        "has_pv": False,
    },
    "ES02": {
        "master_emu_sn": "emu26",
        "cabinet_sns": (
            "emu21",
            "emu22",
            "emu23",
            "emu24",
            "emu25",
            "emu26",
        ),
        "has_pv": True,
    },
}


class StationEnergyDataError(ValueError):
    """t_emu 数据无法构造成完整三链路记录。"""


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
        raise StationEnergyDataError(f"数据源请求失败，HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise StationEnergyDataError("数据源请求失败或未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise StationEnergyDataError("数据源未返回 JSON 对象")
    errors = payload.get("errors")
    if errors:
        code = (
            errors[0].get("code")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict)
            else None
        )
        suffix = f"：{code}" if code else ""
        raise StationEnergyDataError(f"数据源返回错误{suffix}")
    return payload


def _number(value, field):
    if isinstance(value, bool):
        raise StationEnergyDataError(f"{field} 必须是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StationEnergyDataError(f"{field} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise StationEnergyDataError(f"{field} 必须是有限数值")
    return number


def _timestamp(record, field, source):
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StationEnergyDataError(f"{source} 缺少时间字段 {field}")
    return value.strip()


def _split_signed(values):
    negative = sum(-value for value in values if value < 0)
    positive = sum(value for value in values if value > 0)
    return negative, positive


def _config_text(config, field):
    value = config.get(field) if isinstance(config, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise StationEnergyDataError(f"配置缺少 {field}")
    return value.strip()


def _payload_list(payload, source):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise StationEnergyDataError(f"{source} 未返回记录数组")
    if any(not isinstance(row, dict) for row in rows):
        raise StationEnergyDataError(f"{source} 数组中的每条记录必须是对象")
    return rows


def _station_config(station_id):
    if not isinstance(station_id, str) or station_id.strip() not in STATION_CONFIGS:
        raise StationEnergyDataError("station_id 只支持 ES01 或 ES02")
    station_id = station_id.strip()
    return station_id, STATION_CONFIGS[station_id]


def _station_rows(emu_rows, station_id, cabinet_sns):
    if not isinstance(emu_rows, list):
        raise StationEnergyDataError("t_emu 记录必须是数组")
    if any(not isinstance(row, dict) for row in emu_rows):
        raise StationEnergyDataError("t_emu 数组中的每条记录必须是对象")

    selected = [
        row
        for row in emu_rows
        if str(row.get("f_es_sn", "")).strip() == station_id
    ]
    rows_by_sn = {}
    for row in selected:
        emu_sn = str(row.get("emu_sn", "")).strip()
        if not emu_sn:
            raise StationEnergyDataError(f"场站 {station_id} 存在缺少 emu_sn 的记录")
        if emu_sn in rows_by_sn:
            raise StationEnergyDataError(f"场站 {station_id} 的 {emu_sn} 记录重复")
        rows_by_sn[emu_sn] = row

    expected = set(cabinet_sns)
    actual = set(rows_by_sn)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise StationEnergyDataError(
            f"场站 {station_id} 储能柜集合不匹配；"
            f"缺少 {missing}；多出 {unexpected}"
        )
    return rows_by_sn


def build_station_source_record(*, station_id, emu_rows):
    """将 t_emu:list 全量记录转换为指定场站的三链路标准输入。"""
    station_id, station = _station_config(station_id)
    cabinet_sns = station["cabinet_sns"]
    rows_by_sn = _station_rows(emu_rows, station_id, cabinet_sns)
    master_emu_sn = station["master_emu_sn"]
    master_row = rows_by_sn[master_emu_sn]

    cabinet_values = []
    battery_values = []
    pcs_values = []
    source_times = {}
    for cabinet_sn in cabinet_sns:
        row = rows_by_sn[cabinet_sn]
        source_times[f"emu:{cabinet_sn}"] = _timestamp(
            row, "last_time_iso", f"t_emu:{cabinet_sn}"
        )
        cabinet_values.append(
            _number(row.get("latest_power"), f"t_emu:{cabinet_sn}.latest_power")
        )
        battery_values.append(
            _number(row.get("battery_power"), f"t_emu:{cabinet_sn}.battery_power")
        )
        pcs_values.extend((
            _number(row.get("pcs1_power"), f"t_emu:{cabinet_sn}.pcs1_power"),
            _number(row.get("pcs2_power"), f"t_emu:{cabinet_sn}.pcs2_power"),
        ))

    cabinet_charge, cabinet_discharge = _split_signed(cabinet_values)
    bms_charge, bms_discharge = _split_signed(battery_values)
    pcs_charge, pcs_discharge = _split_signed(pcs_values)

    grid_kw = _number(
        master_row.get("latest_grid_power"),
        f"t_emu:{master_emu_sn}.latest_grid_power",
    ) / 1000.0
    grid_export, grid_import = _split_signed([grid_kw])

    if station["has_pv"]:
        pv_dc_power = _number(
            master_row.get("input_ac_solar_power"),
            f"t_emu:{master_emu_sn}.input_ac_solar_power",
        )
        pv_ac_power = _number(
            master_row.get("latest_solar_power"),
            f"t_emu:{master_emu_sn}.latest_solar_power",
        )
    else:
        pv_dc_power = 0.0
        pv_ac_power = 0.0

    record = {
        "bus_id": station_id,
        "data_time": _timestamp(
            master_row, "last_time_iso", f"t_emu:{master_emu_sn}"
        ),
        "pv_dc_power": pv_dc_power,
        "pv_ac_power": pv_ac_power,
        "load_power": _number(
            master_row.get("load_power"), f"t_emu:{master_emu_sn}.load_power"
        ),
        "cabinet_charge_power": cabinet_charge,
        "cabinet_discharge_power": cabinet_discharge,
        "pcs_charge_power": pcs_charge,
        "pcs_discharge_power": pcs_discharge,
        "bms_charge_power": bms_charge,
        "bms_discharge_power": bms_discharge,
        "grid_import_power": grid_import,
        "grid_export_power": grid_export,
        "storage_aux_power": 0.0,
        "device_status": {},
        "source_times": source_times,
    }
    return normalize_source_record(record)


def fetch_station_source_record(station_id, config, request_json=None):
    """读取一次 t_emu:list 并返回指定场站的三链路标准输入。"""
    _station_config(station_id)
    timeout = config.get("timeout_seconds", 10) if isinstance(config, dict) else 10
    timeout = _number(timeout, "timeout_seconds")
    if timeout <= 0:
        raise StationEnergyDataError("timeout_seconds 必须大于 0")
    request_json = request_json or _request_json
    payload = request_json(
        _config_text(config, "emu_url"),
        _config_text(config, "emu_token"),
        timeout,
    )
    return build_station_source_record(
        station_id=station_id,
        emu_rows=_payload_list(payload, "t_emu:list"),
    )
