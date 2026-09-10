#!/usr/bin/env python3
"""供 Node-RED exec 节点调用的场站能效看板入口。"""

import json
import math
import os
import sys
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Support both the repository's m2/ entrypoint and the existing flat Node-RED
# installation, where this script sits next to the m2/ package.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = (
    SCRIPT_DIRECTORY if (SCRIPT_DIRECTORY / "m2").is_dir()
    else SCRIPT_DIRECTORY.parent
)
for import_directory in (PROJECT_ROOT, PROJECT_ROOT / ".local"):
    sys.path.insert(0, str(import_directory))

from m2.station_efficiency_history import (
    HistoryError,
    build_calendar_day_dashboard,
    build_event_history,
    calendar_day_bounds,
    time_in_zone,
)
from m2.station_efficiency_job import (
    cleanup_device_history,
    process_station_minute,
)
from m2.station_efficiency_nocobase import (
    StationEfficiencyStoreError,
    fetch_dashboard_events,
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
LOCAL_GROWALL_URL = getattr(local_config, "GROWALL_URL", "")
LOCAL_GROWALL_TOKEN = getattr(local_config, "GROWALL_TOKEN", "")
LOCAL_PV_INVERTER_SNS_BY_STATION = getattr(
    local_config,
    "PV_INVERTER_SNS_BY_STATION",
    {"ES02": ("emu1", "emu2", "emu3", "emu4", "emu5", "emu21", "emu22", "emu23", "emu24")},
)
LOCAL_PV_INVERTER_RATED_POWER_KW = getattr(
    local_config,
    "PV_INVERTER_RATED_POWER_KW",
    60,
)
LOCAL_DEVICE_SAMPLE_MAX_AGE_MINUTES = getattr(
    local_config,
    "DEVICE_SAMPLE_MAX_AGE_MINUTES",
    2,
)
LOCAL_DEVICE_POINT_RETENTION_DAYS = getattr(
    local_config,
    "DEVICE_POINT_RETENTION_DAYS",
    30,
)
LOCAL_BATTERY_MAX_TEMPERATURE_FIELD = getattr(
    local_config,
    "BATTERY_MAX_TEMPERATURE_FIELD",
    "max_temp",
)
LOCAL_BATTERY_HOT_CLUSTER_FIELD = getattr(
    local_config,
    "BATTERY_HOT_CLUSTER_FIELD",
    "",
)
DEFAULT_EVENT_OUTBOX_PATH = str(
    PROJECT_ROOT / "runtime" / "m2" / "energy_efficiency_event_outbox.sqlite3"
)
LOCAL_EVENT_OUTBOX_PATH = getattr(
    local_config,
    "EVENT_OUTBOX_PATH",
    DEFAULT_EVENT_OUTBOX_PATH,
)


def _default_bottleneck_rules():
    """Return the production rule baseline for each supported station."""
    common = {
        "enabled": True,
        "chain_low_efficiency_threshold_pct": 85,
        "chain_low_efficiency_trigger_minutes": 2,
        "chain_low_efficiency_recovery_minutes": 2,
        "inverter_min_running_power_kw": 5,
        "inverter_low_load_threshold_pct": 20,
        "inverter_trigger_minutes": 3,
        "inverter_recovery_minutes": 2,
        "temperature_rise_window_minutes": 5,
        "temperature_rise_threshold_c": 3,
        "temperature_trigger_minutes": 2,
        "temperature_recovery_minutes": 2,
        "version": 1,
        "updated_at": "2026-08-27T00:00:00+08:00",
    }
    return {
        station_id: {"station_id": station_id, **common}
        for station_id in ("ES01", "ES02")
    }


LOCAL_BOTTLENECK_RULES = getattr(
    local_config,
    "BOTTLENECK_RULES",
    _default_bottleneck_rules(),
)

BUSINESS_TIMEZONE = "Asia/Shanghai"

_PUBLIC_WARNING_MESSAGES = {
    "best_effort_failed": "部分瓶颈处理失败，已保留可用结果",
    "bottleneck_rule_unavailable": "瓶颈规则不可用，已跳过事件评估",
    "growall_unavailable": "Growall 数据不可用，已跳过逆变器设备处理",
}
_PUBLIC_WARNING_STAGES = {
    "battery_point_build",
    "inverter_point_build",
    "device_point_save",
    "bottleneck_rule",
    "bottleneck_history",
    "bottleneck_event_save",
    "event_outbox",
}


class EntrypointError(ValueError):
    """可以安全返回给 Node-RED 的入口参数或配置错误。"""

    def __init__(self, code, message, exit_code):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def parse_request(argv):
    """校验固定操作、场站和历史查询日期。"""
    values = list(argv)
    if values == ["cleanup"]:
        return "cleanup", None
    if (
        len(values) < 2
        or len(values) != {"dashboard": 2, "minute": 2, "history": 3, "events": 4}.get(values[0])
        or values[1] not in {"ES01", "ES02"}
    ):
        raise EntrypointError("invalid_arguments", "参数不正确", 2)
    for value in values[2:]:
        _parse_query_date(value)
    return values[0], values[1]


def _parse_query_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise EntrypointError("invalid_arguments", "日期必须为有效的 YYYY-MM-DD", 2) from None


def _history_bounds(values, timezone_name, now):
    zone = ZoneInfo(timezone_name)
    today = now.astimezone(zone).date()
    first = _parse_query_date(values[2])
    last = _parse_query_date(values[-1])
    if first > last or last > today or (last - first).days >= 31:
        raise EntrypointError("invalid_arguments", "日期范围须按先后顺序、不能晚于今天，且最多31天", 2)
    start = datetime.combine(first, time.min, zone)
    end = datetime.combine(last + timedelta(days=1), time.min, zone)
    return start, end


def _required_config_text(environ, field, local_value):
    value = environ.get(field, local_value)
    if not isinstance(value, str) or not value.strip():
        raise EntrypointError("missing_config", "服务器缺少运行配置", 3)
    return value.strip()


def _optional_config_text(environ, field, local_value):
    value = environ.get(field, local_value)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _optional_config_text_alias(environ, primary_field, fallback_field, local_value):
    if primary_field in environ:
        return _optional_config_text(environ, primary_field, local_value)
    return _optional_config_text(environ, fallback_field, local_value)


def _runtime_number(environ, field, local_value, *, integer=False):
    value = environ.get(field, local_value)
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3) from exc
    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3)
    if integer and str(value).strip() != str(number):
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3)
    return number


def _fixed_inverter_rated_power(environ):
    rated_power_kw = _runtime_number(
        environ,
        "M2_PV_INVERTER_RATED_POWER_KW",
        LOCAL_PV_INVERTER_RATED_POWER_KW,
    )
    if rated_power_kw != 60.0:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3)
    return rated_power_kw


def _optional_field(value):
    return value.strip() if isinstance(value, str) else ""


def load_runtime_config(environ, require_nocobase=False, require_source=True):
    """优先读取环境变量，未配置时使用同目录临时配置。"""
    source_text = _required_config_text if require_source else _optional_config_text
    url = source_text(environ, "VIFA_EMU_URL", LOCAL_EMU_URL)
    token = source_text(
        environ,
        "VIFA_EMU_TOKEN",
        LOCAL_EMU_TOKEN,
    )

    timeout = _runtime_number(
        environ,
        "M2_REQUEST_TIMEOUT_SECONDS",
        LOCAL_REQUEST_TIMEOUT_SECONDS,
    )

    timezone_name = str(environ.get("M2_TIMEZONE", LOCAL_TIMEZONE)).strip()
    if timezone_name != BUSINESS_TIMEZONE:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3)
    try:
        ZoneInfo(BUSINESS_TIMEZONE)
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
        "growall_url": _optional_config_text_alias(
            environ,
            "VIFA_GROWALL_URL",
            "M2_GROWALL_URL",
            LOCAL_GROWALL_URL,
        ),
        "growall_token": _optional_config_text_alias(
            environ,
            "VIFA_GROWALL_TOKEN",
            "M2_GROWALL_TOKEN",
            LOCAL_GROWALL_TOKEN,
        ),
        "pv_inverter_sns_by_station": deepcopy(LOCAL_PV_INVERTER_SNS_BY_STATION),
        "pv_inverter_rated_power_kw": _fixed_inverter_rated_power(environ),
        "device_sample_max_age_minutes": _runtime_number(
            environ,
            "M2_DEVICE_SAMPLE_MAX_AGE_MINUTES",
            LOCAL_DEVICE_SAMPLE_MAX_AGE_MINUTES,
        ),
        "device_point_retention_days": _runtime_number(
            environ,
            "M2_DEVICE_POINT_RETENTION_DAYS",
            LOCAL_DEVICE_POINT_RETENTION_DAYS,
            integer=True,
        ),
        "battery_max_temperature_field": _optional_field(
            LOCAL_BATTERY_MAX_TEMPERATURE_FIELD,
        ),
        "battery_hot_cluster_field": _optional_field(
            LOCAL_BATTERY_HOT_CLUSTER_FIELD,
        ),
        "event_outbox_path": _required_config_text(
            environ,
            "M2_EVENT_OUTBOX_PATH",
            LOCAL_EVENT_OUTBOX_PATH,
        ),
        "bottleneck_rules": deepcopy(LOCAL_BOTTLENECK_RULES),
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


def _config_for_station(config, station_id):
    station_config = deepcopy(config)
    rules = station_config.get("bottleneck_rules")
    if isinstance(rules, dict) and station_id in rules:
        station_config["bottleneck_rule"] = rules[station_id]
    return station_config


def _public_warning(warning):
    raw_warning = warning if isinstance(warning, dict) else {}
    code = raw_warning.get("code")
    if code not in _PUBLIC_WARNING_MESSAGES:
        code = "unknown_warning"
    stage = raw_warning.get("stage")
    if stage not in _PUBLIC_WARNING_STAGES:
        stage = "minute_job"
    return {
        "stage": stage,
        "code": code,
        "message": _PUBLIC_WARNING_MESSAGES.get(code, "分钟任务部分处理失败"),
    }


def public_minute_result(result, station_id):
    """Return only the JSON-safe minute summary expected by Node-RED."""
    minute_point = result["minute_point"]
    saved_record = result["saved_record"]
    warnings = [
        _public_warning(warning)
        for warning in (result.get("warnings") or [])
    ]
    event_updates = list(result.get("event_updates") or [])
    event_persistence = result.get("event_persistence")
    if not isinstance(event_persistence, dict):
        event_persistence = {
            "attempted": 0,
            "saved": 0,
            "failed": 0,
            "outbox_pending": 0,
        }
    return {
        "operation": "minute",
        "station_id": station_id,
        "data_time": minute_point["data_time"],
        "minute_point": dict(minute_point),
        "saved_id": saved_record.get("id"),
        "device_points_saved": result.get("device_points_saved", 0),
        "event_update_count": len(event_updates),
        "event_persistence": {
            field: event_persistence.get(field)
            for field in ("attempted", "saved", "failed", "outbox_pending")
        },
        "warning_count": len(warnings),
        "warnings": warnings,
    }


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
    fetch_events=fetch_dashboard_events,
    process_minute=process_station_minute,
    cleanup_history=cleanup_device_history,
    now=None,
):
    """执行一次看板读取或分钟保存并返回结果和退出码。"""
    try:
        operation, station_id = parse_request(argv)
        config = load_runtime_config(
            environ, require_nocobase=True,
            require_source=operation not in {"history", "events"},
        )
        if operation in {"history", "events"}:
            query_time = now or datetime.now(ZoneInfo(config["timezone"]))
            start, end = _history_bounds(argv, config["timezone"], query_time)
            events = fetch_events(station_id, start.isoformat(), end.isoformat(), config)
            if operation == "events":
                dashboard = build_event_history(
                    station_id, config["timezone"], start.isoformat(), end.isoformat(), events,
                )
            else:
                points = fetch_points(station_id, start.isoformat(), end.isoformat(), config)
                cutoff = min(query_time, end - timedelta(minutes=1))
                dashboard = build_dashboard(
                    station_id, config["timezone"], cutoff.isoformat(), None, points, events,
                )
                dashboard["operation"] = "history"
                dashboard["range"]["cutoff_time"] = min(query_time, end).astimezone(start.tzinfo).isoformat()
                dashboard["summary"] = dashboard.pop("summary_today")
                dashboard.pop("realtime")
            dashboard["station_id"] = station_id
            return {"status": "ok", "data": dashboard}, 0
        if operation == "cleanup":
            result = cleanup_history(config)
            return {
                "status": "ok",
                "data": {"operation": "cleanup", **result},
            }, 0
        if operation == "minute":
            minute_output = process_minute(
                station_id,
                _config_for_station(config, station_id),
            )
            return {
                "status": minute_output["status"],
                "data": public_minute_result(minute_output, station_id),
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
        events = fetch_events(
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
            events,
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
