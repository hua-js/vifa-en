import copy
import importlib.util
import json
import unittest

from fastapi.testclient import TestClient
from m3.tests.test_pv_publication import bundle
from m3.tests.test_pv_forecast_tools import load


def query():
    assert importlib.util.find_spec('m3.worker.services.pv_query') is not None,'query service missing'
    from m3.worker.services import pv_query
    return pv_query


class Repo:
    def __init__(self):
        run,points=bundle()
        self.run={**run,'id':1,'createdAt':run['generated_at'],'updatedAt':run['generated_at']}
        self.points=[{**p,'run_pk':1,'id':i+1} for i,p in enumerate(points)]
    def latest_run(self,at):return copy.deepcopy(self.run)
    def read_points(self,pk):return copy.deepcopy(self.points)


class QueryTests(unittest.TestCase):
    def test_complete_forecast_returns_curve_energy_peak_without_training_snapshot(self):
        repo=Repo();result=query().latest_forecast(repo,'2026-09-09T22:16:00+08:00')
        self.assertEqual(result['status'],'ok')
        data=result['data'];self.assertEqual(len(data['points']),96)
        self.assertEqual(data['freshness'],'fresh')
        self.assertEqual(data['remaining_full_points'],96)
        self.assertAlmostEqual(data['forecast_energy_kwh'],sum(float(p['forecast_kw']) for p in repo.points)/4)
        self.assertEqual(data['peak_kw'],max(float(p['forecast_kw']) for p in repo.points))
        self.assertNotIn('source_manifest',data);self.assertNotIn('model_config',data)

    def test_empty_stale_and_expired_are_explicit(self):
        repo=Repo();saved=repo.run;repo.run=None
        self.assertEqual(query().latest_forecast(repo,'2026-09-09T22:16:00+08:00')['status'],'empty')
        repo.run=saved
        self.assertEqual(query().latest_forecast(repo,'2026-09-10T01:00:00+08:00')['data']['freshness'],'stale')
        result=query().latest_forecast(repo,'2026-09-10T22:30:00+08:00')['data']
        self.assertEqual(result['freshness'],'expired');self.assertEqual(result['remaining_full_points'],0)

    def test_unfinished_foreign_future_or_incomplete_batch_is_rejected(self):
        for issue in ('running','station','future','missing'):
            repo=Repo()
            if issue=='running':repo.run['status']='running'
            if issue=='station':repo.run['es_sn']='ES01'
            if issue=='future':repo.run['as_of']='2026-09-10T22:15:00+08:00'
            if issue=='missing':repo.points.pop()
            with self.subTest(issue=issue),self.assertRaises(ValueError):query().latest_forecast(repo,'2026-09-09T22:16:00+08:00')

    def test_repository_selects_latest_completed_at_cutoff_without_requiring_whole_table(self):
        publisher=load('publish-pv-forecast.py');repo=publisher.ForecastRepository('test-credential-placeholder')
        self.assertTrue(callable(getattr(repo,'latest_run',None)),'latest query missing')
        seen=[]
        def request(*a,**kw):
            seen.append(kw['query']);return {'data':[{'id':1}],'meta':{'page':1,'count':20,'totalPage':20}}
        repo.request=request
        self.assertEqual(repo.latest_run('2026-09-09T22:16:00+08:00'),{'id':1})
        filters=json.loads(seen[0]['filter'])
        self.assertEqual(filters['status'],{'$eq':'completed'});self.assertEqual(filters['es_sn'],{'$eq':'ES02'})
        self.assertEqual(seen[0]['pageSize'],1);self.assertEqual(seen[0]['sort'],'-as_of,-id')


class QueryAPITests(unittest.TestCase):
    def app(self,builder):
        assert importlib.util.find_spec('m3.worker.pv_query_app') is not None,'query app missing'
        from m3.worker.pv_query_app import create_app
        return create_app(lambda:builder)

    def test_readonly_route_and_no_store(self):
        with TestClient(self.app(lambda:{'status':'empty','data':None})) as client:
            r=client.get('/api/pv/ES02/latest');self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertEqual(client.get('/api/pv/ES01/latest').status_code,404)
            self.assertEqual(client.post('/api/pv/ES02/latest').status_code,405)
            self.assertEqual(client.get('/api/pv/ES02/latest?debug=true').status_code,400)

    def test_errors_never_echo_sensitive_detail(self):
        def fail():raise ValueError('secret-value should not leak')
        with TestClient(self.app(fail)) as client:
            r=client.get('/api/pv/ES02/latest')
            self.assertEqual(r.status_code,502);self.assertNotIn('secret-value',r.text)


if __name__=='__main__':unittest.main()
