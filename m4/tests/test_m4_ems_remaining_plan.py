"""Offline cases for remaining-day CRUD previews; never submit any operations."""
import unittest
from datetime import timedelta

from m4.settings.ems_model_update import ModelUpdateError
from m4.settings.ems_remaining_plan import EMSRemainingPlanAdapter
from m4.tests import test_m4_ems_model_update as fixtures


class RemainingPlanTests(unittest.TestCase):
    setUp = fixtures.ModelUpdateTests.setUp
    def remaining(self):
        return EMSRemainingPlanAdapter(self.reader).preview(
            'station-2', self.payload, self.config, now=self.now)

    def full_day(self):
        count = int((self.start.replace(hour=0, minute=0)+timedelta(days=1)-self.start).total_seconds()/900)
        self.payload['plan'] = [dict(timestamp=(self.start+timedelta(minutes=15*i)).isoformat(),
            mode='idle', target_power_kw=0) for i in range(count)]
        self.payload['request']['points'] = [dict(timestamp=p['timestamp'], load_forecast_kw=800,
            pv_forecast_kw=0) for p in self.payload['plan']]

    def test_merge_contiguous_equal_power_and_keep_idle_gap(self):
        self.full_day()
        for i in (0, 1, 3):
            self.payload['plan'][i].update(mode='discharge', target_power_kw=540)
        result = self.remaining()
        self.assertEqual(len(result['operations']['create']), 2)
        self.assertEqual(result['schedule'][0]['end_time'], '14:45:00')
        self.assertEqual(result['schedule'][1]['start_time'], '15:00:00')
        self.assertEqual(result['schedule'][0]['kw'], 90)
        self.assertEqual(result['preserved_record_ids'], [7])
        self.assertFalse(result['execution_ready'])
        self.assertIsNone(result['execution_order'])

    def test_same_future_slot_updates_and_idle_removes_old_future(self):
        self.full_day()
        self.payload['plan'][0].update(mode='discharge', target_power_kw=540)
        first = dict(self.row, id=8, start_time='14:15:00', end_time='14:30:00', type='discharge', kw=80)
        stale = dict(self.row, id=9, start_time='16:00:00', end_time='17:00:00')
        self.reader._read_table.return_value = [self.row, first, stale]
        result = self.remaining()
        self.assertEqual(len(result['operations']['create']), 0)
        self.assertEqual(result['operations']['update'][0]['query']['filterByTk'], 8)
        self.assertEqual(result['operations']['destroy'][0]['query']['filterByTk'], 9)
        self.opener.open.assert_not_called()

    def test_active_row_crossing_cutover_is_not_silently_changed(self):
        self.full_day()
        self.reader._read_table.return_value = [dict(self.row, start_time='14:00:00', end_time='18:00:00')]
        with self.assertRaisesRegex(ModelUpdateError, '连续切换'):
            self.remaining()

    def test_missing_tail_or_gap_or_future_overpower_rejected(self):
        self.full_day()
        self.payload['plan'].pop()
        with self.assertRaises(ModelUpdateError):
            self.remaining()
        self.full_day()
        self.payload['plan'][2]['timestamp'] = self.payload['plan'][1]['timestamp']
        with self.assertRaises(ModelUpdateError):
            self.remaining()
        self.full_day()
        self.payload['plan'][2].update(mode='discharge', target_power_kw=1000)
        with self.assertRaises(ModelUpdateError):
            self.remaining()

    def test_identical_future_row_is_not_rewritten(self):
        self.full_day()
        self.payload['plan'][0].update(mode='discharge', target_power_kw=540)
        self.reader._read_table.return_value = [dict(self.row, id=8,
            start_time='14:15:00', end_time='14:30:00', type='discharge', kw=90)]
        result = self.remaining()
        self.assertEqual(result['unchanged_record_ids'], [8])
        self.assertFalse(any(result['operations'].values()))

    def test_demand_and_soc_are_recomputed_from_forecasts_and_power(self):
        self.full_day()
        self.payload['request']['constraints']['demand_limit_kw'] = 700
        with self.assertRaisesRegex(ModelUpdateError, '需量'):
            self.remaining()
        self.payload['request']['constraints']['demand_limit_kw'] = 2000
        self.payload['request']['capability']['initial_soc_pct'] = 2
        self.payload['plan'][0].update(mode='discharge', target_power_kw=540, expected_soc_pct=80)
        with self.assertRaisesRegex(ModelUpdateError, 'SOC'):
            self.remaining()
