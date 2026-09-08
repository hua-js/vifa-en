"""Read-only UI projection of completed AI runs; no live model calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient
from m4_settings.api import create_app
from m4_selection.live_chain import run_chain
import test_m4_live_chain as fixture


def save_mock_transport(payload, output, answer):
    """External model boundary: persist the same artifacts as OllamaSelector."""
    (output / 'ai-answer.txt').write_text(answer)
    (output / 'ai-transport.json').write_text(json.dumps(dict(
        model=payload['model'], wall_seconds=0.01, completed=True, done_reason='stop')))
    return answer


class AiResultsTests(unittest.TestCase):
    def setUp(self):
        self.chain = fixture.LiveChainTests(); self.chain.setUp()
        self.addCleanup(self.chain.doCleanups)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        f = self.chain.fixture
        # An unknown keyword is avoided in RED so the missing endpoint is the failure.
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'M4_AI_RESULTS_DIR': str(self.root)}):
            app = create_app(f.path, control_reader=f.controls,
                input_service=SimpleNamespace(fetch=f.fetch), candidate_service=f.candidates,
                selection_service=f.service)
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def get(self, station='station-1'):
        return self.client.get(f'/m4-api/stations/{station}/ai-selection')

    def record(self, changing=False):
        def model(payload, output):
            answer = self.chain.model(payload, output)
            if changing:
                self.chain.fixture.client.devices[1]['latest_soc'] = 49.0
            return save_mock_transport(payload, output, answer)
        self.folder = self.root / 'example'
        return run_chain(self.chain.api, model, self.folder)

    def edit(self, name, change):
        file = self.folder / name
        value = json.loads(file.read_text()); change(value)
        file.write_text(json.dumps(value))

    def test_empty_and_station_isolation(self):
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get().json()['status'], 'empty')
        self.record()
        self.assertEqual(self.get('station-2').json()['status'], 'empty')
        self.assertEqual(self.get('unknown').status_code, 404)

    def test_blocked_live_check_keeps_ai_proposal_and_historical_plan(self):
        self.record(changing=True)
        response = self.get()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json(); record = data['record']
        self.assertEqual(data['usage'], 'historical_preview')
        self.assertEqual(data['dispatch_status'], 'not_dispatched')
        self.assertEqual(record['status'], 'blocked_post_ai_validation')
        self.assertEqual(record['ai_status'], 'accepted')
        self.assertEqual(record['proposal']['selected_candidate_id'], 'balanced')
        self.assertIsNone(record['selected'])
        self.assertEqual(len(record['candidates']), 3)
        self.assertEqual(len(record['candidates'][0]['plan']), 96)
        self.assertIn('peak_demand_exceed_kw', record['candidates'][0]['metrics'])
        self.assertNotIn('base_url', response.text)
        self.assertNotIn('raw_answer', response.text)
        self.assertEqual(len(self.chain.model_calls), 1)  # GET never infers again.

    def test_success_remains_historical_and_refresh_reads_new_record(self):
        self.record()
        self.assertEqual(self.get().json()['record']['status'], 'completed')
        self.assertEqual(self.get().json()['usage'], 'historical_preview')
        self.edit('report.json', lambda r: r.update(station_id='station-2'))
        self.assertEqual(self.get().status_code, 503)

    def test_corrupt_latest_report_does_not_fall_back_to_old_success(self):
        self.record()
        later = self.root / 'later'; later.mkdir()
        (later / 'report.json').write_text('{"station_id":')
        self.assertEqual(self.get().status_code, 503)

    def test_tampered_plan_or_ai_assessment_is_not_displayed_as_verified(self):
        self.record()
        original = (self.folder / 'candidates.json').read_text()
        self.edit('candidates.json', lambda r: r['result']['stations'][0]['optimization_result']['candidates'][0]['plan'][0].update(target_power_kw=999999.0))
        self.assertEqual(self.get().status_code, 503)
        (self.folder / 'candidates.json').write_text(original)
        self.edit('report.json', lambda r: r['ai']['proposal'].update(selected_candidate_id='cost'))
        self.assertEqual(self.get().status_code, 503)

    def test_model_config_digest_mismatch_rejected(self):
        self.record()
        self.edit('model-config.json', lambda r: r.update(model='different-model'))
        self.assertEqual(self.get().status_code, 503)

    def test_incomplete_transport_or_changed_raw_answer_cannot_claim_verified_ai(self):
        self.record()
        transport = (self.folder / 'ai-transport.json').read_text()
        for change in [dict(completed=False), dict(done_reason='length'), dict(wall_seconds=float('inf'))]:
            with self.subTest(change=change):
                self.edit('ai-transport.json', lambda r: r.update(change))
                self.assertEqual(self.get().status_code, 503)
                (self.folder / 'ai-transport.json').write_text(transport)
        (self.folder / 'ai-answer.txt').write_text('different answer')
        self.assertEqual(self.get().status_code, 503)

    def test_missing_transport_is_not_a_verified_model_response(self):
        self.record()
        (self.folder / 'ai-transport.json').unlink()
        self.assertEqual(self.get().status_code, 503)


if __name__ == '__main__': unittest.main()
