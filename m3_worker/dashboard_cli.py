"""One-shot, compact JSON dashboard entrypoint for a fixed Node-RED Exec node."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import json
import os
import sys
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from m3_worker.clients.http import RetryPolicy
from m3_worker.clients.nocobase_api import NocoBaseApiClient
from m3_worker.clients.raw_energy_api import RawEnergySourceClient
from m3_worker.config import DashboardSettings
from m3_worker.dashboard_contracts import DashboardEnvelope
from m3_worker.errors import M3Error
from m3_worker.services.persisted_dashboard_service import PersistedDashboardService


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _error_payload(code: str, message: str) -> dict[str, object]:
    return {"status": "error", "error": {"code": code, "message": message}}


def _compact_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def build_dashboard(
    settings: DashboardSettings,
    *,
    client_factory: Callable[..., Any] = httpx.Client,
    service_factory: Callable[..., Any] = PersistedDashboardService,
    clock: Callable[[], datetime] | None = None,
) -> DashboardEnvelope:
    """Own and close the two least-privilege outbound HTTP clients."""

    clock = clock or (lambda: datetime.now(SHANGHAI))
    timeout = httpx.Timeout(connect=2.0, read=15.0, write=15.0, pool=2.0)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    source_http = None
    nocobase_http = None
    try:
        source_http = client_factory(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
        )
        nocobase_http = client_factory(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
        )
        source = RawEnergySourceClient(
            str(settings.raw_source_url),
            settings.raw_source_api_token.get_secret_value(),
            source_http,
            allowed_station_ids=settings.station_ids,
        )
        api = NocoBaseApiClient(
            str(settings.nocobase_base_url),
            settings.nocobase_api_key.get_secret_value(),
            nocobase_http,
            RetryPolicy(),
        )
        service = service_factory(
            source=source,
            api=api,
            bindings=settings.stations,
            clock=clock,
        )
        envelope = service.build()
        if not isinstance(envelope, DashboardEnvelope):
            raise M3Error(
                "dashboard_contract_invalid", "Dashboard result is invalid"
            )
        return envelope
    finally:
        first_error: Exception | None = None
        for client in (source_http, nocobase_http):
            if client is None:
                continue
            try:
                client.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None and sys.exc_info()[0] is None:
            raise first_error


def execute(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    builder: Callable[[DashboardSettings], DashboardEnvelope] = build_dashboard,
) -> tuple[dict[str, object], int]:
    """Execute one fixed dashboard read and return its public payload and exit code."""

    if list(argv) != ["dashboard"]:
        return _error_payload("invalid_arguments", "参数不正确"), 2
    try:
        settings = DashboardSettings.from_env(environ)
    except (TypeError, ValueError, ValidationError):
        return _error_payload("missing_config", "服务器缺少运行配置"), 3
    except Exception:
        return _error_payload("internal_error", "服务器内部错误"), 1

    try:
        envelope = builder(settings)
        if not isinstance(envelope, DashboardEnvelope):
            raise M3Error(
                "dashboard_contract_invalid", "Dashboard result is invalid"
            )
        payload = envelope.model_dump(mode="json")
        DashboardEnvelope.model_validate(payload)
        return payload, 0
    except M3Error as error:
        if error.code.startswith("source_"):
            return _error_payload("source_error", "历史实测数据读取失败"), 4
        if error.code == "sink_contract_invalid" or error.code.startswith(
            "dashboard_contract_"
        ):
            return _error_payload("contract_error", "预测结果格式不正确"), 6
        if error.code.startswith("sink_"):
            return _error_payload("nocobase_error", "预测结果读取失败"), 5
        return _error_payload("internal_error", "服务器内部错误"), 1
    except (TypeError, ValueError, ValidationError):
        return _error_payload("contract_error", "预测结果格式不正确"), 6
    except Exception:
        return _error_payload("internal_error", "服务器内部错误"), 1


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdout=None,
    *,
    builder: Callable[[DashboardSettings], DashboardEnvelope] = build_dashboard,
) -> int:
    """Write exactly one compact JSON line for Node-RED and return an exit code."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    runtime_environment = os.environ if environ is None else environ
    output = sys.stdout if stdout is None else stdout
    payload, exit_code = execute(
        arguments,
        runtime_environment,
        builder=builder,
    )
    try:
        text = _compact_json(payload)
    except (TypeError, ValueError, OverflowError):
        text = _compact_json(_error_payload("internal_error", "服务器内部错误"))
        exit_code = 1
    output.write(text + "\n")
    return exit_code
