"""执行一次场站分钟效率读取、计算和保存。"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from m2.station_efficiency_history import build_minute_point
from m2.station_efficiency_nocobase import save_minute_point
from m2.station_energy_backend import calculate_bus
from m2.station_energy_data_adapter import fetch_station_source_record


FORMULA_VERSION = "energy-chain-v1"


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
        "pv_storage": _chain_from_result(
            result,
            "pv_storage_dc_efficiency",
        ),
        "storage_load": _chain_from_result(
            result,
            "storage_load_efficiency",
        ),
        "pv_load": _chain_from_result(
            result,
            "pv_load_efficiency",
        ),
    }


def process_station_minute(
    station_id,
    config,
    source_request_json=None,
    store_request_json=None,
):
    """处理一个场站的当前分钟；本函数自身不负责定时。"""
    source = fetch_station_source_record(
        station_id,
        config,
        request_json=source_request_json,
    )
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
            calculation["calculated_at"],
            config,
            "calculated_at",
        ),
    )
    saved_record = save_minute_point(
        minute_point,
        config,
        request_json=store_request_json,
    )
    return {
        "calculation_result": calculation,
        "minute_point": minute_point,
        "saved_record": saved_record,
    }
