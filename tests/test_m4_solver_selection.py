import importlib
import itertools
import unittest
from fractions import Fraction
from unittest.mock import patch

import numpy as np

from m4_optimizer.solver import RawSolveResult
from m4_optimizer.metrics import calculate_metrics
from m4_selection import select_candidate
from test_m4_selection import fixture, policy


class SolverSelectionTests(unittest.TestCase):
    def implementation(self):
        spec = importlib.util.find_spec('m4_selection.solver_selection')
        self.assertIsNotNone(spec, 'M4 requires a solver decision entry point')
        return importlib.import_module('m4_selection.solver_selection')

    def test_solver_respects_cost_soc_and_profile_policy(self):
        module = self.implementation()
        request, result = fixture(powers={'cost': {0: -5.0, 95: 5.0}})
        for metric, expected in [('energy_cost', 'cost'),
                                 ('preferred_soc_deviation', 'balanced'),
                                 ('profile_priority', 'balanced')]:
            with self.subTest(metric=metric):
                answer = module.select_candidate_with_solver(request, result,
                    policy(request, metric=metric, metric_tolerance=0.0))
                self.assertEqual(answer.selected.profile_id, expected)
                self.assertEqual(answer.selector_version, 'pyomo-highs-selection-v2')
                self.assertEqual(answer.dispatch_status, 'not_dispatched')

    def test_decimal_tolerance_keeps_boundary_without_chained_ties(self):
        module = self.implementation()
        request, result = fixture(export=True,
            unused={'cost': {0: 0.000004}, 'pv': {0: 0.000008}})
        answer = module.select_candidate_with_solver(request, result,
            policy(request, tie_order=['pv', 'cost', 'balanced']))
        self.assertEqual(answer.selected.profile_id, 'cost')
        self.assertEqual(answer.steps[-1].remaining_ids, ['balanced', 'cost'])

    def test_both_demand_layers_override_preferred_profile(self):
        module = self.implementation()
        request, result = fixture(powers={
            'balanced': {0: 8.0, 95: -8.0},
            'cost': {0: 4.0, 1: 4.0, 95: -8.0},
            'pv': {0: 4.0, 95: -4.0},
        })
        request.constraints.demand_limit_kw = 100.0
        for candidate in result.candidates:
            for point in candidate.plan:
                point.demand_exceed_kw = max(point.grid_import_kw - 100.0, 0.0)
            candidate.metrics = calculate_metrics(request, candidate.plan)
        answer = module.select_candidate_with_solver(request, result,
            policy(request, metric='profile_priority', metric_tolerance=0.0,
                   demand_peak_tolerance_kw=0.0, demand_energy_tolerance_kwh=0.0))
        self.assertEqual(answer.steps[0].remaining_ids, ['cost', 'pv'])
        self.assertEqual(answer.steps[1].remaining_ids, ['pv'])
        self.assertEqual(answer.selected.profile_id, 'pv')

    def test_invalid_preferred_candidate_is_excluded(self):
        module = self.implementation()
        request, result = fixture()
        result.candidates[0].plan[0].expected_soc_pct += 1.0
        answer = module.select_candidate_with_solver(request, result,
            policy(request, metric='profile_priority', metric_tolerance=0.0))
        self.assertEqual(answer.selected.profile_id, 'cost')
        self.assertEqual(answer.excluded[0].profile_id, 'balanced')

    def test_policy_and_device_blocks_do_not_require_solver(self):
        module = self.implementation()
        request, result = fixture()
        with patch.object(module, 'solve_pyomo_model', side_effect=AssertionError('must not solve')):
            self.assertEqual(module.select_candidate_with_solver(request, result).status,
                             'pending_policy')
            request.capability.available = False
            self.assertEqual(module.select_candidate_with_solver(request, result,
                policy(request)).status, 'device_unavailable')

    def test_nonoptimal_or_invalid_solver_answer_cannot_select(self):
        module = self.implementation()
        for status, x in [('timeout', None), ('feasible', np.array([1., 0., 0.])),
                          ('optimal', np.array([.5, .5, 0.])),
                          ('optimal', np.array([0., 0., 1.]))]:
            with self.subTest(status=status, x=x):
                raw = RawSolveResult(status=status, x=x, objective_value=0.0,
                                     message='controlled termination', mip_gap=0.0)
                with patch.object(module, 'solve_pyomo_model', return_value=raw):
                    with self.assertRaises(ValueError):
                        module._solve_minimum({'balanced': Fraction(0),
                                              'cost': Fraction(1), 'pv': Fraction(2)})

    def test_ordinal_model_handles_large_and_close_finite_values(self):
        module = self.implementation()
        minimum = module._solve_minimum({
            'balanced': Fraction('1e300'), 'cost': Fraction('1e-300'),
            'pv': Fraction('-1e300'),
        })
        self.assertEqual(minimum, 'pv')
        minimum = module._solve_minimum({
            'balanced': Fraction('0.10000000000000001'),
            'cost': Fraction('0.1'), 'pv': Fraction('0.10000000000000002'),
        })
        self.assertEqual(minimum, 'cost')

    def test_all_preference_and_tie_orders_match_independent_baseline(self):
        module = self.implementation()
        request, result = fixture(powers={'cost': {0: -5.0, 95: 5.0}})
        for metric, order in itertools.product(
                ['profile_priority', 'energy_cost', 'preferred_soc_deviation',
                 'pv_unabsorbed_energy_kwh'], itertools.permutations(['balanced', 'cost', 'pv'])):
            config = policy(request, metric=metric, metric_tolerance=0.0, tie_order=list(order))
            expected = select_candidate(request, result, config)
            actual = module.select_candidate_with_solver(request, result, config)
            self.assertEqual(actual.selected, expected.selected)
            self.assertEqual(actual.steps, expected.steps)


if __name__ == '__main__':
    unittest.main()
