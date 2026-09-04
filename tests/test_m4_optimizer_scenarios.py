import unittest
from unittest.mock import patch

import m4_optimizer.service as service_module
from m4_optimizer import M4Optimizer
from m4_optimizer.lexicographic import ProfileSolveResult
from m4_optimizer.validation import ResultValidationError
from tests.m4_optimizer_test_support import (
    candidate_by_id,
    make_demand_peak_request,
    make_midday_pv_request,
    make_price_arbitrage_request,
    make_request,
    make_zero_pv_export_request,
    no_storage_unused_pv_energy,
    sum_power,
)


def metric_objective_value(metrics, layer):
    values = {
        "demand_peak": metrics.peak_demand_exceed_kw,
        "demand_duration": metrics.demand_exceed_energy_kwh,
        "soc_preferred_deviation": metrics.preferred_soc_deviation,
        "energy_cost": metrics.energy_cost,
        "pv_unused": (
            metrics.grid_export_energy_kwh
            + metrics.pv_unabsorbed_energy_kwh
        ),
        "throughput": metrics.throughput_energy_kwh,
    }
    return sum(weight * values[name] for name, weight in layer.terms.items())


class M4OptimizerScenarioTests(unittest.TestCase):
    def test_optimizer_returns_three_candidates_in_fixed_order(self):
        source_request = make_request()
        request = make_request(
            station_id="station-1",
            profiles=list(reversed(source_request.profiles)),
        )

        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)

        self.assertEqual(result.station_id, "station-1")
        self.assertEqual(
            [candidate.profile_id for candidate in result.candidates],
            ["balanced", "cost", "pv"],
        )
        self.assertTrue(
            all(candidate.status in {"optimal", "feasible"} for candidate in result.candidates)
        )
        self.assertTrue(all(len(candidate.plan) == 96 for candidate in result.candidates))

    def test_each_candidate_keeps_its_profile_version(self):
        request = make_request()

        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)

        expected = {profile.profile_id: profile.profile_version for profile in request.profiles}
        self.assertEqual(
            {item.profile_id: item.profile_version for item in result.candidates},
            expected,
        )

    def test_cost_profile_charges_in_valley_and_discharges_at_peak(self):
        request = make_price_arbitrage_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "cost")
        self.assertGreater(sum_power(candidate, "charge", range(0, 24)), 0.0)
        self.assertGreater(sum_power(candidate, "discharge", range(48, 72)), 0.0)
        self.assertLessEqual(
            abs(candidate.metrics.terminal_soc_pct - request.capability.initial_soc_pct),
            request.constraints.terminal_soc_tolerance_pct + 1e-6,
        )

    def test_pv_profile_stores_midday_surplus_before_export(self):
        request = make_midday_pv_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "pv")
        self.assertGreater(sum_power(candidate, "charge", range(40, 56)), 0.0)
        self.assertLess(
            candidate.metrics.grid_export_energy_kwh
            + candidate.metrics.pv_unabsorbed_energy_kwh,
            no_storage_unused_pv_energy(request),
        )
        self.assertTrue(request.constraints.grid_export_enabled)
        self.assertGreater(candidate.metrics.grid_export_energy_kwh, 0.0)
        self.assertLessEqual(candidate.metrics.grid_export_energy_kwh, 80.0 + 1e-6)
        self.assertGreater(candidate.metrics.pv_unabsorbed_energy_kwh, 0.0)
        self.assertIn("PV_UNABSORBED", candidate.risk_codes)
        self.assertTrue(
            all(point.grid_export_kw <= 20.0 + 1e-7 for point in candidate.plan)
        )
        self.assertGreater(sum(point.grid_import_kw for point in candidate.plan), 0.0)
        self.assertTrue(
            all(
                not (point.grid_import_kw > 1e-7 and point.grid_export_kw > 1e-7)
                for point in candidate.plan
            )
        )
        self.assertEqual(len(candidate.risk_codes), len(candidate.risk_messages))
        self.assertTrue(all(message.strip() for message in candidate.risk_messages))

    def test_demand_profile_reports_unavoidable_exceedance(self):
        request = make_demand_peak_request(max_discharge_kw=20.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "balanced")
        self.assertAlmostEqual(
            candidate.metrics.peak_demand_exceed_kw,
            60.0,
            delta=2e-6,
        )
        self.assertLessEqual(
            max(point.target_power_kw for point in candidate.plan), 20.0 + 1e-6
        )
        for point in candidate.plan[32:36]:
            self.assertEqual(point.mode, "discharge")
            self.assertAlmostEqual(point.target_power_kw, 20.0, delta=2e-6)
            self.assertAlmostEqual(point.demand_exceed_kw, 60.0, delta=2e-6)

    def test_disabled_storage_returns_an_idle_plan(self):
        result = M4Optimizer(model_version="m4-milp-v1").optimize(
            make_request(available=False)
        )
        for candidate in result.candidates:
            self.assertIn(candidate.status, {"optimal", "feasible"})
            self.assertEqual(len(candidate.plan), 96)
            self.assertTrue(all(point.mode == "idle" for point in candidate.plan))
            self.assertTrue(
                all(point.target_power_kw == 0.0 for point in candidate.plan)
            )

    def test_station_requests_do_not_share_identity_or_results(self):
        optimizer = M4Optimizer(model_version="m4-milp-v1")
        station_1 = optimizer.optimize(
            make_request(station_id="station-1", load_kw=100.0)
        )
        station_2 = optimizer.optimize(
            make_request(station_id="station-2", load_kw=300.0)
        )
        self.assertEqual(station_1.station_id, "station-1")
        self.assertEqual(station_2.station_id, "station-2")
        self.assertNotEqual(
            candidate_by_id(station_1, "balanced").metrics.max_grid_import_kw,
            candidate_by_id(station_2, "balanced").metrics.max_grid_import_kw,
        )

    def test_sufficient_battery_power_eliminates_demand_exceedance(self):
        request = make_demand_peak_request(max_discharge_kw=200.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "balanced")
        self.assertLessEqual(candidate.metrics.peak_demand_exceed_kw, 1e-6)

    def test_disabled_export_reports_unabsorbed_pv_without_export_command(self):
        request = make_midday_pv_request(
            grid_export_enabled=False,
            grid_export_limit_kw=0.0,
        )
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "pv")
        self.assertIn("PV_UNABSORBED", candidate.risk_codes)
        self.assertTrue(all(point.grid_export_kw <= 1e-7 for point in candidate.plan))

    def test_zero_sell_price_never_reports_export_revenue(self):
        request = make_midday_pv_request(sell_price_per_kwh=0.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.metrics.export_revenue, 0.0)

    def test_final_solution_respects_every_recorded_objective_lock(self):
        request = make_price_arbitrage_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        profiles = {profile.profile_id: profile for profile in request.profiles}
        for candidate in result.candidates:
            profile = profiles[candidate.profile_id]
            for layer, record in zip(
                profile.objective_order,
                candidate.layers,
                strict=True,
            ):
                final_value = metric_objective_value(candidate.metrics, layer)
                self.assertLessEqual(
                    final_value,
                    record.best_value + record.lock_tolerance + 1e-6,
                )

    def test_zero_pv_zero_load_with_export_enabled_never_exports(self):
        result = M4Optimizer(model_version="m4-milp-v1").optimize(
            make_zero_pv_export_request()
        )

        for candidate in result.candidates:
            self.assertIn(candidate.status, {"optimal", "feasible"})
            self.assertEqual(len(candidate.plan), 96)
            self.assertTrue(
                all(point.grid_export_kw <= 1e-7 for point in candidate.plan)
            )
            self.assertGreaterEqual(candidate.metrics.pv_self_use_kwh, 0.0)

    def test_abnormal_buy_sell_spread_cannot_create_export_without_pv(self):
        result = M4Optimizer(model_version="m4-milp-v1").optimize(
            make_zero_pv_export_request(
                buy_price_per_kwh=0.1,
                sell_price_per_kwh=10.0,
            )
        )

        for candidate in result.candidates:
            self.assertIn(candidate.status, {"optimal", "feasible"})
            self.assertTrue(
                all(point.grid_export_kw <= 1e-7 for point in candidate.plan)
            )

    def test_same_input_returns_deterministic_auditable_candidates(self):
        request = make_request()
        optimizer = M4Optimizer(model_version="m4-milp-v1")

        first = optimizer.optimize(request)
        second = optimizer.optimize(request)

        self.assertEqual(self.stable_result(first), self.stable_result(second))
        for candidate in first.candidates:
            self.assertTrue(candidate.plan_version.strip())
            self.assertIn(request.request_id, candidate.plan_version)
            self.assertEqual(len(candidate.risk_codes), len(candidate.risk_messages))

    def test_public_failure_statuses_have_empty_outputs_and_stable_versions(self):
        request = make_request()

        def optimize_failures():
            results = [
                ProfileSolveResult("infeasible", None, (), "infeasible", 0.01),
                ProfileSolveResult("timeout", None, (), "time limit", 0.02),
                ProfileSolveResult("error", None, (), "solver error", 0.03),
            ]
            with patch("m4_optimizer.service.solve_profile", side_effect=results):
                return M4Optimizer(model_version="m4-milp-v1").optimize(request)

        output = optimize_failures()
        repeated = optimize_failures()

        self.assertEqual(
            [candidate.status for candidate in output.candidates],
            ["infeasible", "timeout", "error"],
        )
        for candidate in output.candidates:
            self.assertEqual(candidate.plan, [])
            self.assertIsNone(candidate.metrics)
            self.assertTrue(candidate.plan_version.strip())
            self.assertIn(request.request_id, candidate.plan_version)
            self.assertEqual(len(candidate.risk_codes), len(candidate.risk_messages))
        self.assertEqual(
            [candidate.plan_version for candidate in output.candidates],
            [candidate.plan_version for candidate in repeated.candidates],
        )

    def test_candidate_processing_errors_do_not_stop_later_profiles(self):
        stages = (
            ("materialize_plan", ValueError("bad materialization")),
            ("calculate_metrics", ValueError("bad metrics")),
            ("validate_candidate", ResultValidationError("bad validation")),
        )

        for stage_name, failure in stages:
            with self.subTest(stage=stage_name):
                original = getattr(service_module, stage_name)
                call_count = 0

                def fail_first(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise failure
                    return original(*args, **kwargs)

                with patch(
                    f"m4_optimizer.service.{stage_name}", side_effect=fail_first
                ):
                    output = M4Optimizer(model_version="m4-milp-v1").optimize(
                        make_request()
                    )

                failed, *later = output.candidates
                self.assertEqual(failed.status, "error")
                self.assertEqual(failed.plan, [])
                self.assertIsNone(failed.metrics)
                self.assertGreater(len(failed.layers), 0)
                self.assertTrue(failed.plan_version.strip())
                self.assertIn(stage_name, failed.solver_message)
                self.assertEqual(failed.risk_codes, ["CANDIDATE_PROCESSING_ERROR"])
                self.assertEqual(len(failed.risk_codes), len(failed.risk_messages))
                self.assertTrue(
                    all(candidate.status in {"optimal", "feasible"} for candidate in later)
                )

    def test_unexpected_candidate_programming_error_is_not_swallowed(self):
        with patch(
            "m4_optimizer.service.calculate_metrics",
            side_effect=RuntimeError("programming failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming failure"):
                M4Optimizer(model_version="m4-milp-v1").optimize(make_request())

    @staticmethod
    def stable_result(result):
        payload = result.model_dump()
        payload.pop("started_at")
        payload.pop("finished_at")
        for candidate in payload["candidates"]:
            candidate.pop("solve_seconds")
        return payload
