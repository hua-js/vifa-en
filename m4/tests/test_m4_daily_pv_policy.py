"""Guard the incident's missing midday forecast and saved-plan reuse."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory

from m4.settings.daily_inputs import DailyInputService
from m4.settings.daily_plans import DailyPlanService
from m4.settings.daily_pv_policy import POLICY, fill_night_gaps
from m4.settings.timeseries import InputDataError


class DailyPVPolicyTests(TestCase):
    def setUp(self):
        self.start = datetime.fromisoformat('2026-09-16T00:00:00+08:00')

    def test_night_gaps_fill_but_existing_predictions_are_preserved(self):
        values = [None]*20 + [100.0]*60 + [None]*16
        values[0] = 7.0
        filled, missing = fill_night_gaps(values, self.start)
        self.assertEqual(filled[0], 7.0)
        self.assertEqual(filled[1:20], [0.0]*19)
        self.assertEqual(filled[20:80], [100.0]*60)
        self.assertEqual(filled[80:], [0.0]*16)
        self.assertEqual(len(missing), 35)
        self.assertIsNone(values[1])

    def test_each_daylight_boundary_and_midday_gap_blocks(self):
        for slot in (20, 48, 56, 79):
            with self.subTest(slot=slot):
                values = [100.0]*96
                values[slot] = None
                with self.assertRaisesRegex(InputDataError, '白天光伏预测不完整'):
                    fill_night_gaps(values, self.start)

    def test_previous_day_batch_ending_at_0615_cannot_create_daily_input(self):
        source = dict(values=[0.0]*24+[7.413345]+[None]*71, coverage_points=25,
            forecast_start=(self.start-timedelta(hours=17, minutes=45)).isoformat(),
            forecast_end=(self.start+timedelta(hours=6, minutes=15)).isoformat())
        service = DailyInputService(SimpleNamespace(client=None))
        with patch('m4.settings.daily_inputs.load_pv_forecast', return_value=source), patch('m4.settings.daily_inputs.validate_pv_source'):
            with self.assertRaisesRegex(InputDataError, '白天光伏预测不完整'):
                service._pv('station-2', self.start, self.start)

    def test_night_only_gap_source_has_new_policy(self):
        source = dict(values=[None]*20+[100.0]*76, coverage_points=76,
            forecast_start=(self.start+timedelta(hours=5)).isoformat(),
            forecast_end=(self.start+timedelta(days=1,hours=5)).isoformat())
        service = DailyInputService(SimpleNamespace(client=None))
        with patch('m4.settings.daily_inputs.load_pv_forecast', return_value=source), patch('m4.settings.daily_inputs.validate_pv_source'):
            result=service._pv('station-2', self.start, self.start)
        self.assertEqual(result['gap_policy'], POLICY)
        self.assertEqual(result['zero_filled_points'], 20)
        self.assertEqual(result['values'][20:], [100.0]*76)

    def test_legacy_completed_job_is_not_reused_as_current_plan(self):
        with TemporaryDirectory() as root:
            service = DailyPlanService(None, None, root)
            service.jobs['station-2'] = dict(station_id='station-2', status='completed',
                request=dict(source_versions=dict(pv_gap_policy='outside_forecast_window_zero')))
            result=service.latest('station-2')
            self.assertEqual(result['status'], 'empty')
            self.assertIsNone(result['result'])
            self.assertEqual(service.jobs['station-2']['status'], 'completed')
