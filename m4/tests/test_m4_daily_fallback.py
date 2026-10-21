"""Economic rejection must not remove an independently safe EMS fallback."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from m4.optimizer.contracts import CandidateResult, LayerResult, OptimizationRequest
from m4.optimizer.metrics import calculate_metrics
from m4.settings.daily_comparison import compare_daily_plan, comparison_input_sha256
from m4.settings.daily_fallback import POLICY
from m4.settings.ems_simulation import EMS_BASELINE_POLICY, simulate_ems_day
from m4.settings.objectives import get_daily_profiles
from m4.tests.m4_optimizer_test_support import make_request


def day_fixture(*, marked=True, peak_price=1.0, load_kw=20.0):
    raw = make_request().model_dump()
    raw['station_id'] = 'station-2'
    raw['profiles'] = [p.model_dump() for p in get_daily_profiles('station-2')]
    raw['capability'].update(initial_soc_pct=98.0, energy_capacity_kwh=100.0,
        charge_efficiency=1.0, discharge_efficiency=1.0,
        max_charge_kw=100.0, max_discharge_kw=20.0)
    raw['constraints'].update(soc_min_pct=1.0, soc_max_pct=98.0,
        preferred_soc_min_pct=2.0, preferred_soc_max_pct=98.0,
        terminal_soc_tolerance_pct=0.0, demand_limit_kw=1000.0,
        grid_import_limit_kw=1000.0, cycle_cost_per_kwh=0.0)
    raw['peak_reserve_policy'] = dict(version='peak-reserve-v4', terminal_soc_min_pct=2.0)
    raw['source_versions'].update(terminal_policy='daily-operating-floor-v1',
        terminal_inventory_price='0.2', daily_policy='m4-daily-operating-floor-v1',
        baseline_policy=EMS_BASELINE_POLICY)
    if marked:
        raw['source_versions']['daily_fallback_policy'] = POLICY
    for i, p in enumerate(raw['points']):
        p.update(load_forecast_kw=load_kw, pv_forecast_kw=0.0,
            tariff_period='gu' if i < 32 else 'feng' if 56 <= i < 72 else 'ping',
            buy_price_per_kwh=0.2 if i < 32 else peak_price if 56 <= i < 72 else 0.5,
            sell_price_per_kwh=0.1)
    request = OptimizationRequest.model_validate(raw)
    schedule = [dict(start_time=a, end_time=b, mode='discharge', power_kw=20.0,
        repeat='daily', soc_min_pct=1.0, soc_max_pct=98.0)
        for a, b in [('08:00:00', '12:00:00'), ('14:00:00', '18:00:00')]]
    plan, simulation = simulate_ems_day(request.capability, request.constraints,
        request.points, schedule, pv_dispatch_policy=request.pv_dispatch_policy)
    baseline = dict(schema_version='m4-ems-daily-baseline-v1', station_id='station-2',
        basis='ems_rule_simulation', controller_version=EMS_BASELINE_POLICY,
        input_sha256=comparison_input_sha256(request), controls_version='controls-test',
        initial_state_version='midnight-test', initial_state_at=request.plan_start_at.isoformat(),
        initial_soc_pct=98.0, schedule=schedule, simulation=simulation,
        plan=[p.model_dump(mode='json') for p in plan])
    optimized_schedule = [dict(row, soc_min_pct=2.0) for row in schedule]
    optimized_schedule[0].update(start_time='19:00:00', end_time='21:00:00')
    plan, _ = simulate_ems_day(request.capability, request.constraints, request.points,
        optimized_schedule, pv_dispatch_policy=request.pv_dispatch_policy)
    candidate = CandidateResult(profile_id='cost', profile_version='test',
        plan_version='optimized-test', status='feasible', solver_message='fixture',
        solve_seconds=0.0, plan=plan, metrics=calculate_metrics(request, plan),
        layers=[LayerResult(name='peak-reserve-shortfall', best_value=0.0, lock_tolerance=1e-6)],
        risk_codes=[], risk_messages=[])
    return request, baseline, candidate


class DailyFallbackTests(unittest.TestCase):
    def compare(self, request, baseline, candidate):
        return compare_daily_plan(request, candidate, baseline=baseline,
            controls_version='controls-test', terminal_soc_target_pct=2.0)

    def test_below_gate_uses_safe_plan_without_changing_benchmark_or_candidate_saving(self):
        request, baseline, candidate = day_fixture()
        original = deepcopy(baseline)
        result = self.compare(request, baseline, candidate)
        self.assertEqual(baseline, original)
        self.assertEqual(result['recommended_source'], 'ems_fallback')
        self.assertEqual(result['fallback_status'], 'ready')
        self.assertEqual(result['optimization_status'], 'not_adopted')
        self.assertIn('100元', result['optimization_reason'])
        self.assertGreater(result['net_savings_yuan'], 0)
        self.assertLess(result['net_savings_yuan'], 100)
        self.assertAlmostEqual(result['baseline']['metrics']['terminal_soc_pct'], 1.0)
        self.assertGreaterEqual(result['recommended']['metrics']['terminal_soc_pct'], 2.0-1e-5)
        self.assertTrue(all(p['target_power_kw'] == 0 or p['target_power_kw'] > 10
            for p in result['recommended']['plan']))
        self.assertGreater(result['recommended']['metrics']['energy_cost'], result['baseline_cost_yuan'])
        self.assertAlmostEqual(result['optimized_cost_yuan'], candidate.metrics.energy_cost)

    def test_above_gate_still_selects_optimizer(self):
        request, baseline, candidate = day_fixture(peak_price=3.0)
        result = self.compare(request, baseline, candidate)
        self.assertGreaterEqual(result['net_savings_yuan'], 100)
        self.assertEqual(result['recommended_source'], 'optimized')
        self.assertEqual(result['optimization_status'], 'adopted')
        self.assertEqual(result['fallback_status'], 'not_needed')

    def test_missing_candidate_does_not_bypass_fallback_demand_safety(self):
        request, baseline, _ = day_fixture(load_kw=2000.0)
        result = self.compare(request, baseline, None)
        self.assertEqual(result['fallback_status'], 'blocked')
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['recommended'])
        self.assertTrue(result['optimization_reason'])
        self.assertIn('暂无可下发方案', result['fallback_reason'])

    def test_unmarked_history_keeps_original_comparison_shape(self):
        request, baseline, candidate = day_fixture(marked=False)
        result = self.compare(request, baseline, candidate)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['recommended'])
        self.assertNotIn('fallback_status', result)
        self.assertNotIn('optimization_reason', result)

    def test_safe_small_discharge_tail_is_filtered_and_its_soc_is_recomputed(self):
        from m4.settings.daily_fallback import build_fallback
        request, baseline, _ = day_fixture()
        request.source_versions.update(dispatch_power_policy='minimum-action-v1',
            minimum_dispatch_power_kw='10')
        baseline['input_sha256'] = comparison_input_sha256(request)
        result = build_fallback(request, baseline)
        self.assertEqual(result['plan'][59]['mode'], 'idle')
        self.assertEqual(result['plan'][59]['target_power_kw'], 0)
        self.assertAlmostEqual(result['plan'][-1]['expected_soc_pct'], 3.0)
        self.assertAlmostEqual(result['metrics']['terminal_soc_pct'], 3.0)

    def test_final_optimizer_safety_rejection_uses_fallback_and_is_not_adopted(self):
        from m4.settings.ems_model_update import validate_dispatch_safety
        from m4.settings.daily_fallback import _filtered_plan
        request, baseline, candidate = day_fixture(peak_price=3.0)
        request.source_versions.update(dispatch_power_policy='minimum-action-v1',
            minimum_dispatch_power_kw='10')
        baseline['input_sha256'] = comparison_input_sha256(request)
        candidate.plan = _filtered_plan(request,
            [p.model_dump(mode='json') for p in candidate.plan])
        candidate.metrics = calculate_metrics(request, candidate.plan)
        optimizer_points = [p.model_dump(mode='json') for p in candidate.plan]

        def final_check(raw, points):
            if points == optimizer_points:
                raise ValueError('optimizer final safety rejected')
            return validate_dispatch_safety(raw, points)

        with patch('m4.settings.ems_model_update.validate_dispatch_safety', side_effect=final_check):
            result = self.compare(request, baseline, candidate)
        self.assertGreaterEqual(result['net_savings_yuan'], 100)
        self.assertEqual(result['optimization_status'], 'not_adopted')
        self.assertIn('下发安全', result['optimization_reason'])
        self.assertEqual(result['recommended_source'], 'ems_fallback')
        self.assertEqual(result['fallback_status'], 'ready')
