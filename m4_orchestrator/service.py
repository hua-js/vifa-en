"""Offline orchestration for independent station optimization runs."""

from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence
from uuid import uuid4

from m4_optimizer import M4Optimizer
from m4_optimizer.contracts import OptimizationRequest, OptimizationResult

from .contracts import (
    InputSummary,
    M4OrchestrationResult,
    OrchestrationError,
    StationInput,
    StationOrchestrationResult,
)


SCHEMA_VERSION = "m4-orchestration-v2"


class Optimizer(Protocol):
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        raise NotImplementedError


class M4Orchestrator:
    """Run one isolated optimizer call for each valid station input."""

    def __init__(
        self,
        *,
        model_version: str,
        orchestrator_version: str,
        optimizer: Optimizer | None = None,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(model_version, str) or not model_version.strip():
            raise ValueError("model_version must be non-blank")
        if (
            not isinstance(orchestrator_version, str)
            or not orchestrator_version.strip()
        ):
            raise ValueError("orchestrator_version must be non-blank")

        self.model_version = model_version
        self.orchestrator_version = orchestrator_version
        self.optimizer = (
            optimizer
            if optimizer is not None
            else M4Optimizer(model_version=model_version)
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run_id_factory = run_id_factory or (lambda: str(uuid4()))

    def run(
        self, station_inputs: Sequence[StationInput]
    ) -> M4OrchestrationResult:
        if not station_inputs:
            raise ValueError("station_inputs must not be empty")

        started_at = self.clock()
        top_level_errors: list[OrchestrationError] = []
        if self._has_duplicate_valid_station_ids(station_inputs):
            duplicate_error = OrchestrationError(
                code="DUPLICATE_STATION_ID",
                message="duplicate station IDs prevent optimization",
            )
            top_level_errors.append(duplicate_error)
            station_results = [
                self._input_error_result(station_input)
                if station_input.error is not None
                else self._duplicate_result(station_input, duplicate_error)
                for station_input in station_inputs
            ]
        else:
            station_results = [
                self._input_error_result(station_input)
                if station_input.error is not None
                else self._optimize_station(station_input)
                for station_input in station_inputs
            ]

        finished_at = self.clock()
        successful_results = sorted(
            (
                item
                for item in station_results
                if item.status == "optimized"
            ),
            key=lambda item: (item.station_id or "", item.input_ref),
        )
        error_results = sorted(
            (
                item
                for item in station_results
                if item.status != "optimized"
            ),
            key=lambda item: item.input_ref,
        )
        station_results = [*successful_results, *error_results]
        return M4OrchestrationResult(
            schema_version=SCHEMA_VERSION,
            run_id=self.run_id_factory(),
            started_at=started_at,
            finished_at=finished_at,
            orchestrator_version=self.orchestrator_version,
            model_version=self.model_version,
            overall_status="failed" if top_level_errors else "completed",
            stations=station_results,
            errors=top_level_errors,
        )

    @staticmethod
    def _has_duplicate_valid_station_ids(
        station_inputs: Sequence[StationInput],
    ) -> bool:
        station_ids = [
            station_input.request.station_id
            for station_input in station_inputs
            if station_input.request is not None
        ]
        return len(station_ids) != len(set(station_ids))

    @staticmethod
    def _input_error_result(
        station_input: StationInput,
    ) -> StationOrchestrationResult:
        if station_input.error is None:
            raise ValueError("input error result requires an input error")
        return StationOrchestrationResult(
            input_ref=station_input.input_ref,
            station_id=station_input.station_id_hint,
            request_id=station_input.request_id_hint,
            status="input_error",
            error=station_input.error,
        )

    @staticmethod
    def _duplicate_result(
        station_input: StationInput,
        duplicate_error: OrchestrationError,
    ) -> StationOrchestrationResult:
        if station_input.request is None:
            raise ValueError("duplicate result requires a valid request")
        return StationOrchestrationResult(
            input_ref=station_input.input_ref,
            station_id=station_input.request.station_id,
            request_id=station_input.request.request_id,
            status="input_error",
            error=duplicate_error,
        )

    def _optimize_station(
        self,
        station_input: StationInput,
    ) -> StationOrchestrationResult:
        if station_input.request is None:
            raise ValueError("optimization requires a valid request")
        request = station_input.request
        summary = self._input_summary(request)

        try:
            optimization_result = self.optimizer.optimize(request)
            if (
                optimization_result.station_id != request.station_id
                or optimization_result.request_id != request.request_id
            ):
                raise ValueError("optimizer result identity mismatch")

            if any(
                candidate.status in {"optimal", "feasible"}
                for candidate in optimization_result.candidates
            ):
                return StationOrchestrationResult(
                    input_ref=station_input.input_ref,
                    station_id=request.station_id,
                    request_id=request.request_id,
                    status="optimized",
                    input_summary=summary,
                    optimization_result=optimization_result,
                )

            return StationOrchestrationResult(
                input_ref=station_input.input_ref,
                station_id=request.station_id,
                request_id=request.request_id,
                status="no_usable_candidate",
                input_summary=summary,
                optimization_result=optimization_result,
                error=OrchestrationError(
                    code="NO_USABLE_CANDIDATE",
                    message="optimizer returned no usable candidate",
                ),
            )
        except Exception:
            return StationOrchestrationResult(
                input_ref=station_input.input_ref,
                station_id=request.station_id,
                request_id=request.request_id,
                status="optimization_error",
                input_summary=summary,
                error=OrchestrationError(
                    code="OPTIMIZATION_ERROR",
                    message="station optimization failed",
                ),
            )

    @staticmethod
    def _input_summary(request: OptimizationRequest) -> InputSummary:
        return InputSummary(
            plan_start_at=request.plan_start_at,
            input_observed_at=request.input_observed_at,
            source_versions=request.source_versions,
            horizon_points=request.horizon_points,
            initial_soc_pct=request.capability.initial_soc_pct,
            energy_capacity_kwh=request.capability.energy_capacity_kwh,
            max_charge_kw=request.capability.max_charge_kw,
            max_discharge_kw=request.capability.max_discharge_kw,
            demand_limit_kw=request.constraints.demand_limit_kw,
            grid_import_limit_kw=request.constraints.grid_import_limit_kw,
            grid_export_enabled=request.constraints.grid_export_enabled,
            grid_export_limit_kw=request.constraints.grid_export_limit_kw,
        )
