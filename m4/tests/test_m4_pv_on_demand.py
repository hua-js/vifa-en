import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from m4.settings.pv_on_demand import PreparedInputs, PVService, PVPreparationError


def inputs(refresh=True, ready=True):
    sources = {key: {'status': 'ready' if ready else 'error'} for key in ('load', 'tariff', 'controls', 'realtime')}
    sources['pv'] = {'status': 'error' if refresh else 'ready', 'refresh_required': refresh, 'run_id': 'new-run'}
    return {'sources': sources}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(station_id='station-2')
        self.service = Mock()
        self.service.generate.return_value = ('job', 'new-run')

    def test_valid_batch_is_reused(self):
        fetch = Mock(return_value=inputs(False))
        PreparedInputs(fetch, self.service)(self.config)
        self.service.generate.assert_not_called()

    def test_missing_batch_is_generated_once_then_identity_checked(self):
        fetch = Mock(side_effect=[inputs(), inputs(), inputs(False)])
        result = PreparedInputs(fetch, self.service)(self.config)
        self.assertEqual(result['pv_preparation']['run_id'], 'new-run')
        self.service.generate.assert_called_once()

    def test_bad_other_sources_do_not_trigger_generation(self):
        PreparedInputs(Mock(return_value=inputs(ready=False)), self.service)(self.config)
        self.service.generate.assert_not_called()

    def test_other_source_becomes_invalid_during_recheck(self):
        fetch = Mock(side_effect=[inputs(), inputs(ready=False)])
        result = PreparedInputs(fetch, self.service)(self.config)
        self.service.generate.assert_not_called()
        self.assertEqual(result['sources']['load']['status'], 'error')

    def test_wrong_batch_stops(self):
        other = inputs(False);other['sources']['pv']['run_id'] = 'other-run'
        with self.assertRaises(PVPreparationError):
            PreparedInputs(Mock(side_effect=[inputs(), inputs(), other]), self.service)(self.config)

    def test_failure_does_not_retry_and_releases_lock(self):
        self.service.generate.side_effect = PVPreparationError('failed')
        prepare = PreparedInputs(Mock(return_value=inputs()), self.service)
        with self.assertRaises(PVPreparationError):prepare(self.config)
        self.service.generate.assert_called_once()
        self.assertTrue(prepare.lock.acquire(False));prepare.lock.release()

    def test_station_one_never_triggers(self):
        PreparedInputs(Mock(return_value=inputs()), self.service)(SimpleNamespace(station_id='station-1'))
        self.service.generate.assert_not_called()


class ServiceTests(unittest.TestCase):
    def job(self, status, identity='a'):
        return {'job_id': identity, 'status': status, 'result': {'status': 'completed', 'es_sn': 'ES02', 'verified_points': 96, 'run_id': 'new'}}

    @patch('m4.settings.pv_on_demand.time.sleep')
    def test_poll_same_job_without_reposting(self, _):
        service = PVService('test-token')
        service.request = Mock(side_effect=[self.job('queued'), self.job('running'), self.job('completed')])
        self.assertEqual(service.generate(Mock()), ('a', 'new'))
        self.assertEqual([call.args[0] for call in service.request.call_args_list], ['POST','GET','GET'])

    @patch('m4.settings.pv_on_demand.time.sleep')
    def test_replaced_job_stops(self, _):
        service = PVService('test-token');service.request = Mock(side_effect=[self.job('queued'),self.job('completed','b')])
        with self.assertRaises(PVPreparationError):service.generate(Mock())

    def test_failed_job_stops(self):
        service = PVService('test-token');service.request = Mock(return_value=self.job('failed'))
        with self.assertRaises(PVPreparationError):service.generate(Mock())
        service.request.assert_called_once_with('POST')

    @patch('m4.settings.pv_on_demand.time.monotonic', side_effect=[0,601])
    def test_timeout_stops_without_reposting(self, _):
        service = PVService('test-token');service.request = Mock(return_value=self.job('running'))
        with self.assertRaises(PVPreparationError):service.generate(Mock())
        service.request.assert_called_once_with('POST')


if __name__ == '__main__': unittest.main()


class CandidateJobApiTests(unittest.TestCase):
    def test_background_job_is_idempotent_and_station_isolated(self):
        import tempfile
        import threading
        import time
        from pathlib import Path
        from fastapi.testclient import TestClient
        from m4.settings.api import create_app
        gate = threading.Event()
        started = threading.Event()
        calls = []
        class Candidates:
            def calculate(self, station, version, progress):
                calls.append(station)
                progress('pv_refresh', '模拟光伏更新')
                started.set()
                gate.wait(3)
                return {'usage': 'preview_only', 'station_id': station}
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory)/'settings.sqlite3',
                control_reader=Mock(), input_service=Mock(), candidate_service=Candidates())
            with TestClient(app) as client:
                path = '/m4-api/stations/station-2/candidate-jobs'
                identity = '11111111-1111-4111-8111-111111111111'
                body = {'request_id': identity, 'configuration_version': 'v1'}
                try:
                    response = client.post(path, json=body)
                    self.assertEqual(response.status_code, 202)
                    self.assertTrue(started.wait(1))
                    self.assertEqual(client.post(path, json=body).status_code, 202)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(client.get(path, params={'request_id': identity}).json()['stage'], 'pv_refresh')
                    self.assertEqual(client.get(path.replace('station-2','station-1'), params={'request_id': identity}).status_code, 404)
                finally:
                    gate.set()
                for _ in range(100):
                    job = client.get(path, params={'request_id': identity}).json()
                    if job['status'] != 'running': break
                    time.sleep(.01)
                self.assertEqual(job['status'], 'completed')
                self.assertEqual(job['result']['usage'], 'preview_only')
