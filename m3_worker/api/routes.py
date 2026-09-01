"""The approved M3 Worker operations endpoints."""

from datetime import timedelta
import math
import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from pydantic import ValidationError

from m3_worker.api.dependencies import StationDep, require_admin
from m3_worker.api.models import (
    ChampionResponse,
    CustomPerformanceResponse,
    CustomPerformanceSeriesResponse,
    CustomResultPointResponse,
    CustomResultSeriesResponse,
    CustomRunRequest,
    CustomRunResponse,
    CustomRunResultResponse,
    HealthResponse,
    JobResponse,
    ManualRunRequest,
    StationStateResponse,
)
from m3_worker.contracts import SERIES_IDS
from m3_worker.custom_forecast_contracts import ALLOWED_INTERVAL_SECONDS, RUN_ID_PATTERN
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


def _custom_run_response(request: Request, run) -> CustomRunResponse:
    try:
        if run.station_id not in request.app.state.settings.station_ids:
            raise ValueError("custom run station")
        config = run.config
        return CustomRunResponse(
            run_id=run.run_id,
            station_id=run.station_id,
            status=run.status,
            history_start=config.history_start,
            history_end=config.history_end,
            history_days=config.history_days,
            forecast_start=config.forecast_start,
            forecast_end=config.forecast_end,
            forecast_days=config.forecast_days,
            interval_seconds=config.interval_seconds,
            points_per_day=config.points_per_day,
            expected_points_per_series=config.expected_points_per_series,
            model_policy=config.model_policy,
            model_manifest=run.model_manifest,
            source_manifest=run.source_manifest,
            error_code=_safe_code(run.record.error_code),
            started_at=run.started_at,
            completed_at=run.completed_at,
            evaluated_at=run.evaluated_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


def _custom_service_error(error: M3Error) -> HTTPException:
    if error.code == "request_invalid":
        return HTTPException(status_code=422, detail="request_invalid")
    if error.code == "idempotency_conflict":
        return HTTPException(status_code=409, detail="idempotency_conflict")
    if error.code == "job_capacity_exceeded":
        return HTTPException(status_code=429, detail="job_capacity_exceeded")
    if error.code == "job_service_closed":
        return HTTPException(status_code=503, detail="job_service_closed")
    return HTTPException(status_code=503, detail="custom_forecast_unavailable")


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


@router.post(
    "/stations/{station_id}/runs/custom-forecast",
    status_code=202,
)
def run_custom_forecast(
    request: Request,
    station_id: StationDep,
    payload: Annotated[CustomRunRequest, Body()],
) -> CustomRunResponse:
    try:
        run = request.app.state.resources.custom_forecasts.submit(
            station_id,
            payload,
            requested_by="m3_operations_api",
        )
        return _custom_run_response(request, run)
    except M3Error as error:
        raise _custom_service_error(error) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@router.get("/custom-forecast-runs/{run_id}")
def custom_forecast_state(
    request: Request,
    run_id: Annotated[str, Path(min_length=1, max_length=128, pattern=RUN_ID_PATTERN)],
) -> CustomRunResponse:
    try:
        run = request.app.state.resources.custom_forecasts.get(run_id)
        if run is None or run.station_id not in request.app.state.settings.station_ids:
            raise HTTPException(status_code=404, detail="not_found")
        return _custom_run_response(request, run)
    except M3Error as error:
        raise _custom_service_error(error) from error


@router.get("/stations/{station_id}/custom-forecast-runs/latest")
def latest_custom_forecast(
    request: Request,
    station_id: StationDep,
    interval_seconds: Annotated[int, Query()],
    forecast_days: Annotated[int, Query(ge=1, le=7)],
) -> CustomRunResponse:
    if interval_seconds not in ALLOWED_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="request_invalid")
    try:
        run = request.app.state.resources.custom_forecasts.latest(
            station_id,
            interval_seconds=interval_seconds,
            forecast_days=forecast_days,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="not_found")
        return _custom_run_response(request, run)
    except HTTPException:
        raise
    except M3Error as error:
        raise _custom_service_error(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@router.get("/custom-forecast-runs/{run_id}/result")
def custom_forecast_result(
    request: Request,
    run_id: Annotated[str, Path(min_length=1, max_length=128, pattern=RUN_ID_PATTERN)],
) -> CustomRunResultResponse:
    try:
        result = request.app.state.resources.custom_forecasts.result(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        run, points = result
        if run.station_id not in request.app.state.settings.station_ids:
            raise HTTPException(status_code=404, detail="not_found")
        if points:
            points = request.app.state.resources.custom_evaluations.overlay_actuals(
                run, points
            )
        grouped: dict[str, list[dict]] = {unique_id: [] for unique_id in SERIES_IDS}
        for point in points:
            unique_id = point.get("unique_id")
            if unique_id not in grouped:
                raise ValueError("custom point series")
            grouped[unique_id].append(point)
        series = []
        for unique_id in SERIES_IDS:
            rows = grouped[unique_id]
            model_names = {row.get("model_name") for row in rows}
            if rows and (
                len(rows) != run.config.expected_points_per_series
                or len(model_names) != 1
                or not all(isinstance(name, str) and name for name in model_names)
            ):
                raise ValueError("custom result series")
            series.append(
                CustomResultSeriesResponse(
                    unique_id=unique_id,
                    unit="kW" if unique_id == "station_total_load" else "%",
                    model_name=next(iter(model_names)) if model_names else "none",
                    points=[
                        CustomResultPointResponse(
                            target_time=row["target_time"],
                            horizon_step=row["horizon_step"],
                            forecast_value=row["forecast_value"],
                            actual_value=row["actual_value"],
                            actual_quality=row["actual_quality"],
                            absolute_percentage_error=row[
                                "absolute_percentage_error"
                            ],
                        )
                        for row in rows
                    ],
                )
            )
        return CustomRunResultResponse(
            run=_custom_run_response(request, run),
            series=series,
        )
    except HTTPException:
        raise
    except M3Error as error:
        raise _custom_service_error(error) from error
    except (ValidationError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@router.get("/stations/{station_id}/custom-forecast-performance")
def custom_forecast_performance(
    request: Request,
    station_id: StationDep,
    interval_seconds: Annotated[int, Query()],
    forecast_days: Annotated[int, Query(ge=1, le=7)],
    history_days: Annotated[int, Query(ge=7, le=90)],
) -> CustomPerformanceResponse:
    if interval_seconds not in ALLOWED_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="request_invalid")
    try:
        value = request.app.state.resources.custom_evaluations.performance(
            station_id=station_id,
            interval_seconds=interval_seconds,
            forecast_days=forecast_days,
            history_days=history_days,
        )
        return CustomPerformanceResponse(
            station_id=value["station_id"],
            lookback_days=7,
            interval_seconds=value["interval_seconds"],
            forecast_days=value["forecast_days"],
            model_policy=value["model_policy"],
            calculated_at=value["calculated_at"],
            series=[
                CustomPerformanceSeriesResponse.model_validate(item)
                for item in value["series"]
            ],
        )
    except M3Error as error:
        raise _custom_service_error(error) from error
    except (ValidationError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=500, detail="internal_error") from error


@router.get("/jobs/{job_id}")
def job_state(
    request: Request,
    job_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> JobResponse:
    job = request.app.state.resources.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(request, job)
