import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pyomo.environ as pyo
from pyomo.common.collections import ComponentMap
from pyomo.contrib.appsi.base import TerminationCondition
from scipy.sparse import csr_matrix

import m4.optimizer.solver as solver_module
from m4.optimizer.model import build_model
from m4.optimizer.solver import MilpProblem, ObjectiveLock, solve_milp
from m4.tests.m4_optimizer_test_support import make_request


class M4OptimizerSolverTests(unittest.TestCase):
    def test_zero_objective_locks_preserve_feasibility_and_other_locks(self):
        for bound, expected in ((0.0, 'optimal'), (1e-6, 'optimal'), (-1e-6, 'infeasible')):
            with self.subTest(bound=bound):
                result = solve_milp(self.make_one_variable_problem(), np.array([-1.0]),
                    (ObjectiveLock(np.zeros(1), bound),
                     ObjectiveLock(np.ones(1), 0.4)), 2.0, 0.0)
                self.assertEqual(result.status, expected)
                if expected == 'optimal':
                    np.testing.assert_allclose(result.x, [0.4], atol=1e-7)
                else:
                    self.assertIsNone(result.x)

    def test_solver_finds_the_cheapest_binary_choice(self):
        problem = MilpProblem(
            integrality=np.array([1, 1], dtype=np.uint8),
            lower_bounds=np.array([0.0, 0.0]),
            upper_bounds=np.array([1.0, 1.0]),
            matrix=csr_matrix([[1.0, 1.0]]),
            constraint_lower=np.array([1.0]),
            constraint_upper=np.array([np.inf]),
        )
        result = solve_milp(problem, np.array([1.0, 2.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "optimal")
        np.testing.assert_allclose(result.x, [1.0, 0.0], atol=1e-7)

    def test_solver_maps_an_infeasible_problem(self):
        problem = MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix([[1.0]]),
            constraint_lower=np.array([2.0]),
            constraint_upper=np.array([np.inf]),
        )
        result = solve_milp(problem, np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.x)

    def solve_mock(self, termination, values, best=0.5, bound=0.5, problem=None):
        backend = SimpleNamespace(config=SimpleNamespace(), highs_options={})
        result = SimpleNamespace(termination_condition=termination,
                                 best_feasible_objective=best,
                                 best_objective_bound=bound)

        def solve(model):
            backend.variables = tuple(model.component_data_objects(pyo.Var))
            return result

        backend.solve = solve
        backend.set_instance = lambda model: None
        backend.get_primals = lambda variables: ComponentMap(zip(variables, values))
        with patch("m4.optimizer.solver.Highs", return_value=backend) as factory:
            solved = solve_milp(problem or self.make_one_variable_problem(),
                                np.array([1.0]), (), 2.0, 0.01)
        factory.assert_called_once_with(only_child_vars=True)
        self.assertFalse(backend.config.load_solution)
        self.assertLessEqual(backend.config.time_limit, 2.0)
        self.assertEqual(backend.config.mip_gap, 0.01)
        self.assertEqual(backend.highs_options["mip_feasibility_tolerance"], 1e-9)
        return solved

    def test_time_limited_finite_incumbent_is_feasible(self):
        result = self.solve_mock(TerminationCondition.maxTimeLimit, [0.5], bound=0.4)
        self.assertEqual(result.status, "feasible")
        self.assertAlmostEqual(result.objective_value, 0.5)
        self.assertAlmostEqual(result.mip_gap, 0.2)

    def test_time_limit_without_an_incumbent_is_timeout(self):
        result = self.solve_mock(TerminationCondition.maxTimeLimit, [], best=None)
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.x)

    def test_success_with_nonzero_gap_is_only_feasible(self):
        result = self.solve_mock(TerminationCondition.optimal, [0.5], bound=0.49)
        self.assertEqual(result.status, "feasible")
        self.assertAlmostEqual(result.mip_gap, 0.02)

    def test_success_with_zero_or_near_zero_gap_is_optimal(self):
        threshold = solver_module.PROVEN_OPTIMAL_MIP_GAP_TOLERANCE
        for gap in (0.0, threshold / 2.0):
            with self.subTest(gap=gap):
                result = self.solve_mock(TerminationCondition.optimal, [0.5],
                                         bound=0.5 * (1.0 - gap))
                self.assertEqual(result.status, "optimal")

    def test_success_without_finite_bound_does_not_prove_optimality(self):
        for bound in (None, float("nan"), float("inf")):
            with self.subTest(bound=bound):
                result = self.solve_mock(TerminationCondition.optimal, [0.5], bound=bound)
                self.assertEqual(result.status, "feasible")

    def test_optimal_without_a_finite_incumbent_is_error(self):
        for values, best in (([float("nan")], 0.5), ([float("inf")], 0.5), ([], None)):
            with self.subTest(values=values):
                result = self.solve_mock(TerminationCondition.optimal, values, best=best)
                self.assertEqual(result.status, "error")
                self.assertIsNone(result.x)

    def test_infeasible_and_error_terminations_ignore_backend_values(self):
        for termination, expected in ((TerminationCondition.infeasible, "infeasible"),
                                      (TerminationCondition.unbounded, "error"),
                                      (TerminationCondition.error, "error"),
                                      (TerminationCondition.unknown, "error")):
            with self.subTest(termination=termination):
                result = self.solve_mock(termination, [0.5])
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.x)

    def test_native_model_keeps_no_old_solution_or_previous_objective_locks(self):
        built = build_model(make_request())
        first = solve_milp(built.problem, built.objectives["throughput"], (), 2.0, 0.0)
        self.assertEqual(first.status, "optimal")
        impossible = ObjectiveLock(built.objectives["throughput"], -1.0)
        second = solve_milp(built.problem, built.objectives["throughput"], (impossible,), 2.0, 0.0)
        self.assertEqual(second.status, "infeasible")
        self.assertIsNone(second.x)
        third = solve_milp(built.problem, built.objectives["throughput"], (), 2.0, 0.0)
        self.assertEqual(third.status, "optimal")
        self.assertTrue(all(var.value is None for var in built.problem.variables))
        self.assertFalse(hasattr(built.problem.model, "objective_locks"))

    def test_parallel_requests_do_not_share_models_or_solutions(self):
        def run(objective):
            return solve_milp(self.make_one_variable_problem(), np.array([objective]), (), 2.0, 0.0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            low, high = list(pool.map(run, (1.0, -1.0)))
        self.assertEqual((low.status, high.status), ("optimal", "optimal"))
        np.testing.assert_allclose(low.x, [0.0])
        np.testing.assert_allclose(high.x, [1.0])

    def test_exhausted_budget_does_not_start_backend(self):
        with patch("m4.optimizer.solver.Highs") as factory:
            result = solve_milp(self.make_one_variable_problem(), np.array([1.0]), (), 0.0, 0.0)
        self.assertEqual(result.status, "timeout")
        factory.assert_not_called()

    def test_model_translation_consumes_the_remaining_budget(self):
        clock = [0.0]
        with (
            patch("m4.optimizer.solver.monotonic", side_effect=lambda: clock[0]),
            patch("m4.optimizer.solver.Highs") as factory,
        ):
            backend = factory.return_value
            backend.set_instance.side_effect = lambda model: clock.__setitem__(0, 3.0)
            result = solve_milp(self.make_one_variable_problem(), np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "timeout")
        backend.set_instance.assert_called_once()
        backend.solve.assert_not_called()

    def make_one_variable_problem(self):
        return MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix((0, 1)),
            constraint_lower=np.array([], dtype=float),
            constraint_upper=np.array([], dtype=float),
        )
