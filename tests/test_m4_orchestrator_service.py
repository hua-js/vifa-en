import unittest

from m4_orchestrator import M4Orchestrator, OrchestrationError, StationInput
from tests.m4_optimizer_test_support import make_request
from tests.m4_orchestrator_test_support import (
    DeterministicOptimizer,
    FIXED_FINISHED_AT,
    FIXED_STARTED_AT,
    make_optimization_result,
)


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
            all(item.selection_status == "pending_ai" for item in result.stations)
        )
        self.assertTrue(
            all(item.selected_candidate_id is None for item in result.stations)
        )
        self.assertTrue(
            all(item.dispatch_status == "not_dispatched" for item in result.stations)
        )
        self.assertTrue(all(item.ems_task_id is None for item in result.stations))
        self.assertEqual(result.schema_version, "m4-orchestration-v1")
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
