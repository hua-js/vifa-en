"""Real current-task -> daily -> remaining-day boundaries; no scoring bypass."""
import copy
from datetime import datetime,timedelta
from unittest import TestCase
from unittest.mock import Mock,patch
from m4.settings.daily_inputs import DailyInputService
from m4.settings.runtime_config import effective_configuration
from m4.settings.live_inputs import LiveInputService,request_from_inputs
from m4.settings.rolling_plans import prepare_remaining_request
from m4.tests.test_m4_live_inputs import Client,Controls,NOW,START
from m4.tests.test_m4_realtime import configuration
from m4.tests.m4_optimizer_test_support import make_profiles
from m4.tests.test_m4_load_accuracy import row

DAY=NOW.replace(hour=0,minute=0)

class CurrentClient(Client):
    def list_rows(self,table,**kwargs):
        if table=='t_es_data' and kwargs.get('limit')!=1:
            return [dict(es_sn=self.station,emus_soc=50.0,timestamp=DAY.isoformat())]
        return super().list_rows(table,**kwargs)

class FrozenDatetime(datetime):
    @classmethod
    def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

class CurrentWindowTests(TestCase):
    def setUp(self):
        self.client=CurrentClient()
        for device in self.client.devices:device['latest_power']=0.0
        self.controls=Controls()
        self.controls.result.update(power_scope='cabinet',configured_cabinet_count=2,
            control_policy_version='m4-control-policy-v5-need-import-limit',
            storage_capacity=dict(scope='station',source_station_id='ES01',source_table='t_es',source_field='es_power_storage',energy_capacity_kwh=500),
            source_health=dict(schedule='ready'),schedule=[dict(start_time='00:00:00',end_time='08:00:00',
                mode='charge',power_kw=40.0,repeat='daily'),dict(start_time='08:00:00',end_time='24:00:00',
                mode='discharge',power_kw=45.0,repeat='daily')])
        self.config=effective_configuration(configuration())
        self.live=LiveInputService(self.client,self.controls)

    def daily(self):
        with patch('m4.settings.daily_inputs.datetime',FrozenDatetime):
            return DailyInputService(self.live).fetch(self.config,DAY.date(),now=NOW)

    def test_current_day_cannot_be_stitched_into_future_24_hours(self):
        inputs=self.live.fetch(self.config,now=NOW)
        self.assertEqual(inputs['status'],'blocked')
        self.assertEqual(inputs['sources']['load']['coverage_points'],47)
        self.assertEqual(inputs['sources']['load']['values'][47:],[None]*49)
        self.assertEqual(inputs['points'],[])
        with self.assertRaises(ValueError):request_from_inputs(self.config,inputs,make_profiles(),now=NOW)
        self.assertFalse(any('energy_forecast_' in table for table,_ in self.client.calls))

    def test_same_current_task_supports_daily_and_remaining_47_points(self):
        inputs=self.daily()
        self.assertTrue(inputs['can_compare'],inputs['checks'])
        self.assertEqual(len(inputs['points']),96)
        request,_,_,anchor,_=prepare_remaining_request(self.config,inputs,NOW)
        self.assertEqual(request.horizon_points,47)
        self.assertEqual(request.plan_start_at,START)
        self.assertEqual(request.points[-1].timestamp+timedelta(minutes=15),DAY+timedelta(days=1))
        self.assertEqual(request.source_versions['load_run_id'],inputs['sources']['load']['run_id'])
        self.assertEqual(anchor['measured_soc_pct'],50)

    def test_remaining_request_rejects_other_station_and_stale_samples(self):
        inputs=self.daily();self.assertTrue(inputs['can_compare'],inputs['checks'])
        for field in ('station_id','current_soc','current_power'):
            changed=copy.deepcopy(inputs)
            if field=='station_id':changed[field]='station-2'
            else:changed['sources'][field]['observed_at']=(NOW-timedelta(hours=1)).isoformat()
            with self.subTest(field=field),self.assertRaises(ValueError):
                prepare_remaining_request(self.config,changed,NOW)

    def test_missing_or_bad_current_task_blocks_daily_and_remaining(self):
        for evidence in (None,row('31',now=NOW,station='ES01')):
            self.client.current_load_result=Mock(return_value=evidence)
            inputs=self.daily()
            self.assertFalse(inputs['can_compare'])
            with self.assertRaises(ValueError):prepare_remaining_request(self.config,inputs,NOW)

    def test_remaining_rechecks_score_even_if_ready_flag_is_forged(self):
        inputs=self.daily();self.assertTrue(inputs['can_compare'],inputs['checks'])
        inputs['sources']['load']['accuracy_gate']['evidence']['current_score']['mape_percent']=31
        with self.assertRaisesRegex(ValueError,'证据已变化'):
            prepare_remaining_request(self.config,inputs,NOW)

    def test_remaining_rejects_high_score_with_current_evidence_version(self):
        from m4.settings.load_accuracy import assess
        inputs=self.daily()
        gate=inputs['sources']['load']['accuracy_gate']
        gate['evidence']['current_score']['mape_percent']=31
        inputs['sources']['load']['accuracy_gate']=assess(gate['evidence'],'station-1',NOW)
        with self.assertRaisesRegex(ValueError,'MAPE'):
            prepare_remaining_request(self.config,inputs,NOW)
