#!/usr/bin/env python3
"""Create the three M3 custom-forecast collections through NocoBase.

The command is offline-only unless ``--execute`` is explicitly supplied. It
never destroys, updates, or retries creation of a collection.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


COLLECTION_NAMES = (
    "energy_forecast_manual_runs",
    "energy_forecast_manual_points",
    "energy_forecast_manual_evaluations",
)
COLLECTION_TITLES = {
    "energy_forecast_manual_runs": "M3 自定义预测任务",
    "energy_forecast_manual_points": "M3 自定义预测点",
    "energy_forecast_manual_evaluations": "M3 自定义预测评估",
}
COLLECTION_DESCRIPTIONS = {
    "energy_forecast_manual_runs": "保存 M3 用户自定义预测任务、模型与数据来源清单。",
    "energy_forecast_manual_points": "保存 M3 自定义预测任务的负荷与 SOC 预测点。",
    "energy_forecast_manual_evaluations": "保存 M3 自定义预测的可比实绩评估结果。",
}
FIELD_TITLES = {
    "id": "主键 ID",
    "run_id": "预测任务 ID",
    "station_id": "场站 ID",
    "idempotency_key": "幂等键",
    "history_start": "历史数据开始时间",
    "history_end": "历史数据结束时间",
    "history_days": "历史数据天数",
    "forecast_start": "预测开始时间",
    "forecast_end": "预测结束时间",
    "forecast_days": "预测天数",
    "interval_seconds": "输出间隔（秒）",
    "points_per_day": "每日预测点数",
    "expected_points_per_series": "单序列预期点数",
    "model_policy": "模型策略",
    "status": "任务状态",
    "model_manifest": "模型清单",
    "source_manifest": "数据源清单",
    "content_hash": "内容哈希",
    "error_code": "错误码",
    "requested_by": "请求人",
    "started_at": "开始执行时间",
    "completed_at": "完成时间",
    "evaluated_at": "评估时间",
    "run_pk": "预测任务主键",
    "unique_id": "序列标识",
    "target_time": "目标时间",
    "horizon_step": "预测步数",
    "model_name": "模型名称",
    "raw_forecast": "原始预测值",
    "forecast_value": "预测值",
    "baseline_forecast_value": "基线预测值",
    "is_clipped": "是否截断",
    "actual_value": "实际值",
    "actual_quality": "实际值质量",
    "actual_source_revision": "实际值数据版本",
    "actual_recorded_at": "实际值记录时间",
    "absolute_percentage_error": "绝对百分比误差",
    "evaluation_key": "评估键",
    "current_score": "当前负载评分",
    "window_start": "评估窗口开始",
    "window_end": "评估窗口结束",
    "expected_count": "预期点数",
    "valid_count": "有效点数",
    "zero_actual_count": "实际值为零点数",
    "mape_percent": "MAPE（%）",
    "mae": "MAE",
    "smape_percent": "sMAPE（%）",
    "wape_percent": "WAPE（%）",
    "median_ape_percent": "APE 中位数（%）",
    "p90_ape_percent": "APE P90（%）",
    "baseline_mape_percent": "基线 MAPE（%）",
    "relative_baseline_improvement_percent": "相对基线提升（%）",
    "outcome": "评估结果",
    "calculated_at": "计算时间",
    "run": "预测任务",
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
SYSTEM_FIELD_NAMES = frozenset({"createdAt", "createdBy", "updatedAt", "updatedBy"})
SYSTEM_FIELD_TITLES = {
    "createdAt": '{{t("Created at")}}',
    "createdBy": '{{t("Created by")}}',
    "updatedAt": '{{t("Last updated at")}}',
    "updatedBy": '{{t("Last updated by")}}',
}
TOKEN = re.compile(r"[\x21-\x7e]{16,4096}\Z")
NUMERIC_TYPE = re.compile(r"numeric\(([1-9][0-9]*),([0-9]+)\)\Z")
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class DeploymentError(RuntimeError):
    """The schema deployment cannot continue safely."""


class NoRedirect(HTTPRedirectHandler):
    """Do not forward an administrative bearer token across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def contract_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "nocobase_collections.json"


def load_contract() -> dict[str, Any]:
    try:
        value = json.loads(contract_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeploymentError("无法读取 M3 NocoBase 集合契约") from error
    if (
        type(value) is not dict
        or value.get("version") != 5
        or type(value.get("collections")) is not list
    ):
        raise DeploymentError("M3 NocoBase 集合契约格式不正确")
    return value


def nocobase_type(type_name: object) -> tuple[str, dict[str, int]]:
    mapping = {
        "bigint": "bigInt",
        "text": "text",
        "timestamptz": "date",
        "smallint": "integer",
        "integer": "integer",
        "jsonb": "json",
        "boolean": "boolean",
    }
    if type(type_name) is not str:
        raise DeploymentError("集合契约包含无效字段类型")
    if type_name in mapping:
        return mapping[type_name], {}
    matched = NUMERIC_TYPE.fullmatch(type_name)
    if matched is not None:
        precision = int(matched.group(1))
        scale = int(matched.group(2))
        if scale > precision:
            raise DeploymentError(f"无效 decimal 精度：{type_name}")
        return "decimal", {"precision": precision, "scale": scale}
    raise DeploymentError(f"collections:create 不支持契约字段类型：{type_name}")


def field_ui_options(name: str, field_type: str, type_options: dict[str, int]) -> dict[str, Any]:
    title = FIELD_TITLES.get(name)
    if title is None:
        raise DeploymentError(f"字段 {name} 缺少 NocoBase 显示名称")
    if field_type == "text":
        return {
            "interface": "textarea",
            "uiSchema": {
                "type": "string",
                "title": title,
                "x-component": "Input.TextArea",
            },
        }
    if field_type in {"integer", "bigInt"}:
        return {
            "interface": "integer",
            "uiSchema": {
                "type": "number",
                "title": title,
                "x-component": "InputNumber",
                "x-component-props": {"stringMode": True, "step": "1"},
                "x-validator": "integer",
            },
        }
    if field_type == "decimal":
        scale = type_options.get("scale", 0)
        step = "1" if scale == 0 else f"0.{'0' * (scale - 1)}1"
        return {
            "interface": "number",
            "uiSchema": {
                "type": "number",
                "title": title,
                "x-component": "InputNumber",
                "x-component-props": {"stringMode": True, "step": step},
            },
        }
    if field_type == "date":
        return {
            "interface": "datetime",
            "timezone": True,
            "uiSchema": {
                "type": "string",
                "title": title,
                "x-component": "DatePicker",
                "x-component-props": {"showTime": True, "utc": True},
            },
        }
    if field_type == "json":
        return {
            "interface": "json",
            "jsonb": True,
            "uiSchema": {
                "type": "object",
                "title": title,
                "x-component": "Input.JSON",
                "x-component-props": {"autoSize": {"minRows": 5}},
                "default": None,
            },
        }
    if field_type == "boolean":
        return {
            "interface": "checkbox",
            "uiSchema": {
                "type": "boolean",
                "title": title,
                "x-component": "Checkbox",
            },
        }
    raise DeploymentError(f"字段 {name} 缺少 NocoBase 界面类型映射")


def system_field_payload(name: str, definition: dict[str, Any]) -> dict[str, Any]:
    if definition.get("nullable") is not True or definition.get("client_writable") is not False:
        raise DeploymentError(f"系统字段 {name} 必须是只读可空字段")
    if definition.get("interface") != name:
        raise DeploymentError(f"系统字段 {name} 的 interface 不正确")
    if name in {"createdAt", "updatedAt"}:
        expected_management = {"on_create": "current_timestamp"}
        if name == "updatedAt":
            expected_management["on_update"] = "current_timestamp"
        if (
            definition.get("type") != "timestamptz"
            or definition.get("server_managed") != expected_management
        ):
            raise DeploymentError(f"系统时间字段 {name} 的契约不正确")
        return {
            "name": name,
            "type": "date",
            "interface": name,
            "field": name,
            "uiSchema": {
                "title": SYSTEM_FIELD_TITLES[name],
                "type": "datetime",
                "x-component": "DatePicker",
                "x-component-props": {
                    "dateFormat": "YYYY-MM-DD",
                    "picker": "date",
                },
                "x-read-pretty": True,
            },
        }
    expected_management = {"on_create": "current_user"}
    if name == "updatedBy":
        expected_management["on_update"] = "current_user"
    expected_foreign_key = "createdById" if name == "createdBy" else "updatedById"
    if (
        definition.get("type") != "belongsTo"
        or definition.get("target") != "users"
        or definition.get("foreign_key") != expected_foreign_key
        or definition.get("target_key") != "id"
        or definition.get("server_managed") != expected_management
    ):
        raise DeploymentError(f"系统人员字段 {name} 的契约不正确")
    return {
        "name": name,
        "type": "belongsTo",
        "interface": name,
        "target": "users",
        "foreignKey": expected_foreign_key,
        "targetKey": "id",
        "uiSchema": {
            "title": SYSTEM_FIELD_TITLES[name],
            "type": "object",
            "x-component": "AssociationField",
            "x-component-props": {
                "fieldNames": {"label": "nickname", "value": "id"},
            },
            "x-read-pretty": True,
        },
    }


def field_payload(name: str, definition: object) -> dict[str, Any]:
    if (
        (IDENTIFIER.fullmatch(name) is None and name not in SYSTEM_FIELD_NAMES)
        or type(definition) is not dict
    ):
        raise DeploymentError("集合契约包含无效字段")
    if name in SYSTEM_FIELD_NAMES:
        return system_field_payload(name, definition)
    field_type, type_options = nocobase_type(definition.get("type"))
    nullable = definition.get("nullable")
    if type(nullable) is not bool:
        raise DeploymentError(f"字段 {name} 未明确 nullable")
    payload: dict[str, Any] = {
        "name": name,
        "type": field_type,
        "allowNull": nullable,
        **type_options,
        **field_ui_options(name, field_type, type_options),
    }
    if definition.get("primary_key") is True:
        payload["primaryKey"] = True
    if definition.get("identity") is True:
        payload["autoIncrement"] = True
    managed = definition.get("server_managed")
    generated = definition.get("generated_on_create")
    if managed is not None:
        if type(managed) is not dict:
            raise DeploymentError(f"字段 {name} 的 server_managed 无效")
        if managed.get("on_create") == "current_timestamp":
            payload["defaultToCurrentTime"] = True
        if managed.get("on_update") == "current_timestamp":
            payload["onUpdateToCurrentTime"] = True
    if generated is not None:
        if generated != "current_timestamp":
            raise DeploymentError(f"字段 {name} 的生成规则不受支持")
        payload["defaultToCurrentTime"] = True
    return payload


def index_payloads(collection: dict[str, Any], field_names: set[str]) -> list[dict[str, Any]]:
    raw_indexes = collection.get("indexes")
    if type(raw_indexes) is not list:
        raise DeploymentError(f"集合 {collection.get('name')} 缺少 indexes")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in raw_indexes:
        if type(raw) is not dict:
            raise DeploymentError("集合索引格式不正确")
        name = raw.get("name")
        fields = raw.get("fields")
        unique = raw.get("unique")
        if (
            type(name) is not str
            or IDENTIFIER.fullmatch(name) is None
            or name in names
            or type(fields) is not list
            or not fields
            or any(type(field) is not str or field not in field_names for field in fields)
            or type(unique) is not bool
        ):
            raise DeploymentError(f"集合 {collection.get('name')} 包含无效索引")
        names.add(name)
        result.append({"name": name, "fields": fields, "unique": unique})
    return result


def association_field(collection_name: str) -> dict[str, Any] | None:
    if collection_name == "energy_forecast_manual_runs":
        return None
    return {
        "name": "run",
        "type": "belongsTo",
        "interface": "m2o",
        "uiSchema": {
            "title": FIELD_TITLES["run"],
            "x-component": "AssociationField",
            "x-component-props": {"multiple": False},
        },
        "target": "energy_forecast_manual_runs",
        "foreignKey": "run_pk",
        "targetKey": "id",
        "onDelete": "RESTRICT",
        "constraints": True,
    }


def validate_foreign_key(collection: dict[str, Any]) -> None:
    name = collection.get("name")
    foreign_keys = collection.get("foreign_keys")
    if name == "energy_forecast_manual_runs":
        if foreign_keys != []:
            raise DeploymentError(f"集合 {name} 不应包含外键")
        return
    expected = {
        "fields": ["run_pk"],
        "references": {
            "collection": "energy_forecast_manual_runs",
            "fields": ["id"],
        },
        "on_delete": "restrict",
    }
    if type(foreign_keys) is not list or len(foreign_keys) != 1:
        raise DeploymentError(f"集合 {name} 必须只有一个 run_pk 外键")
    actual = foreign_keys[0]
    if type(actual) is not dict or any(actual.get(key) != value for key, value in expected.items()):
        raise DeploymentError(f"集合 {name} 的 run_pk 外键与脚本不一致")


def validate_contract_id(collection_name: str, fields: dict[str, Any]) -> None:
    definition = fields.get("id")
    if type(definition) is not dict or any(
        definition.get(key) != expected
        for key, expected in {
            "type": "bigint",
            "nullable": False,
            "primary_key": True,
            "identity": True,
        }.items()
    ):
        raise DeploymentError(f"集合 {collection_name} 的 id 必须是非空自增 bigint 主键")


def build_payloads() -> list[dict[str, Any]]:
    contract = load_contract()
    selected = {
        item.get("name"): item
        for item in contract["collections"]
        if type(item) is dict and item.get("name") in COLLECTION_NAMES
    }
    if tuple(name for name in COLLECTION_NAMES if name in selected) != COLLECTION_NAMES:
        raise DeploymentError("集合契约未包含全部三张自定义预测表")
    payloads: list[dict[str, Any]] = []
    for name in COLLECTION_NAMES:
        collection = selected[name]
        validate_foreign_key(collection)
        raw_fields = collection.get("fields")
        if type(raw_fields) is not dict or not raw_fields:
            raise DeploymentError(f"集合 {name} 缺少 fields")
        validate_contract_id(name, raw_fields)
        fields = [field_payload(field_name, definition) for field_name, definition in raw_fields.items()]
        association = association_field(name)
        if association is not None:
            if "run_pk" not in raw_fields:
                raise DeploymentError(f"集合 {name} 缺少 run_pk")
            fields.append(association)
        indexes = index_payloads(collection, set(raw_fields))
        payloads.append(
            {
                "name": name,
                "title": COLLECTION_TITLES[name],
                "description": COLLECTION_DESCRIPTIONS[name],
                "autoGenId": False,
                "timestamps": False,
                "createdAt": False,
                "updatedAt": False,
                "createdBy": False,
                "updatedBy": False,
                "sortable": False,
                "filterTargetKey": "id",
                "fields": fields,
                "indexes": indexes,
            }
        )
    return payloads


def strict_origin(value: str, *, allow_http: bool) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DeploymentError("M3_NOCOBASE_BASE_URL 必须是完整 HTTP(S) 源地址")
    parsed = urlsplit(value)
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentError("M3_NOCOBASE_BASE_URL 必须是不含凭据、路径或参数的安全源地址")
    try:
        port = parsed.port
    except ValueError as error:
        raise DeploymentError("M3_NOCOBASE_BASE_URL 端口无效") from error
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"


def read_token_file(path_value: str) -> str:
    path = Path(path_value)
    try:
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise DeploymentError("Schema API Key 文件不是普通文件")
        if file_stat.st_mode & 0o077:
            raise DeploymentError("Schema API Key 文件权限必须为 0600 或更严格")
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except DeploymentError:
        raise
    except (OSError, UnicodeError) as error:
        raise DeploymentError("无法读取 Schema API Key 文件") from error
    if len(lines) != 1:
        raise DeploymentError("Schema API Key 文件必须只有一行非空内容")
    return lines[0]


def schema_token() -> str:
    direct = os.environ.get("M3_NOCOBASE_SCHEMA_API_KEY", "").strip()
    token_file = os.environ.get("M3_NOCOBASE_SCHEMA_API_KEY_FILE", "").strip()
    if bool(direct) == bool(token_file):
        raise DeploymentError(
            "必须且只能配置 M3_NOCOBASE_SCHEMA_API_KEY 或 M3_NOCOBASE_SCHEMA_API_KEY_FILE"
        )
    value = direct if direct else read_token_file(token_file)
    if TOKEN.fullmatch(value) is None:
        raise DeploymentError("Schema API Key 格式无效")
    return value


class NocoBaseSchemaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(NoRedirect())

    def _decode(self, response) -> dict[str, Any]:  # noqa: ANN001
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > self._max_response_bytes:
                    raise DeploymentError("NocoBase 响应超过大小限制")
            except ValueError:
                pass
        body = response.read(self._max_response_bytes + 1)
        if len(body) > self._max_response_bytes:
            raise DeploymentError("NocoBase 响应超过大小限制")
        try:
            value = json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DeploymentError("NocoBase 返回了无效 JSON") from error
        if type(value) is not dict or value.get("errors"):
            raise DeploymentError("NocoBase 拒绝了集合请求")
        return value

    def _request(self, request: Request, *, missing_is_none: bool = False) -> dict[str, Any] | None:
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise DeploymentError(f"NocoBase 集合请求失败，HTTP {response.status}")
                return self._decode(response)
        except HTTPError as error:
            if missing_is_none and error.code == 404:
                return None
            if error.code in {301, 302, 303, 307, 308}:
                raise DeploymentError("NocoBase 返回重定向，已拒绝转发管理令牌") from error
            if error.code in {401, 403}:
                raise DeploymentError("Schema API Key 无权管理 NocoBase 集合") from error
            raise DeploymentError(f"NocoBase 集合请求失败，HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise DeploymentError("NocoBase 集合请求连接失败，未自动重试") from error

    def get_collection(self, name: str) -> dict[str, Any] | None:
        query = urlencode((("filterByTk", name), ("appends[]", "fields")))
        request = Request(
            f"{self._base_url}/api/collections:get?{query}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        envelope = self._request(request, missing_is_none=True)
        if envelope is None or envelope.get("data") is None:
            return None
        data = envelope.get("data")
        if type(data) is not dict:
            raise DeploymentError("NocoBase collection:get 响应格式不正确")
        return data

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self._base_url}/api/collections:create",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        envelope = self._request(request)
        data = envelope.get("data") if envelope is not None else None
        if type(data) is not dict or data.get("name") != payload["name"]:
            raise DeploymentError("NocoBase collections:create 响应格式不正确")
        return data


def normalized_index_fields(index: object) -> tuple[str, ...]:
    if type(index) is not dict or type(index.get("fields")) is not list:
        return ()
    result: list[str] = []
    for field in index["fields"]:
        if type(field) is str:
            result.append(field)
        elif type(field) is dict and type(field.get("name")) is str:
            result.append(field["name"])
        else:
            return ()
    return tuple(result)


def verify_collection(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual.get("name") != expected["name"]:
        raise DeploymentError(f"集合 {expected['name']} 回读名称不一致")
    for key in ("autoGenId", "timestamps", "filterTargetKey"):
        if actual.get(key) != expected[key]:
            raise DeploymentError(f"集合 {expected['name']} 配置 {key} 回读不一致")
    actual_fields = actual.get("fields")
    if type(actual_fields) is not list:
        raise DeploymentError(f"集合 {expected['name']} 回读缺少 fields")
    actual_by_name = {
        item.get("name"): item
        for item in actual_fields
        if type(item) is dict and type(item.get("name")) is str
    }
    expected_names = {field["name"] for field in expected["fields"]}
    actual_names = set(actual_by_name)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        details = []
        if missing:
            details.append("缺少 " + ", ".join(missing))
        if unexpected:
            details.append("多出 " + ", ".join(unexpected))
        raise DeploymentError(f"集合 {expected['name']} 字段集合不一致：{'；'.join(details)}")
    id_field = actual_by_name["id"]
    if any(
        id_field.get(key) != value
        for key, value in {
            "type": "bigInt",
            "allowNull": False,
            "primaryKey": True,
            "autoIncrement": True,
        }.items()
    ):
        raise DeploymentError(f"集合 {expected['name']} 的 id 不是非空自增 bigint 主键")
    for field in expected["fields"]:
        actual_field = actual_by_name.get(field["name"])
        if type(actual_field) is not dict:
            raise DeploymentError(f"集合 {expected['name']} 字段 {field['name']} 回读不一致")
        checked_options = (
            "type",
            "allowNull",
            "primaryKey",
            "autoIncrement",
            "precision",
            "scale",
            "defaultToCurrentTime",
            "onUpdateToCurrentTime",
            "target",
            "foreignKey",
            "targetKey",
            "onDelete",
            "constraints",
            "interface",
            "timezone",
            "jsonb",
        )
        if any(key in field and actual_field.get(key) != field[key] for key in checked_options):
            raise DeploymentError(f"集合 {expected['name']} 字段 {field['name']} 配置回读不一致")
        expected_ui = field.get("uiSchema")
        actual_ui = actual_field.get("uiSchema")
        if type(expected_ui) is dict and (
            type(actual_ui) is not dict
            or actual_ui.get("title") != expected_ui.get("title")
            or actual_ui.get("x-component") != expected_ui.get("x-component")
        ):
            raise DeploymentError(f"集合 {expected['name']} 字段 {field['name']} 界面配置回读不一致")
    actual_indexes = actual.get("indexes")
    if type(actual_indexes) is not list:
        raise DeploymentError(f"集合 {expected['name']} 回读缺少 indexes")
    indexed = {
        (item.get("name"), normalized_index_fields(item), item.get("unique"))
        for item in actual_indexes
        if type(item) is dict
    }
    for index in expected["indexes"]:
        identity = (index["name"], tuple(index["fields"]), index["unique"])
        if identity not in indexed:
            raise DeploymentError(f"集合 {expected['name']} 索引 {index['name']} 回读不一致")


def physical_columns(payload: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for field in payload["fields"]:
        if field["type"] == "belongsTo":
            column = field.get("foreignKey")
            if type(column) is not str:
                continue
        else:
            column = field["name"]
        if column not in result:
            result.append(column)
    return result


def execute(args: argparse.Namespace, payloads: list[dict[str, Any]]) -> None:
    base_value = args.base_url or os.environ.get("M3_NOCOBASE_BASE_URL", "")
    base_url = strict_origin(base_value, allow_http=args.allow_http)
    client = NocoBaseSchemaClient(
        base_url,
        schema_token(),
        timeout_seconds=args.timeout_seconds,
        max_response_bytes=args.max_response_bytes,
    )
    existing = [payload["name"] for payload in payloads if client.get_collection(payload["name"]) is not None]
    if existing:
        raise DeploymentError(f"检测到同名集合，未创建任何表：{', '.join(existing)}")
    created: list[str] = []
    active_name: str | None = None
    try:
        for payload in payloads:
            active_name = payload["name"]
            client.create_collection(payload)
            created.append(payload["name"])
            actual = client.get_collection(payload["name"])
            if actual is None:
                raise DeploymentError(f"集合 {payload['name']} 创建后无法回读")
            verify_collection(actual, payload)
            print(f"created-and-verified {payload['name']}")
            active_name = None
    except Exception as error:
        if active_name is not None and active_name not in created:
            print(f"创建状态未知，必须先人工检查：{active_name}", file=sys.stderr)
        if created:
            print(
                "已创建但未自动删除：" + ", ".join(created),
                file=sys.stderr,
            )
        raise error


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="通过 POST /api/collections:create 创建三张 M3 自定义预测表；默认不联网。"
    )
    value.add_argument("--execute", action="store_true", help="执行预检、创建和回读核验")
    value.add_argument("--show-payloads", action="store_true", help="输出将提交的 JSON，不联网")
    value.add_argument("--base-url", help="NocoBase 安全源地址；默认读取 M3_NOCOBASE_BASE_URL")
    value.add_argument("--allow-http", action="store_true", help="仅用于可信测试网，允许 HTTP")
    value.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    value.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    return value


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.timeout_seconds <= 60:
        raise DeploymentError("timeout-seconds 必须为 1–60 的整数")
    if not 1024 <= args.max_response_bytes <= 16 * 1024 * 1024:
        raise DeploymentError("max-response-bytes 必须在 1 KiB–16 MiB 之间")
    payloads = build_payloads()
    if args.show_payloads:
        summaries = []
        for payload in payloads:
            physical_fields = physical_columns(payload)
            metadata_fields = [field["name"] for field in payload["fields"]]
            summaries.append(
                {
                    "name": payload["name"],
                    "explicit_primary_key": "id",
                    "physical_column_count": len(physical_fields),
                    "physical_columns": physical_fields,
                    "metadata_field_count": len(metadata_fields),
                    "metadata_fields": metadata_fields,
                }
            )
        print(
            json.dumps(
                {"schema_summary": summaries, "collections": payloads},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    if not args.execute:
        print("offline-plan-only: 未连接 NocoBase，未创建任何表", file=sys.stderr)
        for payload in payloads:
            physical_count = len(physical_columns(payload))
            print(
                f"planned {payload['name']} physical_columns={physical_count} "
                f"metadata_fields={len(payload['fields'])} indexes={len(payload['indexes'])}",
                file=sys.stderr,
            )
        return 0
    execute(args, payloads)
    print("all-custom-forecast-collections-created-and-verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as error:
        print(f"deployment-error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
