import unittest
from datetime import datetime, timedelta, timezone
from m4.settings.load_accuracy import assess, SHANGHAI

NOW=datetime(2026,9,10,10,tzinfo=timezone.utc)
def row(value='30', now=NOW, station='ES02'):
 start=now.astimezone(SHANGHAI).replace(hour=0,minute=0,second=0,microsecond=0)
 run=dict(id=808,station_id=station,run_id='current',status='succeeded',forecast_start=start.isoformat(),forecast_end=(start+timedelta(days=1)).isoformat(),completed_at=(start-timedelta(minutes=1)).isoformat(),interval_seconds=900,forecast_days=1,expected_points_per_series=96,model_manifest={'selection_policy':'weekly_load_v2'})
 points=[dict(run_id='current',unique_id='station_total_load',target_time=(start+timedelta(minutes=15*i)).isoformat(),horizon_step=i+1,forecast_value=100+float(value),actual_value=100 if i==0 else None,actual_quality='valid' if i==0 else None) for i in range(96)]
 return {'run':run,'points':points,'current_score':dict(policy='load-night-weighted-mape-v1',run_id=run['run_id'],unique_id='station_total_load',mape_percent=float(value),actual_count=1,valid_count=1,calculated_at=now.isoformat())}
class AccuracyTests(unittest.TestCase):
 def test_exact_threshold_allowed(self):self.assertEqual(assess(row(),'station-2',NOW)['status'],'ready')
 def test_above_threshold_blocked(self):self.assertEqual(assess(row('30.000001'),'station-2',NOW)['status'],'blocked')
 def test_single_valid_point_allowed_without_weekly_minimum(self):
  result=assess(row('10'),'station-2',NOW);self.assertEqual(result['status'],'ready');self.assertEqual(result['valid_count'],1);self.assertTrue(result['provisional'])
 def test_invalid_pairs_and_zero_excluded(self):
  r=row('10');r['points'][1].update(actual_quality='valid',actual_value=0)
  r['points'][2].update(actual_quality='valid',actual_value='NaN')
  r['points'][3].update(actual_quality='invalid',actual_value=1000)
  r['current_score']['actual_count']=2
  g=assess(r,'station-2',NOW);self.assertEqual(g['status'],'ready');self.assertEqual(g['mape_percent'],'10.0');self.assertEqual(g['zero_actual_count'],1)
 def test_empty_pairs_block(self):
  r=row();r['points'][0]['actual_value']=None;self.assertEqual(assess(r,'station-2',NOW)['status'],'unavailable')
 def test_not_current_day_block(self):
  self.assertEqual(assess(row(),'station-2',NOW+timedelta(days=1))['status'],'unavailable')
 def test_wrong_station_or_series_block(self):
  r=row();r['run']['station_id']='ES01';self.assertEqual(assess(r,'station-2',NOW)['status'],'unavailable')
  r=row();r['points'][0]['unique_id']='storage_soc';self.assertEqual(assess(r,'station-2',NOW)['status'],'unavailable')
 def test_gate_uses_backend_score_not_standard_mape(self):
  r=row('10');r['points'][0]['forecast_value']=900
  self.assertEqual(assess(r,'station-2',NOW)['mape_percent'],'10.0')
 def test_missing_or_mismatched_backend_score_blocks(self):
  for key,value in [('policy','old'),('run_id','other'),('actual_count',2),('valid_count',True),('mape_percent',-1),('mape_percent','NaN')]:
   r=row('10');r['current_score'][key]=value
   self.assertEqual(assess(r,'station-2',NOW)['status'],'unavailable')
  r=row();del r['current_score']
  self.assertEqual(assess(r,'station-2',NOW)['status'],'unavailable')
 def test_read_timestamp_does_not_change_source_version(self):
  r=row();before=assess(r,'station-2',NOW)['version']
  r['current_score']['calculated_at']=(NOW+timedelta(seconds=2)).isoformat()
  self.assertEqual(assess(r,'station-2',NOW)['version'],before)
 def test_score_calculated_during_read_is_allowed(self):
  r=row();r['current_score']['calculated_at']=(NOW+timedelta(seconds=2)).isoformat()
  self.assertEqual(assess(r,'station-2',NOW)['status'],'ready')


class SolverEntryTests(unittest.TestCase):
 def test_rolling_gate_blocks_optimizer(self):
  import tempfile
  from pathlib import Path
  from unittest.mock import Mock
  from m4.tests.test_m4_live_inputs import Client,Controls,NOW as LIVE_NOW
  from m4.tests.test_m4_realtime import configuration
  from m4.settings.live_inputs import LiveInputService
  from m4.settings.store import SettingsStore
  from m4.settings.candidates import CandidateService,CandidateError
  client=Client();client.current_load_result=lambda station:row('31',now=LIVE_NOW,station=station)
  controls=Controls();optimizer=Mock()
  with tempfile.TemporaryDirectory() as directory:
   store=SettingsStore(Path(directory)/'settings.db')
   config=store.save('station-1',configuration().parameters,expected_revision=0)
   service=CandidateService(store=store,fetch_inputs=lambda c:LiveInputService(client,controls).fetch(c,now=LIVE_NOW),
       read_controls=controls.fetch,optimizer=optimizer,clock=lambda:LIVE_NOW)
   with self.assertRaises(CandidateError) as caught:service.calculate('station-1',config.version)
   self.assertEqual(caught.exception.status_code,422)
   self.assertTrue(any('MAPE' in issue for issue in caught.exception.detail['issues']))
   optimizer.optimize.assert_not_called()
 def test_daily_gate_blocks_optimizer_even_if_ready_flag_forged(self):
  import tempfile
  from unittest.mock import Mock,patch
  from m4.settings.daily_plans import DailyPlanService
  from m4.settings.load_accuracy import assess
  gate=assess(row('31'),'station-2',NOW)
  config=Mock();config.station_id='station-2'
  store=Mock();store.get.return_value=config
  inputs=Mock();inputs.fetch.return_value={'can_compare':True,'sources':{'load':{'accuracy_gate':gate}}}
  with tempfile.TemporaryDirectory() as directory:
   service=DailyPlanService(store,inputs,directory)
   with patch('m4.settings.daily_plans.prepare_ems_day',return_value=(Mock(),{},None)),patch('m4.settings.daily_plans.M4Optimizer') as optimizer:
    service._run('station-2','test-job')
    optimizer.assert_not_called()
   self.assertEqual(service.jobs['station-2']['status'],'failed')
   self.assertIn('MAPE',service.jobs['station-2']['message'])

class CurrentResultTests(unittest.TestCase):
 def fixture(self):
  from m4.settings.m3_current_result import M3CurrentResult
  from unittest.mock import Mock
  evidence=row('10');run=evidence['run'];run['run_id']='f98ae7a6-c749-4ddf-b1c0-e90488136a20'
  evidence['current_score']['run_id']=run['run_id']
  result={'run':run,'series':[{'unique_id':'station_total_load','unit':'kW','points':evidence['points'],'current_score':evidence['current_score']}]}
  client=M3CurrentResult();client.get=Mock(side_effect=[dict(run),result])
  return client,result
 def test_worker_result_overlay_used_and_soc_ignored(self):
  client,result=self.fixture()
  result['series'].append({'unique_id':'storage_soc','unit':'%','points':[]})
  gate=assess(client.read('ES02'),'station-2',NOW)
  self.assertEqual(gate['status'],'ready');self.assertEqual(gate['valid_count'],1)
  self.assertIn('interval_seconds=900&forecast_days=1',client.get.call_args_list[0].args[0])
  self.assertTrue(client.get.call_args_list[1].args[0].endswith('/result'))
 def test_wrong_result_identity_rejected(self):
  client,result=self.fixture();result['run']['station_id']='ES01'
  with self.assertRaises(ValueError):client.read('ES02')
 def test_duplicate_load_series_rejected(self):
  client,result=self.fixture();result['series']*=2
  with self.assertRaises(ValueError):client.read('ES02')
 def test_read_failure_blocks_without_database_fallback(self):
  from m4.settings.load_accuracy import read_gate
  from unittest.mock import Mock
  client=Mock();client.current_load_result.side_effect=TimeoutError
  self.assertEqual(read_gate(client,'station-2',NOW)['status'],'unavailable')
  client.list_rows.assert_not_called()
 def test_transport_uses_get_and_bearer_and_closes(self):
  import tempfile,json
  from pathlib import Path
  from unittest.mock import Mock,patch
  from m4.settings.m3_current_result import M3CurrentResult
  response=Mock(status=200);response.read.return_value=json.dumps({'ok':True}).encode()
  connection=Mock();connection.getresponse.return_value=response
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'token';path.write_text('test-only-m3-admin-token')
   with patch.dict('os.environ',{'M4_M3_ADMIN_TOKEN_FILE':str(path),'M4_M3_SOCKET_PATH':'/tmp/test-worker.sock'}),patch('m4.settings.m3_current_result.SocketConnection',return_value=connection) as factory:
    route='/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1'
    self.assertEqual(M3CurrentResult().get(route),{'ok':True})
    factory.assert_called_once_with('/tmp/test-worker.sock')
    connection.request.assert_called_once_with('GET',route,headers={'Authorization':'Bearer test-only-m3-admin-token','Accept':'application/json'})
    connection.close.assert_called_once()
