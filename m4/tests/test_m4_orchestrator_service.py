import unittest
from copy import deepcopy
from pathlib import Path

from m4.orchestrator import (
    M4Orchestrator,
    OrchestrationError,
    StationInput,
    load_station_input,
)
from m4.tests.m4_optimizer_test_support import make_request
from m4.tests.m4_orchestrator_test_support import (
    DeterministicOptimizer,
    FIXED_FINISHED_AT,
    FIXED_STARTED_AT,
    assert_nested_close,
    make_optimization_result,
)


MOCK_DIR = Path(__file__).resolve().parents[2] / "m4" / "mock" / "orchestration"
STATION_1 = MOCK_DIR / "station-1.json"
STATION_2 = MOCK_DIR / "station-2.json"


def without_runtime_measurements(payload: dict[str, object]) -> dict[str, object]:
    normalized = deepcopy(payload)
    for field in ("run_id", "started_at", "finished_at"):
        normalized.pop(field)
    for station in normalized["stations"]:
        optimization_result = station["optimization_result"]
        for field in ("started_at", "finished_at"):
            optimization_result.pop(field)
        for candidate in optimization_result["candidates"]:
            candidate.pop("solve_seconds")
    return normalized


class M4OrchestratorRealAcceptanceTests(unittest.TestCase):
    def test_committed_mock_inputs_are_independent_and_complete(self):
        first = load_station_input(STATION_1).request
        second = load_station_input(STATION_2).request

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(
            [first.station_id, second.station_id],
            ["station-1", "station-2"],
        )
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertNotEqual(
            first.capability.energy_capacity_kwh,
            second.capability.energy_capacity_kwh,
        )
        self.assertNotEqual(
            first.constraints.demand_limit_kw,
            second.constraints.demand_limit_kw,
        )
        self.assertNotEqual(first.source_versions, second.source_versions)
        self.assertEqual(len(first.points), 96)
        self.assertEqual(len(second.points), 96)

    def test_real_two_station_run_returns_six_complete_candidates(self):
        result = M4Orchestrator(
            model_version="m4-stage-a-v1",
            orchestrator_version="m4-orchestrator-b1-v1",
            clock=lambda: FIXED_STARTED_AT,
            run_id_factory=lambda: "acceptance-run-001",
        ).run([load_station_input(STATION_2), load_station_input(STATION_1)])

        self.assertEqual(result.overall_status, "completed")
        self.assertEqual(len(result.stations), 2)
        for station in result.stations:
            self.assertEqual(
                [
                    candidate.profile_id
                    for candidate in station.optimization_result.candidates
                ],
                ["balanced", "cost", "pv"],
            )
            for candidate in station.optimization_result.candidates:
                self.assertIn(candidate.status, {"optimal", "feasible"})
                self.assertEqual(len(candidate.plan), 96)
            self.assertEqual(station.selection_status, "pending_selection")
            self.assertIsNone(station.selected_candidate_id)
            self.assertEqual(station.dispatch_status, "not_dispatched")
            self.assertIsNone(station.ems_task_id)

    def test_real_two_station_runs_are_deterministic_within_solver_tolerance(self):
        def execute(run_id: str):
            return M4Orchestrator(
                model_version="m4-stage-a-v1",
                orchestrator_version="m4-orchestrator-b1-v1",
                clock=lambda: FIXED_STARTED_AT,
                run_id_factory=lambda: run_id,
            ).run([load_station_input(STATION_1), load_station_input(STATION_2)])

        first = without_runtime_measurements(
            execute("determinism-run-001").model_dump(mode="json")
        )
        second = without_runtime_measurements(
            execute("determinism-run-002").model_dump(mode="json")
        )

        assert_nested_close(self, first, second)


def station_input(station_id: str, *, input_ref: str | None = None) -> StationInput:
    return StationInput(
        input_ref=input_ref or f"{station_id}.json",
        request=make_request(
            station_id=station_id,
            request_id=f"request-{station_id}",
        ),
    )


def invalid_station_input(
    *,
    input_ref: str = "invalid.json",
    station_id_hint: str | None = None,
    request_id_hint: str | None = None,
) -> StationInput:
    return StationInput(
        input_ref=input_ref,
        station_id_hint=station_id_hint,
        request_id_hint=request_id_hint,
        error=OrchestrationError(
            code="INPUT_JSON_ERROR",
            message="input file is not valid JSON",
        ),
    )


class M4OrchestratorServiceTests(unittest.TestCase):
    def setUp(self):
        self.optimizer = DeterministicOptimizer()
        self.orchestrator = self.make_orchestrator(self.optimizer)

    def make_orchestrator(self, optimizer):
        times = iter((FIXED_STARTED_AT, FIXED_FINISHED_AT))
        return M4Orchestrator(
            model_version="test-model-v1",
            orchestrator_version="test-orchestrator-v1",
            optimizer=optimizer,
            clock=lambda: next(times),
            run_id_factory=lambda: "run-test-001",
        )

    def test_two_stations_are_optimized_independently_and_sorted(self):
        station_2 = station_input("station-2")
        station_1 = station_input("station-1")

        result = self.orchestrator.run([station_2, station_1])

        self.assertEqual(result.overall_status, "completed")
        self.assertEqual(
            [item.station_id for item in result.stations],
            ["station-1", "station-2"],
        )
        self.assertEqual(self.optimizer.calls, ["station-2", "station-1"])
        self.assertTrue(all(item.status == "optimized" for item in result.stations))
        self.assertTrue(
            all(item.selection_status == "pending_selection" for item in result.stations)
        )
        self.assertTrue(
            all(item.selected_candidate_id is None for item in result.stations)
        )
        self.assertTrue(
            all(item.dispatch_status == "not_dispatched" for item in result.stations)
        )
        self.assertTrue(all(item.ems_task_id is None for item in result.stations))
        self.assertEqual(result.schema_version, "m4-orchestration-v2")
        self.assertEqual(result.run_id, "run-test-001")
        self.assertEqual(result.started_at, FIXED_STARTED_AT)
        self.assertEqual(result.finished_at, FIXED_FINISHED_AT)

        summary = result.stations[0].input_summary
        request = station_1.request
        self.assertEqual(summary.plan_start_at, request.plan_start_at)
        self.assertEqual(summary.source_versions, request.source_versions)
        self.assertEqual(summary.horizon_points, 96)
        self.assertEqual(summary.initial_soc_pct, 50.0)
        self.assertEqual(summary.demand_limit_kw, 120.0)

    def test_input_and_optimizer_failures_do_not_block_a_healthy_station(self):
        secret = 'SECRET_EXCEPTION /private/station.json {"token":"hidden"} TRACEBACK'
        optimizer = DeterministicOptimizer(
            failures_by_station_id={"station-failing": RuntimeError(secret)}
        )
        orchestrator = self.make_orchestrator(optimizer)
        bad_input = invalid_station_input(station_id_hint="station-input-bad")
        failing_station = station_input("station-failing")
        healthy_station = station_input("station-healthy")

        result = orchestrator.run([bad_input, failing_station, healthy_station])

        self.assertEqual(result.overall_status, "partial_failure")
        self.assertEqual(optimizer.calls, ["station-failing", "station-healthy"])
        status_by_id = {item.station_id: item.status for item in result.stations}
        self.assertEqual(status_by_id["station-healthy"], "optimized")
        self.assertEqual(status_by_id["station-failing"], "optimization_error")
        self.assertEqual(status_by_id["station-input-bad"], "input_error")
        failing_result = next(
            item for item in result.stations if item.station_id == "station-failing"
        )
        self.assertEqual(failing_result.error.code, "OPTIMIZATION_ERROR")
        rendered = result.model_dump_json()
        for sensitive_value in (
            "SECRET_EXCEPTION",
            "/private/station.json",
            '"token":"hidden"',
            "TRACEBACK",
        ):
            self.assertNotIn(sensitive_value, rendered)

    def test_successes_sort_by_station_id_then_errors_sort_by_input_ref(self):
        healthy_b = station_input("station-b", input_ref="success-a")
        hinted_input_error = invalid_station_input(
            input_ref="error-c",
            station_id_hint="station-00-hint",
            request_id_hint="request-hinted",
        )
        optimization_error = station_input(
            "station-z-error",
            input_ref="error-a",
        )
        no_candidate = station_input(
            "station-0-no-candidate",
            input_ref="error-b",
        )
        healthy_a = station_input("station-a", input_ref="success-z")
        optimizer = DeterministicOptimizer(
            results_by_station_id={
                "station-0-no-candidate": make_optimization_result(
                    no_candidate.request,
                    candidate_statuses=("infeasible", "timeout", "error"),
                )
            },
            failures_by_station_id={
                "station-z-error": RuntimeError("redacted optimizer failure")
            },
        )

        result = self.make_orchestrator(optimizer).run(
            [
                healthy_b,
                hinted_input_error,
                optimization_error,
                no_candidate,
                healthy_a,
            ]
        )

        self.assertEqual(result.overall_status, "partial_failure")
        self.assertEqual(
            [
                (item.status, item.input_ref, item.station_id)
                for item in result.stations
            ],
            [
                ("optimized", "success-z", "station-a"),
                ("optimized", "success-a", "station-b"),
                ("optimization_error", "error-a", "station-z-error"),
                (
                    "no_usable_candidate",
                    "error-b",
                    "station-0-no-candidate",
                ),
                ("input_error", "error-c", "station-00-hint"),
            ],
        )

    def test_duplicate_station_ids_reject_every_station_before_solving(self):
        first_duplicate = station_input("station-1", input_ref="station-1-a.json")
        unique = station_input("station-2")
        second_duplicate = station_input("station-1", input_ref="station-1-b.json")

        result = self.orchestrator.run(
            [first_duplicate, unique, second_duplicate]
        )

        self.assertEqual(result.overall_status, "failed")
        self.assertEqual(self.optimizer.calls, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].code, "DUPLICATE_STATION_ID")
        self.assertTrue(all(item.status == "input_error" for item in result.stations))
        self.assertTrue(
            all(
                item.error.code == "DUPLICATE_STATION_ID"
                for item in result.stations
            )
        )

    def test_duplicate_station_ids_preserve_existing_input_errors(self):
        bad_input = invalid_station_input(
            station_id_hint="station-bad",
            request_id_hint="request-bad",
        )

        result = self.orchestrator.run(
            [station_input("station-1"), bad_input, station_input("station-1")]
        )

        bad_result = next(
            item for item in result.stations if item.station_id == "station-bad"
        )
        self.assertEqual(self.optimizer.calls, [])
        self.assertEqual(bad_result.status, "input_error")
        self.assertEqual(bad_result.error.code, "INPUT_JSON_ERROR")
        self.assertEqual(result.errors[0].code, "DUPLICATE_STATION_ID")

    def test_empty_inputs_raise_value_error(self):
        with self.assertRaises(ValueError):
            self.orchestrator.run([])

    def test_all_input_errors_produce_failed(self):
        result = self.orchestrator.run(
            [
                invalid_station_input(
                    station_id_hint="station-bad",
                    request_id_hint="request-bad",
                )
            ]
        )

        self.assertEqual(result.overall_status, "failed")
        self.assertEqual(self.optimizer.calls, [])
        self.assertEqual(result.stations[0].status, "input_error")
        self.assertEqual(result.stations[0].station_id, "station-bad")
        self.assertEqual(result.stations[0].request_id, "request-bad")

    def test_failure_only_candidates_produce_no_usable_candidate(self):
        source = station_input("station-1")
        optimizer = DeterministicOptimizer(
            results_by_station_id={
                "station-1": make_optimization_result(
                    source.request,
                    candidate_statuses=("infeasible", "timeout", "error"),
                )
            }
        )

        result = self.make_orchestrator(optimizer).run([source])

        station = result.stations[0]
        self.assertEqual(result.overall_status, "failed")
        self.assertEqual(station.status, "no_usable_candidate")
        self.assertEqual(station.error.code, "NO_USABLE_CANDIDATE")
        self.assertIsNotNone(station.optimization_result)

    def test_mismatched_optimizer_result_identity_is_an_optimization_error(self):
        for mismatch in (
            {"station_id": "station-other"},
            {"request_id": "request-other"},
        ):
            with self.subTest(mismatch=mismatch):
                source = station_input("station-1")
                optimizer = DeterministicOptimizer(
                    results_by_station_id={
                        "station-1": make_optimization_result(
                            source.request,
                            candidate_statuses=("optimal",),
                            **mismatch,
                        )
                    }
                )

                result = self.make_orchestrator(optimizer).run([source])

                station = result.stations[0]
                self.assertEqual(station.status, "optimization_error")
                self.assertEqual(station.error.code, "OPTIMIZATION_ERROR")
                self.assertIsNone(station.optimization_result)

    def test_keyboard_interrupt_is_not_swallowed(self):
        class InterruptingOptimizer:
            def optimize(self, request):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.make_orchestrator(InterruptingOptimizer()).run(
                [station_input("station-1")]
            )

    def test_constructor_rejects_blank_versions(self):
        for field in ("model_version", "orchestrator_version"):
            with self.subTest(field=field):
                values = {
                    "model_version": "test-model-v1",
                    "orchestrator_version": "test-orchestrator-v1",
                }
                values[field] = "   "
                with self.assertRaisesRegex(ValueError, field):
                    M4Orchestrator(**values)


if __name__ == "__main__":
    unittest.main()
