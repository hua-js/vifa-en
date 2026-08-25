"""三条能效链路真实数据源的访问、字段转换与聚合。"""

import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from m2.station_energy_backend import normalize_source_record


ES02_PV_INVERTER_SNS = (
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


class StationEnergyDataError(ValueError):
    """数据源字段无法构造成完整三链路记录。"""


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
        code = errors[0].get("code") if isinstance(errors, list) and isinstance(errors[0], dict) else None
        suffix = f"：{code}" if code else ""
        raise StationEnergyDataError(f"数据源返回错误{suffix}")
    return payload


def _config_text(config, field):
    value = config.get(field) if isinstance(config, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise StationEnergyDataError(f"配置缺少 {field}")
    return value.strip()


def _payload_list(payload, source):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise StationEnergyDataError(f"{source} 未返回记录数组")
    return rows


def _payload_record(payload, source):
    record = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(record, dict):
        raise StationEnergyDataError(f"{source} 未返回设备记录")
    return record


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
    charge = sum(-value for value in values if value < 0)
    discharge = sum(value for value in values if value > 0)
    return charge, discharge


def _one_record_by_field(rows, identity_field, identity, source):
    if not isinstance(rows, list):
        raise StationEnergyDataError(f"{source} 记录必须是数组")
    if any(not isinstance(row, dict) for row in rows):
        raise StationEnergyDataError(f"{source} 数组中的每条记录必须是对象")
    matches = [row for row in rows if str(row.get(identity_field, "")).strip() == identity]
    if len(matches) != 1:
        raise StationEnergyDataError(f"{source} 必须且只能包含一条 {identity} 记录")
    return matches[0]


def extract_station_layout(payload, station_id):
    """从 en:list 树中提取一个电站的储能柜和 PCS 基地址。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise StationEnergyDataError("en:list 返回格式无效")

    cabinets = {}

    def visit(node, inherited_station_id=None):
        if not isinstance(node, dict):
            return
        es = node.get("es") if isinstance(node.get("es"), dict) else {}
        node_station_id = str(
            es.get("sn") or es.get("code") or inherited_station_id or ""
        ).strip()
        if (
            node_station_id == station_id
            and node.get("node_type") == "ess"
            and not node.get("is_aggregate")
        ):
            cabinet_sn = str(node.get("sn", "")).strip()
            local_url = str(node.get("local_url", "")).strip().rstrip("/")
            if not cabinet_sn or not local_url:
                raise StationEnergyDataError("储能柜缺少 sn 或 local_url")
            cabinets[cabinet_sn] = {
                "sn": cabinet_sn,
                "local_url": local_url,
                "is_master": node.get("is_master") is True,
            }
        for child in node.get("children") or []:
            visit(child, node_station_id)

    for root in payload["data"]:
        visit(root)

    ordered = [cabinets[sn] for sn in sorted(cabinets)]
    masters = [item["sn"] for item in ordered if item["is_master"]]
    if not ordered:
        raise StationEnergyDataError(f"场站 {station_id} 没有储能柜")
    if len(masters) != 1:
        raise StationEnergyDataError(f"场站 {station_id} 必须且只能有一个主柜")
    return {
        "station_id": station_id,
        "master_emu_sn": masters[0],
        "cabinets": [
            {"sn": item["sn"], "local_url": item["local_url"]}
            for item in ordered
        ],
    }


def fetch_station_source_record(station_id, config, request_json=None):
    """读取一个场站当前原始数据并返回三链路标准记录。"""
    if not isinstance(station_id, str) or not station_id.strip():
        raise StationEnergyDataError("station_id 不能为空")
    station_id = station_id.strip()
    timeout = config.get("timeout_seconds", 10) if isinstance(config, dict) else 10
    timeout = _number(timeout, "timeout_seconds")
    if timeout <= 0:
        raise StationEnergyDataError("timeout_seconds 必须大于 0")
    request_json = request_json or _request_json

    tree_payload = request_json(
        _config_text(config, "station_tree_url"),
        _config_text(config, "station_tree_token"),
        timeout,
    )
    layout = extract_station_layout(tree_payload, station_id)
    emu_rows = _payload_list(
        request_json(
            _config_text(config, "emu_url"),
            _config_text(config, "emu_token"),
            timeout,
        ),
        "t_emu:list",
    )

    pcs_token = _config_text(config, "pcs_token")
    pcs_records_by_cabinet = {}
    for cabinet in layout["cabinets"]:
        cabinet_sn = cabinet["sn"]
        pcs_records_by_cabinet[cabinet_sn] = []
        for index in (1, 2):
            url = (
                f"{cabinet['local_url']}/api/t_pcs_{index}:get"
                "?filter=%7B%7D"
            )
            payload = request_json(url, pcs_token, timeout)
            pcs_records_by_cabinet[cabinet_sn].append(
                _payload_record(payload, f"{cabinet_sn}.t_pcs_{index}:get")
            )

    growall_rows = []
    if station_id == "ES02":
        growall_rows = _payload_list(
            request_json(
                _config_text(config, "growall_url"),
                _config_text(config, "growall_token"),
                timeout,
            ),
            "t_growall:list",
        )

    return build_station_source_record(
        station_id=station_id,
        master_emu_sn=layout["master_emu_sn"],
        cabinet_sns=tuple(item["sn"] for item in layout["cabinets"]),
        emu_rows=emu_rows,
        growall_rows=growall_rows,
        pcs_records_by_cabinet=pcs_records_by_cabinet,
    )


def build_station_source_record(
    *,
    station_id,
    master_emu_sn,
    cabinet_sns,
    emu_rows,
    growall_rows,
    pcs_records_by_cabinet,
):
    """把一个场站的实时原始记录转换成纯计算模块的标准输入。"""
    pv_inverter_sns = ES02_PV_INVERTER_SNS if station_id == "ES02" else ()
    cabinet_rows = {}
    source_times = {}
    for cabinet_sn in cabinet_sns:
        row = _one_record_by_field(emu_rows, "emu_sn", cabinet_sn, "t_emu")
        if str(row.get("f_es_sn", "")).strip() != station_id:
            raise StationEnergyDataError(f"{cabinet_sn} 不属于场站 {station_id}")
        cabinet_rows[cabinet_sn] = row
        source_times[f"emu:{cabinet_sn}"] = _timestamp(
            row, "last_time_iso", f"t_emu:{cabinet_sn}"
        )

    if master_emu_sn not in cabinet_rows:
        raise StationEnergyDataError("主柜不在场站储能柜列表中")
    master_row = cabinet_rows[master_emu_sn]

    pv_rows = {}
    for inverter_sn in pv_inverter_sns:
        row = _one_record_by_field(
            growall_rows, "sn", inverter_sn, "t_growall"
        )
        pv_rows[inverter_sn] = row
        source_times[f"pv_inverter:{inverter_sn}"] = _timestamp(
            row, "timestamp", f"t_growall:{inverter_sn}"
        )

    pcs_values = []
    seen_pcs_sns = set()
    for cabinet_sn in cabinet_sns:
        records = pcs_records_by_cabinet.get(cabinet_sn)
        if not isinstance(records, list) or len(records) != 2:
            raise StationEnergyDataError(f"{cabinet_sn} 必须包含两台 PCS")
        if any(not isinstance(record, dict) for record in records):
            raise StationEnergyDataError(
                f"{cabinet_sn} PCS 数组中的每条记录必须是对象"
            )
        for record in records:
            if str(record.get("fk_emu_sn", "")).strip() != cabinet_sn:
                raise StationEnergyDataError(f"{cabinet_sn} 的 PCS 柜号不匹配")
            pcs_sn = str(record.get("sn", "")).strip()
            if not pcs_sn:
                raise StationEnergyDataError(f"{cabinet_sn} 的 PCS 缺少 sn")
            if pcs_sn in seen_pcs_sns:
                raise StationEnergyDataError(f"PCS sn 重复：{pcs_sn}")
            seen_pcs_sns.add(pcs_sn)
            source_times[f"pcs:{pcs_sn}"] = _timestamp(
                record, "timestamp", f"PCS:{pcs_sn}"
            )
            pcs_values.append(_number(record.get("a6039"), f"PCS:{pcs_sn}.a6039"))

    cabinet_meter_values = [
        _number(row.get("latest_power"), f"t_emu:{sn}.latest_power")
        for sn, row in cabinet_rows.items()
    ]
    battery_values = [
        _number(row.get("battery_power"), f"t_emu:{sn}.battery_power")
        for sn, row in cabinet_rows.items()
    ]
    cabinet_charge, cabinet_discharge = _split_signed(cabinet_meter_values)
    bms_charge, bms_discharge = _split_signed(battery_values)
    pcs_charge, pcs_discharge = _split_signed(pcs_values)
    grid_kw = _number(
        master_row.get("latest_grid_power"),
        f"t_emu:{master_emu_sn}.latest_grid_power",
    ) / 1000.0
    grid_export, grid_import = _split_signed([grid_kw])

    record = {
        "bus_id": station_id,
        "data_time": _timestamp(
            master_row, "last_time_iso", f"t_emu:{master_emu_sn}"
        ),
        "pv_dc_power": sum(
            _number(row.get("a1"), f"t_growall:{sn}.a1")
            for sn, row in pv_rows.items()
        ),
        "pv_ac_power": sum(
            _number(row.get("a35"), f"t_growall:{sn}.a35")
            for sn, row in pv_rows.items()
        ),
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
