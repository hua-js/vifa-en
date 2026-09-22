"""Offline regression cases for daily-only coordination; no upstream calls."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from m4.settings.daily_dispatch import DailyScheduleAdapter, SCHEMA
from m4.settings.daily_schedule import DailyScheduleService, POLICY, CONFIRMED
from m4.settings.ems_model_update import ZONE, ModelUpdateError
from m4.tests import test_m4_ems_model_update as model_fixtures
from m4.tests import test_m4_ems_remaining_plan as remaining_fixtures
from m4.settings.ems_remaining_plan import EMSRemainingPlanAdapter


class TodayValidTests(unittest.TestCase):
    setUp = model_fixtures.ModelUpdateTests.setUp
    full_day = remaining_fixtures.RemainingPlanTests.full_day

    def test_threshold_and_today_valid_setpoint_preserve_advice(self):
        self.full_day()
        for i, power in enumerate((10.0, 10.1, 300.0)):
            self.payload['plan'][i].update(mode='discharge', target_power_kw=power)
        before = deepcopy(self.payload)
        result = EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True).preview(
            'station-2', self.payload, self.config, now=self.now)
        self.assertEqual(len(result['schedule']), 1)
        self.assertEqual(result['schedule'][0]['start_time'], '14:30:00')
        self.assertEqual(result['schedule'][0]['kw'], 600)
        self.assertEqual(result['schedule'][0]['repeat'], '今日有效')
        self.assertEqual(self.payload, before)

    def test_legacy_cutover_loses_daily_repeat(self):
        self.full_day()
        self.reader._read_table.return_value = [dict(self.row,
            start_time='14:00:00', end_time='15:00:00', kw=600)]
        result = EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True).preview(
            'station-2', self.payload, self.config, now=self.now)
        update = result['operations']['update'][0]['body']
        self.assertEqual(update, dict(end_time='14:15:00', repeat='今日有效'))

    def test_expiry_only_deletes_old_owned_rows_and_migrates_legacy(self):
        old = dict(self.row, id=8, m4_run_id=str(uuid4()), m4_plan_date='2026-09-16')
        other = dict(old, id=9, es_sn=['ES01'])
        self.reader._read_table.return_value = [self.row, old, other]
        payload = dict(schema_version=SCHEMA, kind='expire', station_id='station-2',
            date='2026-09-17', run_id=str(uuid4()), effective_at=self.start.isoformat())
        adapter = DailyScheduleAdapter(self.reader)
        result = adapter.preview('station-2', payload, self.config, now=self.now)
        self.assertEqual([op['query']['filterByTk'] for op in result['operations']['destroy']], [8])
        self.assertEqual(result['operations']['update'][0]['body'], {'repeat': '今日有效'})
        self.assertEqual(result['operations']['update'][0]['query']['filterByTk'], 7)
        self.assertFalse(result['operations']['create'])

    def test_unknown_owned_date_blocks_cleanup(self):
        self.reader._read_table.return_value = [dict(self.row, m4_run_id=str(uuid4()))]
        payload = dict(schema_version=SCHEMA, kind='expire', station_id='station-2',
            date='2026-09-17', run_id=str(uuid4()), effective_at=self.start.isoformat())
        with self.assertRaisesRegex(ModelUpdateError, '日期'):
            DailyScheduleAdapter(self.reader).preview('station-2', payload, self.config, now=self.now)


class DailyCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daily = Mock(root=Path(self.temp.name), running=set())
        self.config = SimpleNamespace(station_id='station-2', version='v1')
        self.daily.store.get.return_value = self.config
        self.writer = Mock()
        self.writer.status.return_value = {'enabled': True}
        self.service = DailyScheduleService(self.daily, self.writer)
        self.day = datetime.now(ZONE).date().isoformat()
        self.state = dict(station_id='station-2', date=self.day, policy_version=POLICY,
            status='waiting', expired_rows_checked=True)
        self.service._save(self.state)
        self.daily.latest.return_value = dict(status='empty')

    def test_frozen_plan_survives_restart_without_reading_new_candidate(self):
        self.state.update(status='completed', confirmed_daily_run_id=str(uuid4()))
        self.service._save(self.state)
        restarted = DailyScheduleService(self.daily, self.writer)
        restarted._run('station-2', None)
        self.writer.submit.assert_not_called()
        self.daily.latest.assert_not_called()
        self.daily.start.assert_not_called()

    def test_interrupted_write_is_not_retried(self):
        self.state.update(status='writing', pending_run_id=str(uuid4()))
        self.service._save(self.state)
        self.assertEqual(self.service.latest('station-2')['status'], 'blocked')
        self.service._run('station-2', None)
        self.writer.submit.assert_not_called()
        self.daily.start.assert_not_called()

    def test_stale_explicit_version_cannot_become_automatic_dispatch(self):
        self.service._run('station-2', str(uuid4()))
        self.writer.submit.assert_not_called()
        self.daily.start.assert_not_called()
        self.assertEqual(self.service.latest('station-2')['status'], 'failed')

    def test_daily_solver_does_not_occupy_night_coordinator(self):
        self.daily.running.add('station-2')
        self.assertNotIn('station-2', self.service.running)
        with patch('m4.settings.daily_schedule.configured_window', return_value=True), \
                patch('m4.settings.daily_schedule.read_window', return_value={'period_types': []}), \
                patch('m4.settings.daily_schedule.build_payload', return_value={}), \
                patch('m4.settings.daily_schedule.night_payload', return_value={'kind': 'night_fallback'}), \
                patch.object(self.service, '_submit', return_value=True) as submit:
            self.daily.latest.return_value = {'status': 'running'}
            self.service._run('station-2', None)
        submit.assert_called_once()
        self.daily.start.assert_not_called()

    def test_control_version_change_regenerates_before_first_dispatch(self):
        self.daily.latest.return_value = dict(status='completed', run_id=str(uuid4()),
            request={'source_versions': {'configuration': 'v1', 'controls': 'old'}},
            result={'record': {'daily_comparison': {'date': self.day, 'recommended': {'plan': []}}}})
        with patch('m4.settings.daily_schedule.planning_controls', return_value={'version': 'new'}), \
                patch('m4.settings.daily_schedule.configured_window', return_value=False):
            self.service._run('station-2', None)
        self.writer.submit.assert_not_called()
        self.daily.start.assert_called_once_with('station-2')

    def test_confirmed_night_window_is_not_rewritten(self):
        self.state['active_plan'] = {'kind': 'night_fallback', 'status': CONFIRMED}
        self.service._save(self.state)
        with patch('m4.settings.daily_schedule.build_payload') as build:
            self.service._run('station-2', None)
        build.assert_not_called()
        self.writer.submit.assert_not_called()
        self.daily.start.assert_called_once_with('station-2')


class DailyPayloadTests(unittest.TestCase):
    def setUp(self):
        from m4.tests.m4_optimizer_test_support import make_request
        from m4.optimizer.contracts import GRID_CHARGING_POLICY
        self.request = make_request(station_id='station-2').model_dump(mode='json')
        self.request['source_versions'].update(configuration='v1', grid_charging_policy=GRID_CHARGING_POLICY)
        for source in self.request['points']:
            source['tariff_period'] = 'ping'
        self.points = [dict(timestamp=p['timestamp'], mode='idle', target_power_kw=0.0,
            expected_soc_pct=50.0, grid_import_kw=80.0, grid_export_kw=0.0,
            pv_unabsorbed_kw=0.0, demand_exceed_kw=0.0) for p in self.request['points']]
        self.daily = dict(station_id='station-2', status='completed', run_id=str(uuid4()),
            request=self.request, result={'record': {'daily_comparison': dict(
                date='2026-09-04', recommended_source='optimized', recommended={'plan': self.points})}})
        self.config = SimpleNamespace(station_id='station-2', version='v1')
        self.now = datetime(2026, 9, 4, 12, 1, tzinfo=ZONE)

    def build(self):
        from m4.settings.daily_dispatch import daily_payload
        with patch('m4.settings.daily_dispatch.matches_current_daily_policy', return_value=True):
            return daily_payload(self.daily, self.config, self.now)

    def test_dispatch_only_crops_future_without_resolving_or_mutating_daily(self):
        original = deepcopy(self.daily)
        payload = self.build()
        self.assertEqual(len(payload['plan']), 47)
        self.assertEqual(payload['effective_at'], '2026-09-04T12:15:00+08:00')
        self.assertEqual(payload['plan'], self.points[49:])
        self.assertEqual(payload['full_request'], original['request'])
        self.assertEqual(payload['request']['capability']['initial_soc_pct'], 50)
        self.assertEqual(self.daily, original)

    def test_configuration_change_blocks_daily_dispatch(self):
        self.config.version = 'v2'
        with self.assertRaisesRegex(ModelUpdateError, '配置'):
            self.build()

    def test_gap_in_whole_day_is_rejected_before_cropping(self):
        self.points[0]['timestamp'] = self.points[1]['timestamp']
        with self.assertRaisesRegex(ModelUpdateError, '不连续'):
            self.build()

    def test_previous_day_cannot_be_submitted(self):
        self.now += timedelta(days=1)
        with self.assertRaisesRegex(ModelUpdateError, '尚未就绪'):
            self.build()

    def test_last_quarter_never_creates_next_day_record(self):
        self.now = self.now.replace(hour=23, minute=45)
        with self.assertRaisesRegex(ModelUpdateError, '未来时段'):
            self.build()
