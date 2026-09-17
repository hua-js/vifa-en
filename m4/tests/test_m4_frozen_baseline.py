"""Regression: changing the execution schedule must not change the baseline."""
from copy import deepcopy
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from m4.settings.auto_plans import AutomaticPlans


class FrozenBaselineTests(unittest.TestCase):
    def test_real_live_reader_allows_missing_execution_schedule_only_for_fixed_path(self):
        from m4.settings.live_inputs import LiveInputService
        from m4.settings.frozen_baseline import planning_controls
        from m4.settings.schedule_power import cabinet_power_limits
        raw = dict(station_id='station-2', version='live-b', status='ready',
            power_scope='cabinet', source_health=dict(schedule='unavailable'),
            schedule=[], demand=dict(need_kw=1000))
        live = LiveInputService(Mock(), SimpleNamespace(fetch=lambda station: deepcopy(raw)))
        with self.assertRaises(ValueError):
            live._controls('station-2')
        fixed = planning_controls(live._controls('station-2', validate_schedule=False))
        self.assertEqual(cabinet_power_limits(fixed), dict(max_charge_kw=100, max_discharge_kw=90))
        raw['demand']['need_kw'] = -1
        with self.assertRaises(ValueError):
            live._controls('station-2', validate_schedule=False)

    def test_live_schedule_is_not_a_comparison_dependency(self):
        from m4.settings.frozen_baseline import planning_controls
        raw = dict(station_id='station-2', version='live-a', status='ready',
            power_scope='cabinet', control_policy_version='test',
            source_health=dict(schedule='ready', demand='ready'),
            schedule=[dict(start_time='19:00:00', end_time='21:00:01',
                mode='discharge', power_kw=1, repeat='daily')],
            demand=dict(need_kw=1000), storage_capacity=dict(energy_capacity_kwh=3132))
        first = planning_controls(raw)
        altered = deepcopy(raw)
        altered.update(version='live-b', schedule=[], warnings=['调试中'])
        altered['source_health']['schedule'] = 'unavailable'
        second = planning_controls(altered)
        self.assertEqual(first['version'], second['version'])
        self.assertEqual(first['schedule'][-1]['end_time'], '21:00:00')
        self.assertEqual(first['schedule'][-1]['power_kw'], 90)
        self.assertEqual(raw['schedule'][0]['end_time'], '21:00:01')
        altered['demand']['need_kw'] = 900
        self.assertNotEqual(first['version'], planning_controls(altered)['version'])

    def test_baseline_change_invalidates_version(self):
        from m4.settings.frozen_baseline import baseline_version
        from shared.project import get_project
        from unittest.mock import patch
        station = deepcopy(get_project().station('station-2'))
        old = baseline_version('station-2')
        station.ems_baseline['schedule'][0]['power_kw'] += 1
        with patch('m4.settings.frozen_baseline.get_project', return_value=SimpleNamespace(
                station=lambda _: station)):
            self.assertNotEqual(old, baseline_version('station-2'))

    def test_failed_window_retries_are_bounded_and_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Mock()
            service.running = set()
            service.start.return_value = {'status': 'running'}
            service.latest.return_value = {'status': 'failed'}
            auto = AutomaticPlans(service, Path(folder))
            auto.tick(1800)
            auto.tick(1815)
            self.assertEqual(service.start.call_count, 2)
            auto.tick(1860)
            self.assertEqual(service.start.call_count, 4)
            AutomaticPlans(service, Path(folder)).tick(1920)
            self.assertEqual(service.start.call_count, 6)
            auto.tick(1980)
            self.assertEqual(service.start.call_count, 6)
            auto.tick(2700)
            self.assertEqual(service.start.call_count, 8)

    def test_success_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Mock(running=set())
            service.start.return_value = {'status': 'running'}
            service.latest.return_value = {'status': 'completed'}
            auto = AutomaticPlans(service, Path(folder))
            auto.tick(1800)
            auto.tick(1860)
            self.assertEqual(service.start.call_count, 2)

    def test_synchronous_failure_retries_without_reading_old_success(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Mock(running=set())
            service.start.side_effect = lambda station: (_ for _ in ()).throw(ValueError('start failed'))
            service.latest.return_value = {'status': 'completed'}
            auto = AutomaticPlans(service, Path(folder))
            with self.assertLogs('m4.settings.auto_plans', level='ERROR'):
                auto.tick(1800)
                auto.tick(1860)
            self.assertEqual(service.start.call_count, 4)
            service.latest.assert_not_called()

    def test_status_read_failure_does_not_block_other_station(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Mock(running=set())
            service.start.return_value = {'status': 'running'}
            def latest(station):
                if station == 'station-1':
                    raise ValueError('unreadable')
                return {'status': 'failed'}
            service.latest.side_effect = latest
            auto = AutomaticPlans(service, Path(folder))
            auto.tick(1800)
            with self.assertLogs('m4.settings.auto_plans', level='ERROR'):
                auto.tick(1860)
            self.assertEqual(service.start.call_count, 3)
            service.start.assert_called_with('station-2')

    def test_reference_soc_applies_in_idle_gaps_and_current_safety_wins(self):
        from m4.tests.test_m4_optimizer_pv_policy import pv_request
        from m4.settings.ems_simulation import simulate_ems_day
        request = pv_request(pv=300, soc=79, export=True, export_limit=1000)
        schedule = [dict(start_time='01:00:00', end_time='01:15:00',
            mode='charge', power_kw=0, repeat='daily', soc_min_pct=20, soc_max_pct=80)]
        for upper in (80, 79.5):
            request.constraints.soc_max_pct = upper
            plan, _ = simulate_ems_day(request.capability, request.constraints,
                request.points, schedule, pv_dispatch_policy='load_first_storage_priority')
            self.assertAlmostEqual(plan[0].expected_soc_pct, upper)
        request.capability.initial_soc_pct = 21
        request.points[0].pv_forecast_kw = 0
        request.points[0].load_forecast_kw = 200
        request.constraints.demand_limit_kw = 0
        plan, _ = simulate_ems_day(request.capability, request.constraints,
            request.points, schedule)
        self.assertAlmostEqual(plan[0].expected_soc_pct, 20)

    def test_unready_error_keeps_specific_failed_check(self):
        from m4.settings.rolling_plans import prepare_remaining_request
        from datetime import datetime, timezone
        with self.assertRaisesRegex(ValueError, 'EMS 全天模拟基线：时段重叠'):
            prepare_remaining_request(SimpleNamespace(station_id='station-2'),
                dict(station_id='station-2', can_compare=False, checks=[dict(
                    label='EMS 全天模拟基线', status='missing', detail='时段重叠')]),
                datetime.now(timezone.utc))


if __name__ == '__main__':
    unittest.main()
