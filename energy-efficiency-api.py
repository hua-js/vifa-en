#!/usr/bin/env python3
"""供 Node-RED exec 节点调用的场站能效看板入口。"""

import json
import math
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from m2.station_efficiency_history import (
    HistoryError,
    build_calendar_day_dashboard,
    calendar_day_bounds,
    time_in_zone,
)
from m2.station_efficiency_job import process_station_minute
from m2.station_efficiency_nocobase import (
    StationEfficiencyStoreError,
    fetch_minute_points,
)
from m2.station_energy_backend import BackendError, calculate_bus
from m2.station_energy_data_adapter import (
    StationEnergyDataError,
    fetch_station_source_record,
)

try:
    import energy_efficiency_local_config as local_config
except ImportError:
    local_config = None

LOCAL_EMU_URL = getattr(local_config, "EMU_URL", "")
LOCAL_EMU_TOKEN = getattr(local_config, "EMU_TOKEN", "")
LOCAL_NOCOBASE_BASE_URL = getattr(local_config, "NOCOBASE_BASE_URL", "")
LOCAL_NOCOBASE_TOKEN = getattr(local_config, "NOCOBASE_TOKEN", "")
LOCAL_TIMEZONE = getattr(local_config, "TIMEZONE", "Asia/Shanghai")
LOCAL_REQUEST_TIMEOUT_SECONDS = getattr(
    local_config,
    "REQUEST_TIMEOUT_SECONDS",
    15,
)


class EntrypointError(ValueError):
    """可以安全返回给 Node-RED 的入口参数或配置错误。"""

    def __init__(self, code, message, exit_code):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def parse_request(argv):
    """只接受 dashboard/minute 加 ES01/ES02。"""
    values = list(argv)
    if (
        len(values) != 2
        or values[0] not in {"dashboard", "minute"}
        or values[1] not in {"ES01", "ES02"}
    ):
        raise EntrypointError("invalid_arguments", "参数不正确", 2)
    return values[0], values[1]


def _required_config_text(environ, field, local_value):
    value = environ.get(field, local_value)
    if not isinstance(value, str) or not value.strip():
        raise EntrypointError("missing_config", "服务器缺少运行配置", 3)
    return value.strip()


def load_runtime_config(environ, require_nocobase=False):
    """优先读取环境变量，未配置时使用同目录临时配置。"""
    url = _required_config_text(environ, "VIFA_EMU_URL", LOCAL_EMU_URL)
    token = _required_config_text(
        environ,
        "VIFA_EMU_TOKEN",
        LOCAL_EMU_TOKEN,
    )

    try:
        timeout = float(
            environ.get(
                "M2_REQUEST_TIMEOUT_SECONDS",
                LOCAL_REQUEST_TIMEOUT_SECONDS,
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise EntrypointError(
            "missing_config",
            "服务器运行配置不正确",
            3,
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise EntrypointError(
            "missing_config",
            "服务器运行配置不正确",
            3,
        )

    timezone_name = str(
        environ.get("M2_TIMEZONE", LOCAL_TIMEZONE)
    ).strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EntrypointError(
            "missing_config",
            "服务器运行配置不正确",
            3,
        ) from exc

    config = {
        "emu_url": url,
        "emu_token": token,
        "timeout_seconds": timeout,
        "timezone": timezone_name,
    }
    if require_nocobase:
        config.update({
            "nocobase_base_url": _required_config_text(
                environ,
                "M2_NOCOBASE_BASE_URL",
                LOCAL_NOCOBASE_BASE_URL,
            ),
            "nocobase_token": _required_config_text(
                environ,
                "M2_NOCOBASE_TOKEN",
                LOCAL_NOCOBASE_TOKEN,
            ),
        })
    return config


def _error_payload(code, message):
    return {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
        },
    }


def _public_realtime(source, calculation, timezone_name):
    public_source = dict(source)
    public_source["data_time"] = time_in_zone(
        source["data_time"],
        timezone_name,
        "data_time",
    )
    if isinstance(source.get("source_times"), dict):
        public_source["source_times"] = {
            key: time_in_zone(value, timezone_name, f"source_times.{key}")
            for key, value in source["source_times"].items()
        }

    public_calculation = dict(calculation)
    for field in ("data_time", "calculated_at"):
        if calculation.get(field):
            public_calculation[field] = time_in_zone(
                calculation[field],
                timezone_name,
                field,
            )
    return {
        "inputs": public_source,
        "result": public_calculation,
    }


def execute(
    argv,
    environ,
    fetch_station=fetch_station_source_record,
    calculate=calculate_bus,
    build_dashboard=build_calendar_day_dashboard,
    fetch_points=fetch_minute_points,
    process_minute=process_station_minute,
):
    """执行一次看板读取或分钟保存并返回结果和退出码。"""
    try:
        operation, station_id = parse_request(argv)
        config = load_runtime_config(environ, require_nocobase=True)
        if operation == "minute":
            minute_output = process_minute(station_id, config)
            minute_point = minute_output["minute_point"]
            saved_record = minute_output["saved_record"]
            return {
                "status": "ok",
                "data": {
                    "operation": "minute",
                    "station_id": station_id,
                    "data_time": minute_point["data_time"],
                    "saved_id": saved_record.get("id"),
                },
            }, 0

        source = fetch_station(station_id, config)
        calculation = calculate(source)
        start_time, end_time = calendar_day_bounds(
            source["data_time"],
            config["timezone"],
        )
        points = fetch_points(
            station_id,
            start_time,
            end_time,
            config,
        )
        dashboard = build_dashboard(
            station_id,
            config["timezone"],
            source["data_time"],
            _public_realtime(source, calculation, config["timezone"]),
            points,
            [],
        )
        return {"status": "ok", "data": dashboard}, 0
    except EntrypointError as exc:
        return _error_payload(exc.code, exc.message), exc.exit_code
    except StationEnergyDataError:
        return _error_payload(
            "source_error",
            "场站实时数据读取失败",
        ), 4
    except StationEfficiencyStoreError:
        return _error_payload(
            "store_error",
            "分钟效率数据读写失败",
        ), 6
    except (BackendError, HistoryError):
        return _error_payload(
            "calculation_error",
            "场站能效计算失败",
        ), 5
    except Exception:
        return _error_payload(
            "internal_error",
            "服务器内部错误",
        ), 1


def _compact_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def main(argv=None, environ=None, stdout=None):
    """输出恰好一行 JSON，并以退出码告知 Node-RED 成功或失败。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    runtime_environment = os.environ if environ is None else environ
    output = sys.stdout if stdout is None else stdout

    payload, exit_code = execute(arguments, runtime_environment)
    try:
        text = _compact_json(payload)
    except (TypeError, ValueError, OverflowError):
        text = _compact_json(
            _error_payload("internal_error", "服务器内部错误")
        )
        exit_code = 1
    output.write(text + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
