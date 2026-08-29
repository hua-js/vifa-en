"""M3 Worker resource composition and FastAPI lifespan."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
from threading import Lock
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import httpx

from m3_worker.api.routes import health_router, router
from m3_worker.clients.alert_api import NodeRedAlertClient
from m3_worker.clients.http import RetryPolicy
from m3_worker.clients.nocobase_api import NocoBaseApiClient
from m3_worker.clients.raw_energy_api import RawEnergySourceClient
from m3_worker.clients.source_api import STATION_ID_PATTERN
from m3_worker.config import Settings
from m3_worker.contracts import validate_shanghai_timestamp
from m3_worker.domain.forecasting import forecast_one_safe
from m3_worker.errors import M3Error
from m3_worker.scheduler.runner import SchedulerRunner
from m3_worker.services.acceptance_service import AcceptanceService
from m3_worker.services.acceptance_run_service import AcceptanceRunService
from m3_worker.services.custom_forecast_repository import CustomForecastRepository
from m3_worker.services.custom_forecast_evaluation_service import (
    CustomForecastEvaluationService,
)
from m3_worker.services.custom_forecast_service import CustomForecastService
from m3_worker.services.forecast_service import (
    ForecastService,
    verify_statsforecast_runtime,
)
from m3_worker.services.job_service import JobService
from m3_worker.services.station_cache import StationCache
from m3_worker.sinks.forecast_sink import ForecastSink


LOGGER = logging.getLogger("m3_worker")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SAFE_ALERT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _safe_code(error: BaseException) -> str:
    code = error.code if isinstance(error, M3Error) else "internal_error"
    if (
        not isinstance(code, str)
        or not 1 <= len(code) <= 64
        or not code[0].islower()
        or any(
            not (character.islower() or character.isdigit() or character == "_")
            for character in code
        )
    ):
        return "internal_error"
    return code


def _log_alert(station_id: str, task: str, code: str, at: datetime) -> None:
    safe_station = (
        station_id
        if isinstance(station_id, str)
        and STATION_ID_PATTERN.fullmatch(station_id) is not None
        else "unknown"
    )
    safe_task = (
        task
        if isinstance(task, str) and SAFE_ALERT_NAME.fullmatch(task) is not None
        else "scheduler_alert"
    )
    safe_code = (
        code
        if isinstance(code, str) and SAFE_ALERT_NAME.fullmatch(code) is not None
        else "internal_error"
    )
    try:
        validate_shanghai_timestamp(at, "alert time", quarter_hour=False)
        safe_at = at
    except (TypeError, ValueError):
        safe_at = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
    LOGGER.warning(
        "m3_task_alert station_id=%s task=%s error_code=%s at=%s",
        safe_station,
        safe_task,
        safe_code,
        safe_at.isoformat(),
    )


@dataclass
class WorkerResources:
    settings: Settings
    source_http: object
    nocobase_http: object
    forecast_service: object
    acceptance_service: object
    custom_forecasts: object
    custom_evaluations: object
    scheduler: object
    jobs: object
    clock: Callable[[], datetime]
    alert_sink: Callable[[str, str, str, datetime], None]
    statsforecast_version: str
    alert_client: object | None = None
    recovery_ready: bool = False
    _startup_statsforecast_version: str = field(init=False, repr=False)
    _close_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._startup_statsforecast_version = self.statsforecast_version

    def _alert(self, station_id: str, task: str, error: BaseException, at: datetime) -> None:
        try:
            self.alert_sink(station_id, task, _safe_code(error), at)
        except Exception:
            pass

    def recover(self) -> bool:
        """Recover forecast and acceptance state independently for every station."""

        raw_now = self.clock()
        try:
            validate_shanghai_timestamp(raw_now, "recovery time", quarter_hour=False)
        except (TypeError, ValueError) as error:
            safe_at = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
            for station_id in self.settings.station_ids:
                self._alert(station_id, "startup_recovery", error, safe_at)
            self.recovery_ready = False
            return False
        recovery_at = raw_now.replace(
            minute=(raw_now.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        failed = False
        try:
            if (
                self.statsforecast_version != self._startup_statsforecast_version
                or verify_statsforecast_runtime()
                != self._startup_statsforecast_version
            ):
                raise M3Error(
                    "model_version_invalid", "StatsForecast runtime version changed"
                )
        except Exception as error:
            failed = True
            for station_id in self.settings.station_ids:
                self._alert(station_id, "startup_recovery", error, recovery_at)
        else:
            for station_id in self.settings.station_ids:
                try:
                    self.forecast_service.bootstrap(station_id, recovery_at)
                    self.forecast_service.select_models(station_id)
                except Exception as error:
                    failed = True
                    self._alert(station_id, "startup_recovery", error, recovery_at)
        if getattr(self.settings, "acceptance_enabled", True):
            for station_id in self.settings.station_ids:
                try:
                    self.acceptance_service.reconcile_writing_batches(station_id)
                    self.acceptance_service.reconcile_run_summary(station_id)
                except Exception as error:
                    failed = True
                    self._alert(
                        station_id, "acceptance_reconcile", error, recovery_at
                    )
        try:
            self.custom_forecasts.recover()
        except Exception as error:
            failed = True
            for station_id in self.settings.station_ids:
                self._alert(
                    station_id, "custom_forecast_recovery", error, recovery_at
                )
        if not failed:
            try:
                failed = any(
                    self.forecast_service.state(station_id).state
                    not in {"ready", "degraded"}
                    for station_id in self.settings.station_ids
                )
            except Exception:
                failed = True
        self.recovery_ready = not failed
        return self.recovery_ready

    def current_ready(self) -> bool:
        """Recheck version metadata and every station state for current health."""

        if not self.recovery_ready:
            return False
        try:
            if (
                self.statsforecast_version != self._startup_statsforecast_version
                or verify_statsforecast_runtime()
                != self._startup_statsforecast_version
            ):
                return False
            return all(
                self.forecast_service.state(station_id).state
                in {"ready", "degraded"}
                for station_id in self.settings.station_ids
            )
        except Exception:
            return False

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        first_error = None
        for resource in (
            self.jobs,
            self.custom_forecasts,
            self.source_http,
            self.nocobase_http,
        ):
            try:
                resource.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def build_resources(
    settings: Settings,
    *,
    clock: Callable[[], datetime] | None = None,
    alert_sink: Callable[[str, str, str, datetime], None] | None = None,
) -> WorkerResources:
    """Assemble fixed-target Source and NocoBase clients and owned services."""

    clock = clock or (lambda: datetime.now(SHANGHAI))
    supplied_alert_sink = alert_sink
    timeout = httpx.Timeout(connect=2.0, read=15.0, write=15.0, pool=2.0)
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
    source_http = None
    nocobase_http = None
    jobs = None
    custom_forecasts = None
    try:
        source_http = httpx.Client(
            timeout=timeout, limits=limits, follow_redirects=False
        )
        nocobase_http = httpx.Client(
            timeout=timeout, limits=limits, follow_redirects=False
        )
        retry = RetryPolicy()
        observation_source = RawEnergySourceClient(
            str(settings.raw_source_url),
            settings.raw_source_api_token.get_secret_value(),
            source_http,
            allowed_station_ids=settings.station_ids,
        )
        alert_client = NodeRedAlertClient(
            str(settings.source_base_url),
            settings.source_api_token.get_secret_value(),
            source_http,
            retry,
        )

        def http_alert_sink(
            station_id: str, task: str, code: str, at: datetime
        ) -> None:
            try:
                alert_client.send(station_id, task, code, at)
            except Exception:
                _log_alert(station_id, task, code, at)

        effective_alert_sink = supplied_alert_sink or http_alert_sink
        api = NocoBaseApiClient(
            str(settings.nocobase_base_url),
            settings.nocobase_api_key.get_secret_value(),
            nocobase_http,
            retry,
        )
        run_service = AcceptanceRunService(api)
        custom_repository = CustomForecastRepository(api)
        sink = ForecastSink(api)
        caches = {
            station_id: StationCache(station_id)
            for station_id in settings.station_ids
        }
        locked_version = verify_statsforecast_runtime()
        forecast_service = ForecastService(
            observation_source, sink, caches, forecast_one_safe, clock
        )
        acceptance_service = AcceptanceService(
            run_service=run_service,
            observation_source=observation_source,
            api=api,
            sink=sink,
            forecast_service=forecast_service,
            now=clock,
        )
        custom_forecasts = CustomForecastService(
            custom_repository,
            observation_source,
            clock,
            station_ids=settings.station_ids,
            max_workers=1,
            max_pending=16,
        )
        custom_evaluations = CustomForecastEvaluationService(
            custom_repository,
            observation_source,
            clock,
        )
        scheduler = SchedulerRunner(
            settings.station_ids,
            forecast_service,
            acceptance_service,
            alert_sink=effective_alert_sink,
            clock=clock,
            acceptance_enabled=settings.acceptance_enabled,
            custom_evaluation_service=custom_evaluations,
        )
        jobs = JobService(
            scheduler.run_manual,
            clock,
            station_ids=settings.station_ids,
            max_workers=2,
            max_pending=16,
            max_retained=256,
        )
        return WorkerResources(
            settings=settings,
            source_http=source_http,
            nocobase_http=nocobase_http,
            forecast_service=forecast_service,
            acceptance_service=acceptance_service,
            custom_forecasts=custom_forecasts,
            custom_evaluations=custom_evaluations,
            scheduler=scheduler,
            jobs=jobs,
            clock=clock,
            alert_sink=effective_alert_sink,
            statsforecast_version=locked_version,
            alert_client=alert_client,
        )
    except Exception:
        for resource in (jobs, custom_forecasts, source_http, nocobase_http):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                LOGGER.warning("m3_resource_cleanup_failed error_code=resource_close_failed")
        raise


def create_app(
    settings: Settings | None = None,
    resources: WorkerResources | object | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    """Create an app whose production settings/resources are built lazily in lifespan."""

    if resources is not None and settings is None:
        raise ValueError("settings are required when resources are supplied")
    lifecycle_lock = Lock()
    lifecycle_used = False

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal lifecycle_used
        with lifecycle_lock:
            if lifecycle_used:
                raise RuntimeError("application lifespan already used")
            lifecycle_used = True
        owned_resources = resources
        owned_settings = settings
        if owned_settings is None:
            owned_settings = Settings.from_env()
        if owned_resources is None:
            owned_resources = build_resources(owned_settings)
        application.state.settings = owned_settings
        application.state.resources = owned_resources
        application.state.clock = getattr(
            owned_resources,
            "clock",
            lambda: datetime.now(SHANGHAI),
        )
        application.state.scheduler_enabled = start_scheduler
        application.state.recovery_attempted = False
        application.state.recovery_ready = False
        primary_error: BaseException | None = None
        try:
            if start_scheduler:
                application.state.recovery_attempted = True
                recovered = await asyncio.to_thread(owned_resources.recover)
                application.state.recovery_ready = bool(recovered)
                owned_resources.scheduler.start()
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            stop_error: BaseException | None = None
            close_error: BaseException | None = None
            if start_scheduler:
                try:
                    owned_resources.scheduler.stop()
                except BaseException as error:
                    stop_error = error
            try:
                scheduler_live = bool(
                    start_scheduler
                    and getattr(owned_resources.scheduler, "running", False)
                )
            except BaseException as error:
                scheduler_live = True
                if stop_error is None:
                    stop_error = error
            if not scheduler_live:
                try:
                    owned_resources.close()
                except BaseException as error:
                    close_error = error
            if primary_error is None:
                if stop_error is not None:
                    if isinstance(stop_error, M3Error):
                        raise stop_error
                    raise M3Error(
                        "scheduler_stop_failed", "Scheduler shutdown failed"
                    ) from stop_error
                if scheduler_live:
                    raise M3Error(
                        "scheduler_stop_timeout", "Scheduler remains active"
                    )
                if close_error is not None:
                    if isinstance(close_error, M3Error):
                        raise close_error
                    raise M3Error(
                        "resource_close_failed", "Worker resource shutdown failed"
                    ) from close_error
            elif stop_error is not None or close_error is not None:
                LOGGER.warning(
                    "m3_lifespan_cleanup_failed error_code=resource_close_failed"
                )

    application = FastAPI(
        title="VIFA M3 Forecast Worker",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"code": "request_invalid"})

    @application.exception_handler(HTTPException)
    async def safe_http_error(_request: Request, error: HTTPException) -> JSONResponse:
        codes = {
            401: "unauthorized",
            404: "not_found",
            409: "conflict",
            422: "request_invalid",
            429: "job_capacity_exceeded",
            503: "job_service_unavailable",
        }
        return JSONResponse(
            status_code=error.status_code,
            content={"code": codes.get(error.status_code, "internal_error")},
            headers=error.headers,
        )

    application.include_router(health_router)
    application.include_router(router)
    return application


# Uvicorn imports this object without requiring production environment at import time.
app = create_app()
