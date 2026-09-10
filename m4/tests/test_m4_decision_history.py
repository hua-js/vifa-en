"""Historical reads must never run the optimizer or bypass evidence checks."""
import json
import os
from uuid import uuid4
import unittest
from m4.tests import test_m4_decision_chain as fixture
from m4.selection.decision_chain import run_chain
from m4.settings.decision_results import DecisionResultsReader


class DecisionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DecisionChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.reader=DecisionResultsReader(self.f.root)

    def add_run(self, custom=False):
        identity=str(uuid4());path=self.f.root/'station-1'/('custom-output' if custom else identity)
        result=run_chain(self.f.api,path,station_id='station-1',run_id=identity)
        self.assertEqual(result['status'],'completed')
        return identity,path

    def test_paginated_list_detail_and_input_context_are_read_only(self):
        first,p1=self.add_run();second,p2=self.add_run()
        os.utime(p1/'report.json',ns=(1_000_000,1_000_000));os.utime(p2/'report.json',ns=(2_000_000,2_000_000))
        before=list(self.f.api.calls)
        view=self.reader.history('station-1',limit=1)
        self.assertEqual(view['items'][0]['run_id'],second)
        self.assertEqual(view['next_offset'],1)
        other=self.reader.history('station-1',limit=1,offset=1)
        self.assertEqual(other['items'][0]['run_id'],first);self.assertIsNone(other['next_offset'])
        detail=self.reader.by_run('station-1',first)['record']
        self.assertEqual(detail['run_id'],first)
        self.assertIn('initial_soc_pct',detail['input_summary'])
        self.assertEqual(before,self.f.api.calls)

    def test_corrupt_entry_is_explicit_and_never_replaces_requested_record(self):
        first,_=self.add_run();broken,path=self.add_run()
        (path/'report.json').write_text('{broken')
        view=self.reader.history('station-1')
        self.assertTrue(any(item['status']=='unreadable' for item in view['items']))
        self.assertEqual(self.reader.by_run('station-1',first)['record']['run_id'],first)
        with self.assertRaises(ValueError):self.reader.by_run('station-1',broken)

    def test_station_uuid_bounds_and_custom_cli_directory(self):
        identity,_=self.add_run(custom=True)
        self.assertEqual(self.reader.by_run('station-1',identity)['record']['run_id'],identity)
        self.assertEqual(self.reader.history('station-2')['items'],[])
        with self.assertRaises(FileNotFoundError):self.reader.by_run('station-2',identity)
        for value in ['../station-2','bad','00000000-0000-0000-0000-000000000000']:
            with self.assertRaises(ValueError):self.reader.by_run('station-1',value)
        for kwargs in [{'limit':0},{'limit':21},{'offset':-1}]:
            with self.assertRaises(ValueError):self.reader.history('station-1',**kwargs)

    def test_api_history_detail_and_invalid_queries(self):
        identity,_=self.add_run();base='/m4-api/stations/station-1/'
        response=self.f.client.get(base+'decision-history?limit=1');self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['items'][0]['run_id'],identity)
        detail=self.f.client.get(base+'decision-results/'+identity);self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.json()['record']['run_id'],identity)
        self.assertEqual(self.f.client.get(base+'decision-results/'+str(uuid4())).status_code,404)
        self.assertEqual(self.f.client.get(base+'decision-results/not-a-uuid').status_code,422)
        self.assertEqual(self.f.client.get(base+'decision-history?limit=500').status_code,422)

    def test_duplicate_identity_and_symlink_directory_are_rejected(self):
        identity,path=self.add_run()
        import shutil
        shutil.copytree(path,path.parent/'duplicate')
        with self.assertRaises(ValueError):self.reader.by_run('station-1',identity)
        (self.f.root/'station-2').symlink_to(path.parent,target_is_directory=True)
        with self.assertRaises(ValueError):self.reader.history('station-2')

if __name__=='__main__':unittest.main()
