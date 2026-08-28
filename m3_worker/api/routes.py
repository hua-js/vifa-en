"""The five approved M3 Worker operations endpoints."""

from datetime import timedelta
import math
import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import ValidationError

from m3_worker.api.dependencies import StationDep, require_admin
from m3_worker.api.models import (
    ChampionResponse,
    HealthResponse,
    JobResponse,
    ManualRunRequest,
    StationStateResponse,
)
from m3_worker.contracts import SERIES_IDS
from m3_worker.errors import M3Error


MODEL_NAMES = frozenset({"SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"})
SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
SAFE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?\Z")

health_router = APIRouter(tags=["health"])
router = APIRouter(
    prefix="/v1",
    tags=["m3-operations"],
    dependencies=[Depends(require_admin)],
)


def _safe_code(value: object) -> str | None:
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and SAFE_CODE.fullmatch(value)
        else "internal_error"
    )


def _job_response(request: Request, job) -> JobResponse:
    try:
        if job.station_id not in request.app.state.settings.station_ids:
            raise ValueError("job station")
        return JobResponse(
            job_id=job.job_id,
            station_id=job.station_id,
            task=job.task,
            status=job.status,
            error_code=_safe_code(job.error_code),
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@health_router.get("/health")
def health(request: Request) -> HealthResponse:
    enabled = request.app.state.scheduler_enabled
    attempted = request.app.state.recovery_attempted
    recovered = request.app.state.recovery_ready
    resources = request.app.state.resources
    try:
        scheduler_running = bool(
            getattr(resources.scheduler, "running", False)
        )
        scheduler_healthy = bool(
            getattr(resources.scheduler, "healthy", True)
        )
        scheduler_state = getattr(
            resources.scheduler, "lifecycle_state", "stopped"
        )
    except Exception:
        scheduler_running = False
        scheduler_healthy = False
        scheduler_state = "stopped"
    raw_version = getattr(resources, "statsforecast_version", None)
    version = (
        raw_version
        if isinstance(raw_version, str) and SAFE_VERSION.fullmatch(raw_version)
        else None
    )
    current_ready = False
    if attempted and recovered:
        try:
            readiness_check = getattr(resources, "current_ready", None)
            current_ready = (
                bool(readiness_check())
                if callable(readiness_check)
                else recovered
            )
        except Exception:
            current_ready = False
    if not attempted:
        dependencies = "initializing"
    elif current_ready and version is not None:
        dependencies = "ready"
    else:
        dependencies = "failed"
    if not enabled:
        scheduler = "disabled"
    elif scheduler_running and scheduler_state == "stopping":
        scheduler = "stopping"
    elif (
        scheduler_running
        and scheduler_healthy
        and scheduler_state == "running"
    ):
        scheduler = "running"
    elif scheduler_running:
        scheduler = "unhealthy"
    else:
        scheduler = "stopped"
    ready = (
        enabled
        and current_ready
        and version is not None
        and scheduler_running
        and scheduler_healthy
        and scheduler_state == "running"
    )
    return HealthResponse(
        status="ok" if ready else "initializing",
        scheduler=scheduler,
        dependencies=dependencies,
        statsforecast_version=version,
    )


@router.get("/stations/{station_id}/state")
def station_state(request: Request, station_id: StationDep) -> StationStateResponse:
    try:
        state = request.app.state.resources.forecast_service.state(station_id)
        champion_responses = {}
        champions = getattr(state, "champions", {})
        if not isinstance(champions, dict):
            raise ValueError("champions")
        for unique_id in SERIES_IDS:
            champion = champions.get(unique_id)
            if champion is None:
                champion_responses[unique_id] = None
                continue
            if champion.model_name not in MODEL_NAMES:
                raise ValueError("champion model")
            cv_mape = champion.cv_mape_percent
            if cv_mape is not None and (
                isinstance(cv_mape, bool)
                or not isinstance(cv_mape, (int, float))
                or not math.isfinite(cv_mape)
            ):
                raise ValueError("champion score")
            version = champion.statsforecast_version
            if not isinstance(version, str) or SAFE_VERSION.fullmatch(version) is None:
                raise ValueError("champion version")
            champion_responses[unique_id] = ChampionResponse(
                model_name=champion.model_name,
                cv_mape_percent=cv_mape,
                selected_at=champion.selected_at,
                training_start=champion.training_start,
                training_end=champion.training_end,
                statsforecast_version=version,
                selection_reason=_safe_code(champion.selection_reason),
            )
        last_published_at = getattr(state, "last_published_at", None)
        stale = False
        if last_published_at is not None:
            stale = request.app.state.clock() - last_published_at > timedelta(minutes=30)
        public_state = "stale" if stale else getattr(state, "state")
        return StationStateResponse(
            state=public_state,
            champions=champion_responses,
            last_source_at=getattr(state, "last_source_at", None),
            last_published_at=last_published_at,
            last_error_code=_safe_code(getattr(state, "last_error_code", None)),
            stale=stale,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


def _submit(request: Request, station_id: str, task: str) -> JobResponse:
    try:
        return _job_response(
            request, request.app.state.resources.jobs.submit(station_id, task)
        )
    except M3Error as error:
        status = 429 if error.code == "job_capacity_exceeded" else 503
        code = (
            error.code
            if error.code in {"job_capacity_exceeded", "job_service_closed"}
            else "internal_error"
        )
        raise HTTPException(status_code=status, detail=code) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@router.post("/stations/{station_id}/runs/forecast", status_code=202)
def run_forecast(
    request: Request,
    station_id: StationDep,
    _payload: Annotated[ManualRunRequest, Body()],
) -> JobResponse:
    return _submit(request, station_id, "forecast")


@router.post("/stations/{station_id}/runs/model-selection", status_code=202)
def run_model_selection(
    request: Request,
    station_id: StationDep,
    _payload: Annotated[ManualRunRequest, Body()],
) -> JobResponse:
    return _submit(request, station_id, "model_selection")


@router.get("/jobs/{job_id}")
def job_state(
    request: Request,
    job_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> JobResponse:
    job = request.app.state.resources.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(request, job)
