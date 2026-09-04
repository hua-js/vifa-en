import unittest
from unittest.mock import patch

import numpy as np
from scipy.optimize import OptimizeResult
from scipy.sparse import csr_matrix

from m4_optimizer.solver import MilpProblem, solve_milp


class M4OptimizerSolverTests(unittest.TestCase):
    def test_solver_finds_the_cheapest_binary_choice(self):
        problem = MilpProblem(
            integrality=np.array([1, 1], dtype=np.uint8),
            lower_bounds=np.array([0.0, 0.0]),
            upper_bounds=np.array([1.0, 1.0]),
            matrix=csr_matrix([[1.0, 1.0]]),
            constraint_lower=np.array([1.0]),
            constraint_upper=np.array([np.inf]),
        )
        result = solve_milp(
            problem,
            objective=np.array([1.0, 2.0]),
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
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
        result = solve_milp(
            problem,
            objective=np.array([1.0]),
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.x)

    def test_time_limited_finite_incumbent_is_feasible(self):
        problem = self.make_one_variable_problem()
        fake = OptimizeResult(
            status=1,
            x=np.array([0.5]),
            message="time limit",
            mip_gap=0.2,
        )
        with patch("m4_optimizer.solver.milp", return_value=fake):
            result = solve_milp(problem, np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "feasible")
        self.assertAlmostEqual(result.objective_value, 0.5)

    def test_time_limit_without_an_incumbent_is_timeout(self):
        problem = self.make_one_variable_problem()
        fake = OptimizeResult(status=1, x=None, message="time limit")
        with patch("m4_optimizer.solver.milp", return_value=fake):
            result = solve_milp(problem, np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.x)

    def make_one_variable_problem(self):
        return MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix((0, 1)),
            constraint_lower=np.array([], dtype=float),
            constraint_upper=np.array([], dtype=float),
        )
