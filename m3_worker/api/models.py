"""Explicit public response models for the M3 operations API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from m3_worker.contracts import SeriesId
from m3_worker.custom_forecast_contracts import (
    CustomForecastRequest,
    ModelPolicy,
    RunStatus,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualRunRequest(ApiModel):
    pass


class CustomRunRequest(CustomForecastRequest):
    pass


class ChampionResponse(ApiModel):
    model_name: str
    cv_mape_percent: float | None
    selected_at: datetime
    training_start: datetime
    training_end: datetime
    statsforecast_version: str
    selection_reason: str | None = None


class StationStateResponse(ApiModel):
    state: Literal["initializing", "ready", "degraded", "stale"]
    champions: dict[SeriesId, ChampionResponse | None]
    last_source_at: datetime | None
    last_published_at: datetime | None
    last_error_code: str | None
    stale: bool


class JobResponse(ApiModel):
    job_id: str
    station_id: str
    task: Literal["forecast", "model_selection"]
    status: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None


class CustomRunResponse(ApiModel):
    run_id: str
    station_id: str
    status: RunStatus
    history_start: datetime
    history_end: datetime
    history_days: int
    forecast_start: datetime
    forecast_end: datetime
    forecast_days: int
    interval_seconds: int
    points_per_day: int
    expected_points_per_series: int
    model_policy: ModelPolicy
    model_manifest: dict[str, object] | None
    source_manifest: dict[str, object] | None
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CustomResultPointResponse(ApiModel):
    target_time: datetime
    horizon_step: int
    forecast_value: float
    actual_value: float | None
    actual_quality: Literal["valid", "invalid"] | None
    absolute_percentage_error: float | None


class CustomResultSeriesResponse(ApiModel):
    unique_id: SeriesId
    unit: Literal["kW", "%"]
    model_name: str
    points: list[CustomResultPointResponse]


class CustomRunResultResponse(ApiModel):
    run: CustomRunResponse
    series: list[CustomResultSeriesResponse]


class CustomPerformanceSeriesResponse(ApiModel):
    unique_id: SeriesId
    mape_percent: float | None
    baseline_mape_percent: float | None
    relative_baseline_improvement_percent: float | None
    scorable_point_count: int
    run_count: int


class CustomPerformanceResponse(ApiModel):
    station_id: str
    lookback_days: Literal[7]
    interval_seconds: int
    forecast_days: int
    model_policy: ModelPolicy
    calculated_at: datetime
    series: list[CustomPerformanceSeriesResponse]


class HealthResponse(ApiModel):
    status: Literal["ok", "initializing"]
    process: Literal["running"] = "running"
    scheduler: Literal[
        "running", "stopping", "unhealthy", "stopped", "disabled"
    ]
    dependencies: Literal["initializing", "ready", "failed"]
    statsforecast_version: str | None


class ErrorResponse(ApiModel):
    code: str
