from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from m4.settings.allocation_records import AllocationRecords, AllocationError, digest
from m4.tests.test_m4_cabinet_allocation import fixture


def setup_service(root):
    x=fixture();run=str(uuid4());state={'calls':0,'configuration':'cfg'}
    req=dict(station_id='station-1',plan_start_at='2026-09-10T00:00:00+08:00',
        source_versions={'configuration':'cfg','controls':'ctrl'},max_input_age_seconds=300,
        constraints={'soc_min_pct':10,'soc_max_pct':90,'demand_limit_kw':500,'grid_import_limit_kw':500},
        capability={'charge_efficiency':1,'discharge_efficiency':1},
        points=[dict(timestamp=f'2026-09-10T{i//4:02}:{i%4*15:02}:00+08:00',load_forecast_kw=200,pv_forecast_kw=0,tariff_period='ping') for i in range(96)])
    job=dict(station_id='station-1',run_id=run,status='completed',request=req,baseline={'schedule':[dict(start_time='00:00:00',end_time='24:00:00',mode='discharge',power_kw=80,repeat='daily')]},result={'record':{'daily_comparison':dict(status='ems',recommended_source='ems',date='2026-09-10',controls_version='ctrl',input_sha256='inputhash',recommended={'plan_version':'ems/test','plan':[]})}})
    state['job']=job
    def fetch(cfg):
        state['calls']+=1
        return {'station_id':'station-1','configuration_version':'cfg','sources':{'realtime':deepcopy(x['snapshot'])}}
    service=AllocationRecords(root,lambda s:deepcopy(state['job']),lambda s:SimpleNamespace(version=state['configuration']),fetch,clock=lambda:datetime.fromisoformat(x['now']))
    return service,run,state


class AllocationRecordsTests(unittest.TestCase):
    def test_saved_restart_read_and_multiple_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp));a=s.create('station-1',str(uuid4()),run)
            again,_,_=setup_service(Path(tmp));self.assertEqual(again.get('station-1',a['allocation_id']),a)
            s.create('station-1',str(uuid4()),run)
            self.assertEqual(len(s.history('station-1',run_id=run)['items']),2)
            self.assertEqual(state['calls'],2)

    def test_idempotent_before_reloading_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp));key=str(uuid4());a=s.create('station-1',key,run)
            state['configuration']='new';self.assertEqual(s.create('station-1',key,run),a)
            self.assertEqual(state['calls'],1)
            with self.assertRaises(AllocationError):s.create('station-1',key,str(uuid4()))

    def test_concurrent_duplicate_saves_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp));key=str(uuid4())
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:s.create('station-1',key,run),range(2)))
            self.assertEqual(results[0],results[1]);self.assertEqual(state['calls'],1)

    def test_no_plan_wrong_run_and_changed_configuration_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp))
            with self.assertRaises(AllocationError):s.create('station-1',str(uuid4()),str(uuid4()))
            state['configuration']='new'
            with self.assertRaises(AllocationError):s.create('station-1',str(uuid4()),run)
            self.assertEqual(list(Path(tmp).rglob('*.json')),[])

    def test_reads_do_not_fetch_and_are_station_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp));a=s.create('station-1',str(uuid4()),run);state['calls']=0
            s.get('station-1',a['allocation_id']);s.history('station-1');self.assertEqual(state['calls'],0)
            with self.assertRaises(AllocationError):s.get('station-2',a['allocation_id'])
            self.assertEqual(s.history('station-2')['items'],[])

    def test_tampering_rejected_and_history_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,_=setup_service(Path(tmp));a=s.create('station-1',str(uuid4()),run)
            path=Path(tmp)/'station-1'/(a['allocation_id']+'.json');d=json.loads(path.read_text());d['result']['slots'][0]['allocatedKw']=999;path.write_text(json.dumps(d))
            with self.assertRaises(AllocationError):s.get('station-1',a['allocation_id'])
            self.assertEqual(s.history('station-1')['items'][0]['status'],'unreadable')

    def test_symlinks_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'records';root.mkdir();(root/'station-1').symlink_to(Path(tmp),target_is_directory=True)
            s,run,_=setup_service(root)
            with self.assertRaises(AllocationError):s.create('station-1',str(uuid4()),run)
            with self.assertRaises(AllocationError):s.get('station-1','../x')

    def test_plan_changes_during_fetch_leaves_no_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,state=setup_service(Path(tmp));fetch=s.fetch_inputs
            def changed(cfg):
                result=fetch(cfg);state['job']['run_id']=str(uuid4());return result
            s.fetch_inputs=changed
            with self.assertRaises(AllocationError) as error:s.create('station-1',str(uuid4()),run)
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(list(Path(tmp).rglob('*.json')),[])

    def test_rehashed_incorrect_result_fails_independent_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,run,_=setup_service(Path(tmp));a=s.create('station-1',str(uuid4()),run)
            path=Path(tmp)/'station-1'/(a['allocation_id']+'.json')
            a.pop('sha256');a['result']['slots'][0]['allocatedKw']=999
            a['sha256']=digest(a);path.write_text(json.dumps(a))
            with self.assertRaises(AllocationError) as error:s.get('station-1',a['allocation_id'])
            self.assertEqual(error.exception.status_code,503)
