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
            pv_forecast_kw=0, tariff_period='ping') for p in self.payload['plan']]

    def test_old_charging_version_is_rejected_before_table_read(self):
        self.full_day()
        self.payload['request']['source_versions'].pop('grid_charging_policy')
        with self.assertRaisesRegex(ModelUpdateError, '规则已更新'):
            self.remaining()
        self.reader._read_table.assert_not_called()

    def test_peak_pv_surplus_can_be_previewed_but_grid_charging_cannot(self):
        self.full_day()
        self.payload['request']['points'][0].update(tariff_period='feng', pv_forecast_kw=840)
        self.payload['plan'][0].update(mode='charge', target_power_kw=30)
        result = self.remaining()
        self.assertEqual(len(result['schedule']), 1)
        self.assertEqual(result['schedule'][0]['type'], 'charge')
        self.reader.reset_mock()
        self.payload['plan'][0]['target_power_kw'] = 41
        with self.assertRaisesRegex(ModelUpdateError, '尖、峰'):
            self.remaining()
        self.reader._read_table.assert_not_called()

    def test_export_permission_uses_540_kw_and_can_overlap_charging(self):
        self.full_day()
        for i in (0, 1):
            self.payload['request']['points'][i].update(pv_forecast_kw=900)
            self.payload['plan'][i].update(mode='charge', target_power_kw=30, grid_export_kw=70)
        result = self.remaining()
        records = result['schedule']
        self.assertEqual({row['type'] for row in records}, {'charge', 'pv_surplus_export'})
        export = next(row for row in records if row['type'] == 'pv_surplus_export')
        charge = next(row for row in records if row['type'] == 'charge')
        self.assertEqual(export['kw'], 540)
        self.assertEqual(export['start_at'], charge['start_at'])
        self.assertEqual(export['end_at'], charge['end_at'])
        body = next(op['body'] for op in result['operations']['create']
                    if op['body']['type'] == 'pv_surplus_export')
        self.assertEqual(body['kw'], 540)
        self.assertEqual(body['explain'], '光伏发电满足负荷及储能充电后，剩余电量上网。')
        self.assertEqual(body['repeat'], '今日有效')

    def test_written_reason_uses_tariff_and_keeps_one_continuous_record(self):
        self.full_day()
        for i, period in enumerate(('feng', 'feng', 'ping')):
            self.payload['request']['points'][i]['tariff_period'] = period
            self.payload['plan'][i].update(mode='discharge', target_power_kw=40)
        result = self.remaining()
        self.assertEqual(len(result['schedule']), 1)
        body = result['operations']['create'][0]['body']
        self.assertEqual(body['explain'], '高价时段放电供负荷，减少高价购电；储能供应部分负荷，减少本时段电网购电。')
        self.assertEqual(result['schedule'][0]['explain'], body['explain'])

    def test_missing_tariff_is_rejected_before_table_read(self):
        self.full_day()
        self.payload['request']['points'][0].pop('tariff_period')
        with self.assertRaisesRegex(ModelUpdateError, '电价时段'):
            self.remaining()
        self.reader._read_table.assert_not_called()

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

    def test_dispatch_requires_strictly_more_than_ten_kw(self):
        for mode in ('charge', 'discharge'):
            for power in (0.000132683, 9.999, 10, 10.000001):
                with self.subTest(mode=mode, power=power):
                    self.full_day()
                    self.payload['plan'][0].update(mode=mode, target_power_kw=power)
                    result = EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True).preview(
                        'station-2', self.payload, self.config, now=self.now)
                    self.assertEqual(len(result['schedule']), int(power > 10))
                    if power > 10:
                        self.assertEqual(result['schedule'][0]['kw'], 600)
                        self.assertEqual(result['schedule'][0]['type'], mode)
                    self.assertEqual(self.payload['plan'][0]['target_power_kw'], power)

    def test_filtered_small_power_removes_conflicting_future_row(self):
        self.full_day()
        self.payload['plan'][0].update(mode='charge', target_power_kw=10)
        self.reader._read_table.return_value = [self.row, dict(self.row, id=8,
            start_time='14:15:00', end_time='14:30:00', kw=600, m4_run_id=self.payload['run_id'])]
        result = self.remaining()
        self.assertFalse(result['schedule'])
        self.assertEqual(result['operations']['destroy'][0]['query']['filterByTk'], 8)

    def test_unmarked_future_record_blocks_replacement_without_deletion(self):
        self.full_day()
        for marker in (None, '', '   '):
            with self.subTest(marker=marker):
                self.reader._read_table.return_value = [dict(self.row, id=8,
                    start_time='16:00:00', end_time='17:00:00', m4_run_id=marker)]
                with self.assertRaisesRegex(ModelUpdateError, '没有计划 ID'):
                    self.remaining()
                self.opener.open.assert_not_called()

    def test_filtered_discharge_cannot_hide_demand_violation(self):
        self.full_day()
        self.payload['plan'][0].update(mode='discharge', target_power_kw=10)
        limit = self.payload['request']['constraints']['demand_limit_kw']
        self.payload['request']['points'][0]['load_forecast_kw'] = limit + 5
        with self.assertRaisesRegex(ModelUpdateError, '需量|购电'):
            self.remaining()

    def test_same_future_slot_updates_and_idle_removes_old_future(self):
        self.full_day()
        self.payload['plan'][0].update(mode='discharge', target_power_kw=540)
        first = dict(self.row, id=8, start_time='14:15:00', end_time='14:30:00', type='discharge', kw=80)
        stale = dict(self.row, id=9, start_time='16:00:00', end_time='17:00:00', m4_run_id=self.payload['run_id'])
        self.reader._read_table.return_value = [self.row, first, stale]
        result = self.remaining()
        self.assertEqual(len(result['operations']['create']), 0)
        self.assertEqual(result['operations']['update'][0]['query']['filterByTk'], 8)
        self.assertEqual(result['operations']['destroy'][0]['query']['filterByTk'], 9)
        self.opener.open.assert_not_called()

    def test_active_row_is_truncated_when_new_plan_is_idle(self):
        self.full_day()
        self.reader._read_table.return_value = [dict(self.row, start_time='14:00:00', end_time='18:00:00')]
        result = self.remaining()
        self.assertEqual(result['cutover_record_ids'], [7])
        self.assertEqual(result['operations']['update'][0]['body'], {'end_time': '14:15:00', 'repeat': '今日有效'})
        self.assertFalse(result['operations']['create'])
        self.assertFalse(result['operations']['destroy'])

    def test_identical_active_tail_updates_only_ownership_metadata(self):
        self.full_day()
        for i in (0, 1):
            self.payload['plan'][i].update(mode='discharge', target_power_kw=300+i*20)
        row = dict(self.row, id=27, start_time='14:00:00', end_time='14:45:00',
                   type='discharge', kw=600)
        self.reader._read_table.return_value = [self.row, row]
        adapter = EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True)
        result = adapter.preview('station-2', self.payload, self.config, now=self.now)
        self.assertEqual(result['carried_record_ids'], [27])
        self.assertFalse(result['operations']['create'])
        self.assertFalse(result['operations']['destroy'])
        body = result['operations']['update'][0]['body']
        self.assertEqual(body, dict(m4_run_id=self.payload['run_id'], m4_plan_date=self.payload['date'], repeat='今日有效',
            explain='储能供应部分负荷，减少本时段电网购电。'))
        row.update(body)
        result = adapter.preview('station-2', self.payload, self.config, now=self.now)
        self.assertFalse(any(result['operations'].values()))

    def test_changed_active_tail_splits_at_cutover_but_overlap_stays_blocked(self):
        for changes in ({'end_time': '14:30:00'}, {'end_time': '15:00:00'},
                        {'kw': 100}, {'type': 'charge'}, {'overlap': True}):
            with self.subTest(changes=changes):
                self.full_day()
                for i in (0, 1):
                    self.payload['plan'][i].update(mode='discharge', target_power_kw=300)
                row = dict(self.row, id=27, start_time='14:00:00', end_time='14:45:00',
                           type='discharge', kw=600)
                row.update({k: v for k, v in changes.items() if k != 'overlap'})
                rows = [self.row, row]
                if changes.get('overlap'):
                    rows.append(dict(row, id=28, start_time='14:30:00'))
                self.reader._read_table.return_value = rows
                adapter = EMSRemainingPlanAdapter(self.reader, fixed_cabinet_power=True)
                if changes.get('overlap'):
                    with self.assertRaisesRegex(ModelUpdateError, '连续切换'):
                        adapter.preview('station-2', self.payload, self.config, now=self.now)
                else:
                    result = adapter.preview('station-2', self.payload, self.config, now=self.now)
                    self.assertEqual(result['cutover_record_ids'], [27])
                    self.assertEqual(result['operations']['update'][0]['body'], {'end_time': '14:15:00', 'repeat': '今日有效'})
                    self.assertEqual(result['operations']['create'][0]['body']['start_time'], '14:15:00')
                    self.assertEqual(result['operations']['create'][0]['body']['end_time'], '14:45:00')
                    self.assertFalse(result['operations']['destroy'])

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
            start_time='14:15:00', end_time='14:30:00', type='discharge', kw=90, repeat='今日有效',
            m4_run_id=self.payload['run_id'], m4_plan_date=self.payload['date'],
            explain='储能供应部分负荷，减少本时段电网购电。')]
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
