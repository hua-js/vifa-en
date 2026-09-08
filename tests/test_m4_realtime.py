import copy
import unittest
from datetime import datetime, timedelta, timezone

from m4_settings.models import StationConfiguration, StationParameters


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
COMPONENTS = ('bcu1_status', 'bcu2_status', 'pcs1_status', 'pcs2_status')


def configuration(station_id='station-1', **changes):
    parameters = dict(
        energy_capacity_kwh=500.0, max_charge_kw=80.0, max_discharge_kw=90.0,
        charge_efficiency=0.94, discharge_efficiency=0.93,
        soc_min_pct=15.0, soc_max_pct=85.0,
        preferred_soc_min_pct=25.0, preferred_soc_max_pct=75.0,
        terminal_soc_tolerance_pct=4.0, grid_import_limit_kw=200.0,
        cycle_cost_per_kwh=0.02, max_input_age_seconds=300,
    )
    parameters.update(changes)
    return StationConfiguration(
        station_id=station_id, revision=1, version=f'{station_id}-settings-v1',
        parameters=StationParameters(**parameters),
    )


def cabinet(emu_sn, soc=50.0, *, station='ES01', age=30, **changes):
    values = dict(
        emu_sn=emu_sn, f_es_sn=station, latest_soc=soc,
        last_time_iso=(NOW - timedelta(seconds=age)).isoformat(),
        emu_status='wait', alert_status=None,
        **{field: 'wait' for field in COMPONENTS},
    )
    values.update(changes)
    return values


class RealtimeTests(unittest.TestCase):
    def snapshot(self, rows=None, config=None, now=NOW, plan_start_at=None):
        # Import inside the test so the first red run reports the missing feature.
        from m4_settings.realtime import build_realtime_snapshot
        return build_realtime_snapshot(
            config or configuration(),
            rows if rows is not None else [cabinet('emu11', 40), cabinet('emu12', 60)],
            now=now, **({'plan_start_at': plan_start_at} if plan_start_at is not None else {}),
        )

    def test_equal_configured_capacity_weights_all_fixed_cabinets(self):
        result = self.snapshot([cabinet('emu11', 40, age=20), cabinet('emu12', 60, age=80)])
        self.assertTrue(result['available'])
        self.assertEqual(result['station_id'], 'station-1')
        self.assertEqual(result['source_station_id'], 'ES01')
        self.assertEqual(result['initial_soc_pct'], 50)
        self.assertEqual(result['full_station_soc_pct'], 50)
        self.assertEqual(result['soc_method'], 'configured_equal_capacity_weighted')
        self.assertEqual(result['energy_capacity_kwh'], 500)
        self.assertEqual(result['capacity_per_cabinet_kwh'], 250)
        self.assertEqual(result['available_energy_capacity_kwh'], 500)
        self.assertEqual(result['available_max_charge_kw'], 80)
        self.assertEqual(result['available_max_discharge_kw'], 90)
        self.assertEqual(result['participating_cabinet_ids'], ['emu11', 'emu12'])
        self.assertEqual(result['excluded_cabinet_ids'], [])
        self.assertEqual(result['participation_status'], 'full')
        self.assertEqual(result['warnings'], [])
        self.assertEqual([c['capacity_kwh'] for c in result['cabinets']], [250, 250])
        self.assertEqual(datetime.fromisoformat(result['observed_at']), NOW - timedelta(seconds=80))
        self.assertEqual(result['issues'], [])
        self.assertTrue(result['source_version'])
        self.assertEqual(result['configuration_version'], 'station-1-settings-v1')

    def test_weighting_does_not_overflow_when_configuration_capacity_is_large(self):
        result = self.snapshot(config=configuration(energy_capacity_kwh=1e308))
        self.assertTrue(result['available'])
        self.assertEqual(result['initial_soc_pct'], 50)

    def test_station_two_uses_six_cabinets_and_excludes_pv_meter_and_other_station(self):
        rows = [cabinet(f'emu{n}', float(n), station='ES02') for n in range(21, 27)]
        rows += [cabinet('emu27', 100, station='ES02', emu_status='fault')]
        rows += [cabinet('emu11', 1), cabinet('emu21', 99, station='ES01')]
        result = self.snapshot(rows, configuration('station-2', energy_capacity_kwh=1200))
        self.assertTrue(result['available'])
        self.assertEqual(result['initial_soc_pct'], 23.5)
        self.assertEqual(result['source_station_id'], 'ES02')
        self.assertEqual(result['capacity_per_cabinet_kwh'], 200)
        self.assertEqual([c['emu_sn'] for c in result['cabinets']], [f'emu{n}' for n in range(21, 27)])

    def test_missing_or_misassigned_cabinet_reduces_capacity_without_enlarging_remaining_cabinet(self):
        for rows in ([cabinet('emu11', 60)],
                     [cabinet('emu11', 60), cabinet('emu12', 30, station='ES02')]):
            with self.subTest(rows=rows):
                result = self.snapshot(rows)
                self.assertTrue(result['available'])
                self.assertEqual(result['initial_soc_pct'], 60)
                self.assertIsNone(result['full_station_soc_pct'])
                self.assertEqual(result['available_energy_capacity_kwh'], 250)
                self.assertEqual(result['participating_cabinet_ids'], ['emu11'])
                self.assertEqual(result['excluded_cabinet_ids'], ['emu12'])
                self.assertEqual(result['participation_status'], 'partial')
                self.assertEqual(result['issues'], [])
                self.assertTrue(result['warnings'])
                self.assertEqual(len(result['cabinets']), 2)
                self.assertEqual(result['cabinets'][1]['capacity_kwh'], 250)
                self.assertTrue(result['cabinets'][1]['issues'])

    def test_duplicate_cabinet_is_ambiguous_and_only_excludes_itself(self):
        rows = [cabinet('emu11', 40), cabinet('emu11', 41), cabinet('emu12', 60)]
        result = self.snapshot(rows)
        self.assertTrue(result['available'])
        self.assertEqual(result['initial_soc_pct'], 60)
        self.assertIsNone(result['full_station_soc_pct'])
        self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
        self.assertTrue(result['cabinets'][0]['issues'])

    def test_operating_states_are_allowed_for_cabinets_and_components(self):
        for status in ('wait', 'standby', 'charge', 'discharge'):
            with self.subTest(status=status):
                row = cabinet('emu11', emu_status=status, **{field: status for field in COMPONENTS})
                self.assertTrue(self.snapshot([row, cabinet('emu12')])['available'])

    def test_pcs_work_is_supported_without_bypassing_alert_or_other_components(self):
        rows=[cabinet('emu11',pcs1_status='work',pcs2_status='work',alert_status='alert'),
              cabinet('emu12',pcs1_status='work',pcs2_status='work',emu_status='discharge')]
        result=self.snapshot(rows)
        self.assertEqual(result['participating_cabinet_ids'],['emu12'])
        self.assertEqual(result['available_energy_capacity_kwh'],250.0)
        self.assertEqual(result['cabinets'][0]['issues'],['alert_status 存在活动告警或未知告警状态'])
        for field in ['emu_status','bcu1_status','bcu2_status']:
            changed=copy.deepcopy(rows);changed[1][field]='work'
            self.assertEqual(self.snapshot(changed)['participating_cabinet_ids'],[])

    def test_unknown_or_unsafe_state_only_excludes_affected_cabinet(self):
        for field in ('emu_status', *COMPONENTS):
            for status in ('stop', 'offline', 'fault', 'debug', 'alert', 'exception', 'other', '', None, 1, []):
                with self.subTest(field=field, status=status):
                    row = cabinet('emu11', **{field: status})
                    result = self.snapshot([row, cabinet('emu12')])
                    self.assertTrue(result['available'])
                    self.assertEqual(result['initial_soc_pct'], 50)
                    self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
                    self.assertFalse(result['cabinets'][0]['available'])
                    self.assertTrue(result['cabinets'][0]['issues'])
            row = cabinet('emu11')
            del row[field]
            with self.subTest(missing_field=field):
                self.assertEqual(self.snapshot([row, cabinet('emu12')])['participating_cabinet_ids'], ['emu12'])

    def test_only_explicit_null_alert_is_accepted(self):
        for alert in ('alert', 'normal', 'ok', '', 'unknown', False, 0, []):
            with self.subTest(alert=alert):
                result = self.snapshot([cabinet('emu11', alert_status=alert), cabinet('emu12')])
                self.assertTrue(result['available'])
                self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
                self.assertEqual(result['initial_soc_pct'], 50)
        row = cabinet('emu11')
        del row['alert_status']
        self.assertEqual(self.snapshot([row, cabinet('emu12')])['participating_cabinet_ids'], ['emu12'])

    def test_soc_must_be_finite_numeric_nonboolean_in_physical_range(self):
        for value in (None, True, False, '50', float('nan'), float('inf'), -0.1, 100.1, [], {}):
            with self.subTest(value=value):
                result = self.snapshot([cabinet('emu11', value), cabinet('emu12', 60)])
                self.assertTrue(result['available'])
                self.assertEqual(result['initial_soc_pct'], 60)
                self.assertIsNone(result['full_station_soc_pct'])
                self.assertIsNone(result['cabinets'][0]['soc_pct'])
        row = cabinet('emu11')
        del row['latest_soc']
        self.assertEqual(self.snapshot([row, cabinet('emu12')])['initial_soc_pct'], 50)

    def test_each_cabinet_safety_bounds_are_checked_before_average(self):
        result = self.snapshot([cabinet('emu11', 10), cabinet('emu12', 90)])
        self.assertIsNone(result['initial_soc_pct'])
        self.assertEqual(result['full_station_soc_pct'], 50)
        self.assertFalse(result['available'])
        self.assertTrue(all(c['issues'] for c in result['cabinets']))
        self.assertTrue(self.snapshot([cabinet('emu11', 15), cabinet('emu12', 85)])['available'])

    def test_observation_requires_timezone_and_fresh_nonfuture_time(self):
        invalid = [None, '', 'not-a-date', '2026-09-07T11:59:30', '2026-09-07', 123,
                   (NOW + timedelta(seconds=1)).isoformat(),
                   (NOW - timedelta(seconds=301)).isoformat()]
        for value in invalid:
            with self.subTest(value=value):
                result = self.snapshot([cabinet('emu11', last_time_iso=value), cabinet('emu12')])
                self.assertTrue(result['available'])
                self.assertEqual(result['initial_soc_pct'], 50)
                self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
                self.assertEqual(datetime.fromisoformat(result['observed_at']), NOW - timedelta(seconds=30))
        self.assertTrue(self.snapshot([cabinet('emu11', age=300), cabinet('emu12')])['available'])
        row = cabinet('emu11')
        del row['last_time_iso']
        self.assertEqual(self.snapshot([row, cabinet('emu12')])['participating_cabinet_ids'], ['emu12'])

    def test_offset_times_and_utc_z_are_supported(self):
        row = cabinet('emu11', last_time_iso='2026-09-07T19:59:00+08:00')
        second = cabinet('emu12', last_time_iso='2026-09-07T11:59:30Z')
        result = self.snapshot([row, second])
        self.assertTrue(result['available'])
        self.assertEqual(datetime.fromisoformat(result['observed_at']), NOW - timedelta(seconds=60))

    def test_naive_reference_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '时区'):
            self.snapshot(now=NOW.replace(tzinfo=None))

    def test_unconfigured_station_shows_cabinets_without_inventing_capacity_or_soc(self):
        result = self.snapshot(config=StationConfiguration(station_id='station-1'))
        self.assertFalse(result['available'])
        self.assertIsNone(result['initial_soc_pct'])
        self.assertIsNone(result['full_station_soc_pct'])
        self.assertIsNone(result['energy_capacity_kwh'])
        self.assertIsNone(result['capacity_per_cabinet_kwh'])
        self.assertIsNone(result['available_energy_capacity_kwh'])
        self.assertIsNone(result['available_max_charge_kw'])
        self.assertIsNone(result['available_max_discharge_kw'])
        self.assertEqual(result['participation_status'], 'none')
        self.assertEqual(result['participating_cabinet_ids'], [])
        self.assertEqual(result['excluded_cabinet_ids'], ['emu11', 'emu12'])
        self.assertIsNone(result['observed_at'])
        self.assertEqual([c['soc_pct'] for c in result['cabinets']], [40, 60])
        self.assertTrue(all(c['capacity_kwh'] is None for c in result['cabinets']))
        self.assertTrue(result['issues'])

    def test_version_depends_on_source_configuration_but_not_refresh_time_or_row_order(self):
        rows = [cabinet('emu11', 40), cabinet('emu12', 60)]
        first = self.snapshot(rows)
        refreshed = self.snapshot(list(reversed(rows)), now=NOW + timedelta(seconds=1))
        self.assertEqual(first['source_version'], refreshed['source_version'])
        self.assertTrue(refreshed['available'])
        expired = self.snapshot(rows, now=NOW + timedelta(seconds=301))
        self.assertNotEqual(first['source_version'], expired['source_version'])
        self.assertFalse(expired['available'])
        changed_soc = copy.deepcopy(rows)
        changed_soc[0]['latest_soc'] = 41
        self.assertNotEqual(first['source_version'], self.snapshot(changed_soc)['source_version'])
        changed_time = copy.deepcopy(rows)
        changed_time[0]['last_time_iso'] = NOW.isoformat()
        self.assertNotEqual(first['source_version'], self.snapshot(changed_time)['source_version'])
        config = configuration().model_copy(update={'version': 'station-1-settings-v2'})
        self.assertNotEqual(first['source_version'], self.snapshot(rows, config)['source_version'])
        irrelevant = rows + [cabinet('emu21', station='ES02')]
        self.assertEqual(first['source_version'], self.snapshot(irrelevant)['source_version'])

    def test_single_alarm_uses_only_healthy_soc_and_half_of_total_power(self):
        result = self.snapshot([cabinet('emu11', 20, age=80, alert_status='alert'), cabinet('emu12', 70)])
        self.assertTrue(result['available'])
        self.assertEqual(result['initial_soc_pct'], 70)
        self.assertEqual(result['full_station_soc_pct'], 45)
        self.assertEqual(result['energy_capacity_kwh'], 500)
        self.assertEqual(result['capacity_per_cabinet_kwh'], 250)
        self.assertEqual(result['available_energy_capacity_kwh'], 250)
        self.assertEqual(result['available_max_charge_kw'], 40)
        self.assertEqual(result['available_max_discharge_kw'], 45)
        self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
        self.assertEqual(result['excluded_cabinet_ids'], ['emu11'])
        self.assertEqual(result['participation_status'], 'partial')
        self.assertEqual(datetime.fromisoformat(result['observed_at']), NOW - timedelta(seconds=30))
        self.assertEqual(result['issues'], [])
        self.assertTrue(any('emu11' in warning and 'alert_status' in warning for warning in result['warnings']))

    def test_station_two_two_participants_keep_one_third_of_total_capability(self):
        rows = [cabinet(f'emu{n}', float(n), station='ES02', alert_status=None if n < 23 else 'alert')
                for n in range(21, 27)]
        result = self.snapshot(rows, configuration('station-2', energy_capacity_kwh=1200,
                                                   max_charge_kw=600, max_discharge_kw=300))
        self.assertTrue(result['available'])
        self.assertEqual(result['participating_cabinet_ids'], ['emu21', 'emu22'])
        self.assertEqual(result['initial_soc_pct'], 21.5)
        self.assertEqual(result['full_station_soc_pct'], 23.5)
        self.assertEqual(result['available_energy_capacity_kwh'], 400)
        self.assertEqual(result['available_max_charge_kw'], 200)
        self.assertEqual(result['available_max_discharge_kw'], 100)
        self.assertEqual([item['capacity_kwh'] for item in result['cabinets']], [200] * 6)

    def test_all_cabinets_excluded_blocks_station_with_no_fabricated_soc_or_time(self):
        result = self.snapshot([cabinet('emu11', 30, alert_status='alert'), cabinet('emu12', 70, age=301)])
        self.assertFalse(result['available'])
        self.assertIsNone(result['initial_soc_pct'])
        self.assertIsNone(result['observed_at'])
        self.assertEqual(result['full_station_soc_pct'], 50)
        self.assertEqual(result['available_energy_capacity_kwh'], 0)
        self.assertEqual(result['available_max_charge_kw'], 0)
        self.assertEqual(result['available_max_discharge_kw'], 0)
        self.assertEqual(result['participation_status'], 'none')
        self.assertEqual(result['participating_cabinet_ids'], [])
        self.assertEqual(result['excluded_cabinet_ids'], ['emu11', 'emu12'])
        self.assertTrue(result['issues'])
        self.assertTrue(all('emu11' not in issue and 'emu12' not in issue for issue in result['issues']))
        self.assertEqual(len(result['warnings']), 2)

    def test_plan_start_freshness_is_checked_for_each_cabinet_before_aggregation(self):
        result = self.snapshot([cabinet('emu11', 30, age=90), cabinet('emu12', 70, age=10)],
                               plan_start_at=NOW + timedelta(seconds=240))
        self.assertTrue(result['available'])
        self.assertEqual(result['initial_soc_pct'], 70)
        self.assertEqual(result['participating_cabinet_ids'], ['emu12'])
        self.assertTrue(any('计划起点' in issue for issue in result['cabinets'][0]['issues']))
        self.assertEqual(result['available_energy_capacity_kwh'], 250)
        boundary = self.snapshot([cabinet('emu11', age=60), cabinet('emu12', age=60)],
                                 plan_start_at=NOW + timedelta(seconds=240))
        self.assertEqual(boundary['participation_status'], 'full')

    def test_naive_plan_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '时区'):
            self.snapshot(plan_start_at=NOW.replace(tzinfo=None))

    def test_missing_saved_version_prevents_any_cabinet_participating(self):
        config = configuration().model_copy(update={'version': None})
        result = self.snapshot(config=config)
        self.assertFalse(result['available'])
        self.assertEqual(result['participation_status'], 'none')
        self.assertEqual(result['participating_cabinet_ids'], [])
        self.assertEqual(result['available_energy_capacity_kwh'], 0)
        self.assertIsNone(result['initial_soc_pct'])
        self.assertIsNone(result['observed_at'])
        self.assertTrue(any('版本' in issue for issue in result['issues']))

    def test_participation_change_alters_version_even_with_identical_raw_rows(self):
        rows = [cabinet('emu11', age=250), cabinet('emu12', age=20)]
        first = self.snapshot(rows)
        unchanged = self.snapshot(rows, now=NOW + timedelta(seconds=10))
        partial = self.snapshot(rows, now=NOW + timedelta(seconds=51))
        self.assertEqual(first['source_version'], unchanged['source_version'])
        self.assertNotEqual(first['source_version'], partial['source_version'])
        self.assertEqual(partial['participating_cabinet_ids'], ['emu12'])
        plan_partial = self.snapshot(rows, plan_start_at=NOW + timedelta(seconds=51))
        self.assertEqual(partial['source_version'], plan_partial['source_version'])


if __name__ == '__main__':
    unittest.main()
