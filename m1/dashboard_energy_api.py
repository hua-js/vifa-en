#!/usr/bin/env python3
"""单客户运营监控看板 API。

NocoBase 只返回目标客户的一个场站、ES01/ES02 两个电站及其能源节点；
Growatt 只输出固定九台逆变器的汇总源数据。响应不包含其他客户或旧告警
等级数据。光伏、储能、负载分别从固定逆变器、固定柜体和精确电站记录
派生，缺少权威源数据时不回退到其他实时表。
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlencode
import urllib.request
from zoneinfo import ZoneInfo


# 默认 Token 供无参数启动使用；部署时仍可通过 --token 覆盖。
DEFAULT_TK = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEsInRlbXAiOnRydWUsImlhdCI6MTc4NjY3ODkzNSwic2lnbkluVGltZSI6MTc4NjY3ODkzNTkxMSwiZXhwIjoxNzg2NzY1MzM1LCJqdGkiOiI0MzEyOGFiOS01M2NhLTRiZjItOGRjNC0zMjIzNjdkNGVjMDUifQ.Fu69uKtaEpZcReCjtpvh-vzOsZ86LB-SDcnLyk7P1bM"
DEFAULT_E606_TK = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEsInJvbGVOYW1lIjoicm9vdCIsImlhdCI6MTc4NzM5MjA1OSwiZXhwIjozMzM0NDk5MjA1OX0.ZJT1Lg_MbkT6R243Nl3ojBwWDmnA-slbLyVgqjn0k9M"

# NocoBase 原看板数据源。
BASE = "http://holobase/api"

# Growatt 逆变器历史表：最近记录排在前面，后续仍按 SN 再次选最新值。
GROWATT_URL = (
    "http://e606pro.hlszh.com:3510/api/t_growall:list?"
    "pageSize=20&sort%5B%5D=-timestamp&filter=%7B%7D"
)

# 两类功率必须读取业务权威表，不能使用可能混入其他客户的 en_realtime.power。
ENERGY_SOURCE_BASE = "https://vifa.hlszh.com/api"
STATION_LOAD_URL = f"{ENERGY_SOURCE_BASE}/t_es_data:list"
CABINET_POWER_URL = f"{ENERGY_SOURCE_BASE}/t_emu:list"

# 目标客户与固定拓扑；服务端先隔离，前端只接收这一份数据。
TARGET_SITE_ID = "363738052231168"
TARGET_STATIONS = (
    {"code": "ES01", "name": "电站1", "cabinet_sns": ("emu11", "emu12")},
    {
        "code": "ES02",
        "name": "电站2",
        "cabinet_sns": (
            "emu21",
            "emu22",
            "emu23",
            "emu24",
            "emu25",
            "emu26",
        ),
        "pv_meter": {"name": "光伏计量表", "emu_sn": "emu27", "inverter_count": 9},
    },
)

# 单条最新有效数据的转换效率严格低于 85% 时触发告警。
PV_EFFICIENCY_MIN_PCT = 85.0
# 聚合储能处于非充放电状态时，自损耗率严格高于 2% 触发告警。
ESS_SELF_LOSS_MAX_PCT = 2.0
ESS_IDLE_STATUSES = frozenset({"wait", "standby", "stop"})
# 聚合负载单条实时功率严格高于 1000 kW 时触发告警。
LOAD_SPIKE_MIN_KW = 1000.0
REFRESH_INTERVAL_SECONDS = 20
ALERT_HISTORY_LIMIT = 1000
DEFAULT_HISTORY_DB = Path(__file__).resolve().parent / "run" / "alert_history.sqlite3"

# Growatt 返回的无时区时间按上海本地时间解释。
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 看板固定保留的 9 台 Growatt 光伏逆变器，缺少数据时仍返回离线卡片。
GROWATT_INVERTER_SNS = (
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


def api(path: str, token: str) -> dict[str, Any]:
    """请求现有 NocoBase API，保持 dashboard_api.py 的失败行为。"""
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return {"error": str(error)}


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """安全取得列表响应中的 data；响应结构异常时按空列表处理。"""
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return data if isinstance(data, list) else []


def _required_items(payload: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """基础数据源失败时显式终止，避免把鉴权失败伪装成零数据。"""
    if not isinstance(payload, dict):
        raise RuntimeError(f"{source} 未返回 JSON 对象")
    if payload.get("error"):
        raise RuntimeError(f"{source} 读取失败：{payload['error']}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"{source} 未返回记录数组")
    return data


def _list_path(
    resource: str,
    *,
    page_size: int,
    fields: str,
    filter_value: dict[str, Any],
) -> str:
    """生成 NocoBase 精确白名单列表请求。"""
    query = urlencode(
        {
            "pageSize": page_size,
            "fields": fields,
            "filter": json.dumps(filter_value, separators=(",", ":")),
        }
    )
    return f"/{resource}:list?{query}"


def _external_list_url(
    url: str,
    *,
    page_size: int,
    fields: str,
    filter_value: dict[str, Any],
    sort: str | None = None,
) -> str:
    """为外部 NocoBase 表生成带精确过滤条件的只读 URL。"""
    params: dict[str, Any] = {
        "pageSize": page_size,
        "fields": fields,
        "filter": json.dumps(filter_value, separators=(",", ":")),
    }
    if sort:
        params["sort"] = sort
    return f"{url}?{urlencode(params)}"


def station_code(item: dict[str, Any]) -> str | None:
    """只识别目标客户约定的 ES01/ES02 电站身份。"""
    explicit = str(item.get("station_code") or "").strip().upper()
    if explicit in {station["code"] for station in TARGET_STATIONS}:
        return explicit
    values = (item.get("sn"), item.get("name"))
    for station in TARGET_STATIONS:
        code = station["code"]
        if any(str(value or "").strip().upper().endswith(code) for value in values):
            return code
    return None


def fetch_raw_data(token: str) -> dict[str, Any]:
    """按目标场站和两座电站白名单构造隔离后的基础响应。"""
    # 1. 场站：请求层和响应层都锁定唯一客户。
    sites_data = api(
        _list_path(
            "site",
            page_size=1,
            fields="id,name,site,status,edge_version,edge_version_type,access_time",
            filter_value={
                "$and": [
                    {"id": {"$eq": int(TARGET_SITE_ID)}},
                    {"status": {"$eq": "enabled"}},
                ]
            },
        ),
        token,
    )
    sites = [
        item
        for item in _required_items(sites_data, "目标客户场站")
        if str(item.get("id")) == TARGET_SITE_ID
    ]
    if len(sites) != 1:
        raise RuntimeError("目标客户场站不存在或无权访问")
    output_sites = [
        {
            "id": item["id"],
            "name": item.get("name", ""),
            "site": item.get("site", ""),
            "status": item.get("status", ""),
            "edge_version": item.get("edge_version", ""),
            "edge_version_type": item.get("edge_version_type", ""),
            "access_time": item.get("access_time", ""),
        }
        for item in sites
    ]

    # 2. 电站：只允许目标场站下的 ES01、ES02，并统一展示名称。
    es_data = api(
        _list_path(
            "es",
            page_size=20,
            fields="id,fk_site,name,sn,status,installed_power,power_storage",
            filter_value={"fk_site": {"$eq": int(TARGET_SITE_ID)}},
        ),
        token,
    )
    station_rows: dict[str, dict[str, Any]] = {}
    for item in _required_items(es_data, "目标客户电站"):
        if str(item.get("fk_site")) != TARGET_SITE_ID:
            continue
        code = station_code(item)
        if code and code not in station_rows:
            station_rows[code] = item
    missing = [
        station["code"]
        for station in TARGET_STATIONS
        if station["code"] not in station_rows
    ]
    if missing:
        raise RuntimeError("目标客户拓扑不完整：缺少 " + "、".join(missing))
    es_list = [station_rows[station["code"]] for station in TARGET_STATIONS]
    es_to_site = {str(item["id"]): item.get("fk_site") for item in es_list}
    output_es = []
    for station, item in zip(TARGET_STATIONS, es_list):
        output_es.append(
            {
                "id": item["id"],
                "name": station["name"],
                "sn": item.get("sn") or station["code"],
                "station_code": station["code"],
                "fk_site": item.get("fk_site"),
                "status": item.get("status", ""),
                "installed_power": item.get("installed_power"),
                "power_storage": item.get("power_storage"),
            }
        )

    # 3. 能源节点：请求只覆盖两座目标电站，层级字段保持原值。
    target_es_ids = [item["id"] for item in es_list]
    nodes_data = api(
        _list_path(
            "en",
            page_size=500,
            fields="id,sn,name,node_type,is_aggregate,run_status,fk_es,parentId",
            filter_value={"fk_es": {"$in": target_es_ids}},
        ),
        token,
    )
    target_es_id_set = {str(item) for item in target_es_ids}
    nodes = [
        item
        for item in _required_items(nodes_data, "目标客户能源节点")
        if str(item.get("fk_es")) in target_es_id_set
    ]
    output_nodes = []
    for item in nodes:
        fk_es = item.get("fk_es")
        output_nodes.append(
            {
                "id": item["id"],
                "sn": item.get("sn", ""),
                "name": item.get("name", ""),
                "node_type": item.get("node_type", ""),
                "is_aggregate": item.get("is_aggregate", False),
                "run_status": item.get("run_status", ""),
                "fk_es": fk_es,
                "fk_site_id": es_to_site.get(str(fk_es)),
                "parentId": item.get("parentId"),
            }
        )

    # 4. 实时数据：只请求目标拓扑中的节点。
    target_node_ids = [item["id"] for item in output_nodes]
    realtime_data = api(
        _list_path(
            "en_realtime",
            page_size=500,
            fields="fk_en,power,soc,run_status,timestamp",
            filter_value={"fk_en": {"$in": target_node_ids}},
        ),
        token,
    )
    target_node_id_set = {str(item) for item in target_node_ids}
    realtime = [
        item
        for item in _required_items(realtime_data, "目标客户实时数据")
        if str(item.get("fk_en")) in target_node_id_set
    ]
    realtime_map = {str(item["fk_en"]): item for item in realtime}
    output_realtime = [
        {
            "fk_en": item["fk_en"],
            "power": item.get("power"),
            "soc": item.get("soc"),
            "run_status": item.get("run_status", ""),
            "timestamp": item.get("timestamp", ""),
        }
        for item in realtime
    ]

    node_statuses: dict[str, int] = {}
    for item in output_nodes:
        status = (
            realtime_map.get(str(item["id"]), {}).get("run_status")
            or item.get("run_status")
            or "unknown"
        )
        node_statuses[status] = node_statuses.get(status, 0) + 1

    return {
        "status": "ok",
        "data": {
            "es_list": output_es,
            "sites": output_sites,
            "nodes": output_nodes,
            "realtime": output_realtime,
            "alerts": [],
            "topology": {
                "customer_site_id": TARGET_SITE_ID,
                "stations": copy.deepcopy(TARGET_STATIONS),
            },
            "stats": {
                "total_sites": len(sites),
                "total_es": len(es_list),
                "total_nodes": len(nodes),
                "total_alerts": 0,
                "node_statuses": node_statuses,
            },
        },
    }


def fetch_external_rows(
    url: str, token: str
) -> tuple[list[dict[str, Any]], str | None]:
    """请求外部列表接口；失败时返回错误文本，不中断基础看板。"""
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return [], str(error)
    if not isinstance(payload, dict):
        return [], "数据源未返回 JSON 对象"
    if payload.get("error"):
        return [], str(payload["error"])
    if payload.get("errors"):
        return [], str(payload["errors"])
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return [], "数据源未返回记录数组"
    return rows, None


def parse_timestamp(value: Any) -> datetime | None:
    """解析接口时间；带时区值保留其含义，无时区值视为上海时间。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Growatt 样例使用“YYYY-MM-DD HH:MM:SS”，业务口径确认为上海时区。
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def normalize_timestamp(value: Any) -> str:
    """统一输出带 +08:00 偏移的上海时区 ISO 时间。"""
    parsed = parse_timestamp(value)
    return parsed.astimezone(SHANGHAI).isoformat() if parsed else ""


def _latest_growatt_rows(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """过滤固定设备，并为每个逆变器 SN 选择 timestamp 最新的一条记录。"""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        sn = str(row.get("sn") or "")
        # 忽略未纳管的 SN，避免接口中的其他设备进入 9 台逆变器区域。
        if sn not in GROWATT_INVERTER_SNS:
            continue
        previous = latest.get(sn)
        row_time = parse_timestamp(row.get("timestamp"))
        previous_time = parse_timestamp(previous.get("timestamp")) if previous else None
        if previous is None or (
            row_time is not None
            and (previous_time is None or row_time > previous_time)
        ):
            latest[sn] = row
    return latest


def fetch_growatt_rows(
    token: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """获取 Growatt 数据，并按固定 SN 顺序返回每台设备的最新记录。"""
    rows, error = fetch_external_rows(GROWATT_URL, token)
    if error:
        return [], error
    latest = _latest_growatt_rows(rows)
    return [latest[sn] for sn in GROWATT_INVERTER_SNS if sn in latest], None


def to_number(value: Any) -> float | None:
    """转换接口数值，并兼容带千分位逗号的字符串。"""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def max_temperature_c(value: Any) -> float | None:
    """将 t_emu.max_temp 转换为有限的摄氏温度数值。"""
    if isinstance(value, bool):
        return None
    temperature = to_number(value)
    return (
        temperature
        if temperature is not None and math.isfinite(temperature)
        else None
    )


def fetch_station_load_sources(
    es_list: list[dict[str, Any]], token: str
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """按完整电站 SN 精确读取 t_es_data.load_power 的最新记录。"""
    sources: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    es_by_code = {
        str(item.get("station_code") or ""): item
        for item in es_list
        if item.get("station_code")
    }
    for station in TARGET_STATIONS:
        code = station["code"]
        es_item = es_by_code.get(code)
        es_sn = str(es_item.get("sn") or "").strip() if es_item else ""
        error_key = f"station_load:{code}"
        if not es_sn:
            errors[error_key] = "目标电站缺少完整 SN"
            continue
        url = _external_list_url(
            STATION_LOAD_URL,
            page_size=1,
            fields="es_sn,load_power,timestamp",
            filter_value={"es_sn": {"$eq": es_sn}},
            sort="-timestamp",
        )
        rows, error = fetch_external_rows(url, token)
        if error:
            errors[error_key] = error
            continue
        if not rows:
            errors[error_key] = "目标电站没有负载记录"
            continue
        row = rows[0]
        if str(row.get("es_sn") or "").strip() != es_sn:
            errors[error_key] = "返回记录的电站 SN 不匹配"
            continue
        power = to_number(row.get("load_power"))
        timestamp = normalize_timestamp(row.get("timestamp"))
        if power is None or power < 0 or not timestamp:
            errors[error_key] = "目标电站负载记录无效"
            continue
        sources[code] = {
            "power": power,
            "timestamp": timestamp,
            "power_source": "t_es_data.load_power",
        }
    return sources, errors


def fetch_station_storage_sources(
    token: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """读取目标柜体功率/时间及光伏表时间；光伏表不参与储能求和。"""
    station_device_sns = {
        station["code"]: list(station["cabinet_sns"]) + (
            [station["pv_meter"]["emu_sn"]] if station.get("pv_meter") else []
        )
        for station in TARGET_STATIONS
    }
    allowed_pairs = {
        (station["code"], cabinet_sn)
        for station in TARGET_STATIONS
        for cabinet_sn in station_device_sns[station["code"]]
    }
    filters = [
        {
            "$and": [
                {"f_es_sn": {"$eq": station["code"]}},
                {"emu_sn": {"$in": station_device_sns[station["code"]]}},
            ]
        }
        for station in TARGET_STATIONS
    ]
    url = _external_list_url(
        CABINET_POWER_URL,
        page_size=len(allowed_pairs),
        fields="emu_sn,f_es_sn,latest_power,max_temp,last_time_iso",
        filter_value={"$or": filters},
    )
    rows, error = fetch_external_rows(url, token)
    if error:
        return {}, {"storage_cabinets": error}

    rows_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        pair = (
            str(row.get("f_es_sn") or "").strip(),
            str(row.get("emu_sn") or "").strip(),
        )
        if pair not in allowed_pairs:
            return {}, {"storage_cabinets": "数据源返回了白名单之外的柜体"}
        if pair in rows_by_pair:
            return {}, {"storage_cabinets": "目标柜体记录重复"}
        rows_by_pair[pair] = row

    sources: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for station in TARGET_STATIONS:
        code = station["code"]
        station_rows = []
        missing = []
        for cabinet_sn in station["cabinet_sns"]:
            row = rows_by_pair.get((code, cabinet_sn))
            if row is None:
                missing.append(cabinet_sn)
            else:
                station_rows.append(row)
        error_key = f"storage_cabinets:{code}"
        # 单柜时间独立保留；同站其他柜缺失不能抹去已取得的上报时间。
        sources[code] = {
            "cabinet_timestamps": {
                str(row.get("emu_sn") or "").strip(): normalize_timestamp(
                    row.get("last_time_iso") or row.get("timestamp")
                )
                for row in station_rows
            },
            "cabinet_max_temperatures_c": {
                str(row.get("emu_sn") or "").strip(): max_temperature_c(row.get("max_temp"))
                for row in station_rows
            },
        }
        if station.get("pv_meter"):
            pv_sn = station["pv_meter"]["emu_sn"]
            pv_row = rows_by_pair.get((code, pv_sn), {})
            sources[code]["pv_meter_timestamp"] = normalize_timestamp(pv_row.get("last_time_iso"))
            if not sources[code]["pv_meter_timestamp"]:
                errors[f"pv_meter:{code}"] = f"光伏计量表 {pv_sn} 时间缺失或无效"
        if missing:
            errors[error_key] = "缺少目标柜体：" + "、".join(missing)
            continue

        powers = [to_number(row.get("latest_power")) for row in station_rows]
        timestamps = [
            normalize_timestamp(row.get("last_time_iso") or row.get("timestamp"))
            for row in station_rows
        ]
        if any(power is None for power in powers) or any(not value for value in timestamps):
            errors[error_key] = "目标柜体功率或时间无效"
            continue
        sources[code].update({
            "power": round(sum(power for power in powers if power is not None), 3),
            "timestamp": max(timestamps),
            "power_source": "t_emu.latest_power",
        })
    return sources, errors


def apply_authoritative_power_sources(
    data: dict[str, Any],
    load_sources: dict[str, dict[str, Any]],
    storage_sources: dict[str, dict[str, Any]],
) -> None:
    """覆盖两类聚合节点功率；权威源缺失时明确置空，禁止旧值回退。"""
    es_code_by_id = {
        str(item.get("id")): str(item.get("station_code") or "")
        for item in data.get("es_list", [])
        if item.get("id") is not None
    }
    realtime = data.setdefault("realtime", [])
    realtime_by_node = {
        str(item.get("fk_en")): item
        for item in realtime
        if item.get("fk_en") is not None
    }
    cabinet_max_temperatures_c = {
        cabinet_sn: temperature
        for source in storage_sources.values()
        for cabinet_sn, temperature in source.get(
            "cabinet_max_temperatures_c", {}
        ).items()
    }
    for node in data.get("nodes", []):
        if node.get("node_type") != "ess" or node.get("is_aggregate"):
            continue
        cabinet_sn = str(node.get("sn") or "").strip()
        if cabinet_sn not in cabinet_max_temperatures_c:
            continue
        node_id = str(node.get("id"))
        realtime_item = realtime_by_node.get(node_id)
        if realtime_item is None:
            realtime_item = {
                "fk_en": node.get("id"),
                "soc": None,
                "run_status": node.get("run_status", ""),
            }
            realtime.append(realtime_item)
            realtime_by_node[node_id] = realtime_item
        realtime_item["temperature_c"] = cabinet_max_temperatures_c[cabinet_sn]
        source = storage_sources.get(es_code_by_id.get(str(node.get("fk_es")), ""), {})
        realtime_item["cabinet_timestamp"] = source.get("cabinet_timestamps", {}).get(cabinet_sn, "")

    source_by_type = {
        "load": (load_sources, "t_es_data.load_power"),
        "ess": (storage_sources, "t_emu.latest_power"),
    }
    for node in data.get("nodes", []):
        node_type = node.get("node_type")
        if not node.get("is_aggregate") or node_type not in source_by_type:
            continue
        node_id = str(node.get("id"))
        realtime_item = realtime_by_node.get(node_id)
        if realtime_item is None:
            realtime_item = {
                "fk_en": node.get("id"),
                "soc": None,
                "run_status": node.get("run_status", ""),
            }
            realtime.append(realtime_item)
            realtime_by_node[node_id] = realtime_item

        station_code_value = es_code_by_id.get(str(node.get("fk_es")), "")
        sources, required_source = source_by_type[node_type]
        source = sources.get(station_code_value)
        if source is None or source.get("power_source") != required_source:
            realtime_item.update(
                {
                    "power": None,
                    "timestamp": "",
                    "power_source": "unavailable",
                }
            )
            continue
        realtime_item.update(
            {
                "power": source["power"],
                "timestamp": source["timestamp"],
                "power_source": source["power_source"],
            }
        )


def calculate_efficiency(
    input_kw: float | None, output_kw: float | None
) -> float | None:
    """计算转换效率：交流输出功率 / 直流输入功率 × 100%。"""
    if input_kw is None or output_kw is None or input_kw <= 0:
        return None
    return round(output_kw / input_kw * 100, 2)


def build_pv_inverters(
    rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """构造固定 9 台逆变器卡片数据，缺记录的设备以离线状态保留。"""
    latest = _latest_growatt_rows(rows or [])
    inverters = []
    # 按固定 SN 列表遍历，确保接口最近 20 条不完整时卡片数量仍为 9。
    for sn in GROWATT_INVERTER_SNS:
        row = latest.get(sn)
        if row is None:
            inverters.append(
                {
                    "sn": sn,
                    "name": f"古瑞瓦特逆变器 {sn}",
                    "timestamp": "",
                    "status_code": None,
                    "run_status": "offline",
                    "online": False,
                    "input_power_kw": None,
                    "output_power_kw": None,
                    "conversion_efficiency": None,
                    "data_quality": "missing",
                }
            )
            continue

        # Growatt 已确认字段：a0=运行状态，a1=直流输入功率，a35=交流输出功率。
        status_code = to_number(row.get("a0"))
        input_kw = to_number(row.get("a1"))
        output_kw = to_number(row.get("a35"))
        efficiency = calculate_efficiency(input_kw, output_kw)
        online = status_code == 1
        inverters.append(
            {
                "sn": sn,
                "name": f"古瑞瓦特逆变器 {sn}",
                "timestamp": normalize_timestamp(row.get("timestamp")),
                "status_code": row.get("a0"),
                "run_status": "generate" if online else "offline",
                "online": online,
                "input_power_kw": input_kw,
                "output_power_kw": output_kw,
                "conversion_efficiency": efficiency,
                "data_quality": "good" if efficiency is not None else "invalid",
            }
        )
    return inverters


def build_pv_efficiency_alerts(
    inverters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按单条最新有效数据生成转换效率下降告警。"""
    alerts = []
    for inverter in inverters:
        efficiency = inverter.get("conversion_efficiency")
        if (
            inverter.get("data_quality") != "good"
            or efficiency is None
            # 等于 85% 不告警，只有严格低于阈值才立即触发。
            or efficiency >= PV_EFFICIENCY_MIN_PCT
        ):
            continue
        alerts.append(
            {
                "id": f"derived:pv_efficiency:{inverter['sn']}",
                "source": "derived",
                "category": "pv_efficiency",
                "device_sn": inverter["sn"],
                "txt": "光伏转换效率下降",
                "start_time": inverter.get("timestamp", ""),
                "fk_site_id": None,
                "fk_en_id": None,
                "value": efficiency,
                "threshold": PV_EFFICIENCY_MIN_PCT,
                "unit": "%",
            }
        )
    return alerts


def build_ess_self_loss_alerts(
    nodes: list[dict[str, Any]],
    realtime: list[dict[str, Any]],
    es_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按目标柜体汇总空闲功率占装机功率的比例生成自损耗告警。"""
    realtime_map = {
        str(item.get("fk_en")): item
        for item in realtime
        if item.get("fk_en") is not None
    }
    es_map = {
        str(item.get("id")): item
        for item in es_list
        if item.get("id") is not None
    }
    alerts = []
    for node in nodes:
        # 只判断电站级聚合储能，避免与下属储能柜重复告警。
        if node.get("node_type") != "ess" or not node.get("is_aggregate"):
            continue
        realtime_item = realtime_map.get(str(node.get("id")))
        if (
            not realtime_item
            or realtime_item.get("power_source") != "t_emu.latest_power"
        ):
            continue
        status = str(realtime_item.get("run_status") or "").lower()
        if status not in ESS_IDLE_STATUSES:
            continue
        power = to_number(realtime_item.get("power"))
        installed_power = to_number(
            es_map.get(str(node.get("fk_es")), {}).get("installed_power")
        )
        if power is None or installed_power is None or installed_power <= 0:
            continue
        loss_power = abs(power)
        loss_pct = round(loss_power / installed_power * 100, 2)
        # 等于阈值不告警，只有单条数据严格越限才立即触发。
        if loss_pct <= ESS_SELF_LOSS_MAX_PCT:
            continue
        alerts.append(
            {
                "id": f"derived:ess_self_loss:{node['id']}",
                "source": "derived",
                "category": "ess_self_loss",
                "device_sn": node.get("sn", ""),
                "txt": "储能自损耗异常",
                "start_time": realtime_item.get("timestamp", ""),
                "fk_site_id": node.get("fk_site_id"),
                "fk_en_id": node.get("id"),
                "value": loss_pct,
                "threshold": ESS_SELF_LOSS_MAX_PCT,
                "unit": "%",
                "power_kw": loss_power,
                "installed_power_kw": installed_power,
            }
        )
    return alerts


def build_load_spike_alerts(
    nodes: list[dict[str, Any]],
    realtime: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按目标电站 t_es_data.load_power 最新值生成阈值越限告警。"""
    realtime_map = {
        str(item.get("fk_en")): item
        for item in realtime
        if item.get("fk_en") is not None
    }
    alerts = []
    for node in nodes:
        # 只判断电站级聚合负载，功率保持原符号且不计算历史差值。
        if node.get("node_type") != "load" or not node.get("is_aggregate"):
            continue
        realtime_item = realtime_map.get(str(node.get("id")))
        if (
            not realtime_item
            or realtime_item.get("power_source") != "t_es_data.load_power"
        ):
            continue
        power = to_number(realtime_item.get("power")) if realtime_item else None
        if power is None or power <= LOAD_SPIKE_MIN_KW:
            continue
        alerts.append(
            {
                "id": f"derived:load_spike:{node['id']}",
                "source": "derived",
                "category": "load_spike",
                "device_sn": node.get("sn", ""),
                "txt": "负载功率突增",
                "start_time": realtime_item.get("timestamp", ""),
                "fk_site_id": node.get("fk_site_id"),
                "fk_en_id": node.get("id"),
                "value": power,
                "threshold": LOAD_SPIKE_MIN_KW,
                "unit": "kW",
            }
        )
    return alerts


def build_result(
    raw: dict[str, Any],
    growatt_rows: list[dict[str, Any]] | None = None,
    load_sources: dict[str, dict[str, Any]] | None = None,
    storage_sources: dict[str, dict[str, Any]] | None = None,
    source_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """追加九台逆变器汇总源，并生成三类无等级异常。"""
    # 深拷贝隔离旁路处理，保证调用方传入的原始看板数据不被原地修改。
    data = copy.deepcopy(raw.get("data", {}))
    apply_authoritative_power_sources(
        data,
        load_sources or {},
        storage_sources or {},
    )
    inverters = build_pv_inverters(growatt_rows)
    pv_alerts = build_pv_efficiency_alerts(inverters)
    target_site_id = next(
        (site.get("id") for site in data.get("sites", []) if site.get("id") is not None),
        TARGET_SITE_ID,
    )
    pv_station = next(
        (item for item in data.get("es_list", []) if station_code(item) == "ES02"),
        None,
    )
    pv_station_id = str(pv_station.get("id")) if pv_station else ""
    pv_aggregate_ids = {
        str(node.get("id"))
        for node in data.get("nodes", [])
        if str(node.get("fk_es")) == pv_station_id
        and node.get("node_type") == "pv_grid_ac"
        and node.get("is_aggregate")
    }
    pv_meter = next(
        (
            node
            for node in data.get("nodes", [])
            if str(node.get("fk_es")) == pv_station_id
            and node.get("node_type") == "meter"
            and str(node.get("parentId")) in pv_aggregate_ids
        ),
        None,
    )
    if pv_meter:
        meter_realtime = next(
            (item for item in data["realtime"] if str(item.get("fk_en")) == str(pv_meter["id"])),
            None,
        )
        if meter_realtime is None:
            meter_realtime = {"fk_en": pv_meter["id"]}
            data["realtime"].append(meter_realtime)
        # 表计自身时间独立于逆变器汇总数据，缺失时不以其他设备时间替代。
        meter_realtime["pv_meter_timestamp"] = (storage_sources or {}).get("ES02", {}).get("pv_meter_timestamp", "")

    for alert in pv_alerts:
        alert["fk_site_id"] = target_site_id
        alert["fk_en_id"] = pv_meter.get("id") if pv_meter else None

    derived_alerts = (
        pv_alerts
        + build_ess_self_loss_alerts(
            data.get("nodes", []),
            data.get("realtime", []),
            data.get("es_list", []),
        )
        + build_load_spike_alerts(
            data.get("nodes", []), data.get("realtime", [])
        )
    )

    # 只输出固定九台白名单设备；不再透传其他协同设备记录。
    data["pv_inverters"] = inverters
    data["alerts"] = [
        alert
        for alert in derived_alerts
        if str(alert.get("fk_site_id") or "") == TARGET_SITE_ID
    ]
    data["thresholds"] = {
        "pv_efficiency_min_pct": PV_EFFICIENCY_MIN_PCT,
        "ess_self_loss_max_pct": ESS_SELF_LOSS_MAX_PCT,
        "load_spike_min_kw": LOAD_SPIKE_MIN_KW,
    }
    data["refresh_interval_seconds"] = REFRESH_INTERVAL_SECONDS

    # 外部权威源失败只记录来源错误，NocoBase 基础响应仍正常返回。
    if source_errors:
        errors = dict(data.get("source_errors", {}))
        errors.update(source_errors)
        data["source_errors"] = errors

    stats = copy.deepcopy(data.get("stats", {}))
    if stats:
        stats["total_alerts"] = len(data["alerts"])
        stats["anomaly_categories"] = {
            category: sum(
                1 for alert in data["alerts"] if alert.get("category") == category
            )
            for category in ("pv_efficiency", "ess_self_loss", "load_spike")
        }
        data["stats"] = stats

    return {"status": raw.get("status", "ok"), "data": data}


def record_alert_history(data: dict[str, Any], db_path: str | Path) -> dict[str, Any]:
    """保存实际检出的告警采样；采集时间相同的记录幂等，不推断恢复状态。"""
    station_ids = {
        str(station["id"]) for station in data.get("es_list", [])
        if station.get("id") is not None
        and str(station.get("fk_site") or "") == TARGET_SITE_ID
        and station_code(station) in {"ES01", "ES02"}
    }
    node_ids = {
        str(node["id"]) for node in data.get("nodes", [])
        if node.get("id") is not None and str(node.get("fk_es")) in station_ids
        and str(node.get("fk_site_id") or "") == TARGET_SITE_ID
    }

    def belongs_to_site(alert: dict[str, Any]) -> bool:
        return (
            str(alert.get("fk_site_id") or "") == TARGET_SITE_ID
            and str(alert.get("fk_en_id") or "") in node_ids
            and alert.get("category") in {"pv_efficiency", "ess_self_loss", "load_spike"}
        )

    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(SHANGHAI).isoformat()
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS alert_samples (
                site_id TEXT NOT NULL,
                alert_id TEXT NOT NULL,
                sample_time TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (site_id, alert_id, sample_time)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS alert_samples_site_time
            ON alert_samples (site_id, sample_time DESC)
        """)
        for alert in data.get("alerts", []):
            sample_time = normalize_timestamp(alert.get("start_time"))
            if (
                not belongs_to_site(alert)
                or not alert.get("id")
                or not sample_time
            ):
                continue
            payload = {**alert, "start_time": sample_time, "recorded_at": recorded_at}
            connection.execute(
                "INSERT OR IGNORE INTO alert_samples VALUES (?, ?, ?, ?, ?)",
                (TARGET_SITE_ID, str(alert["id"]), sample_time, recorded_at,
                 json.dumps(payload, ensure_ascii=False)),
            )
        rows = connection.execute(
            "SELECT payload, recorded_at FROM alert_samples WHERE site_id = ? "
            "ORDER BY sample_time DESC, alert_id",
            (TARGET_SITE_ID,),
        )
        records = []
        total = 0
        first_recorded_at = None
        # 先校验归属，再计数与截取；旧库中的无关记录保留但不展示。
        for payload_text, saved_at in rows:
            payload = json.loads(payload_text)
            if not belongs_to_site(payload):
                continue
            total += 1
            if first_recorded_at is None or saved_at < first_recorded_at:
                first_recorded_at = saved_at
            if len(records) < ALERT_HISTORY_LIMIT:
                records.append(payload)
    return {
        "status": "ok",
        "records": records,
        "total": total,
        "limit": ALERT_HISTORY_LIMIT,
        "first_recorded_at": first_recorded_at,
    }


def attach_alert_history(result: dict[str, Any], db_path: str | Path) -> None:
    """历史存储故障单独上报，避免阻断实时监测。"""
    try:
        result["data"]["alert_history"] = record_alert_history(result["data"], db_path)
    except (OSError, sqlite3.Error, ValueError):
        result["data"]["alert_history"] = {
            "status": "error", "records": [],
            "message": "历史告警读写失败，请检查存储目录权限及磁盘状态。",
        }


def main(argv: list[str] | None = None) -> int:
    """分别使用基础站点和能源源 Token，并输出组合后的单个 JSON 响应。"""
    parser = argparse.ArgumentParser(description="运营监控看板 API")
    parser.add_argument(
        "--token", default=DEFAULT_TK, help="基础 NocoBase Bearer token"
    )
    parser.add_argument(
        "--energy-token",
        default=DEFAULT_E606_TK,
        help="t_es_data / t_emu Bearer token",
    )

    parser.add_argument(
        "--history-db", default=os.environ.get("M1_ALERT_HISTORY_DB") or str(DEFAULT_HISTORY_DB),
        help="历史告警 SQLite 文件；应位于持久化可写目录",
    )
    args = parser.parse_args(argv)

    try:
        token = args.token or DEFAULT_TK
        # 基础看板始终从 NocoBase 接口获取。
        raw = fetch_raw_data(token)
        source_errors: dict[str, str] = {}

        # Growatt 只返回固定九台逆变器白名单。
        growatt_rows, growatt_error = fetch_growatt_rows(DEFAULT_E606_TK)
        if growatt_error:
            source_errors["growatt"] = growatt_error

        # 负载与储能分别使用精确电站、精确柜体白名单数据。
        energy_token = args.energy_token or DEFAULT_E606_TK
        load_sources, load_errors = fetch_station_load_sources(
            raw.get("data", {}).get("es_list", []), energy_token
        )
        storage_sources, storage_errors = fetch_station_storage_sources(
            energy_token
        )
        source_errors.update(load_errors)
        source_errors.update(storage_errors)

        result = build_result(
            raw,
            growatt_rows=growatt_rows,
            load_sources=load_sources,
            storage_sources=storage_sources,
            source_errors=source_errors,
        )
        attach_alert_history(result, args.history_db)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"status": "error", "message": str(error)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
