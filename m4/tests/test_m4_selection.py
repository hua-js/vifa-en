import itertools
import unittest
from datetime import datetime
from decimal import localcontext

from m4.optimizer.contracts import CandidateResult, OptimizationResult, PlanPoint
from m4.optimizer.metrics import calculate_metrics
from m4.tests.m4_optimizer_test_support import make_request
from m4.selection import SelectionPolicy, select_candidate


def policy(request, **overrides):
    values = dict(station_id=request.station_id, policy_id='test-only', version='v1',
                  metric='energy_cost', demand_peak_tolerance_kw=0.000001,
                  demand_energy_tolerance_kwh=0.000001, metric_tolerance=0.000001,
                  tie_order=['balanced', 'cost', 'pv'])
    values.update(overrides)
    return SelectionPolicy(**values)


def fixture(*, export=False, powers=None, unused=None):
    request = make_request()
    request.capability.charge_efficiency = 1.0
    request.capability.discharge_efficiency = 1.0
    request.constraints.preferred_soc_min_pct = 50.0
    request.constraints.grid_export_enabled = export
    request.constraints.grid_export_limit_kw = 10.0 if export else 0.0
    for i, point in enumerate(request.points):
        point.load_forecast_kw = 0.0 if export else 100.0
        point.pv_forecast_kw = 0.5 if export else 0.0
        point.sell_price_per_kwh = 1.0 if export else 0.0
        point.buy_price_per_kwh = 2.0 if i == 0 else 0.1
    candidates = []
    for profile in request.profiles:
        pid = profile.profile_id
        energy = 100.0
        plan = []
        for i, point in enumerate(request.points):
            power = (powers or {}).get(pid, {}).get(i, 0.0)  # positive charge
            energy += power * 0.25
            curtailed = (unused or {}).get(pid, {}).get(i, 0.0)
            grid = point.load_forecast_kw + power - point.pv_forecast_kw + curtailed
            plan.append(PlanPoint(timestamp=point.timestamp,
                mode='charge' if power > 0 else 'discharge' if power < 0 else 'idle',
                target_power_kw=abs(power), expected_soc_pct=energy / 2,
                grid_import_kw=max(grid, 0.0), grid_export_kw=max(-grid, 0.0),
                pv_unabsorbed_kw=curtailed,
                demand_exceed_kw=max(grid-request.constraints.demand_limit_kw, 0.0)))
        candidates.append(CandidateResult(profile_id=pid, profile_version=profile.profile_version,
            plan_version=f'{request.request_id}/test/{pid}/{profile.profile_version}',
            status='optimal', solver_message='test', solve_seconds=0.0, plan=plan,
            metrics=calculate_metrics(request, plan), layers=[], risk_codes=[], risk_messages=[]))
    result = OptimizationResult(request_id=request.request_id, station_id=request.station_id,
        plan_start_at=request.plan_start_at, input_observed_at=request.input_observed_at,
        started_at=request.input_observed_at, finished_at=request.input_observed_at,
        model_version='test', solver_name='scipy-highs', solver_version='test',
        source_versions=request.source_versions.copy(), candidates=candidates)
    return request, result


class SelectionTests(unittest.TestCase):
    def test_profile_priority_chooses_balanced_even_when_cost_is_cheaper(self):
        request, result = fixture(powers={'cost': {0: -5.0, 95: 5.0}})
        self.assertLess(result.candidates[1].metrics.energy_cost, result.candidates[0].metrics.energy_cost)
        chosen = select_candidate(request, result, policy(request, metric='profile_priority', metric_tolerance=0.0))
        self.assertEqual(chosen.selected.profile_id, 'balanced')
        self.assertEqual(chosen.selector_version, 'demand-then-profile-v1')
        self.assertEqual(chosen.steps[-1].metric, 'profile_priority')
        self.assertEqual(chosen.steps[-1].values, {'balanced': 0.0, 'cost': 1.0, 'pv': 2.0})
        self.assertIn('候选顺序', chosen.reason)
        self.assertNotIn('SOC', chosen.reason)

    def test_profile_priority_never_overrides_either_demand_layer(self):
        request, result = fixture(powers={'balanced': {0: 8.0, 95: -8.0},
                                         'cost': {0: 4.0, 1: 4.0, 95: -8.0},
                                         'pv': {0: 4.0, 95: -4.0}})
        request.constraints.demand_limit_kw = 100.0
        for candidate in result.candidates:
            for point in candidate.plan:
                point.demand_exceed_kw = max(point.grid_import_kw-100.0, 0.0)
            candidate.metrics = calculate_metrics(request, candidate.plan)
        chosen = select_candidate(request, result, policy(request, metric='profile_priority', metric_tolerance=0.0,
            demand_peak_tolerance_kw=0.0, demand_energy_tolerance_kwh=0.0))
        self.assertEqual(chosen.steps[0].remaining_ids, ['cost', 'pv'])
        self.assertEqual(chosen.steps[1].remaining_ids, ['pv'])
        self.assertEqual(chosen.selected.profile_id, 'pv')

    def test_profile_priority_excludes_invalid_balanced_then_uses_configured_order(self):
        request, result = fixture()
        result.candidates[0].plan[0].expected_soc_pct += 1.0
        chosen = select_candidate(request, result, policy(request, metric='profile_priority', metric_tolerance=0.0))
        self.assertEqual(chosen.selected.profile_id, 'cost')
        self.assertEqual(chosen.excluded[0].profile_id, 'balanced')
        chosen = select_candidate(request, result, policy(request, metric='profile_priority', metric_tolerance=0.0,
            tie_order=['pv', 'balanced', 'cost']))
        self.assertEqual(chosen.selected.profile_id, 'pv')

    def test_profile_priority_rejects_nonzero_rank_tolerance(self):
        request, result = fixture()
        with self.assertRaisesRegex(ValueError, 'profile_priority.*zero'):
            policy(request, metric='profile_priority', metric_tolerance=1.0)

    def test_comparison_does_not_depend_on_callers_decimal_context(self):
        request, result = fixture(export=True, unused={'cost': {0: 0.00404}, 'pv': {0: 0.5}})
        config = policy(request, metric_tolerance=0.001, tie_order=['cost','balanced','pv'])
        for precision in [2, 28, 80]:
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                chosen = select_candidate(request, result, config)
                self.assertEqual(chosen.selected.profile_id, 'balanced')

    def test_unrepresentable_request_horizon_is_value_error(self):
        request, result = fixture()
        request.plan_start_at = datetime.fromisoformat('9999-12-31T23:45:00+08:00')
        request.input_observed_at = request.plan_start_at
        with self.assertRaises(ValueError):
            select_candidate(request, result)

    def test_missing_policy_never_selects(self):
        request, result = fixture()
        selected = select_candidate(request, result)
        self.assertEqual(selected.status, 'pending_policy')
        self.assertIsNone(selected.selected)
        self.assertEqual(selected.dispatch_status, 'not_dispatched')

    def test_explicit_cost_and_soc_preferences_change_choice(self):
        request, result = fixture(powers={'cost': {0: -5.0, 95: 5.0}})
        cost = select_candidate(request, result, policy(request))
        soc = select_candidate(request, result, policy(request, metric='preferred_soc_deviation'))
        self.assertEqual(cost.selected.profile_id, 'cost')
        self.assertEqual(soc.selected.profile_id, 'balanced')
        self.assertIn('净电费', cost.reason)
        self.assertIn('推荐SOC', soc.reason)
        self.assertLessEqual(len(soc.reason), 50)
        self.assertEqual(cost.selected.plan_version, result.candidates[1].plan_version)

    def test_pv_preference_uses_unabsorbed_not_export(self):
        request, result = fixture(export=True, unused={'balanced': {0: 0.5}, 'cost': {0: 0.25}})
        chosen = select_candidate(request, result, policy(request, metric='pv_unabsorbed_energy_kwh'))
        self.assertEqual(chosen.selected.profile_id, 'pv')

    def test_negative_cost_at_tolerance_boundary_and_just_outside(self):
        for amount, expected in [(0.000004, 'cost'), (0.000008, 'balanced')]:
            with self.subTest(amount=amount):
                request, result = fixture(export=True, unused={'cost': {0: amount}, 'pv': {0: 0.5}})
                chosen = select_candidate(request, result, policy(request, tie_order=['cost','pv','balanced']))
                self.assertEqual(chosen.selected.profile_id, expected)

    def test_ties_compare_to_minimum_without_chaining(self):
        request, result = fixture(export=True, unused={'cost': {0: 0.000004}, 'pv': {0: 0.000008}})
        chosen = select_candidate(request, result, policy(request, tie_order=['pv','cost','balanced']))
        self.assertEqual(chosen.selected.profile_id, 'cost')

    def test_two_demand_layers_precede_customer_metric(self):
        request, result = fixture(powers={'balanced': {0: 8.0, 95: -8.0},
                                         'cost': {0: 4.0, 1: 4.0, 2: 4.0, 95: -12.0},
                                         'pv': {0: 4.0, 95: -4.0}})
        request.constraints.demand_limit_kw = 100.0
        for candidate in result.candidates:
            for point in candidate.plan:
                point.demand_exceed_kw = max(point.grid_import_kw-100.0, 0.0)
            candidate.metrics = calculate_metrics(request, candidate.plan)
        chosen = select_candidate(request, result, policy(request, metric='preferred_soc_deviation'))
        self.assertEqual(chosen.selected.profile_id, 'pv')
        self.assertEqual(chosen.steps[0].remaining_ids, ['cost', 'pv'])
        self.assertEqual(chosen.steps[1].remaining_ids, ['pv'])

    def test_candidate_order_does_not_change_choice_or_comparison(self):
        request, result = fixture()
        outputs = []
        for order in itertools.permutations(result.candidates):
            result.candidates = list(order)
            chosen = select_candidate(request, result, policy(request, tie_order=['pv','balanced','cost']))
            self.assertEqual(chosen.selected.profile_id, 'pv')
            outputs.append((chosen.steps, chosen.reason))
        self.assertTrue(all(item == outputs[0] for item in outputs))

    def test_feasible_can_win_over_optimal(self):
        request, result = fixture(powers={'cost': {0: -5.0, 95: 5.0}})
        result.candidates[1].status = 'feasible'
        self.assertEqual(select_candidate(request, result, policy(request)).selected.profile_id, 'cost')

    def test_corrupt_plan_or_metrics_excluded(self):
        for field in ['plan', 'metrics']:
            with self.subTest(field=field):
                request, result = fixture()
                if field == 'plan': result.candidates[0].plan[0].expected_soc_pct += 1.0
                else: result.candidates[0].metrics.energy_cost -= 1.0
                chosen = select_candidate(request, result, policy(request))
                self.assertEqual(chosen.selected.profile_id, 'cost')
                self.assertEqual(chosen.excluded[0].profile_id, 'balanced')
                self.assertEqual(chosen.excluded[0].code, 'validation_failed')

    def test_use_recomputed_metrics_even_if_supplied_error_is_within_validation_tolerance(self):
        request, result = fixture()
        result.candidates[0].metrics.energy_cost += 0.0000005
        chosen = select_candidate(request, result, policy(request, metric_tolerance=0.0))
        self.assertEqual(chosen.selected.profile_id, 'balanced')

    def test_all_unsuccessful_and_single_valid_without_policy(self):
        request, result = fixture()
        for candidate in result.candidates:
            candidate.status='timeout'; candidate.plan=[]; candidate.metrics=None
        self.assertEqual(select_candidate(request, result).status, 'no_usable_candidate')
        result.candidates[1] = fixture()[1].candidates[1]
        self.assertEqual(select_candidate(request, result).status, 'pending_policy')
        self.assertEqual(select_candidate(request, result, policy(request)).selected.profile_id, 'cost')

    def test_unavailable_device_does_not_select_idle_plan(self):
        request, result = fixture()
        request.capability.available=False
        self.assertEqual(select_candidate(request, result, policy(request)).status, 'device_unavailable')

    def test_mismatched_envelope_is_rejected(self):
        for field, value in [('station_id','other'), ('request_id','other'),
                             ('source_versions',{'other':'v1'}),
                             ('plan_start_at',fixture()[0].input_observed_at),
                             ('input_observed_at',fixture()[0].plan_start_at)]:
            with self.subTest(field=field):
                request, result = fixture()
                setattr(result, field, value)
                with self.assertRaises(ValueError): select_candidate(request, result, policy(request))

    def test_duplicate_missing_or_wrong_plan_version_rejected(self):
        for mutation in ['duplicate','missing','plan_version','profile_version']:
            with self.subTest(mutation=mutation):
                request, result = fixture()
                if mutation=='duplicate': result.candidates[1]=result.candidates[0]
                elif mutation=='missing': result.candidates.pop()
                else: setattr(result.candidates[0],mutation,'wrong')
                with self.assertRaises(ValueError): select_candidate(request, result, policy(request))

    def test_invalid_policy_and_nonfinite_mutated_models_rejected(self):
        request, result = fixture()
        for change in [dict(station_id='other'),dict(metric_tolerance=float('nan')),
                       dict(tie_order=['cost','cost','pv']),dict(metric='unknown'),dict(version=' ')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                select_candidate(request,result,policy(request).model_copy(update=change))
        result.candidates[0].metrics.energy_cost=float('nan')
        with self.assertRaises(ValueError): select_candidate(request,result,policy(request))
        request, result = fixture()
        request.points.pop()
        with self.assertRaises(ValueError): select_candidate(request,result)

    def test_inputs_unchanged_and_audit_snapshot_detached(self):
        request, result = fixture()
        config = policy(request)
        before = [v.model_dump_json() for v in (request,result,config)]
        chosen = select_candidate(request,result,config)
        self.assertEqual(before,[v.model_dump_json() for v in (request,result,config)])
        self.assertEqual(len(chosen.input_sha256),64)
        old_hash=chosen.input_sha256
        config.tie_order.reverse()
        self.assertEqual(chosen.policy.tie_order,['balanced','cost','pv'])
        result.candidates[0].solver_message='changed audit'
        self.assertNotEqual(select_candidate(request,result,config).input_sha256,old_hash)
        self.assertEqual(chosen.usage,'preview_only')
