import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from m4_orchestrator import (
    M4OrchestrationResult,
    InputSummary,
    OrchestrationError,
    StationInput,
    StationOrchestrationResult,
)
from tests.m4_orchestrator_test_support import (
    FIXED_FINISHED_AT,
    FIXED_STARTED_AT,
    UTC,
    make_optimization_result,
    make_request,
)


def make_input_summary() -> InputSummary:
    request = make_request()
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


def make_station_result(**overrides: object) -> StationOrchestrationResult:
    request = make_request()
    values: dict[str, object] = {
        "input_ref": "station-1.json",
        "station_id": request.station_id,
        "request_id": request.request_id,
        "status": "optimized",
        "input_summary": make_input_summary(),
        "optimization_result": make_optimization_result(),
        "error": None,
    }
    values.update(overrides)
    return StationOrchestrationResult(**values)


def make_orchestration_result(**overrides: object) -> M4OrchestrationResult:
    values: dict[str, object] = {
        "schema_version": "m4-orchestration-v1",
        "run_id": "run-test-001",
        "started_at": FIXED_STARTED_AT,
        "finished_at": FIXED_FINISHED_AT,
        "orchestrator_version": "test-orchestrator-v1",
        "model_version": "test-model-v1",
        "status": "completed",
        "station_results": [make_station_result()],
        "errors": [],
    }
    values.update(overrides)
    return M4OrchestrationResult(**values)


class M4OrchestratorContractTests(unittest.TestCase):
    def test_station_input_requires_exactly_one_request_or_error(self):
        request = make_request()
        with self.assertRaises(ValidationError):
            StationInput(input_ref="station-1.json")
        with self.assertRaises(ValidationError):
            StationInput(
                input_ref="station-1.json",
                request=request,
                error=OrchestrationError(
                    code="INPUT_JSON_ERROR",
                    message="input is not valid JSON",
                ),
            )

    def test_station_input_rejects_paths_and_extra_fields(self):
        with self.assertRaisesRegex(ValidationError, "safe label"):
            StationInput(input_ref="inputs/station-1.json", request=make_request())
        with self.assertRaises(ValidationError):
            StationInput.model_validate(
                {
                    "input_ref": "station-1.json",
                    "request": make_request(),
                    "unexpected": True,
                }
            )

    def test_station_result_fixes_ai_and_ems_to_inactive_states(self):
        result = make_station_result(status="optimized")
        self.assertEqual(result.selection_status, "pending_ai")
        self.assertIsNone(result.selected_candidate_id)
        self.assertEqual(result.dispatch_status, "not_dispatched")
        self.assertIsNone(result.ems_task_id)
        with self.assertRaises(ValidationError):
            StationOrchestrationResult.model_validate(
                {**result.model_dump(), "selected_candidate_id": "cost"}
            )
        with self.assertRaises(ValidationError):
            StationOrchestrationResult.model_validate(
                {**result.model_dump(), "dispatch_status": "dispatched"}
            )

    def test_station_result_aligns_all_status_payloads(self):
        summary = make_input_summary()
        optimized = make_station_result()
        self.assertEqual(optimized.status, "optimized")

        no_candidate = make_station_result(
            status="no_usable_candidate",
            error=OrchestrationError(
                code="NO_USABLE_CANDIDATE", message="no usable candidate"
            ),
        )
        self.assertEqual(no_candidate.error.code, "NO_USABLE_CANDIDATE")

        input_error = make_station_result(
            status="input_error",
            input_summary=None,
            optimization_result=None,
            error=OrchestrationError(code="INPUT_JSON_ERROR", message="invalid input"),
        )
        self.assertEqual(input_error.input_summary, None)

        optimization_error = make_station_result(
            status="optimization_error",
            input_summary=summary,
            optimization_result=None,
            error=OrchestrationError(
                code="OPTIMIZATION_ERROR", message="optimizer failed"
            ),
        )
        self.assertEqual(optimization_error.optimization_result, None)

    def test_failed_station_requires_safe_error_and_no_input_summary(self):
        with self.assertRaises(ValidationError):
            StationOrchestrationResult(
                input_ref="station-2.json",
                station_id="station-2",
                request_id="request-2",
                status="input_error",
                input_summary=make_input_summary(),
                optimization_result=None,
                error=None,
            )

    def test_station_result_rejects_misaligned_error_codes(self):
        with self.assertRaisesRegex(ValidationError, "NO_USABLE_CANDIDATE"):
            make_station_result(
                status="no_usable_candidate",
                error=OrchestrationError(
                    code="OPTIMIZATION_ERROR", message="wrong error code"
                ),
            )
        with self.assertRaisesRegex(ValidationError, "OPTIMIZATION_ERROR"):
            make_station_result(
                status="optimization_error",
                optimization_result=None,
                error=OrchestrationError(
                    code="INPUT_JSON_ERROR", message="wrong error code"
                ),
            )

    def test_input_summary_has_only_documented_fields(self):
        summary = make_input_summary()
        self.assertEqual(
            set(summary.model_dump()),
            {
                "plan_start_at",
                "input_observed_at",
                "source_versions",
                "horizon_points",
                "initial_soc_pct",
                "energy_capacity_kwh",
                "max_charge_kw",
                "max_discharge_kw",
                "demand_limit_kw",
                "grid_import_limit_kw",
                "grid_export_enabled",
                "grid_export_limit_kw",
            },
        )
        with self.assertRaises(ValidationError):
            InputSummary.model_validate({**summary.model_dump(), "extra": "nope"})

    def test_orchestration_result_requires_aware_ordered_timestamps(self):
        with self.assertRaisesRegex(ValidationError, "UTC offset"):
            make_orchestration_result(started_at=datetime(2026, 9, 4, 0, 0))
        with self.assertRaisesRegex(ValidationError, "finished_at"):
            make_orchestration_result(
                started_at=FIXED_FINISHED_AT,
                finished_at=FIXED_STARTED_AT,
            )

    def test_orchestration_result_recomputes_status_and_restricts_top_level_errors(self):
        input_error = make_station_result(
            status="input_error",
            input_summary=None,
            optimization_result=None,
            error=OrchestrationError(code="INPUT_JSON_ERROR", message="invalid input"),
        )
        partial = make_orchestration_result(
            status="completed", station_results=[make_station_result(), input_error]
        )
        self.assertEqual(partial.status, "partial_failure")

        failed = make_orchestration_result(
            status="completed", station_results=[input_error]
        )
        self.assertEqual(failed.status, "failed")

        top_level_failure = make_orchestration_result(
            errors=[
                OrchestrationError(
                    code="DUPLICATE_STATION_ID", message="duplicate station"
                )
            ]
        )
        self.assertEqual(top_level_failure.status, "failed")

    def test_orchestration_result_requires_nonempty_metadata_and_station_results(self):
        for field in (
            "schema_version",
            "run_id",
            "orchestrator_version",
            "model_version",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    make_orchestration_result(**{field: ""})
        with self.assertRaises(ValidationError):
            make_orchestration_result(station_results=[])
        with self.assertRaises(ValidationError):
            make_orchestration_result(status="unknown")

    def test_orchestration_result_rejects_extra_fields_and_preserves_duplicate_identities(self):
        station = make_station_result()
        duplicated = station.model_copy(update={"input_ref": "station-1-copy.json"})
        result = make_orchestration_result(
            status="failed",
            station_results=[station, duplicated],
        )
        self.assertEqual(len(result.station_results), 2)
        with self.assertRaises(ValidationError):
            M4OrchestrationResult.model_validate(
                {**make_orchestration_result().model_dump(), "extra": True}
            )


if __name__ == "__main__":
    unittest.main()
