"""Regression for day plans that lose terminal energy after dispatch filtering."""
import unittest
import numpy as np

from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import validate_candidate, ResultValidationError
from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.settings.dispatch_power import dispatch_points
from m4.settings.ems_model_update import validate_dispatch_safety
from m4.settings.objectives import get_daily_profiles
from m4.tests.m4_optimizer_test_support import make_request


def small_surplus_request(*, minimum=True):
    raw = make_request().model_dump()
    raw['station_id'] = 'station-2'
    raw['profiles'] = [p.model_dump() for p in get_daily_profiles('station-2')]
    raw['pv_dispatch_policy'] = 'load_first_economic'
    raw['capability'].update(initial_soc_pct=98.0, energy_capacity_kwh=100.0,
        charge_efficiency=1.0, discharge_efficiency=1.0)
    raw['constraints'].update(soc_min_pct=1.0, soc_max_pct=98.0,
        preferred_soc_min_pct=2.0, preferred_soc_max_pct=98.0,
        terminal_soc_tolerance_pct=0.0, grid_export_enabled=True,
        grid_export_limit_kw=100.0, cycle_cost_per_kwh=0.0)
    raw['peak_reserve_policy'] = dict(version='peak-reserve-v4', terminal_soc_min_pct=2.0)
    raw['source_versions']['terminal_policy'] = 'daily-operating-floor-v1'
    if minimum:
        raw['source_versions'].update(dispatch_power_policy='minimum-action-v1',
                                     minimum_dispatch_power_kw='10')
    for i, point in enumerate(raw['points']):
        point.update(load_forecast_kw=100.0 if 32 <= i < 48 or 56 <= i < 72 else 0.0,
            pv_forecast_kw=4.533521 if i == 55 else 0.0,
            tariff_period='gu' if i < 32 else 'feng',
            buy_price_per_kwh=0.2 if i < 32 else 1.0, sell_price_per_kwh=0.1)
    raw['solver_mip_rel_gap'] = 0.0
    return OptimizationRequest.model_validate(raw)


class DailyMinimumActionTests(unittest.TestCase):
    def test_small_surplus_cannot_supply_energy_that_dispatch_will_remove(self):
        request = small_surplus_request()
        result = M4Optimizer(model_version='minimum-action-regression').optimize(request)
        for candidate in result.candidates:
            with self.subTest(profile=candidate.profile_id):
                self.assertIn(candidate.status, ('optimal', 'feasible'), candidate.solver_message)
                validate_candidate(request, candidate)
                self.assertEqual(candidate.plan[55].target_power_kw, 0.0)
                self.assertTrue(all(p.target_power_kw == 0 or p.target_power_kw > 10
                                    for p in candidate.plan))
                validate_dispatch_safety(request.model_dump(mode='json'),
                    dispatch_points([p.model_dump(mode='json') for p in candidate.plan]))

    def test_unversioned_requests_keep_their_original_small_power_behavior(self):
        request = small_surplus_request(minimum=False)
        result = M4Optimizer(model_version='minimum-action-regression').optimize(request)
        cost = next(c for c in result.candidates if c.profile_id == 'cost')
        self.assertIn(cost.status, ('optimal', 'feasible'))
        self.assertGreater(cost.plan[55].target_power_kw, 0)
        self.assertLessEqual(cost.plan[55].target_power_kw, 10)
        with self.assertRaisesRegex(ResultValidationError, 'minimum dispatch power'):
            validate_candidate(small_surplus_request(), cost)

    def test_threshold_is_exclusive_for_charge_and_discharge(self):
        for mode in ('charge', 'discharge'):
            for limit in (10.0, 10.1):
                with self.subTest(mode=mode, limit=limit):
                    raw = make_request().model_dump()
                    start = raw['plan_start_at'].replace(hour=23, minute=45)
                    raw.update(plan_start_at=start, input_observed_at=start, horizon_points=1,
                        source_versions=dict(planning_basis='remaining-day-v1',
                            dispatch_power_policy='minimum-action-v1', minimum_dispatch_power_kw='10'))
                    raw['points'] = [{**raw['points'][0], 'timestamp': start,
                        'tariff_period': 'ping', 'pv_forecast_kw': 0.0}]
                    raw['constraints']['terminal_soc_tolerance_pct'] = 100.0
                    raw['capability']['max_'+mode+'_kw'] = limit
                    built = build_model(OptimizationRequest.model_validate(raw))
                    objective = np.zeros(built.index.size)
                    section = getattr(built.index, mode)
                    objective[section] = -1.0
                    result = solve_milp(built.problem, objective, locks=(), time_limit_seconds=3, mip_rel_gap=0)
                    self.assertEqual(result.status, 'optimal')
                    self.assertAlmostEqual(result.x[section.start], 0.0 if limit == 10 else 10.1)

    def test_unconfirmed_old_policy_cannot_be_adopted(self):
        from m4.tests.test_m4_late_peak_reserve import current_reserve_request
        from m4.settings.daily_policy import matches_current_daily_policy
        from m4.optimizer.contracts import GRID_CHARGING_POLICY
        raw = current_reserve_request().model_dump(mode='json')
        raw['pv_midday_economic'] = True
        raw['source_versions'].update(pv_export_policy='pv-export-flat-tariff-v1',
            grid_charging_policy=GRID_CHARGING_POLICY, pv_export_price=format(0.6, '.17g'))
        for point in raw['points']:
            point['sell_price_per_kwh'] = 0.6
        self.assertTrue(matches_current_daily_policy('station-2', raw))
        raw['source_versions'].pop('dispatch_power_policy')
        raw['source_versions'].pop('minimum_dispatch_power_kw')
        self.assertFalse(matches_current_daily_policy('station-2', raw))

    def test_minimum_action_does_not_silently_relax_storage_priority(self):
        raw = make_request().model_dump()
        start = raw['plan_start_at'].replace(hour=23, minute=45)
        raw.update(plan_start_at=start, input_observed_at=start, horizon_points=1,
            pv_dispatch_policy='load_first_storage_priority',
            source_versions=dict(planning_basis='remaining-day-v1',
                dispatch_power_policy='minimum-action-v1', minimum_dispatch_power_kw='10'))
        raw['points'] = [{**raw['points'][0], 'timestamp': start,
            'tariff_period': 'ping', 'pv_forecast_kw': 104.5}]
        raw['constraints'].update(terminal_soc_tolerance_pct=100.0,
            grid_export_enabled=True, grid_export_limit_kw=100.0)
        built = build_model(OptimizationRequest.model_validate(raw))
        result = solve_milp(built.problem, np.zeros(built.index.size), locks=(),
                            time_limit_seconds=3, mip_rel_gap=0)
        self.assertEqual(result.status, 'infeasible')

    def test_filtered_unsafe_baseline_is_comparable_but_not_recommended(self):
        from m4.settings.daily_comparison import compare_daily_plan, comparison_input_sha256
        from m4.settings.ems_simulation import simulate_ems_day, EMS_BASELINE_POLICY
        request = small_surplus_request()
        request.capability.initial_soc_pct = 6.0
        request.source_versions['baseline_policy'] = EMS_BASELINE_POLICY
        schedule = [dict(start_time='13:45:00', end_time='14:00:00', repeat='daily',
                         mode='charge', power_kw=4.5),
                    dict(start_time='14:00:00', end_time='14:15:00', repeat='daily',
                         mode='discharge', power_kw=20.0)]
        plan, simulation = simulate_ems_day(request.capability, request.constraints,
            request.points, schedule, pv_dispatch_policy=request.pv_dispatch_policy)
        baseline = dict(schema_version='m4-ems-daily-baseline-v1', basis='ems_rule_simulation',
            station_id=request.station_id, input_sha256=comparison_input_sha256(request),
            controls_version='test', controller_version=EMS_BASELINE_POLICY,
            initial_state_version='test', initial_state_at=request.plan_start_at.isoformat(),
            initial_soc_pct=6.0, schedule=schedule, simulation=simulation,
            plan=[p.model_dump(mode='json') for p in plan])
        comparison = compare_daily_plan(request, None, baseline=baseline, controls_version='test')
        self.assertIsNotNone(comparison['baseline'])
        self.assertGreaterEqual(plan[-1].expected_soc_pct, 2.0)
        self.assertEqual(comparison['status'], 'blocked')
        self.assertIsNone(comparison['recommended'])


if __name__ == '__main__':
    unittest.main()
