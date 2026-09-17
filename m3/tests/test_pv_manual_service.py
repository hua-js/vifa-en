import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from m3.tests.test_pv_forecast_tools import load


def manager_class():
    assert importlib.util.find_spec('m3.worker.services.pv_manual_jobs'), 'manual job service missing'
    from m3.worker.services.pv_manual_jobs import ManualJobs
    return ManualJobs


class ManualJobTests(unittest.TestCase):
    def test_one_job_at_a_time_and_completed_result_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            started = threading.Event(); release = threading.Event()
            def operation(kind, directory):
                started.set(); release.wait(3)
                return {'status': 'completed', 'operation': kind, 'verified_points': 96}
            jobs = manager_class()(Path(temp), operation)
            accepted, first = jobs.submit('forecast')
            self.assertTrue(accepted); self.assertTrue(started.wait(2))
            accepted, duplicate = jobs.submit('weather')
            self.assertFalse(accepted); self.assertEqual(first['job_id'], duplicate['job_id'])
            release.set(); jobs.close()
            jobs = manager_class()(Path(temp), operation)
            self.assertEqual(jobs.latest()['status'], 'completed')
            self.assertEqual(jobs.latest()['result']['verified_points'], 96)
            jobs.close()

    def test_failure_is_persisted_without_exception_details(self):
        with tempfile.TemporaryDirectory() as temp:
            def operation(*args): raise ValueError('private credential detail')
            jobs = manager_class()(Path(temp), operation)
            jobs.submit('forecast'); jobs.close()
            result = json.loads((Path(temp)/'latest.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('private credential', json.dumps(result))

    def test_restart_marks_interrupted_and_never_replays_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'latest.json').write_text(json.dumps({'job_id':'previous', 'status':'running', 'operation':'forecast'}))
            seen=[]; jobs=manager_class()(root, lambda *a: seen.append(a))
            self.assertEqual(jobs.latest()['status'], 'interrupted'); self.assertEqual(seen, [])
            with self.assertRaises(BlockingIOError): manager_class()(root, lambda *a: None)
            jobs.close()

    def test_invalid_operation_or_incomplete_result_cannot_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs=manager_class()(Path(temp), lambda *a: {'status':'running'})
            with self.assertRaises(ValueError): jobs.submit('shell')
            jobs.submit('weather'); jobs.close()
            self.assertEqual(jobs.latest()['status'], 'failed')


class ManualAPITests(unittest.TestCase):
    def test_auth_fixed_routes_and_empty_request_contract(self):
        assert importlib.util.find_spec('m3.worker.pv_service_app'), 'manual API missing'
        from m3.worker.pv_service_app import create_app
        with tempfile.TemporaryDirectory() as temp:
            app=create_app(lambda:lambda:{'status':'empty','data':None},
                lambda:manager_class()(Path(temp), lambda kind,directory:{'status':'completed','operation':kind}),
                lambda:'test-service-token-0123456789abcdef')
            headers={'Authorization':'Bearer test-service-token-0123456789abcdef'}
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/pv/ES02/runs').status_code,401)
                self.assertEqual(client.get('/api/pv/ES02/latest',headers=headers).status_code,200)
                self.assertEqual(client.get('/api/pv/ES01/latest',headers=headers).status_code,404)
                self.assertEqual(client.post('/api/pv/ES02/runs',headers=headers,json={'command':'x'}).status_code,400)
                self.assertEqual(client.post('/api/pv/ES02/weather?url=x',headers=headers).status_code,400)
                response=client.post('/api/pv/ES02/runs',headers=headers)
                self.assertEqual(response.status_code,202)
                self.assertEqual(response.json()['data']['operation'],'forecast')
                self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertEqual(client.get('/api/pv/ES02/job?debug=x',headers=headers).status_code,400)
                self.assertEqual(client.get('/health').json()['data']['service'],'pv-manual')


class ManualPipelineTests(unittest.TestCase):
    def test_weather_only_cannot_generate_or_publish_prediction(self):
        module=load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); snapshot=root/'weather'
            def fetch(path): path.mkdir(); return {}, b'{}'
            def imported(rows, summary, path):
                path.write_text(json.dumps({'status':'completed','source_batch_id':'batch'}))
            with patch.object(module.collector,'fetch',side_effect=fetch), \
                 patch.object(module.collector,'prepare_snapshot',return_value=([{'fetched_at':'2026-09-09T23:00:00+08:00'}],{'source_batch_id':'batch'})), \
                 patch.object(module.collector.history,'execute',side_effect=imported), \
                 patch.object(module.generator,'generate') as generate, \
                 patch.object(module.publisher,'execute') as publish:
                result=module.perform('weather',root,root/'training')
                self.assertEqual(result['status'],'completed')
                generate.assert_not_called(); publish.assert_not_called()

    def test_invalid_training_prevents_network_and_database_changes(self):
        module=load('run-pv-manual.py')
        with patch.object(module.refresh,'load_source',side_effect=ValueError('bad SHA')), \
             patch.object(module.collector,'fetch') as fetch:
            with self.assertRaises(ValueError): module.perform('forecast',Path('/unused'),Path('/invalid'))
            fetch.assert_not_called()

    def test_weather_import_failure_prevents_prediction_publication(self):
        module=load('run-pv-manual.py')
        with tempfile.TemporaryDirectory() as temp:
            def fetch(path): path.mkdir(); return {},b'{}'
            with patch.object(module,'refresh_training',return_value=Path(temp)/'training'), \
                 patch.object(module.collector,'fetch',side_effect=fetch), \
                 patch.object(module.collector,'prepare_snapshot',return_value=([{}],{'source_batch_id':'batch'})), \
                 patch.object(module.collector.history,'execute',side_effect=ValueError('unverified')), \
                 patch.object(module.publisher,'execute') as publish:
                with self.assertRaises(ValueError): module.perform('forecast',Path(temp),Path('/unused'))
                publish.assert_not_called()


if __name__=='__main__': unittest.main()
