import unittest
import warnings
from unittest.mock import patch

import numpy as np
from scipy.optimize import OptimizeResult
from scipy.sparse import csr_matrix

import m4_optimizer.solver as solver_module
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

    def test_success_status_with_nonzero_mip_gap_is_only_feasible(self):
        fake = OptimizeResult(
            status=0,
            x=np.array([0.5]),
            message="gap target reached",
            mip_gap=1e-4,
        )

        with patch("m4_optimizer.solver.milp", return_value=fake):
            result = solve_milp(
                self.make_one_variable_problem(), np.array([1.0]), (), 2.0, 0.01
            )

        self.assertEqual(result.status, "feasible")
        self.assertEqual(result.mip_gap, 1e-4)

    def test_success_status_with_zero_or_near_zero_mip_gap_is_optimal(self):
        threshold = solver_module.PROVEN_OPTIMAL_MIP_GAP_TOLERANCE
        for mip_gap in (0.0, threshold / 2.0):
            with self.subTest(mip_gap=mip_gap):
                fake = OptimizeResult(
                    status=0,
                    x=np.array([0.5]),
                    message="optimal",
                    mip_gap=mip_gap,
                )
                with patch("m4_optimizer.solver.milp", return_value=fake):
                    result = solve_milp(
                        self.make_one_variable_problem(),
                        np.array([1.0]),
                        (),
                        2.0,
                        0.01,
                    )
                self.assertEqual(result.status, "optimal")

    def test_only_the_expected_scipy_passthrough_warning_is_suppressed(self):
        targeted = (
            "Unrecognized options detected: {'mip_feasibility_tolerance'}. "
            "These will be passed to HiGHS verbatim."
        )
        combined = (
            "Unrecognized options detected: {'other_option', "
            "'mip_feasibility_tolerance'}. These will be passed to HiGHS verbatim."
        )
        related = "mip_feasibility_tolerance behavior changed"
        fake = OptimizeResult(
            status=0,
            x=np.array([0.0]),
            message="optimal",
            mip_gap=0.0,
        )

        def warning_milp(**kwargs):
            warnings.warn(targeted, RuntimeWarning, stacklevel=2)
            warnings.warn(combined, RuntimeWarning, stacklevel=2)
            warnings.warn(related, RuntimeWarning, stacklevel=2)
            return fake

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch("m4_optimizer.solver.milp", side_effect=warning_milp):
                solve_milp(
                    self.make_one_variable_problem(),
                    np.array([1.0]),
                    (),
                    2.0,
                    0.0,
                )

        self.assertEqual([str(item.message) for item in caught], [combined, related])

    def make_one_variable_problem(self):
        return MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix((0, 1)),
            constraint_lower=np.array([], dtype=float),
            constraint_upper=np.array([], dtype=float),
        )
