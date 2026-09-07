import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import numpy as np
from pydantic import ValidationError

from m4_optimizer.contracts import OptimizationRequest
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.model import build_model
from m4_optimizer.service import M4Optimizer
from m4_optimizer.solver import RawSolveResult, solve_milp
from m4_optimizer.validation import validate_candidate
from m4_optimizer_test_support import make_request, layer


def valley_request(start_hour=0, early=True):
    data = make_request().model_dump()
    start = data['plan_start_at'] + timedelta(hours=start_hour)
    data['plan_start_at'] = start
    data['input_observed_at'] = start - timedelta(minutes=5)
    for index, point in enumerate(data['points']):
        timestamp = start + timedelta(minutes=15 * index)
        point.update(timestamp=timestamp, load_forecast_kw=80.0, pv_forecast_kw=0.0,
                     buy_price_per_kwh=0.2 if timestamp.hour < 8 else 1.6,
                     tariff_period='gu' if timestamp.hour < 8 else 'feng')
    data['capability'].update(initial_soc_pct=10.0, charge_efficiency=1.0,
                              discharge_efficiency=1.0)
    data['constraints'].update(preferred_soc_min_pct=10.0, preferred_soc_max_pct=90.0,
                               terminal_soc_tolerance_pct=0.0, demand_limit_kw=300.0,
                               grid_import_limit_kw=400.0, cycle_cost_per_kwh=0.0)
    data['solver_mip_rel_gap'] = 0.0
    if early:
        for profile in data['profiles']:
            profile['objective_order'].append(
                layer('early-valley', {'valley_charge_delay': 1.0}).model_dump())
    return OptimizationRequest.model_validate(data)


class EarlyValleyContractTests(unittest.TestCase):
    def test_legacy_requests_remain_valid_and_do_not_infer_valley_from_price(self):
        request = make_request()
        self.assertTrue(all(point.tariff_period is None for point in request.points))
        built = build_model(request)
        self.assertEqual(built.valley_charge_windows, ())

    def test_new_preference_is_optional_and_final_only(self):
        request = valley_request()
        OptimizationRequest.model_validate(request.model_dump())
        for change in ('earlier', 'duplicate', 'mixed'):
            with self.subTest(change=change):
                data = request.model_dump()
                order = data['profiles'][0]['objective_order']
                if change == 'earlier':
                    order.insert(2, order.pop())
                elif change == 'duplicate':
                    order.append(order[-1].copy())
                else:
                    order[-2]['terms']['valley_charge_delay'] = 1.0
                    order.pop()
                with self.assertRaises(ValidationError):
                    OptimizationRequest.model_validate(data)

    def test_rolling_window_keeps_natural_days_separate(self):
        built = build_model(valley_request(start_hour=3))
        self.assertEqual(built.valley_charge_windows,
                         (tuple(range(20)), tuple(range(84, 96))))

    def test_only_midnight_connected_same_price_gu_is_eligible(self):
        request = valley_request()
        for index, point in enumerate(request.points):
            if 8 <= index < 32:
                point.buy_price_per_kwh = 0.3
            if 48 <= index < 56:
                point.tariff_period = 'gu'
                point.buy_price_per_kwh = 0.2
        self.assertEqual(build_model(request).valley_charge_windows,
                         (tuple(range(8)), tuple(range(8, 32))))

    def test_uniform_or_incomplete_labels_do_not_create_overnight_window(self):
        for label in ('ping', 'gu', None):
            request = valley_request()
            for point in request.points:
                point.tariff_period = label
                point.buy_price_per_kwh = 0.2
            self.assertEqual(build_model(request).valley_charge_windows, ())
        request = valley_request()
        request.points[10].tariff_period = None
        self.assertEqual(build_model(request).valley_charge_windows, ())


class EarlyValleySolveTests(unittest.TestCase):
    def test_rolling_horizon_preserves_each_days_charge_and_existing_pv_priority(self):
        request = valley_request(start_hour=3)
        for point in request.points:
            if 10 <= point.timestamp.hour < 15:
                point.pv_forecast_kw = 180.0
        built = build_model(request)
        profile = request.profiles[2]
        calls = []
        def record(*args):
            result = solve_milp(*args)
            calls.append(result)
            return result
        with patch('m4_optimizer.lexicographic.solve_milp', side_effect=record):
            result = solve_profile(built, profile, 30.0, 0.0)
        self.assertEqual(result.status, 'optimal', result.message)
        before, after = calls[-2].x, result.x
        for window in built.valley_charge_windows:
            columns = [built.index.charge.start + t for t in window]
            self.assertAlmostEqual(sum(after[columns]), sum(before[columns]), places=5)
        np.testing.assert_allclose(after[built.index.discharge],
                                   before[built.index.discharge], atol=1e-6)
        np.testing.assert_allclose(after[built.index.charge][20:84],
                                   before[built.index.charge][20:84], atol=1e-6)
        self.assertLessEqual(float(built.objectives['pv_unused'] @ after),
                             float(built.objectives['pv_unused'] @ before) + 1e-6)

    def test_all_profiles_frontload_charge_without_changing_other_actions(self):
        request = valley_request()
        built = build_model(request)
        for profile in request.profiles:
            with self.subTest(profile=profile.profile_id):
                calls = []
                def record(*args):
                    result = solve_milp(*args)
                    calls.append(result)
                    return result
                with patch('m4_optimizer.lexicographic.solve_milp', side_effect=record):
                    result = solve_profile(built, profile, 30.0, 0.0)
                self.assertEqual(result.status, 'optimal', result.message)
                before, after = calls[-2].x, result.x
                self.assertAlmostEqual(after[built.index.charge.start], 100.0, places=5)
                np.testing.assert_allclose(after[built.index.discharge],
                                           before[built.index.discharge], atol=1e-6)
                np.testing.assert_allclose(after[built.index.charge][32:],
                                           before[built.index.charge][32:], atol=1e-6)
                self.assertAlmostEqual(sum(after[built.index.charge][:32]),
                                       sum(before[built.index.charge][:32]), places=5)
                for objective in profile.objective_order[:-1]:
                    vector = sum(weight * built.objectives[name]
                                 for name, weight in objective.terms.items())
                    optimum = next(item for item in result.layers if item.name == objective.name)
                    self.assertLessEqual(float(vector @ after),
                                         optimum.best_value + optimum.lock_tolerance + 1e-7)

    def test_midnight_charge_respects_grid_headroom_and_soc(self):
        request = valley_request()
        request.constraints.grid_import_limit_kw = 120.0
        request.points[0].load_forecast_kw = 110.0
        result = M4Optimizer(model_version='test-early').optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
            self.assertEqual(candidate.plan[0].mode, 'charge')
            self.assertAlmostEqual(candidate.plan[0].target_power_kw, 10.0, places=5)
            self.assertAlmostEqual(candidate.plan[1].target_power_kw, 40.0, places=5)
            validate_candidate(request, candidate)

    def test_early_layer_timeout_preserves_previous_valid_plan_and_reports_preference(self):
        request = valley_request()
        def timeout_early(problem, objective, locks, seconds, gap):
            # The new vector only contains charge-delay coefficients.
            built = build_model(request)
            if np.array_equal(objective, built.objectives['valley_charge_delay']):
                return RawSolveResult('timeout', None, None, 'early layer time limit', None)
            return solve_milp(problem, objective, locks, seconds, gap)
        with patch('m4_optimizer.lexicographic.solve_milp', side_effect=timeout_early):
            result = M4Optimizer(model_version='test-early').optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'feasible')
            self.assertIn('EARLY_VALLEY_PREFERENCE_INCOMPLETE', candidate.risk_codes)
            self.assertNotIn('early-valley', [item.name for item in candidate.layers])
            validate_candidate(request, candidate)

    def test_early_layer_feasible_result_does_not_claim_optimal_preference(self):
        request = valley_request()
        built = build_model(request)
        def feasible_early(problem, objective, locks, seconds, gap):
            raw = solve_milp(problem, objective, locks, seconds, gap)
            if np.array_equal(objective, built.objectives['valley_charge_delay']):
                self.assertEqual(gap, 0.0)
                return replace(raw, status='feasible', mip_gap=0.01)
            return raw
        request.solver_mip_rel_gap = 0.01
        with patch('m4_optimizer.lexicographic.solve_milp', side_effect=feasible_early):
            result = M4Optimizer(model_version='test-early').optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'feasible')
            self.assertIn('EARLY_VALLEY_PREFERENCE_INCOMPLETE', candidate.risk_codes)
            validate_candidate(request, candidate)

    def test_prior_feasible_layer_does_not_hide_completed_early_preference(self):
        request = valley_request()
        built = build_model(request)
        def feasible_prior(problem, objective, locks, seconds, gap):
            raw = solve_milp(problem, objective, locks, seconds, gap)
            if np.array_equal(objective, built.objectives['demand_peak']):
                return replace(raw, status='feasible', mip_gap=0.01)
            return raw
        with patch('m4_optimizer.lexicographic.solve_milp', side_effect=feasible_prior):
            result = M4Optimizer(model_version='test-early').optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.status, 'feasible')
            self.assertNotIn('EARLY_VALLEY_PREFERENCE_INCOMPLETE', candidate.risk_codes)
            self.assertAlmostEqual(candidate.plan[0].target_power_kw, 100.0, places=5)


if __name__ == '__main__':
    unittest.main()
