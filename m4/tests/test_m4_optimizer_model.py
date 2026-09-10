import unittest

import numpy as np
import pyomo.environ as pyo

from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.tests.m4_optimizer_test_support import (
    make_battery_load_with_pv_export_request,
    make_request,
    make_zero_pv_export_request,
)


class M4OptimizerModelTests(unittest.TestCase):
    def test_main_model_uses_named_pyomo_variables_constraints_and_expressions(self):
        built = build_model(make_request())
        model = built.problem.model

        self.assertIsInstance(model, pyo.ConcreteModel)
        self.assertEqual(len(model.charge), 96)
        self.assertEqual(len(model.energy), 97)
        self.assertTrue(model.charge_on[0].is_binary())
        self.assertEqual(len(model.power_balance), 96)
        self.assertEqual(len(model.energy_transition), 96)
        self.assertEqual(len(model.storage_exclusivity), 96)
        self.assertEqual(len(model.grid_export_mode), 96)
        self.assertIsInstance(model.energy_cost, pyo.Expression)
        self.assertIs(built.problem.variables[built.index.charge.start], model.charge[0])
        self.assertFalse(hasattr(built.problem, "matrix"))

    def solve(self, request, objective_name="throughput"):
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives[objective_name],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertEqual(result.status, "optimal")
        self.assertIsNotNone(result.x)
        return built, result.x

    def test_unavailable_storage_has_zero_charge_and_discharge(self):
        request = make_request(
            capability=make_request().capability.model_copy(update={"available": False})
        )
        built, x = self.solve(request)

        np.testing.assert_allclose(x[built.index.charge], 0.0, atol=1e-7)
        np.testing.assert_allclose(x[built.index.discharge], 0.0, atol=1e-7)

    def test_energy_state_uses_charge_and_discharge_efficiency(self):
        request = make_request()
        built, x = self.solve(request)

        for t in range(96):
            expected = (
                x[built.index.energy.start + t]
                + request.capability.charge_efficiency
                * x[built.index.charge.start + t]
                * 0.25
                - x[built.index.discharge.start + t]
                * 0.25
                / request.capability.discharge_efficiency
            )
            self.assertAlmostEqual(x[built.index.energy.start + t + 1], expected, places=6)

    def test_model_exposes_every_required_objective_with_expected_coefficients(self):
        request = make_request()
        built = build_model(request)

        self.assertEqual(
            set(built.objectives),
            {
                "demand_peak",
                "demand_duration",
                "soc_preferred_deviation",
                "energy_cost",
                "pv_unused",
                "throughput",
                "valley_charge_delay",
            },
        )
        self.assertEqual(built.objectives["demand_peak"][built.index.peak_demand_exceed], 1.0)
        np.testing.assert_allclose(
            built.objectives["demand_duration"][built.index.demand_exceed], 0.25
        )
        np.testing.assert_allclose(
            built.objectives["soc_preferred_deviation"][built.index.soc_low_deviation],
            0.25,
        )
        np.testing.assert_allclose(
            built.objectives["soc_preferred_deviation"][built.index.soc_high_deviation],
            0.25,
        )
        np.testing.assert_allclose(built.objectives["throughput"][built.index.charge], 0.25)
        np.testing.assert_allclose(built.objectives["throughput"][built.index.discharge], 0.25)
        self.assertAlmostEqual(
            built.objectives["energy_cost"][built.index.grid_import.start], 0.2
        )
        self.assertAlmostEqual(
            built.objectives["pv_unused"][built.index.grid_export.start], 0.25
        )
        self.assertAlmostEqual(
            built.objectives["pv_unused"][built.index.pv_unabsorbed.start], 0.25
        )

    def test_solution_satisfies_power_balance_at_every_point(self):
        request = make_request()
        built, x = self.solve(request)

        for t, point in enumerate(request.points):
            balance = (
                point.pv_forecast_kw
                + x[built.index.grid_import.start + t]
                + x[built.index.discharge.start + t]
                - point.load_forecast_kw
                - x[built.index.charge.start + t]
                - x[built.index.grid_export.start + t]
                - x[built.index.pv_unabsorbed.start + t]
            )
            self.assertAlmostEqual(balance, 0.0, places=6)

    def test_solution_never_charges_and_discharges_or_imports_and_exports(self):
        built, x = self.solve(make_request(), objective_name="energy_cost")

        for t in range(96):
            charge = x[built.index.charge.start + t]
            discharge = x[built.index.discharge.start + t]
            grid_import = x[built.index.grid_import.start + t]
            grid_export = x[built.index.grid_export.start + t]
            self.assertFalse(charge > 1e-7 and discharge > 1e-7)
            self.assertFalse(grid_import > 1e-7 and grid_export > 1e-7)

    def test_energy_bounds_fix_initial_state_and_limit_terminal_soc(self):
        request = make_request()
        built = build_model(request)
        energy_bounds = built.problem.lower_bounds[built.index.energy]
        terminal_upper = built.problem.upper_bounds[built.index.energy.stop - 1]
        initial_energy = request.capability.energy_capacity_kwh * 0.5

        self.assertEqual(energy_bounds[0], initial_energy)
        self.assertEqual(built.problem.upper_bounds[built.index.energy.start], initial_energy)
        self.assertEqual(energy_bounds[1], 20.0)
        self.assertEqual(built.problem.upper_bounds[built.index.energy.start + 1], 180.0)
        self.assertEqual(energy_bounds[-1], 90.0)
        self.assertEqual(terminal_upper, 110.0)

    def test_grid_and_pv_bounds_enforce_no_export_and_physical_limits(self):
        request = make_request()
        built = build_model(request)

        np.testing.assert_allclose(
            built.problem.upper_bounds[built.index.grid_export], 0.0
        )
        np.testing.assert_allclose(
            built.problem.upper_bounds[built.index.grid_import], 150.0
        )
        np.testing.assert_allclose(
            built.problem.upper_bounds[built.index.pv_unabsorbed], 20.0
        )

        no_import_limit = build_model(make_request(
            constraints=make_request().constraints.model_copy(
                update={"grid_import_limit_kw": None}
            )
        ))
        np.testing.assert_allclose(
            no_import_limit.problem.upper_bounds[no_import_limit.index.grid_import],
            200.0,
        )

    def test_demand_exceed_is_soft_variable_above_limit(self):
        request = make_request(
            capability=make_request().capability.model_copy(update={"available": False}),
            constraints=make_request().constraints.model_copy(
                update={"demand_limit_kw": 50.0}
            )
        )
        built, x = self.solve(request, objective_name="demand_duration")

        np.testing.assert_allclose(x[built.index.demand_exceed], 30.0, atol=1e-7)
        built, x = self.solve(request, objective_name="demand_peak")
        self.assertAlmostEqual(x[built.index.peak_demand_exceed], 30.0, places=7)

    def test_export_and_unabsorbed_are_jointly_bounded_by_available_pv(self):
        request = make_zero_pv_export_request(
            buy_price_per_kwh=0.1,
            sell_price_per_kwh=10.0,
        )
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives["energy_cost"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertIn(result.status, {"optimal", "feasible"})
        self.assertIsNotNone(result.x)
        x = result.x

        for t, point in enumerate(request.points):
            exported = x[built.index.grid_export.start + t]
            unabsorbed = x[built.index.pv_unabsorbed.start + t]
            self.assertLessEqual(exported + unabsorbed, point.pv_forecast_kw + 1e-7)

    def test_battery_can_serve_load_while_pv_is_exported(self):
        request = make_battery_load_with_pv_export_request()
        built, x = self.solve(request, objective_name="energy_cost")

        self.assertGreater(x[built.index.discharge.start + 40], 1e-7)
        self.assertGreater(x[built.index.grid_export.start + 40], 1e-7)
        self.assertLessEqual(
            x[built.index.grid_export.start + 40]
            + x[built.index.pv_unabsorbed.start + 40],
            request.points[40].pv_forecast_kw + 1e-7,
        )


if __name__ == "__main__":
    unittest.main()
