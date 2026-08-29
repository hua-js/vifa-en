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
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
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
        or value.get("version") != 3
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


def field_payload(name: str, definition: object) -> dict[str, Any]:
    if IDENTIFIER.fullmatch(name) is None or type(definition) is not dict:
        raise DeploymentError("集合契约包含无效字段")
    field_type, type_options = nocobase_type(definition.get("type"))
    nullable = definition.get("nullable")
    if type(nullable) is not bool:
        raise DeploymentError(f"字段 {name} 未明确 nullable")
    payload: dict[str, Any] = {
        "name": name,
        "type": field_type,
        "allowNull": nullable,
        **type_options,
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
    if name == "created_at":
        payload["interface"] = "createdAt"
    elif name == "updated_at":
        payload["interface"] = "updatedAt"
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
        )
        if any(key in field and actual_field.get(key) != field[key] for key in checked_options):
            raise DeploymentError(f"集合 {expected['name']} 字段 {field['name']} 配置回读不一致")
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
        print(json.dumps({"collections": payloads}, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.execute:
        print("offline-plan-only: 未连接 NocoBase，未创建任何表", file=sys.stderr)
        for payload in payloads:
            print(
                f"planned {payload['name']} fields={len(payload['fields'])} indexes={len(payload['indexes'])}",
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
