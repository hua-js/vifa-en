"""Candidate HTTP boundary: server-owned inputs, freshness and station isolation."""
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from m4_optimizer import M4Optimizer
from m4_settings.api import create_app
from m4_settings.live_inputs import LiveInputService
from m4_settings.store import SettingsStore
from test_m4_live_inputs import Client, Controls, NOW, START
from test_m4_realtime import configuration


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.db'
        self.store = SettingsStore(self.path)
        self.config = self.store.save('station-1', configuration().parameters, expected_revision=0)
        self.client = Client()
        self.controls = Controls()
        self.time = NOW
        self.fetch_calls = []
        self.solve_calls = []

    def fetch(self, config):
        self.fetch_calls.append(config)
        return LiveInputService(self.client, self.controls).fetch(config, now=NOW)

    def service(self, optimizer=None, fetch=None):
        from m4_settings.candidates import CandidateService
        outer = self
        class Recorder:
            def optimize(self, request):
                outer.solve_calls.append(request)
                return (optimizer or M4Optimizer(model_version='test-math')).optimize(request)
        return CandidateService(store=self.store, fetch_inputs=fetch or self.fetch,
            read_controls=self.controls.fetch, optimizer=Recorder(), clock=lambda: self.time)

    def run_once(self, service):
        return service.calculate('station-1', self.config.version)

    def assert_failure(self, service, status, word):
        from m4_settings.candidates import CandidateError
        with self.assertRaises(CandidateError) as caught:
            self.run_once(service)
        self.assertEqual(caught.exception.status_code, status)
        self.assertIn(word, caught.exception.detail['message'])
        self.assertIsNone(service.latest('station-1'))

    def test_real_solver_uses_partial_capacity_and_returns_preview_only_snapshot(self):
        self.client.devices[0]['emu_status'] = 'fault'
        self.controls.result['demand']['need_kw'] = 140.0
        service = self.service()
        result = self.run_once(service)
        self.assertEqual(result['schema_version'], 'm4-live-candidates-v1')
        self.assertEqual(result['usage'], 'preview_only')
        self.assertFalse(result['stale'])
        self.assertEqual(result['expires_at'], START.isoformat())
        self.assertEqual(result['inputs']['sources']['realtime']['participating_cabinet_ids'], ['emu12'])
        request = self.solve_calls[0]
        self.assertEqual(request.capability.energy_capacity_kwh, 250)
        self.assertEqual(request.capability.max_charge_kw, 40)
        self.assertEqual(request.capability.max_discharge_kw, 45)
        station = result['result']['stations'][0]
        self.assertEqual(station['status'], 'optimized')
        self.assertEqual(station['selection_status'], 'pending_selection')
        self.assertIsNone(station['selected_candidate_id'])
        self.assertEqual(station['dispatch_status'], 'not_dispatched')
        self.assertIsNone(station['ems_task_id'])
        self.assertEqual(len(station['optimization_result']['candidates']), 3)
        for candidate in station['optimization_result']['candidates']:
            self.assertIn(candidate['status'], ('optimal', 'feasible'))
            self.assertEqual(len(candidate['plan']), 96)
            self.assertTrue(any(p['target_power_kw'] > 1 for p in candidate['plan']))
            for point in candidate['plan']:
                self.assertLessEqual(point['target_power_kw'], 40.0001 if point['mode']=='charge' else 45.0001)
                self.assertGreaterEqual(point['expected_soc_pct'], 15-.0001)
                self.assertLessEqual(point['expected_soc_pct'], 85.0001)
        self.assertEqual(self.store.get('station-1'), self.config)
        result['request']['capability']['energy_capacity_kwh'] = 99999
        self.assertEqual(service.latest('station-1')['request']['capability']['energy_capacity_kwh'], 250)
        self.assertIsNone(service.latest('station-2'))

    def test_unconfigured_and_outdated_client_versions_do_not_read_or_solve(self):
        from m4_settings.candidates import CandidateError
        service = self.service()
        for station_id, version, expected in [('station-2', 'old',422),('station-1','old',409)]:
            with self.assertRaises(CandidateError) as caught:
                service.calculate(station_id, version)
            self.assertEqual(caught.exception.status_code, expected)
        self.assertFalse(self.fetch_calls)
        self.assertFalse(self.solve_calls)

    def test_incomplete_forecast_carries_source_issues_without_calling_optimizer(self):
        self.client.forecasts.pop()
        service = self.service()
        from m4_settings.candidates import CandidateError
        with self.assertRaises(CandidateError) as caught:
            self.run_once(service)
        self.assertEqual(caught.exception.status_code,422)
        self.assertEqual(caught.exception.detail['inputs']['status'],'blocked')
        self.assertTrue(caught.exception.detail['issues'])
        self.assertFalse(self.solve_calls)

    def test_configuration_changes_during_fetch_prevent_solving(self):
        def fetch(config):
            bundle = self.fetch(config)
            self.store.save('station-1',configuration(max_charge_kw=70).parameters,expected_revision=1)
            return bundle
        self.assert_failure(self.service(fetch=fetch),409,'参数')
        self.assertFalse(self.solve_calls)

    def test_upstream_errors_are_sanitized_and_lock_is_released(self):
        def fetch(config): raise RuntimeError('Bearer secret-internal-url')
        service=self.service(fetch=fetch)
        for _ in range(2): self.assert_failure(service,502,'读取')

    def test_settings_control_or_clock_change_after_solving_rejects_result(self):
        for mutation, word in [('settings','参数'),('controls','控制'),('clock','过期'),('control_error','控制')]:
            with self.subTest(mutation=mutation):
                outer = self
                class MutatingOptimizer:
                    def optimize(self, request):
                        if mutation=='settings':
                            outer.store.save('station-1',configuration(max_charge_kw=70).parameters,expected_revision=outer.config.revision)
                        elif mutation=='controls': outer.controls.result['version']='changed'
                        elif mutation=='control_error': outer.controls.failed=True
                        else: outer.time=START+timedelta(seconds=1)
                        return empty_result(request)
                service=self.service(optimizer=MutatingOptimizer())
                self.assert_failure(service,502 if mutation=='control_error' else 409,word)
                self.config=self.store.get('station-1')
                self.controls=Controls()
                self.time=NOW

    def test_no_usable_candidate_remains_empty_and_latest_reports_staleness(self):
        class EmptyOptimizer:
            def optimize(self, request): return empty_result(request)
        service=self.service(optimizer=EmptyOptimizer())
        result=self.run_once(service)
        station=result['result']['stations'][0]
        self.assertEqual(station['status'],'no_usable_candidate')
        self.assertEqual(result['result']['overall_status'],'failed')
        self.time=START+timedelta(seconds=1)
        self.assertTrue(service.latest('station-1')['stale'])
        self.assertIn('过期',service.latest('station-1')['stale_reason'])
        self.time=NOW
        self.store.save('station-1',configuration(max_charge_kw=70).parameters,expected_revision=1)
        self.assertTrue(service.latest('station-1')['stale'])
        self.assertIn('参数',service.latest('station-1')['stale_reason'])

    def test_failed_new_calculation_clears_previous_candidate(self):
        class EmptyOptimizer:
            def optimize(self, request):return empty_result(request)
        service=self.service(optimizer=EmptyOptimizer())
        self.run_once(service)
        self.client.failures.add('t_emu')
        self.assert_failure(service,422,'输入')

    def test_same_station_busy_does_not_block_other_station(self):
        from m4_settings.candidates import CandidateError
        entered,release=threading.Event(),threading.Event()
        class WaitingOptimizer:
            def optimize(self, request):
                entered.set()
                if not release.wait(5): raise TimeoutError('test release missing')
                return empty_result(request)
        service=self.service(optimizer=WaitingOptimizer())
        results=[]
        worker=threading.Thread(target=lambda:results.append(self.run_once(service)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(CandidateError) as caught:self.run_once(service)
            self.assertEqual(caught.exception.status_code,429)
            with self.assertRaises(CandidateError) as caught:service.calculate('station-2','missing')
            self.assertEqual(caught.exception.status_code,422)
            self.assertIsNone(service.latest('station-1'))
        finally:
            release.set();worker.join(6)
        self.assertEqual(len(results),1)

    def test_http_requires_only_configuration_version_and_same_origin(self):
        class EmptyOptimizer:
            def optimize(self, request):return empty_result(request)
        service=self.service(optimizer=EmptyOptimizer())
        with TestClient(create_app(self.path, candidate_service=service)) as client:
            endpoint='/m4-api/stations/station-1/candidates'
            self.assertIsNone(client.get(endpoint).json())
            body={'configuration_version':self.config.version}
            self.assertEqual(client.post(endpoint,json={**body,'points':[]}).status_code,422)
            self.assertEqual(client.post(endpoint,json=body,headers={'Origin':'https://evil.invalid'}).status_code,403)
            response=client.post(endpoint,json=body)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(client.get(endpoint).json()['request'],response.json()['request'])
            self.assertEqual(client.get('/m4-api/stations/unknown/candidates').status_code,404)
            self.assertEqual(len(self.solve_calls),1)


def empty_result(request):
    from m4_optimizer.contracts import CandidateResult, OptimizationResult
    return OptimizationResult(request_id=request.request_id,station_id=request.station_id,
        plan_start_at=request.plan_start_at,input_observed_at=request.input_observed_at,
        started_at=NOW,finished_at=NOW,model_version='test-math',solver_name='scipy-highs',solver_version='1',
        source_versions=request.source_versions,candidates=[CandidateResult(profile_id=p.profile_id,
            profile_version=p.profile_version,plan_version='test-'+p.profile_id,status='timeout',
            solver_message='time limit',solve_seconds=0.0,plan=[],metrics=None,layers=[],risk_codes=[],risk_messages=[])
            for p in request.profiles])


if __name__=='__main__':unittest.main()
