"""Offline regression cases for the early-valley quality exception."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from m4.settings.load_accuracy import assess, SHANGHAI
from m4.settings.startup_admission import POLICY, require_admission, window
from m4.settings.frozen_baseline import baseline_version
from m4.settings.ems_remaining_plan import EMSRemainingPlanAdapter
from m4.settings.ems_model_update import ModelUpdateError
from m4.tests.test_m4_load_accuracy import row
from m4.tests import test_m4_ems_model_update as model_fixtures


class StartupAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 1, 1, tzinfo=SHANGHAI)
        self.periods = ['gu']*32 + ['ping']*16 + ['gu']*8 + ['feng']*40

    def test_bad_quality_is_allowed_only_in_early_configured_charge_window(self):
        for hour, allowed in ((1, True), (7, True), (8, False), (12, False), (23, False)):
            now = self.now.replace(hour=hour)
            gate = assess(row('35', now=now), 'station-2', now)
            if allowed:
                result = require_admission(gate, 'station-2', self.periods, now)
                self.assertEqual(result['end_at'], now.replace(hour=8, minute=0).isoformat())
            else:
                with self.assertRaises(ValueError):
                    require_admission(gate, 'station-2', self.periods, now)
        self.assertIsNone(window('station-2', self.periods, self.now.replace(hour=7, minute=45)))
        self.assertIsNone(window('station-1', self.periods, self.now))

    def test_no_actual_samples_allowed_but_missing_or_corrupt_evidence_blocked(self):
        evidence = row(now=self.now)
        for point in evidence['points']:
            point.update(actual_value=None, actual_quality=None)
        evidence['current_score'].update(actual_count=0, valid_count=0, mape_percent=None)
        gate = assess(evidence, 'station-2', self.now)
        self.assertIsNotNone(require_admission(gate, 'station-2', self.periods, self.now))
        for corrupt in (None, {**evidence, 'points': evidence['points'][:-1]},
                        {**row(now=self.now), 'current_score': None},
                        {**evidence, 'current_score': None},
                        {**evidence, 'current_score': {**evidence['current_score'], 'run_id': 'wrong'}},
                        {**evidence, 'current_score': {**evidence['current_score'], 'actual_count': 1}}):
            gate = assess(corrupt, 'station-2', self.now)
            with self.assertRaises(ValueError):
                require_admission(gate, 'station-2', self.periods, self.now)

    def test_healthy_quality_uses_normal_path(self):
        gate = assess(row('20', now=self.now), 'station-2', self.now)
        self.assertIsNone(require_admission(gate, 'station-2', self.periods, self.now))


class StartupWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        model_fixtures.ModelUpdateTests.setUp(self)
        self.now = self.now.replace(hour=7)
        self.start = self.now.replace(minute=15)
        end = self.now.replace(hour=8, minute=0)
        self.payload.update(effective_at=self.start.isoformat(),
            valid_until=(self.start+timedelta(minutes=15)).isoformat(),
            end_at=end.isoformat(), finished_at=self.now.isoformat(), source='ems')
        self.payload['plan'] = [dict(timestamp=(self.start+timedelta(minutes=15*i)).isoformat(),
            mode='charge', target_power_kw=20.0) for i in range(3)]
        request = self.payload['request']
        request['plan_start_at'] = self.start.isoformat()
        request['source_versions'].update(startup_policy=POLICY,
            startup_window_end=end.isoformat(), ems_baseline=baseline_version('station-2'),
            startup_tariff_periods=json.dumps(['gu']*32+['ping']*64))
        request['points'] = [dict(timestamp=p['timestamp'], load_forecast_kw=800.0,
            pv_forecast_kw=0.0, tariff_period='gu') for p in self.payload['plan']]
        self.day_row = {**self.row, 'id': 8, 'start_time': '08:00:00',
            'end_time': '12:00:00', 'type': 'discharge'}
        self.reader._read_table.return_value = [self.day_row]

    def preview(self):
        return EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True).preview(
            'station-2', self.payload, self.config, now=self.now)

    def test_preserves_daytime_rows_and_keeps_commissioning_power(self):
        result = self.preview()
        self.assertEqual(result['preserved_record_ids'], [8])
        self.assertFalse(result['operations']['update'])
        self.assertFalse(result['operations']['destroy'])
        body = result['operations']['create'][0]['body']
        self.assertEqual((body['start_time'], body['end_time'], body['kw']), ('07:15:00', '08:00:00', 600))

    def test_cross_boundary_existing_row_blocks_without_transport(self):
        self.reader._read_table.return_value = [{**self.day_row, 'start_time': '07:00:00'}]
        with self.assertRaises(ModelUpdateError):
            self.preview()
        self.opener.open.assert_not_called()

    def test_discharge_or_extended_plan_is_rejected(self):
        original = deepcopy(self.payload)
        self.payload['plan'][0]['mode'] = 'discharge'
        with self.assertRaises(ModelUpdateError):
            self.preview()
        self.payload = original
        self.payload['end_at'] = self.now.replace(hour=12, minute=0).isoformat()
        with self.assertRaises(ModelUpdateError):
            self.preview()


class StartupRollingTests(unittest.TestCase):
    def test_rolling_request_and_replay_stop_before_daytime(self):
        from m4.tests.m4_optimizer_test_support import make_request
        from m4.settings.rolling_plans import prepare_remaining_request
        base = make_request(station_id='station-2')
        now = base.plan_start_at + timedelta(hours=1, minutes=1)
        periods = ['gu']*32 + ['ping']*64
        raw = base.model_dump(mode='json')
        for point, period in zip(raw['points'], periods):
            point['tariff_period'] = period
        sample = dict(station_id='station-2', status='ready', observed_at=now.isoformat())
        inputs = dict(station_id='station-2', can_compare=True, request=raw,
            baseline=dict(schedule=[dict(start_time='00:00:00', end_time='08:00:00',
                repeat='daily', mode='charge', power_kw=20.0)]),
            sources=dict(current_soc=dict(sample, soc_pct=50.0),
                current_power=dict(sample, power_kw=0.0), tariff=dict(period_types=periods),
                load=dict(accuracy_gate=assess(row('35', now=now), 'station-2', now))))
        config = SimpleNamespace(station_id='station-2', parameters=SimpleNamespace(max_input_age_seconds=900))
        request, replay, _, _, _ = prepare_remaining_request(config, inputs, now)
        self.assertEqual(request.plan_start_at, now.replace(minute=15))
        self.assertEqual(request.points[-1].timestamp+timedelta(minutes=15), now.replace(hour=8, minute=0))
        self.assertEqual(request.horizon_points, 27)
        self.assertEqual(len(replay), 27)
        self.assertTrue(all(p.mode in ('charge', 'idle') for p in replay))
        self.assertEqual(inputs['request'], raw)

    def test_temporary_daily_plan_is_rebuilt_after_recovery_or_window_exit(self):
        from m4.settings.rolling_plans import PlanningCoordinator
        for hour, quality, rebuild in ((1, 'blocked', False), (1, 'ready', True), (8, 'blocked', True)):
            now = datetime(2026, 9, 17, hour, 1, tzinfo=SHANGHAI)
            daily, rolling = Mock(), Mock()
            daily.latest.return_value = dict(status='completed', result=dict(record=dict(
                daily_comparison=dict(date=now.date().isoformat()))), request=dict(source_versions=dict(
                    startup_policy=POLICY, startup_tariff_periods=json.dumps(['gu']*32+['ping']*64))))
            with patch('m4.settings.rolling_plans.datetime') as clock, patch(
                    'm4.settings.rolling_plans.read_gate', return_value={'status': quality}):
                clock.now.return_value = now
                PlanningCoordinator(daily, rolling).start('station-2')
            if rebuild:
                daily.start.assert_called_once_with('station-2')
                rolling.start.assert_not_called()
            else:
                rolling.start.assert_called_once_with('station-2')
                daily.start.assert_not_called()
