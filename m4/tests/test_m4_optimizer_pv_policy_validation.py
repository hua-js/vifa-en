"""Reject physically consistent public plans that violate the new PV policy."""

import unittest

from m4.optimizer import M4Optimizer
from m4.optimizer.contracts import OptimizationRequest, PlanPoint
from m4.optimizer.metrics import calculate_metrics
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.tests.m4_optimizer_test_support import make_candidate, make_request


def make_pv_request(*, pv_kw=100.0, export=False, export_limit_kw=0.0):
    payload = make_request().model_dump()
    payload["pv_dispatch_policy"] = "load_first_economic"
    payload["constraints"].update(
        terminal_soc_tolerance_pct=100.0,
        demand_limit_kw=1000.0,
        grid_import_limit_kw=1000.0,
        grid_export_enabled=export,
        grid_export_limit_kw=export_limit_kw,
    )
    for index, point in enumerate(payload["points"]):
        point.update(
            load_forecast_kw=40.0 if index == 0 else 0.0,
            pv_forecast_kw=pv_kw if index == 0 else 0.0,
        )
    return OptimizationRequest.model_validate(payload)


def candidate_with_first_dispatch(
    request, *, charge_kw, import_kw=0.0, export_kw=0.0, unabsorbed_kw=0.0,
):
    """Rebuild every SOC and metric after changing the first dispatch."""
    capability = request.capability
    energy = capability.energy_capacity_kwh * capability.initial_soc_pct / 100.0
    plan = []
    for index, source in enumerate(request.points):
        charge = charge_kw if index == 0 else 0.0
        grid_import = import_kw if index == 0 else 0.0
        energy += charge * capability.charge_efficiency * 0.25
        plan.append(PlanPoint(
            timestamp=source.timestamp,
            mode="charge" if charge else "idle",
            target_power_kw=charge,
            expected_soc_pct=energy / capability.energy_capacity_kwh * 100.0,
            grid_import_kw=grid_import,
            grid_export_kw=export_kw if index == 0 else 0.0,
            pv_unabsorbed_kw=unabsorbed_kw if index == 0 else 0.0,
            demand_exceed_kw=max(grid_import - request.constraints.demand_limit_kw, 0.0),
        ))
    return make_candidate(request, plan, calculate_metrics(request, plan))


class PVPolicyValidationTests(unittest.TestCase):
    def assert_policy_rejects_only_new_rule(self, request, candidate, rule):
        # Passing the legacy validator proves power balance, SOC, device limits,
        # terminal bounds, and all recomputed metrics remain internally valid.
        legacy_request = request.model_copy(update={"pv_dispatch_policy": "legacy"})
        validate_candidate(legacy_request, candidate)
        with self.assertRaisesRegex(ResultValidationError, rule):
            validate_candidate(request, candidate)

    def test_rejects_under_absorption_with_consistent_soc_and_metrics(self):
        request = make_pv_request()
        valid = candidate_with_first_dispatch(request, charge_kw=60.0)
        validate_candidate(request, valid)
        tampered = candidate_with_first_dispatch(
            request, charge_kw=30.0, unabsorbed_kw=30.0,
        )
        self.assert_policy_rejects_only_new_rule(
            request, tampered, "PV surplus absorption",
        )

    def test_rejects_under_absorption_even_when_export_is_at_limit(self):
        request = make_pv_request(pv_kw=220.0, export=True, export_limit_kw=50.0)
        valid = candidate_with_first_dispatch(
            request, charge_kw=100.0, export_kw=50.0, unabsorbed_kw=30.0,
        )
        validate_candidate(request, valid)
        tampered = candidate_with_first_dispatch(
            request, charge_kw=90.0, export_kw=50.0, unabsorbed_kw=40.0,
        )
        self.assert_policy_rejects_only_new_rule(
            request, tampered, "PV surplus absorption",
        )

    def test_rejects_grid_import_during_pv_surplus(self):
        request = make_pv_request()
        valid = candidate_with_first_dispatch(request, charge_kw=60.0)
        validate_candidate(request, valid)
        tampered = candidate_with_first_dispatch(
            request, charge_kw=70.0, import_kw=10.0,
        )
        self.assert_policy_rejects_only_new_rule(
            request, tampered, "PV surplus grid import",
        )

    def test_rejects_curtailment_before_permitted_export_is_saturated(self):
        request = make_pv_request(pv_kw=220.0, export=True, export_limit_kw=50.0)
        valid = candidate_with_first_dispatch(
            request, charge_kw=100.0, export_kw=50.0, unabsorbed_kw=30.0,
        )
        validate_candidate(request, valid)
        tampered = candidate_with_first_dispatch(
            request, charge_kw=100.0, export_kw=40.0, unabsorbed_kw=40.0,
        )
        self.assert_policy_rejects_only_new_rule(
            request, tampered, "PV curtailment before export limit",
        )

    def test_disabled_export_absorbs_past_narrow_preferred_soc_range(self):
        payload = make_pv_request().model_dump()
        payload["constraints"].update(
            preferred_soc_min_pct=49.0,
            preferred_soc_max_pct=50.0,
        )
        request = OptimizationRequest.model_validate(payload)
        result = M4Optimizer(model_version="pv-validation-test").optimize(request)
        for candidate in result.candidates:
            with self.subTest(profile=candidate.profile_id):
                self.assertIn(candidate.status, {"optimal", "feasible"}, candidate.solver_message)
                validate_candidate(request, candidate)
                point = candidate.plan[0]
                self.assertEqual(point.mode, "charge")
                self.assertAlmostEqual(point.target_power_kw, 60.0, delta=1e-6)
                self.assertAlmostEqual(point.pv_unabsorbed_kw, 0.0, delta=1e-6)
                self.assertGreater(point.expected_soc_pct, request.constraints.preferred_soc_max_pct)
                self.assertLessEqual(point.expected_soc_pct, request.constraints.soc_max_pct)
                self.assertGreater(candidate.metrics.preferred_soc_deviation, 0.0)
