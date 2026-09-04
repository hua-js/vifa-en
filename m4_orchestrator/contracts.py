from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from m4_optimizer.contracts import OptimizationRequest, OptimizationResult


OrchestrationStatus = Literal["completed", "partial_failure", "failed"]
StationStatus = Literal[
    "optimized",
    "no_usable_candidate",
    "input_error",
    "optimization_error",
]
ErrorCode = Literal[
    "INPUT_NOT_FOUND",
    "INPUT_READ_ERROR",
    "INPUT_ENCODING_ERROR",
    "INPUT_JSON_ERROR",
    "INPUT_VALIDATION_ERROR",
    "DUPLICATE_STATION_ID",
    "OPTIMIZATION_ERROR",
    "NO_USABLE_CANDIDATE",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrchestrationError(StrictModel):
    code: ErrorCode
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_message(self) -> "OrchestrationError":
        if not self.message.strip():
            raise ValueError("error message must be non-blank")
        return self


class StationInput(StrictModel):
    input_ref: str = Field(min_length=1)
    station_id_hint: str | None = None
    request_id_hint: str | None = None
    request: OptimizationRequest | None = None
    error: OrchestrationError | None = None

    @model_validator(mode="after")
    def validate_payload_state(self) -> "StationInput":
        if (self.request is None) == (self.error is None):
            raise ValueError("station input requires exactly one request or error")
        if any(character in self.input_ref for character in ("/", "\\", "\r", "\n")):
            raise ValueError("input_ref must be a safe label, not a path")
        return self


class InputSummary(StrictModel):
    plan_start_at: datetime
    input_observed_at: datetime
    source_versions: dict[str, str]
    horizon_points: int
    initial_soc_pct: float
    energy_capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    demand_limit_kw: float
    grid_import_limit_kw: float | None
    grid_export_enabled: bool
    grid_export_limit_kw: float


class StationOrchestrationResult(StrictModel):
    input_ref: str = Field(min_length=1)
    station_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    status: StationStatus
    input_summary: InputSummary | None = None
    optimization_result: OptimizationResult | None = None
    error: OrchestrationError | None = None
    selection_status: Literal["pending_ai"] = "pending_ai"
    selected_candidate_id: None = None
    dispatch_status: Literal["not_dispatched"] = "not_dispatched"
    ems_task_id: None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> "StationOrchestrationResult":
        if self.status == "optimized":
            if (
                self.input_summary is None
                or self.optimization_result is None
                or self.error is not None
            ):
                raise ValueError(
                    "optimized station requires input summary and optimization result without error"
                )
        elif self.status == "no_usable_candidate":
            if (
                self.input_summary is None
                or self.optimization_result is None
                or self.error is None
                or self.error.code != "NO_USABLE_CANDIDATE"
            ):
                raise ValueError(
                    "no_usable_candidate station requires summary, result and NO_USABLE_CANDIDATE error"
                )
        elif self.status == "input_error":
            if (
                self.input_summary is not None
                or self.optimization_result is not None
                or self.error is None
            ):
                raise ValueError(
                    "input_error station requires an error without summary or optimization result"
                )
        elif (
            self.input_summary is None
            or self.optimization_result is not None
            or self.error is None
            or self.error.code != "OPTIMIZATION_ERROR"
        ):
            raise ValueError(
                "optimization_error station requires summary, no result and OPTIMIZATION_ERROR error"
            )
        return self


class M4OrchestrationResult(StrictModel):
    schema_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    orchestrator_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    status: OrchestrationStatus
    station_results: list[StationOrchestrationResult] = Field(min_length=1)
    errors: list[OrchestrationError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_run_state(self) -> "M4OrchestrationResult":
        for label, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must have a valid UTC offset")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")

        optimized_count = sum(
            result.status == "optimized" for result in self.station_results
        )
        if self.errors or optimized_count == 0:
            expected_status: OrchestrationStatus = "failed"
        elif optimized_count == len(self.station_results):
            expected_status = "completed"
        else:
            expected_status = "partial_failure"
        if self.errors and expected_status != "failed":
            raise ValueError("top-level errors are only allowed with failed status")
        self.status = expected_status
        return self
