import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from m4_settings.api import create_app
from m4_settings.selection import PolicyPreferences
import test_m4_live_selection as live_fixture
from m4_selection.live_chain import run_chain, make_ai_payload, assess_answer
from m4_selection.ai_config import load_model_config


class Api:
    def __init__(self, client):self.client=client;self.calls=[]
    def request(self, method, path, data=None):
        self.calls.append((method,path))
        response=self.client.request(method,path,json=data)
        response.raise_for_status()
        return response.json()


class LiveChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture=live_fixture.LiveSelectionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        f.policies.save('station-1',PolicyPreferences(metric='profile_priority',metric_tolerance=0.0,
            demand_peak_tolerance_kw=0.0,demand_energy_tolerance_kwh=0.0,tie_order=['balanced','cost','pv']),expected_revision=0)
        # Freeze only the injected input fetcher, just like the candidate service.
        from types import SimpleNamespace
        app=create_app(f.path,control_reader=f.controls,input_service=SimpleNamespace(fetch=f.fetch),candidate_service=f.candidates,selection_service=f.service)
        self.client=TestClient(app);self.addCleanup(self.client.close);self.api=Api(self.client)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.output=Path(self.temp.name)
        self.model_calls=[]

    def model(self,payload,output):
        self.model_calls.append(payload)
        context=json.loads(payload['messages'][1]['content'])
        survivors=context['comparison'][1]['remaining_ids']
        choice=next(pid for pid in context['policy']['tie_order'] if pid in survivors)
        return json.dumps({'snapshot_id':context['snapshot_id'],'selected_candidate_id':choice,'reason':'需量筛选后按候选顺序选择。'},ensure_ascii=False)

    def test_real_solver_to_ai_checked_preview_and_compact_payload(self):
        report=run_chain(self.api,self.model,self.output)
        self.assertEqual(report['status'],'completed',report)
        self.assertEqual(report['dispatch_status'],'not_dispatched')
        self.assertEqual(report['ai']['status'],'accepted')
        self.assertEqual(len(self.model_calls),1)
        context=json.loads(self.model_calls[0]['messages'][1]['content'])
        self.assertEqual(len(context['candidates']),3)
        self.assertNotIn('plan',json.dumps(context))
        self.assertNotIn('emu11',json.dumps(context))
        self.assertEqual(context['policy']['metric'],'profile_priority')
        self.assertTrue((self.output/'candidates.json').exists())
        self.assertTrue((self.output/'report.json').exists())

    def test_missing_forecast_blocks_before_solver_or_ai(self):
        self.fixture.client.forecasts.pop()
        report=run_chain(self.api,self.model,self.output)
        self.assertEqual(report['status'],'blocked_inputs')
        self.assertEqual(self.model_calls,[])
        self.assertFalse(any(method=='POST' for method,_ in self.api.calls))

    def test_ai_wrong_choice_is_recorded_but_not_selected(self):
        def wrong(payload,output):
            good=json.loads(self.model(payload,output))
            good['selected_candidate_id']=next(pid for pid in ['balanced','cost','pv'] if pid!=good['selected_candidate_id'])
            return json.dumps(good)
        report=run_chain(self.api,wrong,self.output)
        self.assertEqual(report['status'],'ai_rejected')
        self.assertIsNone(report['selected'])
        self.assertEqual(report['ai']['status'],'policy_mismatch')
        self.assertTrue(report['ai']['raw_answer'])

    def test_changed_input_after_model_cannot_return_current_selection(self):
        def changing(payload,output):
            answer=self.model(payload,output)
            self.fixture.client.devices[1]['latest_soc']=49.0
            return answer
        report=run_chain(self.api,changing,self.output)
        self.assertEqual(report['status'],'blocked_post_ai_validation')
        self.assertIsNone(report['selected'])
        self.assertEqual(report['ai']['status'],'accepted')

    def test_missing_model_configuration_is_not_ai_success(self):
        report=run_chain(self.api,None,self.output)
        self.assertEqual(report['status'],'blocked_ai_configuration')
        self.assertIsNone(report['selected'])

    def test_disabled_model_config_skips_call_and_retains_effective_config_audit(self):
        config=load_model_config().model_copy(update={'enabled':False})
        report=run_chain(self.api,self.model,self.output,config)
        self.assertEqual(report['status'],'blocked_ai_configuration')
        self.assertEqual(self.model_calls,[])
        self.assertEqual(report['model_config_sha256'],config.digest())
        self.assertEqual(json.loads((self.output/'model-config.json').read_text()),config.model_dump(mode='json'))

    def test_malformed_duplicate_and_wrong_snapshot_answers_rejected(self):
        f=self.fixture;envelope=f.calculate();preview=f.select(1).selection
        payload=make_ai_payload(envelope,preview)
        context=json.loads(payload['messages'][1]['content'])
        good={'snapshot_id':context['snapshot_id'],'selected_candidate_id':preview.selected.profile_id,'reason':'按候选顺序选择。'}
        for text in ['not json',json.dumps({**good,'snapshot_id':'other'}),json.dumps({**good,'extra':1}),json.dumps(good)[:-1]+',"reason":"重复"}',json.dumps({**good,'reason':' '})]:
            with self.subTest(text=text):
                self.assertNotEqual(assess_answer(text,context,preview)['status'],'accepted')


if __name__=='__main__':unittest.main()
