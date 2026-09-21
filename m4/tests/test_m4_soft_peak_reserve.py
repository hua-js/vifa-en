"""Soft running reserve must never relax safety, terminal energy or peak charging."""
from datetime import datetime, timedelta
import unittest

from pydantic import ValidationError

from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.metrics import calculate_metrics, materialize_plan
from m4.optimizer.model import build_model
from m4.optimizer.service import M4Optimizer
from m4.optimizer.solver import solve_milp
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.settings.objectives import get_daily_profiles
from m4.tests.m4_optimizer_test_support import make_candidate, make_request


def soft_request(*, tariffs=('jian', 'feng', 'ping', 'ping'), pv=0.0, initial=50.0,
                 version='peak-reserve-v4'):
    base = make_request(station_id='station-2')
    start = datetime.fromisoformat('2026-09-21T23:00:00+08:00')
    payload = base.model_dump()
    payload.update(plan_start_at=start, input_observed_at=start, horizon_points=4,
        source_versions={'planning_basis': 'remaining-day-v1'},
        profiles=get_daily_profiles('station-2'), pv_dispatch_policy='load_first_economic',
        peak_reserve_policy=dict(version=version, terminal_soc_min_pct=60.0),
        capability=base.capability.model_copy(update={'initial_soc_pct': initial}),
        constraints=base.constraints.model_copy(update=dict(grid_import_limit_kw=300.0,
            demand_limit_kw=300.0, grid_export_enabled=True, grid_export_limit_kw=200.0)),
        points=[p.model_copy(update=dict(timestamp=start+timedelta(minutes=15*i),
            tariff_period=tariffs[i], load_forecast_kw=100.0, pv_forecast_kw=pv,
            buy_price_per_kwh=1.0 if tariffs[i] in ('jian','feng') else 0.2))
            for i,p in enumerate(base.points[:4])])
    try:
        return OptimizationRequest.model_validate(payload)
    except ValidationError as error:
        raise AssertionError(f'New reserve request must be supported: {error}') from error


class SoftPeakReserveTests(unittest.TestCase):
    def optimize(self, request):
        result = M4Optimizer(model_version='soft-reserve-test').optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
            validate_candidate(request, candidate)
            self.assertGreaterEqual(candidate.plan[-1].expected_soc_pct, 60.0-1e-6)
        return result

    def test_low_peak_start_is_feasible_without_peak_grid_charging(self):
        request = soft_request()
        result = self.optimize(request)
        for candidate in result.candidates:
            self.assertTrue(all(p.mode != 'charge' for p in candidate.plan[:2]))
            self.assertTrue(any(p.mode == 'charge' for p in candidate.plan[2:]))
            self.assertIn('PEAK_RESERVE_SHORTFALL', candidate.risk_codes)
            self.assertNotIn('PEAK_RESERVE_PREFERENCE_INCOMPLETE', candidate.risk_codes)
        old = soft_request(version='peak-reserve-v3')
        with self.assertRaisesRegex(ResultValidationError, 'late peak starting'):
            validate_candidate(old, result.candidates[0])

    def test_next_state_may_remain_below_target_while_pv_recovers_energy(self):
        request = soft_request(tariffs=('jian','feng','feng','feng'), pv=140.0)
        result = self.optimize(request)
        for candidate in result.candidates:
            self.assertLess(candidate.plan[0].expected_soc_pct, 60.0)
            for point in candidate.plan:
                self.assertLessEqual(point.target_power_kw if point.mode == 'charge' else 0, 40.0+1e-6)
                self.assertAlmostEqual(point.grid_import_kw, 0.0)

    def test_terminal_shortage_still_makes_all_peak_no_pv_infeasible(self):
        request = soft_request(tariffs=('jian','feng','feng','feng'))
        result = M4Optimizer(model_version='soft-reserve-test').optimize(request)
        self.assertTrue(all(c.status == 'infeasible' and not c.plan for c in result.candidates))

    def test_remaining_tail_without_peaks_can_recover_from_low_start(self):
        self.optimize(soft_request(tariffs=('ping','ping','ping','ping')))

    def test_met_running_target_does_not_report_a_shortfall(self):
        request = soft_request(tariffs=('feng','feng','feng','feng'), initial=60.0)
        for point in request.points:
            point.load_forecast_kw = 0.0
        for candidate in self.optimize(request).candidates:
            self.assertNotIn('PEAK_RESERVE_SHORTFALL', candidate.risk_codes)
            self.assertTrue(all(point.mode == 'idle' for point in candidate.plan))

    def test_soft_reserve_cannot_supply_load_by_crossing_safety_minimum(self):
        request = soft_request(initial=10.0)
        request.points[0].load_forecast_kw = 400.0
        result = M4Optimizer(model_version='soft-reserve-test').optimize(request)
        self.assertTrue(all(c.status == 'infeasible' and not c.plan for c in result.candidates))

    def test_independent_validator_keeps_terminal_minimum_hard(self):
        request = soft_request()
        built = build_model(request)
        # Produce an otherwise valid idle plan, independently of reserve constraints.
        for variable in built.problem.model.charge.values():
            variable.fix(0)
        for variable in built.problem.model.discharge.values():
            variable.fix(0)
        built.problem.model.energy[4].setlb(0)
        solved = solve_milp(built.problem, built.objectives['throughput'], locks=(),
            time_limit_seconds=3, mip_rel_gap=0)
        self.assertEqual(solved.status, 'optimal')
        plan = materialize_plan(request, built, solved.x)
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        with self.assertRaisesRegex(ResultValidationError, 'terminal SOC minimum'):
            validate_candidate(request, candidate)

    def test_soft_objective_prefers_reducing_gap_earlier(self):
        request = soft_request(tariffs=('ping','ping','ping','ping'))
        built = build_model(request)
        # Fix the endpoint so extra terminal energy cannot explain early charging.
        built.problem.model.energy[4].fix(120.0)
        for variable in built.problem.model.discharge.values():
            variable.fix(0)
        solved = solve_milp(built.problem, built.objectives['peak_reserve_shortfall'], locks=(),
            time_limit_seconds=3, mip_rel_gap=0)
        self.assertEqual(solved.status, 'optimal')
        plan = materialize_plan(request, built, solved.x)
        self.assertAlmostEqual(plan[0].expected_soc_pct, 60.0)
        self.assertTrue(all(point.mode == 'idle' for point in plan[1:]))

    def test_physical_grid_limit_may_require_temporary_reserve_use(self):
        request = soft_request(initial=60.0)
        request.constraints.grid_import_limit_kw = 100.0
        request.points[0].load_forecast_kw = 200.0
        for point in request.points[1:]:
            point.load_forecast_kw = 0.0
        for candidate in self.optimize(request).candidates:
            self.assertLess(candidate.plan[0].expected_soc_pct, 60.0)
            self.assertGreaterEqual(min(p.expected_soc_pct for p in candidate.plan), request.constraints.soc_min_pct)
            self.assertTrue(all(p.grid_import_kw <= 100.0+1e-6 for p in candidate.plan))
