import unittest

from m4.optimizer.metrics import calculate_metrics, materialize_plan
from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.tests.m4_optimizer_test_support import (
    make_candidate,
    make_candidate_from_optimizer,
    make_request,
)


class M4OptimizerValidationTests(unittest.TestCase):
    def test_materialized_plan_recomputes_soc_and_power_balance(self):
        request = make_request()
        built = build_model(request)
        solved = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertIsNotNone(solved.x)
        plan = materialize_plan(request, built, solved.x)
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        validate_candidate(request, candidate)

    def test_calculate_metrics_uses_only_public_plan_values(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        metrics = calculate_metrics(request, candidate.plan)

        self.assertAlmostEqual(metrics.import_cost, 1536.0)
        self.assertAlmostEqual(metrics.energy_cost, 1536.0)
        self.assertAlmostEqual(metrics.max_grid_import_kw, 80.0)
        self.assertAlmostEqual(metrics.pv_self_use_kwh, 480.0)
        self.assertAlmostEqual(metrics.pv_self_use_rate, 1.0)
        self.assertAlmostEqual(metrics.terminal_soc_pct, 50.0)
        self.assertAlmostEqual(metrics.throughput_energy_kwh, 0.0)

    def test_validator_rejects_tampered_soc(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][10]["expected_soc_pct"] += 3.0
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "SOC state"):
            validate_candidate(request, tampered)

    def test_validator_rejects_simultaneous_import_and_export(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][4]["grid_import_kw"] = 10.0
        payload["plan"][4]["grid_export_kw"] = 10.0
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "import and export"):
            validate_candidate(request, tampered)

    def test_validator_rejects_export_plus_unabsorbed_above_available_pv(self):
        base = make_request()
        request = make_request(
            constraints=base.constraints.model_copy(
                update={"grid_export_enabled": True, "grid_export_limit_kw": 20.0}
            )
        )
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][0].update(
            {
                "grid_import_kw": 0.0,
                "grid_export_kw": 15.0,
                "pv_unabsorbed_kw": 10.0,
            }
        )
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "PV attribution"):
            validate_candidate(request, tampered)

    def test_metrics_reject_export_plus_unabsorbed_above_available_pv(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][0].update(
            {
                "grid_export_kw": 15.0,
                "pv_unabsorbed_kw": 10.0,
            }
        )
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ValueError, "PV-attributed flows"):
            calculate_metrics(request, tampered.plan)

    def test_validator_rejects_tampered_metrics(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["metrics"]["energy_cost"] += 1.0
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "metric energy_cost"):
            validate_candidate(request, tampered)

    def test_validator_rejects_high_reported_demand_exceed_with_synced_metrics(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        for point in payload["plan"]:
            point["demand_exceed_kw"] = 1.0
        candidate_with_high_demand = type(candidate).model_validate(payload)
        payload["metrics"] = calculate_metrics(
            request, candidate_with_high_demand.plan
        ).model_dump()
        tampered = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "demand exceed"):
            validate_candidate(request, tampered)

    def test_materialize_plan_rejects_significantly_negative_power(self):
        request = make_request()
        built, x = self._solved_vector(request)
        x[built.index.charge.start] = -10.0

        with self.assertRaisesRegex(ValueError, "negative"):
            materialize_plan(request, built, x)

    def test_materialize_plan_rejects_simultaneous_charge_and_discharge(self):
        request = make_request()
        built, x = self._solved_vector(request)
        x[built.index.charge.start] = 10.0
        x[built.index.discharge.start] = 10.0

        with self.assertRaisesRegex(ValueError, "charge and discharge"):
            materialize_plan(request, built, x)

    def test_validator_rejects_nonempty_failed_candidate(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["status"] = "infeasible"
        payload["metrics"] = None
        failed = type(candidate).model_validate(payload)

        with self.assertRaisesRegex(ResultValidationError, "non-success candidate"):
            validate_candidate(request, failed)

    def _solved_vector(self, request):
        built = build_model(request)
        solved = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertIsNotNone(solved.x)
        return built, solved.x.copy()
