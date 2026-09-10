"""Import-safe Uvicorn target for local M3 dashboard integration."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
import httpx

from m3.worker.api.live_dashboard import create_live_dashboard_router
from m3.worker.clients.raw_energy_api import RawEnergySourceClient
from m3.worker.config import parse_station_bindings
from m3.worker.services.live_dashboard_service import (
    DashboardCache,
    LiveDashboardProvider,
    PeriodicDashboardRefresher,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HTML_PATH = PROJECT_ROOT / "场站未来能耗预测.html"
DEFAULT_SECRET_PATH = PROJECT_ROOT / "m3/worker" / ".local/密钥.txt"
DEFAULT_SOURCE_URL = "https://vifa.hlszh.com/api/t_es_data:list"
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class LiveDashboardResources:
    cache: DashboardCache
    refresher: PeriodicDashboardRefresher
    source_http: httpx.Client
    clock: Callable[[], datetime]


def _environment_int(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    if type(raw) is not str or not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{name} must be a positive integer")
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _environment_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive number") from error
    if not value > 0 or value == float("inf"):
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _read_secret(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("live dashboard source credential file cannot be read") from error
    values = [line.strip() for line in lines if line.strip()]
    if len(values) != 1:
        raise ValueError("live dashboard source credential file must contain one token")
    return values[0]


def build_live_dashboard_resources_from_env() -> LiveDashboardResources:
    """Build owned resources; called from lifespan, never at import time."""
    stations_json = os.environ.get("M3_STATIONS_JSON")
    if stations_json is None:
        raise ValueError("M3_STATIONS_JSON is required")
    stations = parse_station_bindings(stations_json)
    source_url = os.environ.get("M3_LIVE_SOURCE_URL", DEFAULT_SOURCE_URL)
    history_days = _environment_int("M3_LIVE_HISTORY_DAYS", "90")
    if not 7 <= history_days <= 90:
        raise ValueError("M3_LIVE_HISTORY_DAYS must be in 7..90")
    refresh_seconds = _environment_float("M3_LIVE_REFRESH_SECONDS", "900")
    secret_path = Path(
        os.environ.get("M3_LIVE_SECRET_FILE", str(DEFAULT_SECRET_PATH))
    )
    token = _read_secret(secret_path)
    clock = lambda: datetime.now(SHANGHAI)
    source_http = httpx.Client(
        timeout=httpx.Timeout(connect=5, read=20, write=10, pool=5),
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        follow_redirects=False,
    )
    try:
        source = RawEnergySourceClient(
            source_url,
            token,
            source_http,
            allowed_station_ids=tuple(item.station_id for item in stations),
        )
        cache = DashboardCache(stations)
        provider = LiveDashboardProvider(
            source,
            clock=clock,
            history_days=history_days,
        )
        refresher = PeriodicDashboardRefresher(
            provider,
            cache,
            stations,
            interval_seconds=refresh_seconds,
        )
    except Exception:
        source_http.close()
        raise
    return LiveDashboardResources(cache, refresher, source_http, clock)


def create_live_dashboard_app(
    resources_factory: Callable[[], LiveDashboardResources],
    html_path: Path,
) -> FastAPI:
    """Create the import-safe shell and own startup/shutdown ordering."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        resources = resources_factory()
        application.state.dashboard_cache = resources.cache
        application.state.clock = resources.clock
        try:
            resources.refresher.start()
            yield
        finally:
            try:
                resources.refresher.stop()
            finally:
                resources.source_http.close()

    application = FastAPI(
        title="VIFA M3 Live Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.include_router(create_live_dashboard_router(html_path))
    return application


app = create_live_dashboard_app(build_live_dashboard_resources_from_env, HTML_PATH)
