"""Same-origin read-only routes for the live M3 dashboard."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from m3.worker.dashboard_contracts import DashboardEnvelope


def create_live_dashboard_router(html_path: Path) -> APIRouter:
    resolved_html = html_path.resolve()
    router = APIRouter()

    @router.get("/energy-forecast-api")
    def dashboard(request: Request) -> DashboardEnvelope:
        return request.app.state.dashboard_cache.snapshot(
            request.app.state.clock()
        )

    @router.get("/", response_class=FileResponse)
    def page() -> FileResponse:
        return FileResponse(
            resolved_html,
            media_type="text/html; charset=utf-8",
        )

    return router
