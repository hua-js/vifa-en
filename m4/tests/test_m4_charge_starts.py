"""Charging continuity must be chosen before valley locks freeze midday slots."""
import json
import unittest
import pyomo.environ as pyo

from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.tests.test_m4_late_peak_reserve import current_reserve_request


class ChargeStartTests(unittest.TestCase):
    def test_new_variables_keep_solver_columns_and_objectives_aligned(self):
        from m4.optimizer.lexicographic import build_layer_objective
        request = current_reserve_request()
        built = build_model(request)
        self.assertEqual(len(built.problem.variables), built.index.size)
        self.assertEqual(built.problem.variables[built.index.charge_start.start].name, 'charge_start[0]')
        self.assertEqual(built.problem.variables[built.index.power_variation.start].name, 'power_change[0]')
        for profile in request.profiles:
            for layer in profile.objective_order:
                self.assertEqual(len(build_layer_objective(built, layer)), built.index.size)

    def test_idle_cannot_bridge_charge_episodes(self):
        for slots, expected in [((0, 1), 1), ((0, 2), 2), ((0, 95), 2), ((), 0)]:
            with self.subTest(slots=slots):
                request = current_reserve_request()
                request.capability.initial_soc_pct = 50.0
                built = build_model(request)
                self.assertIn('charge_starts', built.objectives)
                for t in built.problem.model.periods:
                    built.problem.model.charge[t].fix(20 if t in slots else 0)
                    built.problem.model.discharge[t].fix(0)
                solved = solve_milp(built.problem, built.objectives['charge_starts'],
                    locks=(), time_limit_seconds=5, mip_rel_gap=0)
                self.assertEqual(solved.status, 'optimal', solved.message)
                self.assertAlmostEqual(solved.objective_value, expected)

    def test_equal_price_same_energy_can_use_one_charge_episode(self):
        request = current_reserve_request()
        request.capability.initial_soc_pct = 50.0
        built = build_model(request)
        self.assertIn('charge_starts', built.objectives)
        model = built.problem.model
        for t in model.periods:
            model.discharge[t].fix(0)
            if t not in (0, 1, 2, 3):
                model.charge[t].fix(0)
            else:
                model.charge[t].setub(20.0)
        model.test_charge_energy = pyo.Constraint(expr=sum(model.charge.values()) == 40)
        solved = solve_milp(built.problem, built.objectives['charge_starts'],
            locks=(), time_limit_seconds=5, mip_rel_gap=0)
        self.assertEqual(solved.status, 'optimal', solved.message)
        self.assertAlmostEqual(solved.objective_value, 1)

    def test_charge_continuity_precedes_valley_freeze_but_not_economics(self):
        raw = current_reserve_request().model_dump()
        for profile in raw['profiles']:
            names = [layer['name'] for layer in profile['objective_order']]
            self.assertIn('charge-starts', names)
            self.assertGreater(names.index('charge-starts'), names.index('throughput'))
            self.assertLess(names.index('charge-starts'), names.index('valley-charge-delay'))
        layers = raw['profiles'][0]['objective_order']
        charge = next(layer for layer in layers if layer['name'] == 'charge-starts')
        layers.remove(charge)
        layers.insert(2, charge)
        with self.assertRaises(ValueError):
            OptimizationRequest.model_validate(raw)

    def test_charge_count_lock_cannot_be_relaxed(self):
        raw = current_reserve_request().model_dump()
        charge = next(layer for layer in raw['profiles'][0]['objective_order']
                      if layer['name'] == 'charge-starts')
        charge['absolute_tolerance'] = 1.0
        with self.assertRaisesRegex(ValueError, 'must not relax'):
            OptimizationRequest.model_validate(raw)

    def test_v3_remains_readable_but_cannot_be_current(self):
        from m4.settings.daily_policy import matches_current_daily_policy
        from m4.optimizer.contracts import GRID_CHARGING_POLICY
        raw = current_reserve_request().model_dump(mode='json')
        raw['pv_midday_economic'] = True
        raw['source_versions'].update(pv_export_policy='pv-export-flat-tariff-v1',
            pv_export_price='0', grid_charging_policy=GRID_CHARGING_POLICY)
        self.assertTrue(matches_current_daily_policy('station-2', raw))
        for profile in raw['profiles']:
            profile['profile_version'] = profile['profile_version'].replace(
                'station-2-continuity-v4', 'station-2-continuity-v3')
            profile['objective_order'] = [layer for layer in profile['objective_order']
                if layer['name'] != 'charge-starts']
        OptimizationRequest.model_validate_json(json.dumps(raw))
        self.assertFalse(matches_current_daily_policy('station-2', raw))
