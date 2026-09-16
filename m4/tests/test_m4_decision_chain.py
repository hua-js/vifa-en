"""Compatibility component tests use archived snapshots, not live M3 acceptance."""
import json
import os
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from m4.selection.decision_chain import run_chain, LocalApi
from m4.settings.api import create_app
from m4.settings.decision_results import DecisionResultsReader
from m4.settings.selection import PolicyPreferences
from m4.tests import test_m4_live_selection as live_fixture


class Api:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        response = self.client.request(method, path, json=data)
        response.raise_for_status()
        return response.json()


class DecisionChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = live_fixture.LiveSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.policies.save('station-1', PolicyPreferences(
            metric='profile_priority', metric_tolerance=0.0,
            demand_peak_tolerance_kw=0.0, demand_energy_tolerance_kwh=0.0,
            tie_order=['balanced', 'cost', 'pv']), expected_revision=0)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_id = str(uuid4())
        self.output = self.root / 'station-1' / self.run_id
        with patch.dict(os.environ, {'M4_DECISION_RESULTS_DIR': str(self.root),
                                    'M4_AI_CONFIG': '/missing/disabled/ai-config.json'}):
            app = create_app(fixture.path, control_reader=fixture.controls,
                input_service=SimpleNamespace(fetch=fixture.fetch),
                candidate_service=fixture.candidates, selection_service=fixture.service)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.api = Api(self.client)

    def run_preview(self, **kwargs):
        return run_chain(self.api, self.output, station_id='station-1',
                         run_id=self.run_id, **kwargs)

    def test_full_solver_chain_and_read_only_historical_result(self):
        report = self.run_preview()
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(report['selected']['profile_id'], 'balanced')
        self.assertEqual(report['dispatch_status'], 'not_dispatched')
        self.assertEqual(report['selector_version'], 'pyomo-highs-selection-v2')
        self.assertNotIn('ai', report)
        self.assertFalse((self.output / 'model-config.json').exists())
        before = list(self.api.calls)
        view = self.client.get('/m4-api/stations/station-1/decision-result')
        self.assertEqual(view.status_code, 200, view.text)
        record = view.json()['record']
        self.assertEqual(view.json()['usage'], 'historical_preview_only')
        self.assertEqual(record['selected'], report['selected'])
        self.assertEqual(len(record['comparison']), 3)
        self.assertEqual(len(record['candidates']), 3)
        self.assertTrue(all(len(item['plan']) == 96 for item in record['candidates']))
        self.assertEqual(record['peak_preparation']['plan_version'],report['selected']['plan_version'])
        self.assertEqual(record['peak_preparation']['basis'],'selected_plan_forecast')
        self.assertIn('max_grid_import_kw',record['candidates'][0]['metrics'])
        self.assertEqual(before, self.api.calls)
        self.assertEqual(self.client.get('/m4-api/stations/station-2/decision-result').json()['status'], 'empty')


    def test_missing_preferences_and_forecast_block_before_solving(self):
        self.fixture.policies.save('station-1', None, expected_revision=1)
        report = self.run_preview()
        self.assertEqual(report['status'], 'blocked_configuration')
        self.assertFalse(any(method == 'POST' for method, _ in self.api.calls))
        self.assertIsNone(report['selected'])
        self.assertIsNone(DecisionResultsReader(self.root).latest('station-1')['record']['peak_preparation'])
        self.assertEqual(DecisionResultsReader(self.root).latest('station-1')['record']['status'], 'blocked_configuration')

    def test_missing_input_and_changed_state_during_decision_cannot_select(self):
        self.fixture.client.forecasts.pop()
        report = self.run_preview()
        self.assertEqual(report['status'], 'blocked_inputs')
        self.assertFalse(any(method == 'POST' for method, _ in self.api.calls))

    def test_state_change_after_solver_is_blocked_and_keeps_comparison_evidence(self):
        original = self.api.request
        def changed(method, path, data=None):
            result = original(method, path, data)
            if path.endswith('/candidates'):
                self.fixture.client.devices[0]['latest_soc'] = 49.0
            return result
        self.api.request = changed
        report = self.run_preview()
        self.assertEqual(report['status'], 'blocked_selection')
        self.assertIsNone(report['selected'])
        record = DecisionResultsReader(self.root).latest('station-1')['record']
        self.assertIsNone(record['selected'])
        self.assertEqual(len(record['candidates']), 3)

    def test_corrupt_latest_and_tampered_evidence_never_fall_back(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'candidates.json'
        value = json.loads(path.read_text())
        value['result']['stations'][0]['optimization_result']['candidates'][0]['plan'][0]['target_power_kw'] = 999999.0
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            DecisionResultsReader(self.root).latest('station-1')
        self.assertEqual(self.client.get('/m4-api/stations/station-1/decision-result').status_code, 503)

    def test_completed_report_must_match_selected_plan_and_station(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'report.json'
        value = json.loads(path.read_text())
        value['selected']['profile_id'] = 'cost'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            DecisionResultsReader(self.root).latest('station-1')

    def test_saved_configuration_is_independently_bound_even_if_file_digest_is_updated(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'configuration.json'
        saved = json.loads(path.read_text())
        saved['settings']['parameters']['energy_capacity_kwh'] *= 2
        path.write_text(json.dumps(saved))
        report_path = self.output / 'report.json'
        report = json.loads(report_path.read_text())
        report['evidence_sha256'][path.name] = sha256(path.read_bytes()).hexdigest()
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            DecisionResultsReader(self.root).latest('station-1')

    def test_optimizer_exception_records_blocked_run_without_candidate_plan(self):
        with patch.object(self.fixture.candidates.orchestrator.optimizer, 'optimize', side_effect=RuntimeError('failed')):
            report = self.run_preview()
        self.assertEqual(report['status'], 'blocked_model_solver')
        record = DecisionResultsReader(self.root).latest('station-1')['record']
        self.assertEqual(record['status'], 'blocked_model_solver')
        self.assertIsNone(record['selected'])
        self.assertEqual(record['candidates'], [])

    def test_extended_original_expiry_is_not_accepted_even_if_hash_is_updated(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'candidates.json'
        value = json.loads(path.read_text())
        from m4.selection.decision_chain import timestamp
        value['expires_at'] = (timestamp(value['expires_at']) + timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(value))
        report_path = self.output / 'report.json'
        report = json.loads(report_path.read_text())
        report['evidence_sha256'][path.name] = sha256(path.read_bytes()).hexdigest()
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            DecisionResultsReader(self.root).latest('station-1')

    def test_custom_cli_output_directory_preserves_report_identity(self):
        self.output = self.root / 'station-1' / 'reviewable-output'
        self.assertEqual(self.run_preview()['status'], 'completed')
        record = DecisionResultsReader(self.root).latest('station-1')['record']
        self.assertEqual(record['run_id'], self.run_id)

    def test_completed_report_requires_valid_precheck_inputs_even_with_matching_digest(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'inputs.json'
        value = json.loads(path.read_text())
        value.update(status='blocked', issues=['forecast unavailable'])
        path.write_text(json.dumps(value))
        report_path = self.output / 'report.json'
        report = json.loads(report_path.read_text())
        report['evidence_sha256'][path.name] = sha256(path.read_bytes()).hexdigest()
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            DecisionResultsReader(self.root).latest('station-1')

    def test_completed_report_cannot_contain_blocked_stages_or_issues(self):
        self.assertEqual(self.run_preview()['status'], 'completed')
        path = self.output / 'report.json'
        original = path.read_text()
        for change in (lambda report: report['stages'][-1].update(status='blocked'),
                       lambda report: report.update(issues=['failed'])):
            with self.subTest(change=change):
                report = json.loads(original)
                change(report)
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    DecisionResultsReader(self.root).latest('station-1')

    def test_background_api_completes_without_model_settings_and_second_station_is_blocked(self):
        threads = []
        def thread_factory(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread
        with patch('m4.settings.decision_runs.Thread', side_effect=thread_factory):
            try:
                for station in ('station-1', 'station-2'):
                    endpoint = f'/m4-api/stations/{station}/decision-runs'
                    run_id = str(uuid4())
                    response = self.client.post(endpoint, json={'request_id': run_id})
                    self.assertEqual(response.status_code, 202, response.text)
                    threads[-1].join(15)
                    self.assertFalse(threads[-1].is_alive())
                    job = self.client.get(endpoint).json()['job']
                    self.assertEqual(job['status'], 'finished')
                    self.assertEqual(job['stage'], 'completed' if station == 'station-1' else 'blocked_configuration')
                    retry = self.client.post(endpoint, json={'request_id': run_id})
                    self.assertEqual(retry.status_code, 202)
                    self.assertEqual(retry.json()['job']['run_id'], run_id)
            finally:
                for thread in threads:
                    thread.join(15)

    def test_removed_model_routes_are_absent_and_api_is_same_origin(self):
        for station in ('station-1', 'station-2'):
            for route in ('ai-runs', 'ai-selection'):
                endpoint = f'/m4-api/stations/{station}/{route}'
                self.assertEqual(self.client.get(endpoint).status_code, 404)
                self.assertEqual(self.client.post(endpoint, json={'request_id': str(uuid4())}).status_code, 404)
        self.assertFalse(any('/ai-' in path for path in self.client.get('/openapi.json').json()['paths']))
        endpoint = '/m4-api/stations/station-1/decision-runs'
        self.assertEqual(self.client.post(endpoint, json={'request_id': str(uuid4())},
            headers={'origin': 'https://foreign.test'}).status_code, 403)
        self.assertEqual(self.client.get('/m4-api/stations/unknown/decision-runs').status_code, 404)
        self.assertEqual(self.client.post(endpoint, json={'request_id': '../escape'}).status_code, 422)

    def test_chain_import_and_help_do_not_import_ai_or_contact_services(self):
        script = ('import sys; import m4.selection.decision_chain; '
            'assert not any(name in sys.modules for name in '
            '["m4.selection.ai_config", "m4.selection.ollama", "m4.selection.live_chain", "m4.selection.openai_chat"])')
        root = Path(__file__).resolve().parents[2]
        output = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True, text=True)
        self.assertEqual(output.returncode, 0, output.stderr)
        output = subprocess.run([sys.executable, '-m', 'm4.selection.decision_chain', '--help'],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn('--station', output.stdout)
        self.assertNotIn('--model', output.stdout)
        for url in ('https://127.0.0.1:8844', 'http://example.com', 'http://127.0.0.1:8844/m4'):
            with self.assertRaises(ValueError): LocalApi(url)


if __name__ == '__main__':
    unittest.main()
