import unittest

import numpy as np
from scipy.sparse import csr_matrix

from m4_optimizer.contracts import ObjectiveLayer, ObjectiveProfile
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.model import BuiltModel, VariableIndex
from m4_optimizer.solver import MilpProblem


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
