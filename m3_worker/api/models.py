"""Explicit public response models for the M3 operations API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from m3_worker.contracts import SeriesId


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualRunRequest(ApiModel):
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
