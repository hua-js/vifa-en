"""Read-only persisted Dashboard API served through a Unix socket."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from m3.worker.config import DashboardSettings
from m3.worker.dashboard_cli import build_dashboard
from m3.worker.dashboard_contracts import DashboardEnvelope
from m3.worker.errors import M3Error


SettingsFactory = Callable[[], DashboardSettings]
DashboardBuilder = Callable[[DashboardSettings], DashboardEnvelope]


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _map_error(error: Exception) -> JSONResponse:
    if isinstance(error, M3Error):
        if error.code.startswith("source_"):
            return _error_response(502, "source_error", "历史实测数据读取失败")
        if error.code == "sink_contract_invalid" or error.code.startswith(
            "dashboard_contract_"
        ):
            return _error_response(502, "contract_error", "预测结果格式不正确")
        if error.code.startswith("sink_"):
            return _error_response(502, "nocobase_error", "预测结果读取失败")
    if isinstance(error, (TypeError, ValueError, ValidationError)):
        return _error_response(502, "contract_error", "预测结果格式不正确")
    return _error_response(500, "internal_error", "服务器内部错误")


def create_persisted_dashboard_app(
    settings_factory: SettingsFactory,
    builder: DashboardBuilder,
) -> FastAPI:
    """Create an import-safe app and parse its least-privilege config once."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.dashboard_settings = settings_factory()
        yield

    application = FastAPI(
        title="VIFA M3 Persisted Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/dashboard", response_model=DashboardEnvelope)
    async def dashboard(request: Request) -> Any:
        if request.query_params or await request.body():
            return _error_response(400, "invalid_request", "请求不正确")
        try:
            result = await run_in_threadpool(
                builder, request.app.state.dashboard_settings
            )
            return DashboardEnvelope.model_validate(result)
        except Exception as error:
            return _map_error(error)

    return application


app = create_persisted_dashboard_app(DashboardSettings.from_env, build_dashboard)
