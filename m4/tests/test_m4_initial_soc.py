"""Historical SOC remains real while later charging respects the plan ceiling."""
import unittest

from pydantic import ValidationError

from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.metrics import calculate_metrics, materialize_plan
from m4.optimizer.model import build_model
from m4.optimizer.soc_policy import INITIAL_SOC_POLICY
from m4.optimizer.solver import solve_milp
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.settings.daily_comparison import _baseline_plan, comparison_input_sha256
from m4.settings.ems_model_update import ModelUpdateError, validate_dispatch_safety
from m4.settings.ems_simulation import (EMS_BASELINE_POLICY, LEGACY_EMS_BASELINE_POLICY,
    simulate_ems_day)
from m4.tests.m4_optimizer_test_support import make_request, make_candidate


def request(initial=98.083, *, policy=True):
    raw = make_request().model_dump()
    raw['capability'].update(initial_soc_pct=initial, charge_efficiency=1., discharge_efficiency=1.)
    raw['constraints'].update(soc_min_pct=1., soc_max_pct=98., preferred_soc_min_pct=2.,
        preferred_soc_max_pct=98., terminal_soc_tolerance_pct=100.)
    raw['source_versions']['planning_basis'] = 'whole-station-retrospective-v1'
    if policy:
        raw['source_versions']['initial_soc_policy'] = INITIAL_SOC_POLICY
    for point in raw['points']:
        point.update(pv_forecast_kw=0., tariff_period='ping')
    return OptimizationRequest.model_validate(raw)


SCHEDULE = [dict(start_time='00:00:00', end_time='08:00:00', mode='charge',
                 repeat='daily', power_kw=100.),
            dict(start_time='08:00:00', end_time='24:00:00', mode='discharge',
                 repeat='daily', power_kw=100.)]


class InitialSocTests(unittest.TestCase):
    def test_daily_preparation_uses_real_midnight_soc_and_versions_new_rule(self):
        from m4.settings.daily_baseline import prepare_ems_day
        from m4.settings.daily_pv_policy import POLICY as DAILY_PV_POLICY
        from m4.settings.forecast_source import LOAD_POLICY
        from m4.settings.load_accuracy import assess
        from m4.settings.timeseries import PV_EXPORT_POLICY
        from m4.tests.test_m4_load_accuracy import row, NOW
        from m4.tests.test_m4_realtime import configuration
        evidence = row('10')
        config = configuration('station-2', soc_min_pct=1., soc_max_pct=98.,
            preferred_soc_min_pct=2., preferred_soc_max_pct=98.,
            charge_efficiency=1., discharge_efficiency=1.)
        start = evidence['run']['forecast_start']
        periods = ['gu'] * 32 + ['feng'] * 64
        points = [dict(timestamp=p['target_time'], load_forecast_kw=p['forecast_value'],
            pv_forecast_kw=0., buy_price_per_kwh=.2 if i < 32 else 1.,
            sell_price_per_kwh=.5, tariff_period=periods[i])
            for i, p in enumerate(evidence['points'])]
        sources = dict(
            load=dict(status='ready', version='load', run_id='current', policy=LOAD_POLICY,
                      accuracy_gate=assess(evidence, 'station-2', NOW)),
            pv=dict(status='ready', version='pv', zero_filled_points=0, gap_policy=DAILY_PV_POLICY),
            tariff=dict(status='ready', version='tariff', period_types=periods,
                        export_price_policy=PV_EXPORT_POLICY, sell_price_per_kwh=.5),
            controls=dict(status='ready', station_id='station-2', version='control',
                power_scope='station', source_health={'schedule': 'ready'},
                schedule=SCHEDULE, demand={'need_kw': 300.}),
            initial_soc=dict(status='ready', scope='whole_station_reference', version='soc',
                             observed_at=start, initial_soc_pct=98.083))
        req, baseline, comparison = prepare_ems_day(config,
            dict(station_id='station-2', configuration_version=config.version,
                 plan_start_at=start, fetched_at=NOW.isoformat(), sources=sources, points=points))
        self.assertEqual(req.source_versions['initial_soc_policy'], INITIAL_SOC_POLICY)
        self.assertEqual(req.constraints.soc_max_pct, 98.)
        self.assertAlmostEqual(baseline['plan'][0]['expected_soc_pct'], 98.083)
        self.assertNotEqual(comparison['status'], 'unavailable')

    def test_new_requests_accept_real_initial_soc_up_to_100(self):
        for initial in (98.083, 99., 100.):
            with self.subTest(initial=initial):
                self.assertEqual(request(initial).capability.initial_soc_pct, initial)

    def test_physical_invalid_soc_is_rejected(self):
        for initial in (-1., 100.001, float('nan')):
            with self.subTest(initial=initial), self.assertRaises(ValidationError):
                request(initial)

    def test_legacy_request_still_requires_configured_initial_range(self):
        with self.assertRaisesRegex(ValidationError, 'initial SOC'):
            request(policy=False)

    def test_policy_is_not_implicitly_applied_to_live_requests(self):
        raw = request().model_dump()
        raw['source_versions']['planning_basis'] = 'live-v1'
        with self.assertRaisesRegex(ValidationError, 'initial SOC'):
            OptimizationRequest.model_validate(raw)

    def test_baseline_keeps_real_soc_and_does_not_charge_above_ceiling(self):
        req = request()
        plan, _ = simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE,
                                   allow_initial_soc_excess=True)
        for point in plan[:32]:
            self.assertEqual(point.mode, 'idle')
            self.assertAlmostEqual(point.expected_soc_pct, 98.083)
        self.assertEqual(plan[32].mode, 'discharge')
        self.assertLess(plan[32].expected_soc_pct, 98.)
        candidate = make_candidate(req, plan, calculate_metrics(req, plan))
        validate_candidate(req, candidate, historical_baseline=True)
        validate_dispatch_safety(req.model_dump(mode='json'), [p.model_dump(mode='json') for p in plan])

    def test_100_percent_baseline_stays_idle_without_fabricated_energy_loss(self):
        req = request(100.)
        plan, _ = simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE,
                                   allow_initial_soc_excess=True)
        self.assertEqual(plan[0].expected_soc_pct, 100.)
        self.assertEqual(plan[0].target_power_kw, 0.)

    def test_legacy_simulation_still_rejects_excess(self):
        req = request()
        with self.assertRaisesRegex(ValueError, '零点 SOC'):
            simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE)

    def test_optimizer_can_wait_then_discharge_without_first_slot_forced_recovery(self):
        req = request()
        raw = req.model_dump()
        raw['ems_schedule_modes'] = ['charge'] + ['discharge'] * 95
        req = OptimizationRequest.model_validate(raw)
        built = build_model(req, terminal_soc_target_pct=98.)
        solved = solve_milp(built.problem, built.objectives['throughput'], locks=(),
                            time_limit_seconds=3., mip_rel_gap=0.)
        self.assertEqual(solved.status, 'optimal')
        plan = materialize_plan(req, built, solved.x)
        self.assertEqual(plan[0].mode, 'idle')
        self.assertAlmostEqual(plan[0].expected_soc_pct, 98.083)
        self.assertAlmostEqual(plan[-1].expected_soc_pct, 98.)
        validate_candidate(req, make_candidate(req, plan, calculate_metrics(req, plan)),
                           terminal_soc_target_pct=98.)

    def test_charge_above_ceiling_is_rejected_by_independent_checks(self):
        req = request()
        plan, _ = simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE,
                                   allow_initial_soc_excess=True)
        plan[0] = plan[0].model_copy(update=dict(mode='charge', target_power_kw=1.))
        candidate = make_candidate(req, plan, calculate_metrics(req, plan))
        with self.assertRaisesRegex(ResultValidationError, 'charging above SOC ceiling'):
            validate_candidate(req, candidate, historical_baseline=True)
        with self.assertRaisesRegex(ModelUpdateError, '不能继续充电'):
            validate_dispatch_safety(req.model_dump(mode='json'), [p.model_dump(mode='json') for p in plan])

    def test_idle_inventory_can_remain_at_100_when_no_load_can_absorb_discharge(self):
        raw = request(100.).model_dump()
        raw['constraints']['terminal_soc_tolerance_pct'] = 0.
        for point in raw['points']:
            point['load_forecast_kw'] = 0.
        req = OptimizationRequest.model_validate(raw)
        built = build_model(req, terminal_soc_target_pct=100.)
        solved = solve_milp(built.problem, built.objectives['throughput'], locks=(),
                            time_limit_seconds=3., mip_rel_gap=0.)
        self.assertEqual(solved.status, 'optimal')
        plan = materialize_plan(req, built, solved.x)
        self.assertTrue(all(p.mode == 'idle' and p.expected_soc_pct == 100. for p in plan))
        validate_candidate(req, make_candidate(req, plan, calculate_metrics(req, plan)),
                           terminal_soc_target_pct=100.)

    def test_cannot_recreate_excess_after_soc_returns_to_ceiling(self):
        req = request()
        schedule = [dict(start_time='00:00:00', end_time='00:15:00', mode='discharge',
                         repeat='daily', power_kw=100.),
                    dict(start_time='00:15:00', end_time='24:00:00', mode='charge',
                         repeat='daily', power_kw=100.)]
        plan, _ = simulate_ems_day(req.capability, req.constraints, req.points, schedule,
                                   allow_initial_soc_excess=True)
        self.assertLess(plan[0].expected_soc_pct, 98.)
        self.assertTrue(all(p.expected_soc_pct <= 98. for p in plan[1:]))
        validate_candidate(req, make_candidate(req, plan, calculate_metrics(req, plan)),
                           historical_baseline=True)

    def test_new_baseline_replay_preserves_initial_state(self):
        req = request()
        req.source_versions['baseline_policy'] = EMS_BASELINE_POLICY
        plan, simulation = simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE,
                                            allow_initial_soc_excess=True)
        baseline = dict(schema_version='m4-ems-daily-baseline-v1', basis='ems_rule_simulation',
            controller_version=EMS_BASELINE_POLICY, station_id=req.station_id,
            input_sha256=comparison_input_sha256(req), controls_version='test',
            initial_state_version='test', initial_state_at=req.plan_start_at.isoformat(),
            initial_soc_pct=req.capability.initial_soc_pct, schedule=SCHEDULE,
            plan=[p.model_dump(mode='json') for p in plan], simulation=simulation)
        self.assertAlmostEqual(_baseline_plan(req, baseline, 'test').plan[0].expected_soc_pct, 98.083)

    def test_v8_baseline_can_still_be_replayed_under_its_original_rules(self):
        req = request(50., policy=False)
        req.source_versions['baseline_policy'] = LEGACY_EMS_BASELINE_POLICY
        plan, simulation = simulate_ems_day(req.capability, req.constraints, req.points, SCHEDULE)
        simulation['policy_version'] = LEGACY_EMS_BASELINE_POLICY
        baseline = dict(schema_version='m4-ems-daily-baseline-v1', basis='ems_rule_simulation',
            controller_version=LEGACY_EMS_BASELINE_POLICY, station_id=req.station_id,
            input_sha256=comparison_input_sha256(req), controls_version='test',
            initial_state_version='test', initial_state_at=req.plan_start_at.isoformat(),
            initial_soc_pct=50., schedule=SCHEDULE,
            plan=[p.model_dump(mode='json') for p in plan], simulation=simulation)
        self.assertEqual(_baseline_plan(req, baseline, 'test').plan, plan)
