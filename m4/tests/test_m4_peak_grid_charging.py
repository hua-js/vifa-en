"""Offline regressions for peak charging, comparison and plan reuse boundaries."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np

from m4.optimizer.contracts import GRID_CHARGING_POLICY, OptimizationRequest
from m4.optimizer.metrics import calculate_metrics
from m4.optimizer.model import build_model
from m4.optimizer.solver import solve_milp
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import ResultValidationError, validate_candidate
from m4.settings.daily_comparison import compare_daily_plan, comparison_input_sha256
from m4.settings.daily_policy import matches_current_daily_policy, daily_policy_version, physical_grid_policy
from m4.settings.objectives import get_daily_profiles
from m4.settings.ems_model_update import ModelUpdateError, validate_dispatch_safety
from m4.settings.ems_simulation import EMS_BASELINE_POLICY, simulate_ems_day
from m4.settings.rolling_plans import POLICY, ZONE, RollingPlanService
from m4.tests.m4_optimizer_test_support import make_candidate, make_request
from shared.project import get_project


def comparison_case(tariff="feng"):
    request = make_request(station_id="station-1", pv_dispatch_policy="load_first_economic")
    request.points = [p.model_copy(update=dict(
        pv_forecast_kw=0.0, tariff_period=tariff if 64 <= i < 72 else "gu",
        buy_price_per_kwh=10.0 if 64 <= i < 72 else 0.1))
        for i, p in enumerate(request.points)]
    request.source_versions["baseline_policy"] = EMS_BASELINE_POLICY
    request.constraints.terminal_soc_tolerance_pct = 100.0
    schedule = [dict(start_time="16:00:00", end_time="18:00:00", repeat="daily",
                     mode="charge", power_kw=20.0)]
    plan, simulation = simulate_ems_day(request.capability, request.constraints,
        request.points, schedule, pv_dispatch_policy=request.pv_dispatch_policy)
    baseline = dict(schema_version="m4-ems-daily-baseline-v1", basis="ems_rule_simulation",
        station_id=request.station_id, input_sha256=comparison_input_sha256(request),
        controls_version="audit", controller_version=EMS_BASELINE_POLICY,
        initial_state_version="audit", initial_state_at=request.plan_start_at.isoformat(),
        initial_soc_pct=request.capability.initial_soc_pct, schedule=schedule,
        simulation=simulation, plan=[p.model_dump(mode="json") for p in plan])
    return request, baseline, plan


class PeakGridChargingTests(unittest.TestCase):
    def test_solver_allows_only_pv_surplus_in_both_peak_periods(self):
        for tariff in ("jian", "feng", "ping", "gu"):
            for pv in (0.0, 140.0):
                for policy in ("legacy", "load_first_economic", "load_first_storage_priority", "load_first_export_priority"):
                    with self.subTest(tariff=tariff, pv=pv, policy=policy):
                        base = make_request()
                        start = base.plan_start_at.replace(hour=23, minute=45)
                        request = OptimizationRequest.model_validate({**base.model_dump(),
                            "plan_start_at": start, "input_observed_at": start, "horizon_points": 1,
                            "source_versions": {"planning_basis": "remaining-day-v1"},
                            "pv_dispatch_policy": policy,
                            "points": [base.points[0].model_copy(update=dict(timestamp=start,
                                tariff_period=tariff, pv_forecast_kw=pv))],
                            "constraints": base.constraints.model_copy(update=dict(
                                terminal_soc_tolerance_pct=100.0, grid_import_limit_kw=400.0,
                                grid_export_enabled=True, grid_export_limit_kw=200.0))})
                        built = build_model(request)
                        objective = np.zeros(built.index.size)
                        objective[built.index.charge] = -1.0
                        solved = solve_milp(built.problem, objective, locks=(), time_limit_seconds=3, mip_rel_gap=0)
                        self.assertEqual(solved.status, "optimal")
                        expected = 100.0
                        if tariff in ("jian", "feng") or (policy != "legacy" and pv > 100):
                            expected = max(pv - 100, 0)
                        if policy == "load_first_export_priority" and pv > 100:
                            expected = 0.0
                        self.assertAlmostEqual(solved.x[built.index.charge.start], expected)
                        if tariff in ("jian", "feng") and pv > 100:
                            self.assertAlmostEqual(solved.x[built.index.grid_import.start], 0.0)

    def test_independent_validator_rejects_peak_grid_charging(self):
        for tariff in ("jian", "feng"):
            with self.subTest(tariff=tariff):
                request, _, plan = comparison_case(tariff)
                candidate = make_candidate(request, plan, calculate_metrics(request, plan))
                with self.assertRaisesRegex(ResultValidationError, "peak grid charging"):
                    validate_candidate(request, candidate)

    def test_invalid_ems_baseline_remains_comparable_but_is_not_recommended(self):
        request, baseline, _ = comparison_case()
        result = compare_daily_plan(request, None, baseline=baseline, controls_version="audit")
        self.assertIsNotNone(result["baseline"])
        self.assertIsNotNone(result["baseline_cost_yuan"])
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["recommended"])
        self.assertIsNone(result["recommended_source"])
        self.assertIn("尖、峰", result["reason"])

    def test_invalid_candidate_cannot_fall_back_to_invalid_ems(self):
        request, baseline, plan = comparison_case()
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        result = compare_daily_plan(request, candidate, baseline=baseline, controls_version="audit")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["recommended"])
        self.assertNotIn("沿用", result["reason"])

    def test_valid_cheaper_candidate_can_replace_invalid_ems_baseline(self):
        request, baseline, original = comparison_case()
        schedule = [dict(start_time="00:00:00", end_time="02:00:00", repeat="daily",
                         mode="charge", power_kw=20.0)]
        plan, _ = simulate_ems_day(request.capability, request.constraints, request.points,
            schedule, pv_dispatch_policy=request.pv_dispatch_policy)
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        result = compare_daily_plan(request, candidate, baseline=baseline, controls_version="audit",
            terminal_soc_target_pct=original[-1].expected_soc_pct)
        self.assertEqual(result["status"], "optimized", result["reason"])
        self.assertEqual(result["recommended_source"], "optimized")
        self.assertGreaterEqual(result["net_savings_yuan"], 100)

    def test_flat_period_ems_fallback_is_preserved(self):
        request, baseline, _ = comparison_case("ping")
        result = compare_daily_plan(request, None, baseline=baseline, controls_version="audit")
        self.assertEqual(result["status"], "ems")
        self.assertEqual(result["recommended"], result["baseline"])

    def test_dispatch_recomputes_peak_grid_charging_instead_of_trusting_plan_fields(self):
        request, _, plan = comparison_case()
        points = [p.model_dump(mode="json") for p in plan]
        for point in points:
            point["grid_import_kw"] = 0.0
        with self.assertRaisesRegex(ModelUpdateError, "尖、峰"):
            validate_dispatch_safety(request.model_dump(mode="json"), points)

    def test_dispatch_allows_peak_pv_surplus_charging(self):
        request, _, plan = comparison_case()
        raw = request.model_dump(mode="json")
        raw['points'] = [dict(raw['points'][64], pv_forecast_kw=140.0)]
        point = dict(plan[64].model_dump(mode="json"), target_power_kw=30.0)
        validate_dispatch_safety(raw, [point])
        point['target_power_kw'] = 41.0
        with self.assertRaisesRegex(ModelUpdateError, "尖、峰"):
            validate_dispatch_safety(raw, [point])

    def test_both_stations_reject_old_daily_charging_policy(self):
        for station in ('station-1', 'station-2'):
            with self.subTest(station=station):
                payload = make_request(station_id=station).model_dump(mode='json')
                payload.update(pv_midday_economic=True,
                    profiles=[p.model_dump(mode='json') for p in get_daily_profiles(station)])
                payload['constraints']['grid_import_limit_kw'] = get_project().station(station).grid_import_limit_kw
                for point in payload['points']:
                    point['tariff_period'] = 'ping'
                payload['source_versions'].update(pv_export_policy='pv-export-flat-tariff-v1',
                    pv_export_price='0', project_configuration=get_project().fingerprint,
                    daily_policy=daily_policy_version(station), physical_grid_policy=physical_grid_policy(station),
                    economic_policy='station-1-cost-first-v1', grid_charging_policy=GRID_CHARGING_POLICY)
                if station == 'station-2':
                    payload['peak_reserve_policy'] = dict(version='peak-reserve-v4', terminal_soc_min_pct=20.0)
                    from m4.settings.terminal_policy import POLICY as TERMINAL_POLICY
                    payload['points'][0]['tariff_period'] = 'gu'
                    payload['source_versions'].update(terminal_policy=TERMINAL_POLICY,
                        terminal_inventory_price=format(payload['points'][0]['buy_price_per_kwh'], '.17g'))
                self.assertTrue(matches_current_daily_policy(station, payload))
                payload['points'][0]['tariff_period'] = None
                self.assertFalse(matches_current_daily_policy(station, payload))
                payload['points'][0]['tariff_period'] = 'ping'
                del payload['source_versions']['grid_charging_policy']
                self.assertFalse(matches_current_daily_policy(station, payload))

    def test_current_candidate_with_missing_tariff_is_rejected(self):
        request, _, plan = comparison_case('ping')
        request.source_versions['grid_charging_policy'] = GRID_CHARGING_POLICY
        request.points[64].tariff_period = None
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        with self.assertRaisesRegex(ResultValidationError, 'tariff period'):
            validate_candidate(request, candidate)

    def test_rolling_invalid_fallback_stops_before_preview_and_write(self):
        request, _, plan = comparison_case()
        request.source_versions['configuration'] = 'audit'
        now = datetime.now(ZONE)
        with TemporaryDirectory() as directory:
            daily = Mock(root=Path(directory))
            daily.latest.return_value = dict(status='completed', request=request.model_dump(mode='json'),
                result=dict(record=dict(daily_comparison=dict(date=now.date().isoformat(), status='blocked'))))
            daily.store.get.return_value = SimpleNamespace(version='audit')
            daily.inputs.fetch.return_value = dict(plan_start_at=now.replace(hour=0).isoformat(),
                request=request.model_dump(mode='json'), sources={})
            adapter, writer = Mock(), Mock()
            service = RollingPlanService(daily, ems_remaining_adapter=adapter, ems_table_writer=writer)
            with patch('m4.settings.rolling_plans.configured_window', return_value=False), \
                    patch('m4.settings.rolling_plans.read_correction', return_value={}), \
                    patch('m4.settings.rolling_plans.prepare_remaining_request',
                          return_value=(request, plan, {}, {}, plan[-1].expected_soc_pct)):
                service._run('station-1', 'audit')
            self.assertEqual(service.jobs['station-1']['status'], 'failed')
            self.assertIn('尖、峰', service.jobs['station-1']['message'])
            adapter.preview.assert_not_called()
            self.assertEqual(writer.mock_calls, [])

    def test_old_rolling_charging_policy_is_stale_before_reuse(self):
        with TemporaryDirectory() as directory:
            daily = SimpleNamespace(root=Path(directory), _path=lambda station: None,
                store=SimpleNamespace(get=lambda station: SimpleNamespace(version='audit')),
                latest=lambda station: dict(run_id='audit'))
            service = RollingPlanService(daily)
            service.jobs["station-1"] = dict(station_id="station-1", policy_version=POLICY,
                status="completed", result=dict(request=dict(source_versions={}),
                    configuration_version='audit', daily_run_id='audit',
                    valid_until=(make_request().plan_start_at + timedelta(days=365)).isoformat()))
            self.assertEqual(service.latest("station-1")["status"], "stale")

    def test_new_model_version_identifies_peak_grid_charging_policy(self):
        version = M4Optimizer(model_version="audit")._effective_model_version(make_request())
        self.assertIn("peak-grid-charging-v1", version)
