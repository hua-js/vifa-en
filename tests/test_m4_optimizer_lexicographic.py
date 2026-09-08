import unittest
from unittest.mock import patch

import numpy as np
from scipy.sparse import csr_matrix

from m4_optimizer.contracts import ObjectiveLayer, ObjectiveProfile
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.model import BuiltModel, VariableIndex
from m4_optimizer.solver import MilpProblem, RawSolveResult


class M4OptimizerLexicographicTests(unittest.TestCase):
    def test_zero_tolerance_preserves_primary_optimum(self):
        built = self.make_two_variable_model()
        profile = ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )
        result = solve_profile(built, profile, 2.0, 0.0)
        np.testing.assert_allclose(result.x, [0.0, 10.0], atol=1e-7)

    def test_absolute_tolerance_caps_primary_tradeoff(self):
        built = self.make_two_variable_model()
        profile = ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=1.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )
        result = solve_profile(built, profile, 2.0, 0.0)
        self.assertLessEqual(result.x[0], 1.0 + 1e-7)
        self.assertAlmostEqual(result.x[1], 9.0, places=6)

    def test_error_with_finite_incumbent_does_not_continue_to_next_layer(self):
        built = self.make_two_variable_model()
        profile = ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )
        error_with_x = RawSolveResult(
            status="error",
            x=np.array([0.0, 10.0]),
            objective_value=0.0,
            message="solver error",
            mip_gap=None,
        )
        later_optimal = RawSolveResult(
            status="optimal",
            x=np.array([0.0, 10.0]),
            objective_value=10.0,
            message="optimal",
            mip_gap=0.0,
        )
        with patch(
            "m4_optimizer.lexicographic.solve_milp",
            side_effect=[error_with_x, later_optimal],
        ) as solve:
            result = solve_profile(built, profile, 2.0, 0.0)

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.x)
        solve.assert_called_once()

    def test_feasible_layer_keeps_final_status_conservative(self):
        built = self.make_two_variable_model()
        profile = self.make_two_layer_profile()
        first = self.raw_result("feasible", np.array([0.0, 10.0]), "gap reached")
        second = self.raw_result("optimal", np.array([0.0, 10.0]), "optimal")

        with patch(
            "m4_optimizer.lexicographic.solve_milp", side_effect=[first, second]
        ):
            result = solve_profile(built, profile, 2.0, 0.01)

        self.assertEqual(result.status, "feasible")
        np.testing.assert_allclose(result.x, second.x)
        self.assertEqual(len(result.layers), 2)

    def test_total_time_exhaustion_between_layers_returns_last_incumbent(self):
        built = self.make_two_variable_model()
        profile = self.make_two_layer_profile()
        first = self.raw_result("optimal", np.array([0.0, 10.0]), "optimal")

        with (
            patch("m4_optimizer.lexicographic.solve_milp", return_value=first) as solve,
            patch(
                "m4_optimizer.lexicographic.monotonic",
                side_effect=[0.0, 0.0, 2.0, 2.0],
            ),
        ):
            result = solve_profile(built, profile, 1.0, 0.0)

        self.assertEqual(result.status, "feasible")
        np.testing.assert_allclose(result.x, first.x)
        self.assertEqual(len(result.layers), 1)
        self.assertIn("partial", result.message)
        self.assertIn("time limit", result.message)
        solve.assert_called_once()

    def test_later_timeout_without_x_returns_last_incumbent(self):
        built = self.make_two_variable_model()
        profile = self.make_two_layer_profile()
        first = self.raw_result("optimal", np.array([0.0, 10.0]), "optimal")
        timeout = self.raw_result("timeout", None, "secondary time limit")

        with patch(
            "m4_optimizer.lexicographic.solve_milp", side_effect=[first, timeout]
        ):
            result = solve_profile(built, profile, 2.0, 0.0)

        self.assertEqual(result.status, "feasible")
        np.testing.assert_allclose(result.x, first.x)
        self.assertEqual(len(result.layers), 1)
        self.assertIn("partial", result.message)
        self.assertIn("secondary time limit", result.message)

    def test_later_error_or_infeasible_is_not_disguised_as_feasible(self):
        built = self.make_two_variable_model()
        profile = self.make_two_layer_profile()
        first = self.raw_result("optimal", np.array([0.0, 10.0]), "optimal")

        for status in ("error", "infeasible"):
            with self.subTest(status=status):
                failed = self.raw_result(status, None, f"secondary {status}")
                with patch(
                    "m4_optimizer.lexicographic.solve_milp",
                    side_effect=[first, failed],
                ):
                    result = solve_profile(built, profile, 2.0, 0.0)
                self.assertEqual(result.status, status)
                self.assertIsNone(result.x)
                self.assertEqual(len(result.layers), 1)
                self.assertEqual(result.message, f"secondary {status}")

    def test_first_layer_timeout_without_incumbent_returns_no_plan(self):
        timeout = self.raw_result("timeout", None, "first layer time limit")
        with patch("m4_optimizer.lexicographic.solve_milp", return_value=timeout):
            result = solve_profile(
                self.make_two_variable_model(),
                self.make_two_layer_profile(),
                2.0,
                0.0,
            )

        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.x)
        self.assertEqual(result.layers, ())

    def make_two_layer_profile(self):
        return ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )

    def raw_result(self, status, x, message):
        return RawSolveResult(
            status=status,
            x=x,
            objective_value=None if x is None else 0.0,
            message=message,
            mip_gap=None if x is None else 0.0,
        )

    def make_two_variable_model(self):
        empty = slice(0, 0)
        index = VariableIndex(
            charge=empty,
            discharge=empty,
            grid_import=empty,
            grid_export=empty,
            pv_unabsorbed=empty,
            energy=empty,
            demand_exceed=empty,
            peak_demand_exceed=0,
            charge_on=empty,
            discharge_on=empty,
            grid_import_on=empty,
            soc_low_deviation=empty,
            soc_high_deviation=empty,
            pv_curtail_on=empty,
            pv_storage_full=empty,
            size=2,
        )
        problem = MilpProblem(
            integrality=np.array([0, 0], dtype=np.uint8),
            lower_bounds=np.array([0.0, 0.0]),
            upper_bounds=np.array([np.inf, np.inf]),
            matrix=csr_matrix([[1.0, 1.0]]),
            constraint_lower=np.array([10.0]),
            constraint_upper=np.array([np.inf]),
        )
        objectives = {
            "demand_peak": np.array([1.0, 0.0]),
            "demand_duration": np.zeros(2),
            "soc_preferred_deviation": np.zeros(2),
            "energy_cost": np.array([0.0, 1.0]),
            "pv_unused": np.zeros(2),
            "throughput": np.zeros(2),
        }
        return BuiltModel(problem=problem, index=index, objectives=objectives)
