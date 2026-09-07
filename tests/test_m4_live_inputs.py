import copy
import json
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from m4_settings.models import StationConfiguration
from m4_optimizer_test_support import make_profiles
from test_m4_realtime import cabinet, configuration


SHANGHAI = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 9, 7, 12, 14, tzinfo=SHANGHAI)
START = NOW.replace(minute=15)


class Client:
    def __init__(self, *, station_id='station-1', now=NOW, start=START):
        self.calls = []
        self.lock = threading.Lock()
        self.failures = set()
        self.station = 'ES01' if station_id == 'station-1' else 'ES02'
        self.runs = [dict(id=1, run_id='m3-current', station_id=self.station,
            status='succeeded', interval_seconds=900, expected_points_per_series=96,
            forecast_start=start.isoformat(), forecast_end=(start + timedelta(days=1)).isoformat(),
            completed_at=(now - timedelta(minutes=1)).isoformat(), content_hash='load-fixture')]
        self.forecasts = [dict(run_pk=1, unique_id='station_total_load',
            target_time=(start + timedelta(minutes=15*i)).isoformat(), horizon_step=i+1,
            forecast_value=100.0+i) for i in range(96)]
        roster = ['emu11', 'emu12'] if station_id == 'station-1' else [f'emu{n}' for n in range(21, 27)]
        self.devices = [cabinet(sn, 50.0, station=self.station,
            last_time_iso=(now - timedelta(seconds=30)).isoformat()) for sn in roster]
        if station_id == 'station-2':
            self.devices.append(dict(emu_sn='emu27', f_es_sn='ES02', latest_solar_power=70.0,
                                     last_time_iso=(now - timedelta(seconds=20)).isoformat()))
        self.station_rows = [dict(es_sn=self.station, emus_soc='50.0', grid_power='90.0', load_power='100.0',
                                 ac_solar_power=70.0, timestamp=(now - timedelta(seconds=20)).isoformat())]
        self.periods = [dict(start_time='00:00:00', end_time='23:59:00', period_type='ping')]
        self.rates = [dict(fprice=0.7, hprice=1.1, vprice=0.3)]
        history_start = start.replace(hour=0, minute=0) - timedelta(days=7)
        self.history = [dict(es_sn='ES02', timestamp=(history_start+timedelta(minutes=i)).isoformat(),
                             ac_solar_power=float((i % 1440) // 60)) for i in range(7*1440)]

    def list_rows(self, table, **kwargs):
        with self.lock:
            self.calls.append((table, copy.deepcopy(kwargs)))
        label = 'history' if table == 't_es_data' and kwargs.get('limit') != 1 else table
        if label in self.failures:
            raise RuntimeError('Bearer secret-must-never-appear https://private.invalid')
        mapping = {'energy_forecast_manual_runs': self.runs,
                   'energy_forecast_manual_points': self.forecasts,
                   't_emu': self.devices, 't_es_data': self.station_rows,
                   't_peak_diy': self.periods, 't_rate': self.rates, 'history': self.history}
        return copy.deepcopy(mapping[label])


class Controls:
    def __init__(self, station_id='station-1'):
        self.result = dict(station_id=station_id, status='ready', version='controls-v1',
            demand={'need_kw': 1063.0, 'reserved_kw': 50.0},
            reverse_flow={'re_kw': 40.0, 'enabled': False, 'effective_limit_kw': None},
            schedule=[], warnings=[], issues=['防逆流控制当前未启用，限制功率仅供展示'])
        self.failed = False

    def fetch(self, station_id):
        if self.failed:
            raise RuntimeError('secret-must-never-appear')
        return copy.deepcopy(self.result)


class LiveInputTests(unittest.TestCase):
    def fetch(self, client=None, controls=None, config=None, now=NOW):
        from m4_settings.live_inputs import LiveInputService
        return LiveInputService(client or Client(), controls or Controls()).fetch(config or configuration(), now=now)

    def request(self, config, bundle):
        from m4_settings.live_inputs import request_from_inputs
        return request_from_inputs(config, bundle, make_profiles(), now=NOW)

    def test_complete_sources_build_96_real_points_and_validated_request(self):
        client = Client()
        config = configuration()
        result = self.fetch(client, config=config)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['plan_start_at'], START.isoformat())
        self.assertEqual(result['plan_end_at'], (START+timedelta(days=1)).isoformat())
        self.assertEqual(result['interval_minutes'], 15)
        self.assertEqual(result['horizon_points'], 96)
        self.assertEqual(len(result['points']), 96)
        self.assertEqual(result['points'][0]['load_forecast_kw'], 100)
        self.assertTrue(all(point['pv_forecast_kw'] == 0 and point['sell_price_per_kwh'] == 0 for point in result['points']))
        self.assertEqual(result['sources']['tariff']['period_types'], ['ping'] * 96)
        self.assertTrue(all(point['tariff_period'] == 'ping' for point in result['points']))
        self.assertTrue(all(source['status'] == 'ready' for source in result['sources'].values()))
        self.assertEqual(result['sources']['realtime']['observed_station_soc_pct'], 50)
        self.assertEqual(result['sources']['realtime']['sampled_grid_power_kw'], 90)
        self.assertFalse(any(table == 't_es_data' and values.get('limit') != 1 for table,values in client.calls))
        emu_position = next(i for i,(table,_) in enumerate(client.calls) if table == 't_emu')
        self.assertTrue(all(i < emu_position for i,(table,_) in enumerate(client.calls)
                            if table in ('energy_forecast_manual_points','t_rate','t_peak_diy')))
        request = self.request(config, result)
        self.assertEqual(request.capability.initial_soc_pct, 50)
        self.assertTrue(request.capability.available)
        self.assertEqual(request.constraints.demand_limit_kw, 1063)
        self.assertTrue(request.constraints.grid_export_enabled)
        self.assertEqual(request.constraints.grid_export_limit_kw, 0)
        self.assertEqual(request.input_observed_at.utcoffset(), START.utcoffset())
        self.assertTrue(all(point.tariff_period == 'ping' for point in request.points))
        json.dumps(result, allow_nan=False)

    def test_live_tariff_labels_follow_rolling_window_across_midnight(self):
        now = NOW.replace(hour=23, minute=44)
        start = now.replace(minute=45)
        client = Client(now=now, start=start)
        client.periods = [
            dict(start_time='00:00:00', end_time='08:00:00', period_type='gu'),
            dict(start_time='08:00:00', end_time='18:00:00', period_type='feng'),
            dict(start_time='18:00:00', end_time='23:59:00', period_type='ping'),
        ]
        bundle = self.fetch(client, now=now)
        expected = ['ping'] + ['gu'] * 32 + ['feng'] * 40 + ['ping'] * 23
        self.assertEqual(bundle['status'], 'ready')
        self.assertEqual(bundle['sources']['tariff']['period_types'], expected)
        self.assertEqual([point['tariff_period'] for point in bundle['points']], expected)
        from m4_settings.live_inputs import request_from_inputs
        request = request_from_inputs(configuration(), bundle, make_profiles(), now=now)
        self.assertEqual([point.tariff_period for point in request.points], expected)

    def test_missing_tariff_labels_cannot_be_reported_as_ready_source(self):
        with patch('m4_settings.live_inputs.LiveInputService._tariff', return_value={
                'values': [0.7] * 96, 'version': 'missing-labels', 'source': 'shared_tariff'}):
            bundle = self.fetch()
        self.assertEqual(bundle['status'], 'blocked')
        self.assertEqual(bundle['sources']['tariff']['status'], 'incomplete')
        self.assertEqual(bundle['points'], [])

    def test_relabeling_equal_prices_changes_tariff_source_and_request_versions(self):
        client = Client()
        client.rates = [dict(vprice=0.7, fprice=0.7, hprice=0.7)]
        first_bundle = self.fetch(client)
        first = self.request(configuration(), first_bundle)
        client.periods = [
            dict(start_time='00:00:00', end_time='08:00:00', period_type='gu'),
            dict(start_time='08:00:00', end_time='23:59:00', period_type='ping'),
        ]
        second_bundle = self.fetch(client)
        second = self.request(configuration(), second_bundle)
        self.assertEqual(first_bundle['sources']['tariff']['values'], second_bundle['sources']['tariff']['values'])
        self.assertNotEqual(first.source_versions['tariff'], second.source_versions['tariff'])
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertNotEqual([point.tariff_period for point in first.points],
                            [point.tariff_period for point in second.points])

    def test_station_two_history_and_live_pv_use_correct_sources_and_no_40kw_constraint(self):
        config = configuration('station-2')
        client, controls = Client(station_id='station-2'), Controls('station-2')
        result = self.fetch(client, controls, config)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['sources']['realtime']['sampled_pv_power_kw'], 70)
        self.assertEqual(result['sources']['pv']['method'], 'seven_day_same_slot_median')
        history_call = next(values for table,values in client.calls if table=='t_es_data' and values.get('limit') != 1)
        self.assertEqual(history_call['page_size'], 2000)
        self.assertEqual(history_call['max_pages'], 100)
        self.assertIn('ac_solar_power', history_call['fields'])
        first = self.request(config, result)
        self.assertEqual(first.constraints.grid_export_limit_kw, 23)
        controls.result['reverse_flow']['re_kw'] = 999
        controls.result['version'] = 'controls-v2'
        second = self.request(config, self.fetch(client, controls, config))
        self.assertEqual(first.constraints.grid_export_limit_kw, second.constraints.grid_export_limit_kw)
        self.assertEqual(first.constraints.demand_limit_kw, second.constraints.demand_limit_kw)

    def test_each_failed_required_source_is_independent_and_safe_to_display(self):
        for table, source in [('energy_forecast_manual_runs','load'), ('t_peak_diy','tariff'), ('t_emu','realtime')]:
            with self.subTest(table=table):
                client = Client()
                client.failures.add(table)
                result = self.fetch(client)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['sources'][source]['status'], 'error')
                self.assertEqual(result['sources']['controls']['status'], 'ready')
                self.assertEqual(result['sources']['pv']['status'], 'ready')
                self.assertNotIn('secret-must-never-appear', json.dumps(result))
                with self.assertRaises(ValueError):
                    self.request(configuration(), result)
        controls = Controls()
        controls.failed = True
        result = self.fetch(controls=controls)
        self.assertEqual(result['sources']['controls']['status'], 'error')
        self.assertEqual(result['sources']['realtime']['status'], 'ready')

    def test_missing_next_day_load_is_not_repeated_or_replaced(self):
        client = Client()
        remaining = 47
        client.runs[0].update(expected_points_per_series=remaining,
                             forecast_end=(START+timedelta(minutes=15*remaining)).isoformat())
        client.forecasts = client.forecasts[:remaining]
        result = self.fetch(client)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['sources']['load']['status'], 'incomplete')
        self.assertEqual(result['sources']['load']['coverage_points'], remaining)
        self.assertIsNone(result['sources']['load']['values'][-1])
        self.assertEqual(result['points'], [])

    def test_unconfigured_station_still_reads_sources_and_displays_cabinets(self):
        result = self.fetch(config=StationConfiguration(station_id='station-1'))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['sources']['load']['status'], 'ready')
        self.assertEqual(len(result['sources']['realtime']['cabinets']), 2)
        self.assertIsNone(result['sources']['realtime']['initial_soc_pct'])
        self.assertEqual(len(result['points']), 96)

    def test_alerted_cabinet_is_excluded_while_remaining_cabinet_can_plan(self):
        client = Client()
        client.devices[0].update(alert_status='alert', latest_soc=30.0)
        client.devices[1]['latest_soc'] = 60.0
        result = self.fetch(client)
        self.assertEqual(result['status'], 'ready')
        snapshot = result['sources']['realtime']
        self.assertEqual(snapshot['participating_cabinet_ids'], ['emu12'])
        self.assertEqual(snapshot['excluded_cabinet_ids'], ['emu11'])
        self.assertEqual(snapshot['initial_soc_pct'], 60)
        self.assertEqual(snapshot['full_station_soc_pct'], 45)
        self.assertEqual(snapshot['available_energy_capacity_kwh'], 250)
        self.assertTrue(any('emu11' in item for item in result['warnings']))
        request = self.request(configuration(), result)
        self.assertEqual(request.capability.energy_capacity_kwh, 250)
        self.assertEqual(request.capability.max_charge_kw, 40)
        self.assertEqual(request.capability.max_discharge_kw, 45)
        self.assertEqual(request.capability.initial_soc_pct, 60)
        self.assertEqual(request.constraints.demand_limit_kw, 1063)
        self.assertEqual(len(result['points']), 96)

    def test_all_cabinets_excluded_still_blocks_new_request(self):
        client = Client()
        for row in client.devices:
            row['alert_status'] = 'alert'
        result = self.fetch(client)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['sources']['realtime']['initial_soc_pct'])
        self.assertEqual(result['sources']['realtime']['available_energy_capacity_kwh'], 0)
        with self.assertRaises(ValueError):
            self.request(configuration(), result)
        result['status'] = 'ready'
        with self.assertRaises(ValueError):
            self.request(configuration(), result)

    def test_cabinet_stale_at_plan_start_is_excluded_without_blocking_fresh_peer(self):
        client = Client()
        client.devices[0]['last_time_iso'] = (NOW-timedelta(seconds=250)).isoformat()
        result = self.fetch(client)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['sources']['realtime']['participating_cabinet_ids'], ['emu12'])
        self.assertTrue(any('计划起点' in item for item in result['warnings']))
        self.assertEqual(self.request(configuration(), result).capability.energy_capacity_kwh, 250)

    def test_request_rechecks_participation_scope_capacity_soc_and_oldest_time(self):
        client = Client()
        client.devices[0].update(alert_status='alert', latest_soc=30.0)
        client.devices[1]['latest_soc'] = 60.0
        original = self.fetch(client)
        changes = [
            ('participant', lambda s: s.update(participating_cabinet_ids=['emu11'])),
            ('duplicate', lambda s: s.update(participating_cabinet_ids=['emu12','emu12'])),
            ('excluded', lambda s: s.update(excluded_cabinet_ids=[])),
            ('capacity', lambda s: s.update(available_energy_capacity_kwh=500.0)),
            ('power', lambda s: s.update(available_max_charge_kw=80.0)),
            ('soc', lambda s: s.update(initial_soc_pct=45.0)),
            ('observed', lambda s: s.update(observed_at=NOW.isoformat())),
            ('cabinet_soc', lambda s: s['cabinets'][1].update(soc_pct=90.0)),
            ('huge_cabinet_soc', lambda s: s['cabinets'][1].update(soc_pct=10**1000)),
            ('cabinet_time', lambda s: s['cabinets'][1].update(observed_at=(NOW-timedelta(hours=1)).isoformat())),
            ('cabinet_issue', lambda s: s['cabinets'][1].update(issues=['unknown'])),
        ]
        for label, change in changes:
            bundle = copy.deepcopy(original)
            change(bundle['sources']['realtime'])
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.request(configuration(), bundle)

    def test_configuration_or_station_mismatch_rejects_existing_bundle(self):
        result = self.fetch()
        for config in (configuration().model_copy(update={'version':'new-version'}), configuration('station-2')):
            with self.subTest(config=config.station_id), self.assertRaises(ValueError):
                self.request(config, result)
        controls = Controls('station-2')
        self.assertEqual(self.fetch(controls=controls)['sources']['controls']['status'], 'error')

    def test_fresh_now_but_too_old_at_plan_start_is_blocked(self):
        now = NOW.replace(minute=1)
        result = self.fetch(Client(now=now), now=now)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('计划起点' in issue for issue in result['issues']+result['warnings']))
        with self.assertRaises(ValueError):
            self.request(configuration(), result)

    def test_stale_display_observations_are_not_reported_as_current(self):
        client = Client(station_id='station-2')
        client.station_rows[0]['timestamp'] = (NOW-timedelta(hours=1)).isoformat()
        client.devices[-1]['last_time_iso'] = (NOW-timedelta(hours=1)).isoformat()
        result = self.fetch(client, Controls('station-2'), configuration('station-2'))
        self.assertEqual(result['status'], 'ready')
        snapshot = result['sources']['realtime']
        self.assertIsNone(snapshot['observed_station_soc_pct'])
        self.assertIsNone(snapshot['sampled_grid_power_kw'])
        self.assertIsNone(snapshot['sampled_load_power_kw'])
        self.assertIsNone(snapshot['sampled_pv_power_kw'])
        self.assertTrue(result['warnings'])

    def test_actual_clock_crossing_plan_boundary_blocks_bundle(self):
        with patch('m4_settings.live_inputs._clock_now', side_effect=[NOW, START+timedelta(seconds=1)]):
            result = self.fetch(now=None)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('跨过' in issue for issue in result['issues']))

    def test_missing_pv_history_blocks_only_pv_and_still_reads_latest_cabinets(self):
        client = Client(station_id='station-2')
        client.failures.add('history')
        result = self.fetch(client, Controls('station-2'), configuration('station-2'))
        self.assertEqual(result['sources']['pv']['status'], 'error')
        self.assertEqual(result['sources']['realtime']['status'], 'ready')
        self.assertEqual(result['points'], [])

    def test_midnight_plan_uses_seven_days_completed_before_fetch_date(self):
        now = NOW.replace(hour=23, minute=59)
        start = (now+timedelta(days=1)).replace(hour=0, minute=0)
        client = Client(station_id='station-2', now=now, start=start)
        history_end = now.replace(hour=0, minute=0)
        history_start = history_end-timedelta(days=7)
        client.history = [dict(es_sn='ES02', timestamp=(history_start+timedelta(minutes=i)).isoformat(),
                               ac_solar_power=25.0) for i in range(7*1440)]
        result = self.fetch(client, Controls('station-2'), configuration('station-2'), now)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['plan_start_at'], start.isoformat())
        self.assertEqual(result['sources']['pv']['history_end'], history_end.isoformat())
        self.assertEqual(result['sources']['pv']['values'], [25.0]*96)

    def test_optional_station_sample_failure_does_not_hide_valid_cabinet_inputs(self):
        client = Client()
        client.failures.add('t_es_data')
        result = self.fetch(client)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['sources']['realtime']['available'])
        self.assertIsNone(result['sources']['realtime']['observed_station_soc_pct'])
        self.assertTrue(result['warnings'])
        self.assertNotIn('secret-must-never-appear', json.dumps(result))

    def test_request_rechecks_source_values_and_point_consistency(self):
        from m4_settings.live_inputs import request_from_inputs
        original = self.fetch()
        for source, field in [('load','load_forecast_kw'), ('pv','pv_forecast_kw'), ('tariff','buy_price_per_kwh')]:
            for value in (None, True, -1, float('nan'), float('inf'), '100', 10**1000):
                with self.subTest(source=source, invalid_value=str(value)[:20]):
                    bundle = copy.deepcopy(original)
                    bundle['sources'][source]['values'][0] = value
                    with self.assertRaises(ValueError):
                        request_from_inputs(configuration(), bundle, make_profiles(), now=NOW)
            bundle = copy.deepcopy(original)
            bundle['points'][0][field] += 1
            with self.subTest(changed_field=field), self.assertRaises(ValueError):
                request_from_inputs(configuration(), bundle, make_profiles(), now=NOW)
            bundle = copy.deepcopy(original)
            bundle['sources'][source]['values'].pop()
            with self.subTest(short_source=source), self.assertRaises(ValueError):
                request_from_inputs(configuration(), bundle, make_profiles(), now=NOW)

    def test_request_rechecks_timeline_and_zero_export_price(self):
        from m4_settings.live_inputs import request_from_inputs
        original = self.fetch()
        changes = [
            ('point_time', lambda b: b['points'][0].update(timestamp=(START+timedelta(minutes=15)).isoformat())),
            ('sell_price', lambda b: b['points'][0].update(sell_price_per_kwh=0.1)),
            ('interval', lambda b: b.update(interval_minutes=30)),
            ('horizon', lambda b: b.update(horizon_points=95)),
            ('end_time', lambda b: b.update(plan_end_at=(START+timedelta(days=2)).isoformat())),
        ]
        for label, change in changes:
            bundle = copy.deepcopy(original)
            change(bundle)
            with self.subTest(label=label), self.assertRaises(ValueError):
                request_from_inputs(configuration(), bundle, make_profiles(), now=NOW)

    def test_request_requires_96_valid_tariff_labels_and_matching_point_labels(self):
        original = self.fetch()
        bad_labels = [None, {}, 'ping', ('ping',) * 96, ['ping'] * 95, ['ping'] * 97]
        bad_labels += [[value] + ['ping'] * 95 for value in (None, True, 1, '', 'valley', [], {})]
        for labels in bad_labels:
            bundle = copy.deepcopy(original)
            bundle['sources']['tariff']['period_types'] = labels
            with self.subTest(labels=str(labels)[:40]), self.assertRaises(ValueError):
                self.request(configuration(), bundle)
        missing = copy.deepcopy(original)
        missing['sources']['tariff'].pop('period_types', None)
        with self.assertRaises(ValueError):
            self.request(configuration(), missing)
        for label in ('gu', None):
            changed = copy.deepcopy(original)
            changed['points'][0]['tariff_period'] = label
            with self.subTest(point_label=label), self.assertRaises(ValueError):
                self.request(configuration(), changed)
        missing_point = copy.deepcopy(original)
        missing_point['points'][0].pop('tariff_period', None)
        with self.assertRaises(ValueError):
            self.request(configuration(), missing_point)

    def test_request_rechecks_wall_clock_and_observation_when_reusing_bundle(self):
        from m4_settings.live_inputs import request_from_inputs
        original = self.fetch()
        for clock in (START+timedelta(seconds=1), NOW-timedelta(seconds=1), NOW.replace(tzinfo=None)):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                request_from_inputs(configuration(), original, make_profiles(), now=clock)
        with patch('m4_settings.live_inputs._clock_now', return_value=START+timedelta(days=1)):
            with self.assertRaises(ValueError):
                request_from_inputs(configuration(), original, make_profiles())
        for field, owner, value in [
            ('fetched_at', None, (NOW-timedelta(hours=1)).isoformat()),
            ('fetched_at', None, (NOW+timedelta(seconds=1)).isoformat()),
            ('observed_at', 'realtime', (NOW-timedelta(hours=1)).isoformat()),
            ('observed_at', 'realtime', (NOW+timedelta(seconds=1)).isoformat()),
        ]:
            bundle = copy.deepcopy(original)
            target = bundle if owner is None else bundle['sources'][owner]
            target[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                request_from_inputs(configuration(), bundle, make_profiles(), now=NOW)
        accepted = request_from_inputs(configuration(), original, make_profiles(), now=START)
        self.assertEqual(accepted.plan_start_at, START)


if __name__ == '__main__':
    unittest.main()
