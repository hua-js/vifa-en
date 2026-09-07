import copy
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from m4_settings.timeseries import build_pv_reference, build_tariff_points


SHANGHAI = ZoneInfo('Asia/Shanghai')
START = datetime(2026, 9, 7, tzinfo=SHANGHAI)


def tariffs():
    return [
        {'start_time': '00:00:00', 'end_time': '08:00:00', 'period_type': 'gu'},
        {'start_time': '08:00:00', 'end_time': '18:00:00', 'period_type': 'feng'},
        {'start_time': '18:00:00', 'end_time': '23:59:00', 'period_type': 'ping'},
    ], [{'vprice': '0.2', 'hprice': 1.1, 'fprice': 0.6, 'jprice': None, 'shengu': None}]


def pv_history():
    rows = []
    for day, base in enumerate((1, 3, 100, 7, 9, 11, 13)):
        day_start = START - timedelta(days=7 - day)
        for minute in range(1440):
            rows.append({
                'es_sn': 'ES02',
                'timestamp': (day_start + timedelta(minutes=minute)).isoformat(),
                'ac_solar_power': base + minute // 15,
            })
    return rows


class TariffInputsTests(unittest.TestCase):
    def test_confirmed_last_minute_covers_full_last_quarter(self):
        periods, rates = tariffs()
        result = build_tariff_points(periods, rates, plan_start_at=START)
        self.assertEqual(result['values'], [0.2] * 32 + [1.1] * 40 + [0.6] * 24)
        self.assertEqual(result['period_types'], ['gu'] * 32 + ['feng'] * 40 + ['ping'] * 24)
        self.assertTrue(result['normalized_end_of_day'])
        self.assertEqual(result['source'], 'shared_tariff')

    def test_rolling_forecast_uses_shanghai_time_across_midnight(self):
        periods, rates = tariffs()
        start = datetime(2026, 9, 7, 15, 45, tzinfo=timezone.utc)
        result = build_tariff_points(periods, rates, plan_start_at=start)
        self.assertEqual(result['values'], [0.6] + [0.2] * 32 + [1.1] * 40 + [0.6] * 23)
        self.assertEqual(result['period_types'], ['ping'] + ['gu'] * 32 + ['feng'] * 40 + ['ping'] * 23)

    def test_tariff_labels_change_business_version_even_when_all_prices_are_equal(self):
        periods, rates = tariffs()
        rates[0].update(vprice=0.6, fprice=0.6, hprice=0.6)
        first = build_tariff_points(periods, rates, plan_start_at=START)
        periods[0]['period_type'] = 'ping'
        second = build_tariff_points(periods, rates, plan_start_at=START)
        self.assertEqual(first['values'], second['values'])
        self.assertNotEqual(first['version'], second['version'])
        self.assertEqual(second['period_types'], ['ping'] * 32 + ['feng'] * 40 + ['ping'] * 24)

    def test_equal_price_label_transition_inside_quarter_is_rejected(self):
        periods, rates = tariffs()
        rates[0].update(vprice=0.6, fprice=0.6, hprice=0.6)
        periods[0]['end_time'] = '08:01:00'
        periods[1]['start_time'] = '08:01:00'
        with self.assertRaisesRegex(ValueError, '时段|标签|period_type'):
            build_tariff_points(periods, rates, plan_start_at=START)

    def test_same_label_subdivision_preserves_values_labels_and_business_version(self):
        periods, rates = tariffs()
        original = build_tariff_points(periods, rates, plan_start_at=START)
        periods[0]['end_time'] = '04:07:00'
        periods.append(dict(start_time='04:07:00', end_time='08:00:00', period_type='gu'))
        subdivided = build_tariff_points(periods, rates, plan_start_at=START)
        self.assertEqual(original['values'], subdivided['values'])
        self.assertEqual(original['period_types'], subdivided['period_types'])
        self.assertEqual(original['version'], subdivided['version'])
        rolled = build_tariff_points(periods, rates, plan_start_at=START + timedelta(hours=20))
        self.assertEqual(original['version'], rolled['version'])

    def test_business_version_ignores_row_order_metadata_and_unused_prices(self):
        periods, rates = tariffs()
        first = build_tariff_points(periods, rates, plan_start_at=START)
        for row in periods + rates:
            row.update(id=123, updatedAt='unrelated metadata')
        rates[0]['jprice'] = -100
        periods.reverse()
        second = build_tariff_points(periods, rates, plan_start_at=START)
        self.assertEqual(first['version'], second['version'])
        rates[0]['hprice'] = 1.2
        self.assertNotEqual(first['version'], build_tariff_points(periods, rates, plan_start_at=START)['version'])

    def test_missing_duplicate_and_invalid_referenced_prices_are_rejected(self):
        for bad in (None, True, -1, float('nan'), float('inf'), 'not a price'):
            with self.subTest(bad=bad):
                periods, rates = tariffs()
                rates[0]['vprice'] = bad
                with self.assertRaises(ValueError):
                    build_tariff_points(periods, rates, plan_start_at=START)
        periods, rates = tariffs()
        for bad_rates in ([], rates + rates):
            with self.assertRaises(ValueError):
                build_tariff_points(periods, bad_rates, plan_start_at=START)

    def test_gap_overlap_and_price_change_inside_quarter_are_rejected(self):
        for first_end, second_start in (
            ('07:45:00', '08:00:00'),
            ('08:15:00', '08:00:00'),
            ('08:01:00', '08:01:00'),
        ):
            with self.subTest(first_end=first_end, second_start=second_start):
                periods, rates = tariffs()
                periods[0]['end_time'] = first_end
                periods[1]['start_time'] = second_start
                with self.assertRaises(ValueError):
                    build_tariff_points(periods, rates, plan_start_at=START)

    def test_explicit_end_of_day_needs_no_normalization(self):
        periods, rates = tariffs()
        periods[-1]['end_time'] = '24:00:00'
        result = build_tariff_points(periods, rates, plan_start_at=START)
        self.assertFalse(result['normalized_end_of_day'])
        self.assertEqual(result['values'][-1], 0.6)

    def test_unknown_period_and_invalid_times_are_rejected(self):
        periods, rates = tariffs()
        for field, value in (('period_type', 'unexpected'), ('start_time', '25:00:00'), ('end_time', '00:00:00')):
            bad_periods = copy.deepcopy(periods)
            bad_periods[0][field] = value
            with self.assertRaises(ValueError):
                build_tariff_points(bad_periods, rates, plan_start_at=START)

    def test_plan_start_requires_explicit_timezone_and_quarter_alignment(self):
        periods, rates = tariffs()
        for start in (START.replace(tzinfo=None), START + timedelta(minutes=1), START + timedelta(seconds=1)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                build_tariff_points(periods, rates, plan_start_at=start)


class PhotovoltaicInputsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = pv_history()

    def test_station_without_pv_does_not_require_history(self):
        result = build_pv_reference('station-1', [], plan_start_at=START)
        self.assertEqual(result['values'], [0.0] * 96)
        self.assertEqual(result['method'], 'station_without_pv')
        self.assertEqual(result['valid_days'], [])
        self.assertIsNone(result['history_start'])
        self.assertIsNone(result['history_end'])

    def test_seven_full_days_use_same_slot_median_without_plan_day_leakage(self):
        ignored = [
            {'es_sn': 'ES02', 'timestamp': START.isoformat(), 'ac_solar_power': 9999},
            {'es_sn': 'ES01', 'timestamp': 'irrelevant', 'ac_solar_power': None},
        ]
        result = build_pv_reference('station-2', self.rows + ignored, plan_start_at=START)
        self.assertEqual(result['values'], list(range(9, 105)))
        self.assertEqual(result['method'], 'seven_day_same_slot_median')
        self.assertEqual(result['history_start'], '2026-08-31T00:00:00+08:00')
        self.assertEqual(result['history_end'], '2026-09-07T00:00:00+08:00')
        self.assertEqual(result['valid_days'], ['2026-08-31', '2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05', '2026-09-06'])
        self.assertEqual(result['issues'], [])

    def test_rolling_slots_wrap_at_shanghai_midnight(self):
        result = build_pv_reference('station-2', self.rows, plan_start_at=START + timedelta(hours=23, minutes=45))
        self.assertEqual(result['values'], [104] + list(range(9, 104)))

    def test_next_day_plan_can_use_only_days_complete_at_fetch_time(self):
        result = build_pv_reference(
            'station-2', self.rows, plan_start_at=START + timedelta(days=1),
            history_end_at=START.astimezone(timezone.utc),
        )
        self.assertEqual(result['values'], list(range(9, 105)))
        self.assertEqual(result['history_start'], '2026-08-31T00:00:00+08:00')
        self.assertEqual(result['history_end'], '2026-09-07T00:00:00+08:00')
        self.assertEqual(result['valid_days'][-1], '2026-09-06')

    def test_explicit_history_end_must_be_aware_midnight_no_later_than_plan(self):
        for end in (START.replace(tzinfo=None), START + timedelta(minutes=15), START + timedelta(days=1)):
            with self.subTest(end=end), self.assertRaises(ValueError):
                build_pv_reference('station-2', self.rows, plan_start_at=START, history_end_at=end)

    def test_minutes_have_equal_weight_despite_different_sample_rates(self):
        rows = []
        for row in self.rows:
            rows.append({**row, 'ac_solar_power': 0})
            at = datetime.fromisoformat(row['timestamp'])
            if at.minute == 0 and at.hour == 0:
                rows.append({**row, 'timestamp': (at + timedelta(seconds=30)).isoformat(), 'ac_solar_power': 300})
        result = build_pv_reference('station-2', rows, plan_start_at=START)
        self.assertEqual(result['values'][0], 10.0)
        self.assertEqual(result['values'][1:], [0.0] * 95)

    def test_isolated_missing_minutes_are_tolerated_but_not_consecutive_gaps(self):
        rows = [row for index, row in enumerate(self.rows) if index not in (2, 4, 6)]
        result = build_pv_reference('station-2', rows, plan_start_at=START)
        self.assertEqual(result['values'][0], 9)
        for missing in ((2, 4, 6, 8), (2, 3), (14, 15), (1439, 1440)):
            with self.subTest(missing=missing):
                rows = [row for index, row in enumerate(self.rows) if index not in missing]
                with self.assertRaises(ValueError):
                    build_pv_reference('station-2', rows, plan_start_at=START)

    def test_missing_day_is_rejected_instead_of_using_fewer_days(self):
        with self.assertRaises(ValueError):
            build_pv_reference('station-2', self.rows[1440:], plan_start_at=START)

    def test_duplicate_timestamp_is_deduplicated_and_conflicts_are_rejected(self):
        first = build_pv_reference('station-2', self.rows, plan_start_at=START)
        duplicate = {**self.rows[0], 'timestamp': datetime.fromisoformat(self.rows[0]['timestamp']).astimezone(timezone.utc).isoformat()}
        same = build_pv_reference('station-2', self.rows + [duplicate], plan_start_at=START)
        self.assertEqual(first['values'], same['values'])
        self.assertEqual(first['version'], same['version'])
        with self.assertRaises(ValueError):
            build_pv_reference('station-2', self.rows + [{**duplicate, 'ac_solar_power': 99}], plan_start_at=START)

    def test_invalid_power_and_missing_or_naive_timestamps_are_rejected(self):
        for field, value in (
            ('ac_solar_power', None), ('ac_solar_power', -1), ('ac_solar_power', True),
            ('ac_solar_power', float('nan')), ('ac_solar_power', float('inf')),
            ('timestamp', None), ('timestamp', '2026-08-31T00:00:00'),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                build_pv_reference('station-2', [{**self.rows[0], field: value}] + self.rows[1:], plan_start_at=START)

    def test_source_metadata_and_order_do_not_change_business_version(self):
        first = build_pv_reference('station-2', self.rows, plan_start_at=START)
        changed = [{**row, 'updatedAt': 'not used'} for row in reversed(self.rows)]
        second = build_pv_reference('station-2', changed, plan_start_at=START)
        self.assertEqual(first['version'], second['version'])

    def test_unknown_station_and_unaligned_or_naive_plan_are_rejected(self):
        with self.assertRaises(ValueError):
            build_pv_reference('station-3', [], plan_start_at=START)
        for start in (START.replace(tzinfo=None), START + timedelta(minutes=1)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                build_pv_reference('station-1', [], plan_start_at=start)


if __name__ == '__main__':
    unittest.main()
