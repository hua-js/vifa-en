"""场站级三条能效链路纯计算与数据转换模块。"""

from datetime import datetime, timedelta
import math


DEFAULT_CONFIG = {
    "min_power_kw": 1.0,
    "balance_error_limit_percent": 5.0,
    "max_efficiency_percent": 105.0,
    "time_tolerance_seconds": 30.0,
}

POWER_FIELDS = (
    "pv_dc_power",
    "pv_ac_power",
    "load_power",
    "cabinet_charge_power",
    "cabinet_discharge_power",
    "pcs_charge_power",
    "pcs_discharge_power",
    "bms_charge_power",
    "bms_discharge_power",
    "grid_import_power",
    "grid_export_power",
    "storage_aux_power",
)

SOURCE_REQUIRED_FIELDS = ("bus_id", "data_time", *POWER_FIELDS)

EFFICIENCY_METRICS = (
    "pv_storage_efficiency",
    "pv_storage_dc_efficiency",
    "storage_load_efficiency",
    "pv_load_efficiency",
    "pv_load_dc_efficiency",
    "pv_inverter_efficiency",
    "pcs_charge_efficiency",
    "pcs_discharge_efficiency",
    "cabinet_charge_efficiency",
    "cabinet_discharge_efficiency",
)

CHAIN_METRICS = (
    "pv_storage_efficiency",
    "pv_storage_dc_efficiency",
    "storage_load_efficiency",
    "pv_load_efficiency",
    "pv_load_dc_efficiency",
)

STORAGE_RELATED_METRICS = (
    "pv_storage_efficiency",
    "pv_storage_dc_efficiency",
    "storage_load_efficiency",
    "pcs_charge_efficiency",
    "pcs_discharge_efficiency",
    "cabinet_charge_efficiency",
    "cabinet_discharge_efficiency",
)

PV_RELATED_METRICS = (
    "pv_storage_efficiency",
    "pv_storage_dc_efficiency",
    "pv_load_efficiency",
    "pv_load_dc_efficiency",
    "pv_inverter_efficiency",
)


class BackendError(Exception):
    """可序列化的业务错误。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def _finite_non_negative_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError(
            "invalid_input",
            f"字段 {field} 必须是非负数",
            {"field": field},
        )
    try:
        number = float(value)
    except OverflowError as exc:
        raise BackendError(
            "invalid_input",
            f"字段 {field} 必须是有限非负数",
            {"field": field},
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise BackendError(
            "invalid_input",
            f"字段 {field} 必须是有限非负数",
            {"field": field},
        )
    return number


def normalize_config(raw=None):
    """合并并校验计算配置。"""
    if raw is None:
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        raise BackendError("invalid_config", "config 必须是对象", {"field": "config"})
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise BackendError(
            "invalid_config",
            "config 包含未知字段",
            {"fields": unknown},
        )
    config = dict(DEFAULT_CONFIG)
    for field, value in raw.items():
        try:
            config[field] = _finite_non_negative_number(value, f"config.{field}")
        except BackendError as exc:
            raise BackendError("invalid_config", exc.message, exc.details) from exc
    if config["max_efficiency_percent"] <= 0:
        raise BackendError(
            "invalid_config",
            "config.max_efficiency_percent 必须大于 0",
            {"field": "config.max_efficiency_percent"},
        )
    return config


def parse_data_time(value, field="data_time"):
    """解析必须包含时区的 ISO 8601 时间。"""
    if not isinstance(value, str) or not value.strip():
        raise BackendError(
            "invalid_time",
            f"字段 {field} 必须是带时区的 ISO 8601 字符串",
            {"field": field},
        )
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BackendError(
            "invalid_time",
            f"字段 {field} 不是有效的 ISO 8601 时间",
            {"field": field},
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackendError(
            "invalid_time",
            f"字段 {field} 必须包含时区",
            {"field": field},
        )
    return parsed


def normalize_sample(raw, config=None):
    """将场站/母线样本转换为统一的非负方向字段。"""
    if not isinstance(raw, dict):
        raise BackendError("invalid_input", "sample 必须是对象", {"field": "sample"})
    if "data_time" not in raw:
        raise BackendError(
            "invalid_time",
            "缺少字段 data_time",
            {"field": "data_time"},
        )
    parsed_time = parse_data_time(raw["data_time"])
    bus_id = raw.get("bus_id", "default")
    if not isinstance(bus_id, str) or not bus_id.strip():
        raise BackendError(
            "invalid_input",
            "字段 bus_id 必须是非空字符串",
            {"field": "bus_id"},
        )

    source_times = raw.get("source_times", {})
    if not isinstance(source_times, dict):
        raise BackendError(
            "invalid_time",
            "字段 source_times 必须是对象",
            {"field": "source_times"},
        )
    parsed_source_times = []
    normalized_source_times = {}
    for source, value in source_times.items():
        field = f"source_times.{source}"
        parsed_source_times.append(parse_data_time(value, field))
        normalized_source_times[str(source)] = value.strip()

    device_status = raw.get("device_status", {})
    if not isinstance(device_status, (dict, list, str)):
        raise BackendError(
            "invalid_input",
            "字段 device_status 必须是对象、数组或字符串",
            {"field": "device_status"},
        )

    sample = {
        "bus_id": bus_id.strip(),
        "data_time": raw["data_time"].strip(),
        "device_status": device_status,
        "source_times": normalized_source_times,
        "_data_time": parsed_time,
        "_source_times": parsed_source_times,
    }
    for field in POWER_FIELDS:
        sample[field] = _finite_non_negative_number(raw.get(field, 0.0), field)
    return sample


def normalize_source_record(record):
    """Validate one complete API-independent station source record."""
    if not isinstance(record, dict):
        raise BackendError(
            "source_mapping_error",
            "数据源记录必须是对象",
            {"field": "record"},
        )
    missing = sorted(field for field in SOURCE_REQUIRED_FIELDS if field not in record)
    if missing:
        raise BackendError(
            "source_mapping_error",
            "数据源记录缺少必需字段",
            {"fields": missing},
        )
    try:
        normalized = normalize_sample(record)
    except BackendError as exc:
        safe_details = {
            key: value
            for key, value in exc.details.items()
            if key in {"field", "fields", "span_seconds"}
        }
        raise BackendError(
            "source_mapping_error",
            "数据源记录无法转换为标准结构",
            {"cause": exc.code, **safe_details},
        ) from exc
    return {
        "bus_id": normalized["bus_id"],
        "data_time": normalized["data_time"],
        **{field: normalized[field] for field in POWER_FIELDS},
        "device_status": normalized["device_status"],
        "source_times": normalized["source_times"],
    }


def evaluate_efficiency(numerator, denominator, config):
    """计算百分比效率，并返回该指标的基础质量码。"""
    if denominator == 0:
        return None, ["zero_denominator"]
    if denominator < config["min_power_kw"]:
        return None, ["low_power"]
    efficiency = numerator / denominator * 100.0
    if (
        not math.isfinite(efficiency)
        or efficiency < 0
        or efficiency > config["max_efficiency_percent"]
    ):
        return None, ["invalid_efficiency"]
    return efficiency, []


def abnormal_device_paths(status):
    """递归返回异常状态所在的设备路径，供链路定向屏蔽和事件展示。"""
    abnormal_values = {
        "fault",
        "offline",
        "alarm",
        "communication_error",
        "comm_error",
    }

    paths = set()

    def visit(value, path):
        if isinstance(value, dict):
            identity_key = next(
                (
                    key
                    for key in ("device", "device_name", "equipment", "equipment_name")
                    if isinstance(value.get(key), str) and value[key].strip()
                ),
                None,
            )
            base_path = path
            if identity_key is not None:
                identity = value[identity_key].strip()
                if not base_path or base_path[-1] != identity:
                    base_path = base_path + (identity,)
            for key, child in value.items():
                if key == identity_key:
                    continue
                child_path = base_path + (str(key),)
                if str(key).lower() == "abnormal" and child is True:
                    paths.add(".".join(base_path or child_path))
                else:
                    visit(child, child_path)
        elif isinstance(value, list):
            for child in value:
                visit(child, path)
        elif isinstance(value, str) and value.strip().lower() in abnormal_values:
            paths.add(".".join(path) or "device_status")

    visit(status, ())
    return sorted(paths)


def has_abnormal_device(status):
    """兼容布尔判断调用，具体设备由 abnormal_device_paths 保留。"""
    return bool(abnormal_device_paths(status))


def metrics_for_abnormal_device(device_path):
    """按设备路径只屏蔽其实际参与的效率指标。"""
    name = device_path.lower()
    if any(marker in name for marker in ("pv", "solar", "inverter", "光伏", "逆变")):
        return PV_RELATED_METRICS
    if any(
        marker in name
        for marker in ("pcs", "bms", "battery", "cabinet", "storage", "ess", "电池", "储能", "柜")
    ):
        return STORAGE_RELATED_METRICS
    return CHAIN_METRICS


def detect_quality(sample, result, config):
    """返回全局质量码以及应屏蔽到各指标的质量码。"""
    global_codes = set()
    blockers = {metric: [] for metric in EFFICIENCY_METRICS}

    if result["power_balance_error"] > config["balance_error_limit_percent"]:
        global_codes.add("power_balance_error")
        for metric in CHAIN_METRICS:
            blockers[metric].append("power_balance_error")

    times = [sample["_data_time"]] + sample["_source_times"]
    if times:
        time_span = (max(times) - min(times)).total_seconds()
        if time_span > config["time_tolerance_seconds"]:
            global_codes.add("time_misaligned")
            for metric in CHAIN_METRICS:
                blockers[metric].append("time_misaligned")

    threshold = config["min_power_kw"]

    def active(value):
        return value > 0 if threshold == 0 else value >= threshold

    bms_mixed = active(sample["bms_charge_power"]) and active(
        sample["bms_discharge_power"]
    )
    pcs_mixed = active(sample["pcs_charge_power"]) and active(
        sample["pcs_discharge_power"]
    )
    charge_disagree = active(sample["pcs_charge_power"]) and active(
        sample["bms_discharge_power"]
    )
    discharge_disagree = active(sample["pcs_discharge_power"]) and active(
        sample["bms_charge_power"]
    )
    if bms_mixed:
        global_codes.add("mixed_battery_direction")
        for metric in STORAGE_RELATED_METRICS:
            blockers[metric].append("mixed_battery_direction")
    if pcs_mixed or charge_disagree or discharge_disagree:
        global_codes.add("direction_conflict")
        for metric in STORAGE_RELATED_METRICS:
            blockers[metric].append("direction_conflict")

    abnormal_devices = abnormal_device_paths(sample["device_status"])
    result["abnormal_devices"] = abnormal_devices
    if abnormal_devices:
        global_codes.add("device_abnormal")
        for device in abnormal_devices:
            for metric in metrics_for_abnormal_device(device):
                blockers[metric].append("device_abnormal")

    return sorted(global_codes), blockers


def calculate_bus(raw_sample, raw_config=None):
    """计算单条交流母线的实时能源分配和效率。"""
    config = normalize_config(raw_config)
    p = normalize_sample(raw_sample, config)

    supply = (
        p["pv_ac_power"]
        + p["grid_import_power"]
        + p["cabinet_discharge_power"]
    )
    demand = (
        p["load_power"]
        + p["cabinet_charge_power"]
        + p["grid_export_power"]
        + p["storage_aux_power"]
    )
    balance_delta = supply - demand
    balance_denominator = max(supply, config["min_power_kw"])
    if balance_denominator == 0:
        balance_error = 0.0 if balance_delta == 0 else 100.0
    else:
        balance_error = abs(balance_delta) / balance_denominator * 100.0
    unmetered_loss = max(balance_delta, 0.0)
    load_input_target = p["load_power"] + unmetered_loss

    pv_to_load_input = min(p["pv_ac_power"], load_input_target)
    remaining_input = max(load_input_target - pv_to_load_input, 0.0)
    storage_to_load_input = min(p["cabinet_discharge_power"], remaining_input)
    grid_to_load_input = max(remaining_input - storage_to_load_input, 0.0)

    pv_to_load = min(pv_to_load_input, p["load_power"])
    remaining_load = max(p["load_power"] - pv_to_load, 0.0)
    storage_to_load = min(storage_to_load_input, remaining_load)
    grid_to_load = max(remaining_load - storage_to_load, 0.0)

    pv_surplus = max(
        p["pv_ac_power"] - p["load_power"] - p["storage_aux_power"], 0.0
    )
    pv_to_storage = min(pv_surplus, p["cabinet_charge_power"])
    grid_to_storage = max(p["cabinet_charge_power"] - pv_to_storage, 0.0)

    if p["cabinet_charge_power"] > 0:
        bms_pv = (
            p["bms_charge_power"]
            * pv_to_storage
            / p["cabinet_charge_power"]
        )
    else:
        bms_pv = 0.0
    if p["cabinet_discharge_power"] > 0:
        bms_load = (
            p["bms_discharge_power"]
            * storage_to_load_input
            / p["cabinet_discharge_power"]
        )
    else:
        bms_load = 0.0
    if p["pv_ac_power"] > 0:
        pv_dc_to_storage = (
            p["pv_dc_power"] * pv_to_storage / p["pv_ac_power"]
        )
        pv_dc_to_load = p["pv_dc_power"] * pv_to_load_input / p["pv_ac_power"]
    else:
        pv_dc_to_storage = 0.0
        pv_dc_to_load = 0.0

    intermediate = {
        "pv_storage_efficiency": {
            "numerator_kw": bms_pv,
            "denominator_kw": pv_to_storage,
        },
        "pv_storage_dc_efficiency": {
            "numerator_kw": bms_pv,
            "denominator_kw": pv_dc_to_storage,
        },
        "storage_load_efficiency": {
            "numerator_kw": storage_to_load,
            "denominator_kw": bms_load,
        },
        "pv_load_efficiency": {
            "numerator_kw": pv_to_load,
            "denominator_kw": pv_to_load_input,
        },
        "pv_load_dc_efficiency": {
            "numerator_kw": pv_to_load,
            "denominator_kw": pv_dc_to_load,
        },
        "pv_inverter_efficiency": {
            "numerator_kw": p["pv_ac_power"],
            "denominator_kw": p["pv_dc_power"],
        },
        "pcs_charge_efficiency": {
            "numerator_kw": p["bms_charge_power"],
            "denominator_kw": p["pcs_charge_power"],
        },
        "pcs_discharge_efficiency": {
            "numerator_kw": p["pcs_discharge_power"],
            "denominator_kw": p["bms_discharge_power"],
        },
        "cabinet_charge_efficiency": {
            "numerator_kw": p["bms_charge_power"],
            "denominator_kw": p["cabinet_charge_power"],
        },
        "cabinet_discharge_efficiency": {
            "numerator_kw": p["cabinet_discharge_power"],
            "denominator_kw": p["bms_discharge_power"],
        },
    }

    efficiencies = {}
    quality_by_metric = {}
    all_quality_codes = set()
    for metric, values in intermediate.items():
        efficiency, codes = evaluate_efficiency(
            values["numerator_kw"], values["denominator_kw"], config
        )
        efficiencies[metric] = efficiency
        quality_by_metric[metric] = codes
        all_quality_codes.update(codes)

    load_source_count = sum(
        power > 0 for power in (pv_to_load, storage_to_load, grid_to_load)
    )
    mixed_storage_sources = pv_to_storage > 0 and grid_to_storage > 0
    calculation_mode = (
        "estimated" if load_source_count >= 2 or mixed_storage_sources else "measured"
    )
    if calculation_mode == "estimated":
        all_quality_codes.add("estimated")

    result = {
        "bus_id": p["bus_id"],
        "data_time": p["data_time"],
        "pv_to_storage_power": pv_to_storage,
        "grid_to_storage_power": grid_to_storage,
        "pv_to_load_power": pv_to_load,
        "storage_to_load_power": storage_to_load,
        "grid_to_load_power": grid_to_load,
        "pv_to_load_input_power": pv_to_load_input,
        "storage_to_load_input_power": storage_to_load_input,
        "grid_to_load_input_power": grid_to_load_input,
        "supply_power": supply,
        "demand_power": demand,
        "balance_delta_power": balance_delta,
        "power_balance_error": balance_error,
        "calculation_mode": calculation_mode,
        "quality_codes": sorted(all_quality_codes),
        "quality_by_metric": quality_by_metric,
        "calculated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "intermediate": intermediate,
    }
    result.update(efficiencies)
    global_codes, blockers = detect_quality(p, result, config)
    all_quality_codes.update(global_codes)
    for metric, codes in blockers.items():
        if not codes:
            continue
        result[metric] = None
        result["quality_by_metric"][metric] = sorted(
            set(result["quality_by_metric"][metric]) | set(codes)
        )
        all_quality_codes.update(codes)
    result["quality_codes"] = sorted(all_quality_codes)
    return result


def combine_bus_results(results, config):
    """按分子/分母合并多母线实时结果，禁止平均百分比。"""
    if not results:
        raise BackendError("invalid_input", "buses 不能为空", {"field": "buses"})

    sum_fields = (
        "pv_to_storage_power",
        "grid_to_storage_power",
        "pv_to_load_power",
        "storage_to_load_power",
        "grid_to_load_power",
        "pv_to_load_input_power",
        "storage_to_load_input_power",
        "grid_to_load_input_power",
        "supply_power",
        "demand_power",
        "balance_delta_power",
    )
    station = {field: sum(result[field] for result in results) for field in sum_fields}
    station["bus_id"] = "station"
    station["data_time"] = max(
        (parse_data_time(result["data_time"]) for result in results)
    ).isoformat()
    balance_denominator = max(station["supply_power"], config["min_power_kw"])
    balance_delta_total = sum(
        abs(result["balance_delta_power"]) for result in results
    )
    if balance_denominator == 0:
        station["power_balance_error"] = (
            0.0 if balance_delta_total == 0 else 100.0
        )
    else:
        station["power_balance_error"] = (
            balance_delta_total / balance_denominator * 100.0
        )
    station["calculation_mode"] = (
        "estimated"
        if any(result["calculation_mode"] == "estimated" for result in results)
        else "measured"
    )
    station["calculated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    station_intermediate = {}
    quality_by_metric = {}
    excluded = {}
    quality_codes = set()
    for result in results:
        quality_codes.update(result["quality_codes"])

    for metric in EFFICIENCY_METRICS:
        included = [result for result in results if result[metric] is not None]
        excluded_ids = [result["bus_id"] for result in results if result[metric] is None]
        numerator = sum(
            result["intermediate"][metric]["numerator_kw"] for result in included
        )
        denominator = sum(
            result["intermediate"][metric]["denominator_kw"] for result in included
        )
        station_intermediate[metric] = {
            "numerator_kw": numerator,
            "denominator_kw": denominator,
        }
        value, codes = evaluate_efficiency(numerator, denominator, config)
        station[metric] = value
        quality_by_metric[metric] = codes
        quality_codes.update(codes)
        excluded[metric] = excluded_ids

    times = [parse_data_time(result["data_time"]) for result in results]
    if (max(times) - min(times)).total_seconds() > config["time_tolerance_seconds"]:
        quality_codes.add("time_misaligned")
        for metric in CHAIN_METRICS:
            station[metric] = None
            quality_by_metric[metric] = sorted(
                set(quality_by_metric[metric]) | {"time_misaligned"}
            )

    station["intermediate"] = station_intermediate
    station["quality_by_metric"] = quality_by_metric
    station["excluded_buses_by_metric"] = excluded
    station["quality_codes"] = sorted(quality_codes)
    return station


def calculate_request(payload):
    """处理单母线或多母线实时计算请求。"""
    if not isinstance(payload, dict):
        raise BackendError("invalid_input", "请求必须是对象")
    config = normalize_config(payload.get("config"))
    has_sample = "sample" in payload
    has_buses = "buses" in payload
    if has_sample == has_buses:
        raise BackendError(
            "invalid_input",
            "calculate 请求必须且只能包含 sample 或 buses",
            {"fields": ["sample", "buses"]},
        )
    if has_sample:
        return {
            "operation": "calculate",
            "result": calculate_bus(payload["sample"], config),
        }
    buses = payload["buses"]
    if not isinstance(buses, list) or not buses:
        raise BackendError("invalid_input", "buses 必须是非空数组", {"field": "buses"})
    results = [calculate_bus(sample, config) for sample in buses]
    return {
        "operation": "calculate",
        "buses": results,
        "station": combine_bus_results(results, config),
    }


def _energy_efficiency(numerator, denominator, config):
    if denominator == 0:
        return None, ["zero_denominator"]
    efficiency = numerator / denominator * 100.0
    if (
        not math.isfinite(efficiency)
        or efficiency < 0
        or efficiency > config["max_efficiency_percent"]
    ):
        return None, ["invalid_efficiency"]
    return efficiency, []


def aggregate_samples(samples, raw_config=None):
    """用矩形积分计算单母线时间序列的能量效率。"""
    config = normalize_config(raw_config)
    if not isinstance(samples, list) or len(samples) < 2:
        raise BackendError(
            "invalid_time_series",
            "samples 至少需要两条记录",
            {"field": "samples"},
        )
    results = [calculate_bus(sample, config) for sample in samples]
    times = [parse_data_time(result["data_time"]) for result in results]
    intervals = []
    for index in range(len(times) - 1):
        seconds = (times[index + 1] - times[index]).total_seconds()
        if seconds <= 0:
            raise BackendError(
                "invalid_time_series",
                "samples 时间必须严格递增",
                {"index": index + 1},
            )
        intervals.append(seconds / 3600.0)

    energy_intermediate = {}
    excluded = {}
    quality_by_metric = {}
    quality_codes = set()
    for result in results[:-1]:
        quality_codes.update(result["quality_codes"])
    aggregate = {
        "bus_id": results[0]["bus_id"],
        "start_time": results[0]["data_time"],
        "end_time": results[-1]["data_time"],
        "duration_hours": sum(intervals),
        "calculation_mode": (
            "estimated"
            if any(result["calculation_mode"] == "estimated" for result in results[:-1])
            else "measured"
        ),
        "calculated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    for metric in EFFICIENCY_METRICS:
        numerator_kwh = 0.0
        denominator_kwh = 0.0
        excluded_indexes = []
        for index, hours in enumerate(intervals):
            result = results[index]
            if result[metric] is None:
                excluded_indexes.append(index)
                continue
            values = result["intermediate"][metric]
            numerator_kwh += values["numerator_kw"] * hours
            denominator_kwh += values["denominator_kw"] * hours
        energy_intermediate[metric] = {
            "numerator_kwh": numerator_kwh,
            "denominator_kwh": denominator_kwh,
        }
        value, codes = _energy_efficiency(numerator_kwh, denominator_kwh, config)
        aggregate[metric] = value
        quality_by_metric[metric] = codes
        quality_codes.update(codes)
        excluded[metric] = excluded_indexes

    aggregate["pv_storage_output_energy"] = energy_intermediate[
        "pv_storage_efficiency"
    ]["numerator_kwh"]
    aggregate["bms_pv_charge_energy"] = aggregate["pv_storage_output_energy"]
    aggregate["pv_storage_input_energy"] = energy_intermediate[
        "pv_storage_efficiency"
    ]["denominator_kwh"]
    aggregate["pv_to_storage_energy"] = aggregate["pv_storage_input_energy"]
    aggregate["pv_storage_dc_input_energy"] = energy_intermediate[
        "pv_storage_dc_efficiency"
    ]["denominator_kwh"]
    aggregate["storage_to_load_energy"] = energy_intermediate[
        "storage_load_efficiency"
    ]["numerator_kwh"]
    aggregate["bms_load_discharge_energy"] = energy_intermediate[
        "storage_load_efficiency"
    ]["denominator_kwh"]
    aggregate["pv_to_load_energy"] = energy_intermediate["pv_load_efficiency"][
        "numerator_kwh"
    ]
    aggregate["pv_load_ac_input_energy"] = energy_intermediate[
        "pv_load_efficiency"
    ]["denominator_kwh"]
    aggregate["pv_load_dc_input_energy"] = energy_intermediate[
        "pv_load_dc_efficiency"
    ]["denominator_kwh"]
    aggregate["intermediate"] = energy_intermediate
    aggregate["excluded_intervals_by_metric"] = excluded
    aggregate["quality_by_metric"] = quality_by_metric
    aggregate["quality_codes"] = sorted(quality_codes)
    return aggregate


def _combine_aggregate_results(results, config):
    """按能量分子/分母合并多母线累计结果。"""
    station = {
        "bus_id": "station",
        "start_time": min(parse_data_time(r["start_time"]) for r in results).isoformat(),
        "end_time": max(parse_data_time(r["end_time"]) for r in results).isoformat(),
        "duration_hours": max(result["duration_hours"] for result in results),
        "calculation_mode": (
            "estimated"
            if any(result["calculation_mode"] == "estimated" for result in results)
            else "measured"
        ),
        "calculated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    intermediate = {}
    quality_by_metric = {}
    quality_codes = set()
    excluded = {}
    for metric in EFFICIENCY_METRICS:
        included = [result for result in results if result[metric] is not None]
        numerator = sum(
            result["intermediate"][metric]["numerator_kwh"] for result in included
        )
        denominator = sum(
            result["intermediate"][metric]["denominator_kwh"] for result in included
        )
        intermediate[metric] = {
            "numerator_kwh": numerator,
            "denominator_kwh": denominator,
        }
        value, codes = _energy_efficiency(numerator, denominator, config)
        station[metric] = value
        quality_by_metric[metric] = codes
        quality_codes.update(codes)
        excluded[metric] = [result["bus_id"] for result in results if result[metric] is None]
    for result in results:
        quality_codes.update(result["quality_codes"])
    station["intermediate"] = intermediate
    station["quality_by_metric"] = quality_by_metric
    station["quality_codes"] = sorted(quality_codes)
    station["excluded_buses_by_metric"] = excluded
    station["pv_storage_output_energy"] = intermediate["pv_storage_efficiency"]["numerator_kwh"]
    station["bms_pv_charge_energy"] = station["pv_storage_output_energy"]
    station["pv_storage_input_energy"] = intermediate["pv_storage_efficiency"]["denominator_kwh"]
    station["pv_to_storage_energy"] = station["pv_storage_input_energy"]
    station["pv_storage_dc_input_energy"] = intermediate["pv_storage_dc_efficiency"]["denominator_kwh"]
    station["storage_to_load_energy"] = intermediate["storage_load_efficiency"]["numerator_kwh"]
    station["bms_load_discharge_energy"] = intermediate["storage_load_efficiency"]["denominator_kwh"]
    station["pv_to_load_energy"] = intermediate["pv_load_efficiency"]["numerator_kwh"]
    station["pv_load_ac_input_energy"] = intermediate["pv_load_efficiency"]["denominator_kwh"]
    station["pv_load_dc_input_energy"] = intermediate["pv_load_dc_efficiency"]["denominator_kwh"]
    return station


def aggregate_request(payload):
    """处理单母线或多母线时间序列累计请求。"""
    if not isinstance(payload, dict):
        raise BackendError("invalid_input", "请求必须是对象")
    config = normalize_config(payload.get("config"))
    has_samples = "samples" in payload
    has_buses = "buses" in payload
    if has_samples == has_buses:
        raise BackendError(
            "invalid_input",
            "aggregate 请求必须且只能包含 samples 或 buses",
            {"fields": ["samples", "buses"]},
        )
    if has_samples:
        return {
            "operation": "aggregate",
            "result": aggregate_samples(payload["samples"], config),
        }
    buses = payload["buses"]
    if not isinstance(buses, list) or not buses:
        raise BackendError("invalid_input", "buses 必须是非空数组", {"field": "buses"})
    results = []
    for index, bus in enumerate(buses):
        if not isinstance(bus, dict) or "samples" not in bus:
            raise BackendError(
                "invalid_input",
                "每条母线必须包含 samples",
                {"index": index},
            )
        result = aggregate_samples(bus["samples"], config)
        if "bus_id" in bus:
            result["bus_id"] = str(bus["bus_id"])
        results.append(result)
    return {
        "operation": "aggregate",
        "buses": results,
        "station": _combine_aggregate_results(results, config),
    }


DASHBOARD_METRICS = {
    "pvStorage": "pv_storage_dc_efficiency",
    "storageLoad": "storage_load_efficiency",
    "pvLoad": "pv_load_efficiency",
}


def _hourly_dashboard_trend(samples, config):
    """将同一日的功率样本按小时能量累计为 0–24 点曲线。"""
    results = [calculate_bus(sample, config) for sample in samples]
    times = [parse_data_time(result["data_time"]) for result in results]
    buckets = {
        hour: {
            metric: {"numerator_kwh": 0.0, "denominator_kwh": 0.0}
            for metric in DASHBOARD_METRICS.values()
        }
        for hour in range(24)
    }
    for index in range(len(times) - 1):
        start = times[index]
        end = times[index + 1]
        if end <= start:
            raise BackendError(
                "invalid_time_series",
                "trend_samples 时间必须严格递增",
                {"index": index + 1},
            )
        cursor = start
        while cursor < end:
            elapsed_hours = (cursor - times[0]).total_seconds() / 3600.0
            bucket_index = min(23, max(0, int(elapsed_hours)))
            next_hour = times[0] + timedelta(hours=bucket_index + 1)
            segment_end = min(end, next_hour)
            hours = (segment_end - cursor).total_seconds() / 3600.0
            bucket = buckets[bucket_index]
            result = results[index]
            for metric in DASHBOARD_METRICS.values():
                if result[metric] is None:
                    continue
                values = result["intermediate"][metric]
                bucket[metric]["numerator_kwh"] += values["numerator_kw"] * hours
                bucket[metric]["denominator_kwh"] += (
                    values["denominator_kw"] * hours
                )
            cursor = segment_end

    rows = []
    for hour in range(24):
        row = {"hour": hour}
        for public_name, metric in DASHBOARD_METRICS.items():
            values = buckets[hour][metric]
            value, _ = _energy_efficiency(
                values["numerator_kwh"], values["denominator_kwh"], config
            )
            row[public_name] = value
        rows.append(row)
    rows.append({"hour": 24, **{key: rows[-1][key] for key in DASHBOARD_METRICS}})
    return rows, results


def _validate_dashboard_history(samples, config, current_sample):
    """校验曲线确为同一母线、同一时区的完整 24 小时窗口。"""
    normalized = [normalize_sample(sample, config) for sample in samples]
    bus_ids = {sample["bus_id"] for sample in normalized}
    current_bus_id = current_sample["bus_id"]
    if len(bus_ids) != 1 or current_bus_id not in bus_ids:
        raise BackendError(
            "invalid_time_series",
            "trend_samples 必须与 current 属于同一母线",
            {"bus_ids": sorted(bus_ids), "current_bus_id": current_bus_id},
        )
    times = [sample["_data_time"] for sample in normalized]
    offsets = {time.utcoffset() for time in times}
    if len(offsets) != 1 or current_sample["_data_time"].utcoffset() not in offsets:
        raise BackendError(
            "invalid_time_series",
            "trend_samples 必须使用同一时区偏移",
            {"field": "trend_samples"},
        )
    for index in range(len(times) - 1):
        if times[index + 1] <= times[index]:
            raise BackendError(
                "invalid_time_series",
                "trend_samples 时间必须严格递增",
                {"index": index + 1},
            )
        gap_seconds = (times[index + 1] - times[index]).total_seconds()
        if gap_seconds > 3600.001:
            raise BackendError(
                "invalid_time_series",
                "trend_samples 相邻样本间隔不能超过 1 小时",
                {"index": index + 1, "gap_seconds": gap_seconds},
            )
    duration_seconds = (times[-1] - times[0]).total_seconds()
    if not math.isclose(duration_seconds, 24 * 3600, abs_tol=0.001):
        raise BackendError(
            "invalid_time_series",
            "trend_samples 必须覆盖完整 24 小时",
            {"duration_hours": duration_seconds / 3600.0},
        )
    current_gap = abs(
        (current_sample["_data_time"] - times[-1]).total_seconds()
    )
    if current_gap > config["time_tolerance_seconds"]:
        raise BackendError(
            "invalid_time_series",
            "trend_samples 末点必须与 current 处于同一计算窗口",
            {"difference_seconds": current_gap},
        )


def _dashboard_events(results):
    """把连续质量码合并为带真实起止时间的诊断事件。"""
    ignored = {"zero_denominator", "estimated"}
    labels = {
        "low_power": "低功率",
        "invalid_efficiency": "效率异常",
        "direction_conflict": "方向冲突",
        "mixed_battery_direction": "电池混合方向",
        "time_misaligned": "时间错位",
        "power_balance_error": "功率平衡超限",
        "device_abnormal": "设备异常",
    }
    metric_labels = {
        "pv_storage_efficiency": "光储",
        "pv_storage_dc_efficiency": "光储",
        "storage_load_efficiency": "储用",
        "pv_load_efficiency": "光用",
        "pv_load_dc_efficiency": "光用",
        "pv_inverter_efficiency": "光伏逆变器",
        "pcs_charge_efficiency": "PCS充电",
        "pcs_discharge_efficiency": "PCS放电",
        "cabinet_charge_efficiency": "储能柜充电",
        "cabinet_discharge_efficiency": "储能柜放电",
    }
    ordered = sorted(results, key=lambda item: parse_data_time(item["data_time"]))
    events = []
    active = {}

    def finish(event, end_time, status):
        event["end"] = end_time
        event["status"] = status
        event["impact"] = (
            "、".join(sorted(event.pop("_affected"))) or "相关链路"
        )
        events.append(event)

    for result in ordered:
        occurrences = []
        for code in result["quality_codes"]:
            if code in ignored or code not in labels:
                continue
            devices = (
                result.get("abnormal_devices", [])
                if code == "device_abnormal"
                else ["场站计量链路"]
            ) or ["场站计量链路"]
            for device in devices:
                allowed_metrics = (
                    set(metrics_for_abnormal_device(device))
                    if code == "device_abnormal"
                    else set(metric_labels)
                )
                affected = {
                    metric_labels[metric]
                    for metric, codes in result["quality_by_metric"].items()
                    if metric in allowed_metrics and code in codes
                }
                evidence = (
                    f"功率平衡误差 {result['power_balance_error']:.2f}%"
                    if code == "power_balance_error"
                    else labels[code]
                )
                key = (code, device, tuple(sorted(affected)))
                occurrences.append((key, code, device, affected, evidence))

        present_keys = {occurrence[0] for occurrence in occurrences}
        for key in list(active):
            if key not in present_keys:
                finish(active.pop(key), result["data_time"], "已恢复")
        for key, code, device, affected, evidence in occurrences:
            if key not in active:
                active[key] = {
                    "time": result["data_time"],
                    "start": result["data_time"],
                    "end": result["data_time"],
                    "type": labels[code],
                    "device": device,
                    "evidence": evidence,
                    "impact": "",
                    "status": "持续中",
                    "quality_code": code,
                    "_affected": set(),
                }
            active[key]["end"] = result["data_time"]
            active[key]["evidence"] = evidence
            active[key]["_affected"].update(affected)

    for event in active.values():
        finish(event, event["end"], "持续中")
    events.sort(key=lambda item: parse_data_time(item["start"]))
    return events[-20:]


def build_dashboard_payload(records, config=None):
    """Build the existing dashboard request from complete source records."""
    if not isinstance(records, list) or not records:
        raise BackendError(
            "insufficient_history",
            "数据源没有可用记录",
            {"record_count": 0 if isinstance(records, list) else None},
        )
    normalized = [normalize_source_record(record) for record in records]
    ordered = sorted(normalized, key=lambda item: parse_data_time(item["data_time"]))
    times = [parse_data_time(item["data_time"]) for item in ordered]
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise BackendError(
            "invalid_time_series",
            "数据源记录时间必须唯一",
            {"field": "data_time"},
        )
    current_bus_id = ordered[-1]["bus_id"]
    mismatch_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item["bus_id"] != current_bus_id
        ),
        None,
    )
    if mismatch_index is not None:
        raise BackendError(
            "invalid_time_series",
            "trend_samples 必须与 current 属于同一母线",
            {"field": "bus_id", "index": mismatch_index},
        )
    offsets = {item_time.utcoffset() for item_time in times}
    if len(offsets) != 1:
        raise BackendError(
            "invalid_time_series",
            "trend_samples 必须使用同一时区偏移",
            {"field": "trend_samples"},
        )
    for index in range(len(times) - 1):
        gap_seconds = (times[index + 1] - times[index]).total_seconds()
        if gap_seconds > 3600.001:
            raise BackendError(
                "invalid_time_series",
                "trend_samples 相邻样本间隔不能超过 1 小时",
                {"index": index + 1, "gap_seconds": gap_seconds},
            )

    current = ordered[-1]
    window_start = times[-1] - timedelta(hours=24)
    trend_samples = [
        item
        for item, item_time in zip(ordered, times)
        if window_start <= item_time <= times[-1]
    ]
    if not trend_samples or parse_data_time(trend_samples[0]["data_time"]) != window_start:
        raise BackendError(
            "insufficient_history",
            "数据源记录无法覆盖完整 24 小时",
            {
                "required_start": window_start.isoformat(),
                "current_time": times[-1].isoformat(),
            },
        )

    normalized_config = normalize_config(config)
    _validate_dashboard_history(
        trend_samples,
        normalized_config,
        normalize_sample(current, normalized_config),
    )
    return {
        "operation": "dashboard",
        "current": current,
        "trend_samples": trend_samples,
        "config": {} if config is None else dict(config),
    }


def dashboard_request(payload):
    """构造供 HTML 看板直接渲染的数据。"""
    if not isinstance(payload, dict):
        raise BackendError("invalid_input", "请求必须是对象")
    if "current" not in payload:
        raise BackendError(
            "invalid_input",
            "dashboard 请求缺少 current",
            {"field": "current"},
        )
    config = normalize_config(payload.get("config"))
    normalized_current = normalize_sample(payload["current"], config)
    current_result = calculate_bus(payload["current"], config)
    public_inputs = {
        "bus_id": normalized_current["bus_id"],
        "data_time": normalized_current["data_time"],
        **{field: normalized_current[field] for field in POWER_FIELDS},
    }

    trend_samples = payload.get("trend_samples", [])
    if not isinstance(trend_samples, list):
        raise BackendError(
            "invalid_input",
            "trend_samples 必须是数组",
            {"field": "trend_samples"},
        )
    if len(trend_samples) >= 2:
        _validate_dashboard_history(
            trend_samples, config, normalized_current
        )
        summary = aggregate_samples(trend_samples, config)
        trend, trend_results = _hourly_dashboard_trend(trend_samples, config)
    else:
        summary = {
            "pv_storage_efficiency": None,
            "storage_load_efficiency": None,
            "pv_load_efficiency": None,
            "quality_codes": ["insufficient_history"],
            "calculation_mode": "measured",
        }
        trend = [
            {
                "hour": hour,
                "pvStorage": None,
                "storageLoad": None,
                "pvLoad": None,
            }
            for hour in range(25)
        ]
        trend_results = []

    event_by_time = {result["data_time"]: result for result in trend_results}
    event_by_time[current_result["data_time"]] = current_result
    event_results = list(event_by_time.values())
    return {
        "operation": "dashboard",
        "realtime": {"inputs": public_inputs, "result": current_result},
        "summary_24h": summary,
        "trend": trend,
        "events": _dashboard_events(event_results),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def dispatch_request(payload):
    """按 operation 路由计算请求。"""
    if not isinstance(payload, dict):
        raise BackendError("invalid_input", "请求必须是对象")
    operation = payload.get("operation", "calculate")
    if operation == "calculate":
        return calculate_request(payload)
    if operation == "aggregate":
        return aggregate_request(payload)
    if operation == "dashboard":
        return dashboard_request(payload)
    raise BackendError(
        "unsupported_operation",
        "不支持的 operation",
        {"operation": operation},
    )
