"""Terminal reserve must survive the last peak, not be recharged afterwards."""
import unittest
from shared.project import get_project

from m4.optimizer.contracts import OptimizationRequest, PeakReservePolicy
from m4.optimizer.model import build_model
from m4.optimizer.service import M4Optimizer
from m4.optimizer.solver import solve_milp
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.settings.daily_policy import matches_current_daily_policy, PEAK_RESERVE_DAILY_POLICY
from m4.settings.objectives import get_daily_profiles
from m4.tests.m4_optimizer_test_support import make_request


def reserve_request(version='peak-reserve-v2', peaks=((40, 48), (56, 76))):
    payload = make_request().model_dump()
    payload.update(station_id='station-2', solver_mip_rel_gap=0.0, profiles=[p.model_dump(mode='json') for p in get_daily_profiles('station-2')],
                   peak_reserve_policy={'version': version, 'terminal_soc_min_pct': 2})
    payload['source_versions']['project_configuration'] = get_project().fingerprint
    payload['source_versions']['daily_policy'] = 'm4-daily-peak-reserve-' + version.rsplit('-', 1)[-1]
    payload['capability'].update(initial_soc_pct=2, charge_efficiency=.98, discharge_efficiency=.98)
    payload['constraints'].update(soc_min_pct=1, soc_max_pct=98, preferred_soc_min_pct=2,
                                  preferred_soc_max_pct=98, demand_limit_kw=300, grid_import_limit_kw=300,
                                  cycle_cost_per_kwh=0, terminal_soc_tolerance_pct=0)
    for i, point in enumerate(payload['points']):
        peak = any(a <= i < b for a, b in peaks)
        point.update(load_forecast_kw=100, pv_forecast_kw=0,
                     tariff_period='feng' if peak else 'gu' if i < 28 else 'ping',
                     buy_price_per_kwh=1.1 if peak else .27 if i < 28 else .66)
    if version != 'peak-reserve-v3':
        for profile in payload['profiles']:
            # Archived v1/v2 fixtures must not inherit v3-only objective layers.
            profile['objective_order'] = [layer for layer in profile['objective_order']
                if not ({'discharge_starts', 'power_variation'} & layer['terms'].keys())]
            layers=profile['objective_order']
            reserve=next(layer for layer in layers if layer['name']=='peak-reserve-shortfall')
            layers.remove(reserve)
            layers.insert(2,reserve)
    return OptimizationRequest.model_validate(payload)


class LatePeakReserveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = reserve_request('peak-reserve-v1')
        cls.new = reserve_request()
        optimizer = M4Optimizer(model_version='late-peak-regression')
        cls.old_result = optimizer.optimize(cls.old, terminal_soc_target_pct=1)
        cls.new_result = optimizer.optimize(cls.new, terminal_soc_target_pct=1)

    def test_old_cost_plan_reproduces_peak_end_borrow_and_recharge(self):
        candidate = next(c for c in self.old_result.candidates if c.profile_id == 'cost')
        self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
        self.assertLess(candidate.plan[75].expected_soc_pct, 2 - 1e-4)
        self.assertGreater(sum(p.target_power_kw for p in candidate.plan[76:] if p.mode == 'charge'), 1e-4)
        validate_candidate(self.old, candidate, terminal_soc_target_pct=1)

    def test_all_new_candidates_preserve_reserve_and_need_no_post_peak_recharge(self):
        for candidate in self.new_result.candidates:
            with self.subTest(profile=candidate.profile_id):
                self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
                self.assertGreaterEqual(min(p.expected_soc_pct for p in candidate.plan[55:]), 2 - 1e-6)
                self.assertLess(sum(p.target_power_kw for p in candidate.plan[76:] if p.mode == 'charge'), 1e-4)
                validate_candidate(self.new, candidate, terminal_soc_target_pct=1)
                self.assertIn('/peak-reserve-v2/', candidate.plan_version)

    def test_independent_validator_rejects_old_plan_even_if_terminal_floor_met(self):
        candidate = next(c for c in self.old_result.candidates if c.profile_id == 'cost')
        with self.assertRaisesRegex(ResultValidationError, 'late peak terminal reserve'):
            validate_candidate(self.new, candidate, terminal_soc_target_pct=1)

    def test_last_peak_uses_tariff_blocks_not_fixed_clock(self):
        request = reserve_request(peaks=((20, 28), (64, 80)))
        model = build_model(request).problem.model
        self.assertEqual(list(model.terminal_reserve_states), list(range(64, 97)))
        self.assertNotIn(28, model.late_peak_terminal_reserve)
        self.assertIn(64, model.late_peak_terminal_reserve)
        self.assertIn(96, model.late_peak_terminal_reserve)

    def test_v1_model_and_default_contract_keep_historical_meaning(self):
        self.assertFalse(hasattr(build_model(self.old).problem.model, 'late_peak_terminal_reserve'))
        self.assertEqual(PeakReservePolicy(terminal_soc_min_pct=2).model_dump(),
                         {'version': 'peak-reserve-v1', 'terminal_soc_min_pct': 2.0})

    def test_new_daily_policy_rejects_old_saved_request(self):
        self.assertEqual(PEAK_RESERVE_DAILY_POLICY, 'm4-daily-peak-reserve-v3')
        current=reserve_request('peak-reserve-v3').model_dump(mode='json')
        self.assertTrue(matches_current_daily_policy('station-2', current))
        bounded={**current,'ems_schedule_modes':['idle']*96}
        self.assertFalse(matches_current_daily_policy('station-2', bounded))
        self.assertFalse(matches_current_daily_policy('station-2', self.new.model_dump(mode='json')))
        self.assertFalse(matches_current_daily_policy('station-2', self.old.model_dump(mode='json')))

    def test_last_peak_starting_at_zero_cannot_hide_insufficient_initial_reserve(self):
        request = reserve_request(peaks=((0, 4),))
        request = request.model_copy(update={'capability': request.capability.model_copy(update={'initial_soc_pct': 1})})
        built = build_model(request)
        result = solve_milp(built.problem, built.objectives['throughput'], locks=(),
                            time_limit_seconds=5, mip_rel_gap=0)
        self.assertEqual(result.status, 'infeasible')


if __name__ == '__main__':
    unittest.main()
