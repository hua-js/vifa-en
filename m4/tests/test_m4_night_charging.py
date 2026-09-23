"""Offline night charging without daily plans or forecast endpoints."""
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from shared.project import get_project
from m4.settings.night_charging import build_payload, validate_freshness, POLICY
from m4.settings.startup_admission import ZONE
from m4.settings.ems_remaining_plan import EMSRemainingPlanAdapter
from m4.settings.ems_table_writer import EMSTableWriter
from m4.settings.rolling_plans import PlanningCoordinator, RollingPlanService
from m4.tests.test_m4_realtime import configuration, cabinet


class NightChargingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 22, 0, 1, tzinfo=ZONE)
        self.config = configuration('station-2', energy_capacity_kwh=3132.0,
            max_charge_kw=600.0, max_discharge_kw=600.0, soc_min_pct=2.0, soc_max_pct=98.0,
            preferred_soc_min_pct=5.0, preferred_soc_max_pct=95.0, grid_import_limit_kw=1000.0,
            max_input_age_seconds=900)
        self.run_id = '481d3847-60ec-4357-8805-c1e1227c4034'
        self.devices = [cabinet(sn, 20.0, station='ES02', latest_power=0.0,
            last_time_iso=(self.now-timedelta(seconds=30)).isoformat())
            for sn in get_project().station('station-2').cabinet_sns]
        self.station = dict(es_sn='ES02', timestamp=self.now.isoformat(), load_power=100.0, grid_power=100.0)
        self.controls = dict(station_id='station-2', version='controls-test', status='ready',
            power_scope='cabinet', configured_cabinet_count=len(self.devices),
            source_health=dict(schedule='ready'), demand=dict(need_kw=1000.0),
            storage_capacity=dict(scope='station', source_station_id='ES02', source_table='t_es',
                source_field='es_power_storage', energy_capacity_kwh=3132.0))
        self.tariff = dict(version='tariff-test', period_types=['gu']*32+['ping']*64)
        self.client = Mock()
        self.client.list_rows.side_effect = lambda table, **kw: deepcopy(
            self.devices if table == 't_emu' else [self.station] if table == 't_es_data' else [])
        self.client.current_load_result.side_effect = AssertionError('Must not read forecasts')
        self.live = SimpleNamespace(client=self.client,
            _controls=lambda *a, **k: deepcopy(self.controls), _tariff=lambda *a: deepcopy(self.tariff))

    def payload(self):
        return build_payload(self.live, self.config, self.run_id, now=self.now)

    def test_midnight_charge_does_not_wait_for_forecast_or_daily_plan(self):
        payload = self.payload()
        self.assertEqual(payload['effective_at'], '2026-09-22T00:15:00+08:00')
        self.assertEqual(payload['end_at'], '2026-09-22T08:00:00+08:00')
        self.assertEqual(payload['plan'][0]['mode'], 'charge')
        self.assertIsNone(payload['daily_run_id'])
        self.assertEqual(payload['request']['source_versions']['night_charging_policy'], POLICY)
        self.client.current_load_result.assert_not_called()
        self.assertEqual({c.args[0] for c in self.client.list_rows.call_args_list}, {'t_emu','t_es_data'})

    def test_measured_charging_above_target_keeps_plan_and_setpoint_at_600(self):
        from m4.settings.daily_dispatch import night_payload, DailyScheduleAdapter
        for row in self.devices:
            row['latest_power'] = -650 / len(self.devices)
        self.station['grid_power'] = 750.0
        payload = night_payload(self.payload())
        self.assertEqual(payload['ems_setpoint_kw'], 600)
        self.assertEqual(payload['request']['capability']['max_charge_kw'], 600)
        self.assertLessEqual(max(p['target_power_kw'] for p in payload['plan']), 600)
        reader = SimpleNamespace(_read_table=lambda table: [])
        result = DailyScheduleAdapter(reader).preview('station-2', payload, self.config, now=self.now)
        charges = [op['body'] for op in result['operations']['create'] if op['body']['type'] == 'charge']
        self.assertTrue(charges)
        self.assertTrue(all(row['kw'] == 600 for row in charges))

    def test_measured_power_above_650_is_rejected(self):
        self.devices[0]['latest_power'] = -650 / len(self.devices) - .01
        with self.assertRaisesRegex(ValueError, '当前储能功率超出配置范围'):
            self.payload()

    def test_soc_between_target_and_99_stays_idle_without_clipping(self):
        from m4.settings.ems_model_update import validate_dispatch_safety
        for row in self.devices:
            row['latest_soc'] = 98.5
        payload = self.payload()
        self.assertEqual(payload['request']['capability']['initial_soc_pct'], 98.5)
        self.assertTrue(all(p['mode'] == 'idle' and p['target_power_kw'] == 0 for p in payload['plan']))
        self.assertEqual(self.config.parameters.soc_max_pct, 98)
        validate_dispatch_safety(payload['request'], payload['plan'])

    def test_soc_at_or_above_99_is_not_admitted(self):
        for soc in (99.0, 99.2):
            with self.subTest(soc=soc):
                self.devices[0]['latest_soc'] = soc
                with self.assertRaisesRegex(ValueError, '储能柜状态或SOC'):
                    self.payload()

    def test_full_soc_becomes_idle_and_unequal_soc_never_overcharges_highest_cabinet(self):
        self.devices[0]['latest_soc'] = 98.0
        payload = self.payload()
        self.assertTrue(all(p['mode']=='idle' for p in payload['plan']))
        self.devices[0]['latest_soc'] = 97.0
        payload = self.payload()
        energy = sum(p['target_power_kw']*.25 for p in payload['plan'])
        self.assertLessEqual(energy*self.config.parameters.charge_efficiency/3132*100, 1.000001)

    def test_slow_reads_cannot_dispatch_expired_telemetry(self):
        payload = self.payload()
        validate_freshness(payload, self.config, self.now)
        for key in ('observed_at', 'load_observed_at'):
            stale = deepcopy(payload)
            stale['anchor'][key] = (self.now-timedelta(seconds=899)).isoformat()
            with self.assertRaises(ValueError):
                validate_freshness(stale, self.config, self.now+timedelta(seconds=2))

    def test_stale_or_unavailable_device_and_load_block(self):
        original = deepcopy(self.devices)
        for fields in ({'last_time_iso':(self.now-timedelta(minutes=16)).isoformat()},
                       {'emu_status':'offline'}, {'alert_status':'alert'}):
            self.devices = deepcopy(original)
            self.devices[0].update(fields)
            with self.assertRaises(ValueError): self.payload()
        self.devices = original
        self.station['timestamp'] = (self.now-timedelta(minutes=16)).isoformat()
        with self.assertRaises(ValueError): self.payload()

    def test_daytime_nonvalley_and_demand_excess_block(self):
        self.tariff['period_types'] = ['ping']*96
        with self.assertRaises(ValueError): self.payload()
        self.tariff['period_types'] = ['gu']*32+['ping']*64
        self.station.update(load_power=1001.0, grid_power=1001.0)
        with self.assertRaises(ValueError): self.payload()
        self.now = self.now.replace(hour=8)
        with self.assertRaises(ValueError): self.payload()

    def test_night_entry_does_not_read_daily_status(self):
        daily, rolling = Mock(), Mock()
        daily.inputs.live = self.live
        daily.latest.side_effect = AssertionError('No daily dependency at night')
        with patch('m4.settings.rolling_plans.datetime') as clock:
            clock.now.return_value = self.now
            PlanningCoordinator(daily, rolling).start('station-2')
        rolling.start.assert_called_once_with('station-2')
        daily.start.assert_not_called()

    def test_write_and_readback_mark_only_m4_owned_records(self):
        payload = self.payload()
        old_day = dict(id=7, es_sn=['ES02'], type='discharge', kw=600, repeat='每天重复',
            start_time='08:00:00', end_time='12:00:00', updatedAt='2026-09-21T00:00:00Z')
        rows = [deepcopy(old_day)]
        reader = Mock();reader._read_table.side_effect = lambda _: deepcopy(rows)
        transport = Mock()
        def post(request, **kwargs):
            self.assertEqual(urlsplit(request.full_url).path.split(':')[-1], 'create')
            body = json.loads(request.data)
            self.assertEqual(body['m4_run_id'], self.run_id)
            self.assertEqual(body['m4_plan_date'], '2026-09-22')
            self.assertEqual(body['kw'], 600)
            row = dict(body, id=100+len(rows), updatedAt='2026-09-21T16:01:01Z')
            rows.append(row)
            response = io.BytesIO(json.dumps({'data':row}).encode());response.status=200
            return response
        transport.open.side_effect = post
        with tempfile.TemporaryDirectory() as tmp:
            writer = EMSTableWriter(EMSRemainingPlanAdapter(reader), tmp, 'test-only', enabled=True, opener=transport)
            result = writer.submit('station-2', payload, self.config, now=self.now)
        self.assertEqual(result['status'], 'plan_table_readback_verified')
        self.assertEqual(rows[0], old_day)
        self.assertTrue(all(p['timestamp'] < payload['end_at'] for p in result['confirmed_plan']))

    def test_saved_night_result_survives_absent_daily_plan(self):
        payload = self.payload()
        with tempfile.TemporaryDirectory() as tmp:
            daily = Mock();daily.root=Path(tmp);daily.store.get.return_value=self.config
            daily.latest.side_effect = AssertionError('No daily dependency for night readback')
            service = RollingPlanService(daily)
            from m4.settings.rolling_plans import POLICY as ROLLING_POLICY
            service.jobs['station-2'] = dict(station_id='station-2',status='completed',
                policy_version=ROLLING_POLICY,run_id=self.run_id,result=payload)
            with patch('m4.settings.rolling_plans.datetime') as clock:
                clock.now.return_value=self.now
                clock.fromisoformat.side_effect=datetime.fromisoformat
                self.assertEqual(service.latest('station-2')['run_id'],self.run_id)
