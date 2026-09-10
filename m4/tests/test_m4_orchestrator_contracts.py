import json
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from m4.orchestrator import (
    M4OrchestrationResult,
    InputSummary,
    OrchestrationError,
    StationInput,
    StationOrchestrationResult,
)
from m4.tests.m4_orchestrator_test_support import (
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
        "optimization_result": make_optimization_result(
            candidate_statuses=("optimal",)
        ),
        "error": None,
    }
    values.update(overrides)
    return StationOrchestrationResult(**values)


def make_orchestration_result(**overrides: object) -> M4OrchestrationResult:
    values: dict[str, object] = {
        "schema_version": "m4-orchestration-v2",
        "run_id": "run-test-001",
        "started_at": FIXED_STARTED_AT,
        "finished_at": FIXED_FINISHED_AT,
        "orchestrator_version": "test-orchestrator-v1",
        "model_version": "test-model-v1",
        "overall_status": "completed",
        "stations": [make_station_result()],
        "errors": [],
    }
    values.update(overrides)
    return M4OrchestrationResult(**values)


class M4OrchestratorContractTests(unittest.TestCase):
    def test_schema_and_selection_status_accept_current_version(self):
        for schema, status in (
            ("m4-orchestration-v2", "pending_selection"),
        ):
            with self.subTest(schema=schema, status=status):
                result = make_orchestration_result(
                    schema_version=schema,
                    stations=[make_station_result(selection_status=status)],
                )
                parsed = M4OrchestrationResult.model_validate_json(result.model_dump_json())
                self.assertEqual(parsed.stations[0].selection_status, status)

    def test_schema_rejects_cross_version_and_unknown_states(self):
        baseline = make_orchestration_result().model_dump(mode="json")
        for schema, status in (
            ("m4-orchestration-v1", "pending_ai"),
            ("m4-orchestration-v1", "pending_selection"),
            ("m4-orchestration-v2", "pending_ai"),
            ("m4-orchestration-v3", "pending_selection"),
        ):
            with self.subTest(schema=schema, status=status):
                payload = json.loads(json.dumps(baseline))
                payload["schema_version"] = schema
                payload["stations"][0]["selection_status"] = status
                with self.assertRaises(ValidationError):
                    M4OrchestrationResult.model_validate_json(json.dumps(payload))

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

    def test_input_refs_share_safe_label_validation_on_construction(self):
        usable_result = make_optimization_result(
            candidate_statuses=("optimal",)
        )
        unsafe_labels = (
            "",
            "   ",
            "inputs/station.json",
            "inputs\\station.json",
            "station\r.json",
            "station\n.json",
        )

        for input_ref in unsafe_labels:
            with self.subTest(model="StationInput", input_ref=input_ref):
                with self.assertRaises(ValidationError):
                    StationInput(input_ref=input_ref, request=make_request())
            with self.subTest(
                model="StationOrchestrationResult",
                input_ref=input_ref,
            ):
                with self.assertRaises(ValidationError):
                    make_station_result(
                        input_ref=input_ref,
                        optimization_result=usable_result,
                    )

    def test_input_refs_share_safe_label_validation_from_json(self):
        station_input_payload = StationInput(
            input_ref="input-safe",
            request=make_request(),
        ).model_dump(mode="json")
        station_result_payload = make_station_result(
            input_ref="input-safe",
            optimization_result=make_optimization_result(
                candidate_statuses=("feasible",)
            ),
        ).model_dump(mode="json")
        unsafe_labels = (
            "",
            "   ",
            "inputs/station.json",
            "inputs\\station.json",
            "station\r.json",
            "station\n.json",
        )

        for input_ref in unsafe_labels:
            with self.subTest(model="StationInput", input_ref=input_ref):
                payload = {**station_input_payload, "input_ref": input_ref}
                with self.assertRaises(ValidationError):
                    StationInput.model_validate_json(json.dumps(payload))
            with self.subTest(
                model="StationOrchestrationResult",
                input_ref=input_ref,
            ):
                payload = {**station_result_payload, "input_ref": input_ref}
                with self.assertRaises(ValidationError):
                    StationOrchestrationResult.model_validate_json(
                        json.dumps(payload)
                    )

    def test_station_result_defaults_to_pending_selection_and_inactive_ems(self):
        result = make_station_result(status="optimized")
        self.assertEqual(result.selection_status, "pending_selection")
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
            optimization_result=make_optimization_result(
                candidate_statuses=("infeasible", "timeout", "error")
            ),
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

    def test_optimized_requires_a_usable_candidate_in_models_and_json(self):
        for usable_status in ("optimal", "feasible"):
            with self.subTest(usable_status=usable_status):
                result = make_station_result(
                    optimization_result=make_optimization_result(
                        candidate_statuses=(usable_status,)
                    )
                )
                parsed = StationOrchestrationResult.model_validate_json(
                    result.model_dump_json()
                )
                self.assertEqual(
                    parsed.optimization_result.candidates[0].status,
                    usable_status,
                )

        valid_payload = make_station_result().model_dump(mode="json")
        unusable_statuses = (
            (),
            ("infeasible", "timeout", "error"),
        )
        for candidate_statuses in unusable_statuses:
            optimization_result = make_optimization_result(
                candidate_statuses=candidate_statuses
            )
            with self.subTest(
                validation="construction",
                candidate_statuses=candidate_statuses,
            ):
                with self.assertRaises(ValidationError):
                    make_station_result(
                        optimization_result=optimization_result
                    )
            with self.subTest(
                validation="json",
                candidate_statuses=candidate_statuses,
            ):
                payload = {
                    **valid_payload,
                    "optimization_result": optimization_result.model_dump(
                        mode="json"
                    ),
                }
                with self.assertRaises(ValidationError):
                    StationOrchestrationResult.model_validate_json(
                        json.dumps(payload)
                    )

    def test_no_usable_candidate_forbids_usable_candidates_in_models_and_json(self):
        no_candidate_error = OrchestrationError(
            code="NO_USABLE_CANDIDATE",
            message="optimizer returned no usable candidate",
        )
        for candidate_statuses in (
            (),
            ("infeasible", "timeout", "error"),
        ):
            with self.subTest(
                valid_candidate_statuses=candidate_statuses
            ):
                result = make_station_result(
                    status="no_usable_candidate",
                    optimization_result=make_optimization_result(
                        candidate_statuses=candidate_statuses
                    ),
                    error=no_candidate_error,
                )
                parsed = StationOrchestrationResult.model_validate_json(
                    result.model_dump_json()
                )
                self.assertEqual(parsed.status, "no_usable_candidate")

        valid_payload = make_station_result(
            status="no_usable_candidate",
            optimization_result=make_optimization_result(
                candidate_statuses=("infeasible",)
            ),
            error=no_candidate_error,
        ).model_dump(mode="json")
        for usable_status in ("optimal", "feasible"):
            optimization_result = make_optimization_result(
                candidate_statuses=(usable_status,)
            )
            with self.subTest(
                validation="construction",
                usable_status=usable_status,
            ):
                with self.assertRaises(ValidationError):
                    make_station_result(
                        status="no_usable_candidate",
                        optimization_result=optimization_result,
                        error=no_candidate_error,
                    )
            with self.subTest(
                validation="json",
                usable_status=usable_status,
            ):
                payload = {
                    **valid_payload,
                    "optimization_result": optimization_result.model_dump(
                        mode="json"
                    ),
                }
                with self.assertRaises(ValidationError):
                    StationOrchestrationResult.model_validate_json(
                        json.dumps(payload)
                    )

    def test_input_error_allows_missing_identities_but_no_other_status_does(self):
        input_error = make_station_result(
            station_id=None,
            request_id=None,
            status="input_error",
            input_summary=None,
            optimization_result=None,
            error=OrchestrationError(code="INPUT_JSON_ERROR", message="invalid input"),
        )
        self.assertIsNone(input_error.station_id)
        self.assertIsNone(input_error.request_id)

        status_payloads = (
            ("optimized", None, None),
            (
                "no_usable_candidate",
                make_optimization_result(),
                OrchestrationError(
                    code="NO_USABLE_CANDIDATE", message="no usable candidate"
                ),
            ),
            (
                "optimization_error",
                None,
                OrchestrationError(
                    code="OPTIMIZATION_ERROR", message="optimizer failed"
                ),
            ),
        )
        for status, optimization_result, error in status_payloads:
            with self.subTest(status=status, identity="station_id"):
                with self.assertRaisesRegex(ValidationError, "station_id"):
                    make_station_result(
                        station_id=None,
                        status=status,
                        optimization_result=optimization_result,
                        error=error,
                    )
            with self.subTest(status=status, identity="request_id"):
                with self.assertRaisesRegex(ValidationError, "request_id"):
                    make_station_result(
                        request_id="   ",
                        status=status,
                        optimization_result=optimization_result,
                        error=error,
                    )

        with self.assertRaisesRegex(ValidationError, "station_id"):
            make_station_result(
                station_id=" ",
                status="input_error",
                input_summary=None,
                optimization_result=None,
                error=OrchestrationError(
                    code="INPUT_JSON_ERROR", message="invalid input"
                ),
            )

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
            overall_status="completed", stations=[make_station_result(), input_error]
        )
        self.assertEqual(partial.overall_status, "partial_failure")

        failed = make_orchestration_result(
            overall_status="completed", stations=[input_error]
        )
        self.assertEqual(failed.overall_status, "failed")

        top_level_failure = make_orchestration_result(
            errors=[
                OrchestrationError(
                    code="DUPLICATE_STATION_ID", message="duplicate station"
                )
            ]
        )
        self.assertEqual(top_level_failure.overall_status, "failed")

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
            make_orchestration_result(stations=[])
        with self.assertRaises(ValidationError):
            make_orchestration_result(overall_status="unknown")

    def test_orchestration_result_uses_exact_public_keys_and_preserves_duplicate_identities(self):
        self.assertEqual(
            set(M4OrchestrationResult.model_fields),
            {
                "schema_version",
                "run_id",
                "started_at",
                "finished_at",
                "orchestrator_version",
                "model_version",
                "overall_status",
                "stations",
                "errors",
            },
        )
        station = make_station_result()
        duplicated = station.model_copy(update={"input_ref": "station-1-copy.json"})
        result = make_orchestration_result(
            overall_status="failed",
            stations=[station, duplicated],
        )
        self.assertEqual(len(result.stations), 2)
        self.assertEqual(
            set(result.model_dump()),
            set(M4OrchestrationResult.model_fields),
        )
        with self.assertRaises(ValidationError):
            M4OrchestrationResult.model_validate(
                {**make_orchestration_result().model_dump(), "extra": True}
            )


if __name__ == "__main__":
    unittest.main()
