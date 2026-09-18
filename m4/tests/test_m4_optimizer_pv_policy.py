"""Physical PV routing and economic export, using actual 96-point solves."""
import unittest
from pathlib import Path


from m4.optimizer import M4Optimizer
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.tests.m4_optimizer_test_support import make_request


def pv_request(*, pv=100.0, soc=50.0, export=False, export_limit=0.0,
               sell=0.2, future_buy=1.1, available=True, terminal=100.0):
    payload = make_request().model_dump()
    payload['pv_dispatch_policy'] = 'load_first_economic'
    payload['capability'].update(
        initial_soc_pct=soc, energy_capacity_kwh=522.0, available=available,
        max_charge_kw=100.0, max_discharge_kw=100.0,
    )
    payload['constraints'].update(
        soc_min_pct=2.0, soc_max_pct=95.0,
        preferred_soc_min_pct=2.0, preferred_soc_max_pct=95.0,
        terminal_soc_tolerance_pct=terminal, demand_limit_kw=1000.0,
        grid_import_limit_kw=1000.0, grid_export_enabled=export,
        grid_export_limit_kw=export_limit,
    )
    for i, point in enumerate(payload['points']):
        # Two opportunities only. Later zero load cannot consume battery energy.
        point.update(load_forecast_kw=40.0 if i == 0 else 100.0 if i == 1 else 0.0,
                     pv_forecast_kw=pv if i == 0 else 0.0,
                     buy_price_per_kwh=future_buy, sell_price_per_kwh=sell)
    payload['solver_mip_rel_gap'] = 0.0
    # These analytic examples require exact objective optima. Production profile
    # locks allow small tradeoffs, e.g. 1e-6 / 0.001 can shift a few watts.
    for profile in payload['profiles']:
        for layer in profile['objective_order']:
            layer['absolute_tolerance'] = 0.0
    return OptimizationRequest.model_validate(payload)


def solve(request):
    result = M4Optimizer(model_version='pv-policy-test').optimize(request)
    for candidate in result.candidates:
        assert candidate.status in {'optimal', 'feasible'}, candidate.solver_message
        validate_candidate(request, candidate)
    return result


class PVPolicyTests(unittest.TestCase):
    def test_midday_choice_overrides_only_the_confirmed_window(self):
        from datetime import timedelta, timezone
        from zoneinfo import ZoneInfo
        request = pv_request().model_copy(update={
            'pv_dispatch_policy': 'load_first_storage_priority', 'pv_midday_economic': True})
        start = request.plan_start_at.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0)
        for minute, expected in ((719, 'load_first_storage_priority'),
                                 (720, 'load_first_economic'), (839, 'load_first_economic'),
                                 (840, 'load_first_storage_priority')):
            at = (start + timedelta(minutes=minute)).astimezone(timezone.utc)
            self.assertEqual(request.pv_policy_at(at), expected)

    def test_midday_economics_can_choose_either_storage_or_export(self):
        # Same surplus and export price, different future avoided purchase value.
        for hour, minute, future_buy, charge in ((12, 0, 1.1, 60.0), (12, 0, 0.1, 0.0),
                                                (11, 45, 0.1, 60.0), (14, 0, 0.1, 60.0)):
            payload = pv_request(export=True, export_limit=100.0, sell=0.6,
                                 future_buy=future_buy, terminal=0.0).model_dump()
            payload.update(pv_dispatch_policy='load_first_storage_priority', pv_midday_economic=True)
            shift = payload['plan_start_at'].replace(hour=hour, minute=minute) - payload['plan_start_at']
            payload['plan_start_at'] += shift
            payload['input_observed_at'] += shift
            for point in payload['points']:
                point['timestamp'] += shift
            request = OptimizationRequest.model_validate(payload)
            for candidate in solve(request).candidates:
                point = candidate.plan[0]
                self.assertAlmostEqual(point.target_power_kw if point.mode == 'charge' else 0.0,
                                       charge, delta=1e-5)
                self.assertAlmostEqual(point.grid_export_kw, 60.0-charge, delta=1e-5)

    def test_customer_export_priority_idles_despite_low_export_price(self):
        request = pv_request(export=True, export_limit=100.0, sell=0.0).model_copy(
            update={'pv_dispatch_policy': 'load_first_export_priority'})
        for candidate in solve(request).candidates:
            point = candidate.plan[0]
            self.assertEqual(point.mode, 'idle')
            self.assertAlmostEqual(point.grid_export_kw, 60.0)
            self.assertAlmostEqual(point.pv_unabsorbed_kw, 0.0)
        from m4.settings.ems_simulation import simulate_ems_day
        schedule = [dict(start_time='00:00:00', end_time='24:00:00',
                         repeat='daily', mode='charge', power_kw=100.0)]
        plan, _ = simulate_ems_day(request.capability, request.constraints,
            request.points, schedule, pv_dispatch_policy=request.pv_dispatch_policy)
        self.assertEqual(plan[0].mode, 'idle')
        self.assertAlmostEqual(plan[0].grid_export_kw, 60.0)

    def test_customer_export_limit_conflict_never_falls_back_to_charging(self):
        from m4.settings.ems_simulation import simulate_ems_day
        for enabled, limit in [(True, 50.0), (False, 0.0)]:
            request = pv_request(export=enabled, export_limit=limit).model_copy(
                update={'pv_dispatch_policy': 'load_first_export_priority'})
            result = M4Optimizer(model_version='pv-policy-test').optimize(request)
            self.assertTrue(all(c.status not in {'optimal', 'feasible'} for c in result.candidates))
            with self.assertRaisesRegex(ValueError, '外送限制冲突'):
                simulate_ems_day(request.capability, request.constraints, request.points,
                    [dict(start_time='00:00:00', end_time='24:00:00', repeat='daily',
                          mode='charge', power_kw=100.0)],
                    pv_dispatch_policy=request.pv_dispatch_policy)

    def test_customer_export_validation_rejects_economic_charging(self):
        economic = pv_request(export=True, export_limit=100.0, sell=0.0, terminal=0.0)
        strict = economic.model_copy(update={'pv_dispatch_policy': 'load_first_export_priority'})
        for candidate in solve(economic).candidates:
            with self.assertRaises(ResultValidationError):
                validate_candidate(strict, candidate)

    def test_disabled_export_absorbs_surplus_up_to_physical_limits(self):
        cases = [
            (100.0, 50.0, True, 60.0, 0.0),
            (220.0, 50.0, True, 100.0, 80.0),
            (220.0, 95.0, True, 0.0, 180.0),
            (220.0, 94.0, True, 21.97894736842105, 158.02105263157895),
            (220.0, 50.0, False, 0.0, 180.0),
        ]
        for pv, soc, available, charge, curtail in cases:
            with self.subTest(pv=pv, soc=soc, available=available):
                request = pv_request(pv=pv, soc=soc, available=available)
                for candidate in solve(request).candidates:
                    point = candidate.plan[0]
                    self.assertEqual(point.mode, 'charge' if charge else 'idle')
                    self.assertAlmostEqual(point.target_power_kw, charge, delta=1e-6)
                    self.assertAlmostEqual(point.pv_unabsorbed_kw, curtail, delta=1e-6)
                    self.assertAlmostEqual(point.grid_export_kw, 0.0, delta=1e-6)
                    self.assertAlmostEqual(point.grid_import_kw, 0.0, delta=1e-6)

    def test_export_uses_prices_without_penalizing_lawful_export(self):
        for sell, future_buy, charge, export in [(0.2, 1.1, 60.0, 0.0), (1.2, 0.6, 0.0, 60.0)]:
            with self.subTest(sell=sell):
                request = pv_request(export=True, export_limit=100.0, sell=sell,
                                     future_buy=future_buy, terminal=0.0)
                for candidate in solve(request).candidates:
                    point = candidate.plan[0]
                    self.assertAlmostEqual(point.target_power_kw if point.mode == 'charge' else 0.0, charge, delta=1e-5)
                    self.assertAlmostEqual(point.grid_export_kw, export, delta=1e-5)
                    self.assertAlmostEqual(point.pv_unabsorbed_kw, 0.0, delta=1e-6)
                    self.assertNotEqual(point.mode, 'discharge')

    def test_export_limit_retains_storage_and_curtailment_paths(self):
        request = pv_request(pv=220.0, export=True, export_limit=50.0, sell=1.2, future_buy=0.6)
        for candidate in solve(request).candidates:
            point = candidate.plan[0]
            self.assertAlmostEqual(point.grid_export_kw, 50.0, delta=1e-5)
            self.assertAlmostEqual(point.target_power_kw, 100.0, delta=1e-5)
            self.assertEqual(point.mode, 'charge')
            self.assertAlmostEqual(point.pv_unabsorbed_kw, 30.0, delta=1e-5)

    def test_terminal_constraint_cannot_be_bypassed_by_curtailing_absorbable_pv(self):
        request = pv_request(terminal=0.0)
        payload = request.model_dump()
        payload['points'][1]['load_forecast_kw'] = 0.0
        request = OptimizationRequest.model_validate(payload)
        result = M4Optimizer(model_version='pv-policy-test').optimize(request)
        self.assertTrue(all(c.status == 'infeasible' and not c.plan for c in result.candidates))

    def test_no_pv_still_allows_valley_grid_charging(self):
        from m4.tests.m4_optimizer_test_support import make_price_arbitrage_request
        payload = make_price_arbitrage_request().model_dump()
        payload['pv_dispatch_policy'] = 'load_first_economic'
        request = OptimizationRequest.model_validate(payload)
        for candidate in solve(request).candidates:
            self.assertTrue(any(p.mode == 'charge' and p.target_power_kw > 1.0 for p in candidate.plan[:24]))

    def test_old_midday_plan_remains_valid_only_under_legacy_policy(self):
        root = Path(__file__).resolve().parents[2]
        folder = root/'m4/tests/fixtures/m4_pv_legacy_midday'
        from m4.optimizer.contracts import OptimizationResult
        old_request = OptimizationRequest.model_validate_json((folder/'optimizer-input.json').read_text())
        old_result = OptimizationResult.model_validate_json((folder/'optimizer-result.json').read_text())
        balanced = next(c for c in old_result.candidates if c.profile_id == 'balanced')
        validate_candidate(old_request, balanced)
        payload = old_request.model_dump()
        payload['pv_dispatch_policy'] = 'load_first_economic'
        with self.assertRaisesRegex(ResultValidationError, 'PV load priority|PV surplus absorption'):
            validate_candidate(OptimizationRequest.model_validate(payload), balanced)

    def test_empty_night_interval_can_charge_for_later_load(self):
        payload = pv_request(pv=0.0, terminal=0.0).model_dump()
        payload['points'][0].update(load_forecast_kw=0.0, buy_price_per_kwh=0.1)
        request = OptimizationRequest.model_validate(payload)
        for candidate in solve(request).candidates:
            self.assertEqual(candidate.plan[0].mode, 'charge')
            self.assertGreater(candidate.plan[0].target_power_kw, 1.0)

    def test_same_request_id_with_new_policy_has_distinct_plan_version(self):
        request = pv_request()
        new_result = solve(request)
        old_result = solve(request.model_copy(update={'pv_dispatch_policy': 'legacy'}))
        self.assertNotEqual(new_result.candidates[0].plan_version, old_result.candidates[0].plan_version)
