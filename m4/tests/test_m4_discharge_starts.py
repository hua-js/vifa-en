"""Count actual discharge episodes, including gaps and day boundaries."""
import unittest
import json
from m4.tests.test_m4_late_peak_reserve import reserve_request
from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.optimizer.contracts import OptimizationRequest, GRID_CHARGING_POLICY
from m4.settings.daily_policy import matches_current_daily_policy


class DischargeStartTests(unittest.TestCase):
    def test_full_optimizer_pipeline_with_start_variables(self):
        from m4.optimizer.service import M4Optimizer
        from m4.optimizer.validation import validate_candidate
        from m4.optimizer.lexicographic import build_layer_objective
        request = reserve_request('peak-reserve-v4')
        request.pv_dispatch_policy = 'load_first_export_priority'
        for point in request.points:
            point.load_forecast_kw = 0
        built = build_model(request)
        self.assertEqual(built.index.size, len(built.problem.variables))
        for name in ('discharge_active', 'discharge_start', 'power_variation'):
            column = getattr(built.index, name).start
            expected = 'power_change[0]' if name == 'power_variation' else name+'[0]'
            self.assertEqual(built.problem.variables[column].name, expected)
        for profile in request.profiles:
            for layer in profile.objective_order:
                self.assertEqual(len(build_layer_objective(built, layer)), built.index.size)
        result = M4Optimizer(model_version='discharge-start-pipeline').optimize(request)
        for candidate, profile in zip(result.candidates, request.profiles):
            self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
            self.assertEqual(len(candidate.layers), len(profile.objective_order))
            self.assertEqual(len(candidate.plan), 96)
            validate_candidate(request, candidate)

    def test_idle_cannot_bridge_discharge_episodes(self):
        for slots, expected in [((72, 73), 1), ((72, 74), 2), ((0, 95), 2), ((), 0)]:
            with self.subTest(slots=slots):
                request = reserve_request('peak-reserve-v3')
                request.capability.initial_soc_pct = 50
                built = build_model(request)
                for t in built.problem.model.periods:
                    built.problem.model.charge[t].fix(0)
                    built.problem.model.discharge[t].fix(1 if t in slots else 0)
                result = solve_milp(built.problem, built.objectives['discharge_starts'],
                    locks=(), time_limit_seconds=5, mip_rel_gap=0)
                self.assertEqual(result.status, 'optimal', result.message)
                self.assertAlmostEqual(result.objective_value, expected)

    def test_equal_price_energy_can_be_served_in_one_episode(self):
        import pyomo.environ as pyo
        request = reserve_request('peak-reserve-v3')
        request.capability.initial_soc_pct = 50
        built = build_model(request)
        model = built.problem.model
        for t in model.periods:
            model.charge[t].fix(0)
            if t not in (80, 81, 82, 83):
                model.discharge[t].fix(0)
            else:
                model.discharge[t].setub(1)
        # Equal-price slots, same energy/throughput and terminal SOC as two
        # isolated 1 kW slots, but now the solver may choose their placement.
        model.test_energy = pyo.Constraint(expr=sum(model.discharge.values()) == 2)
        result = solve_milp(built.problem, built.objectives['discharge_starts'],
            locks=(), time_limit_seconds=5, mip_rel_gap=0)
        self.assertEqual(result.status, 'optimal', result.message)
        self.assertAlmostEqual(result.objective_value, 1)

    def test_start_objective_cannot_precede_economics(self):
        payload = reserve_request('peak-reserve-v3').model_dump()
        layers = payload['profiles'][0]['objective_order']
        layer = next(x for x in layers if 'discharge_starts' in x['terms'])
        layers.remove(layer)
        layers.insert(2, layer)
        with self.assertRaisesRegex(ValueError, 'late separate'):
            OptimizationRequest.model_validate(payload)

    def test_old_continuity_plan_is_not_current(self):
        from m4.tests.test_m4_late_peak_reserve import current_reserve_request
        payload = current_reserve_request().model_dump(mode='json')
        payload['pv_midday_economic'] = True
        payload['source_versions'].update(pv_export_policy='pv-export-flat-tariff-v1',
            grid_charging_policy=GRID_CHARGING_POLICY,
            pv_export_price=format(0.6, '.17g'))
        for point in payload['points']:
            point['sell_price_per_kwh'] = 0.6
        self.assertTrue(matches_current_daily_policy('station-2', payload))
        for profile in payload['profiles']:
            profile['profile_version'] = profile['profile_version'].replace('station-2-continuity-v3', 'station-2-continuity-v1')
            profile['objective_order'] = [x for x in profile['objective_order'] if 'discharge_starts' not in x['terms']]
            early = next(x for x in profile['objective_order'] if 'valley_charge_delay' in x['terms'])
            profile['objective_order'].remove(early)
            profile['objective_order'].append(early)
        OptimizationRequest.model_validate_json(json.dumps(payload))
        self.assertFalse(matches_current_daily_policy('station-2', payload))
