import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from m3.tests.test_pv_publication import bundle

SCRIPTS = Path(__file__).resolve().parents[2]/'m3/scripts'


def load(filename):
    path = SCRIPTS/filename
    assert path.exists(), filename+' missing'
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ForecastToolTests(unittest.TestCase):
    def test_new_training_source_mode_requires_its_manifest_before_generation(self):
        module=load('generate-pv-forecast.py')
        self.assertTrue(callable(getattr(module,'load_inputs',None)), 'new training input loader missing')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            (path/'manifest.json').write_text(json.dumps({'format':'wrong','es_sn':'ES02'}))
            with self.assertRaises(ValueError): module.load_inputs(None,path)

    def test_partial_api_page_and_duplicate_run_are_rejected(self):
        module = load('publish-pv-forecast.py')
        client = module.ForecastRepository('test-credential-placeholder')
        client.request = lambda *a, **kw: {'data': [{'id': 1}], 'meta': {'count': 2}}
        with self.assertRaises(ValueError): client.read_points(1)
        client.request = lambda *a, **kw: {'data': [{'id': 1}, {'id': 2}], 'meta': {'count': 2}}
        with self.assertRaises(ValueError): client.read_run('r')

    def test_transport_denies_unrelated_resources_and_actions(self):
        client = load('publish-pv-forecast.py').ForecastRepository('test-credential-placeholder')
        for table, action in [('t_es_data', 'create'), ('energy_pv_forecast_points', 'destroy'),
                              ('energy_pv_forecast_points', 'update')]:
            with self.assertRaises(ValueError): client.request(table, action)

    def test_bundle_tampering_is_rejected_before_any_api_read(self):
        module = load('publish-pv-forecast.py')
        run, points = bundle()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path/'run.json').write_text(json.dumps(run))
            points[0]['forecast_kw'] = '123.000000'
            (path/'points.json').write_text(json.dumps(points))
            with self.assertRaises(ValueError): module.load_bundle(path)

    def test_source_artifact_hash_mismatch_is_rejected(self):
        module = load('generate-pv-forecast.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'inputs').mkdir()
            (root/'inputs/pv_snapshot.jsonl').write_text('tampered')
            (root/'manifest.json').write_text(json.dumps({'files_sha256': {'inputs/pv_snapshot.jsonl': '0'*64}}))
            with self.assertRaises(ValueError): module.checked_file(root, 'inputs/pv_snapshot.jsonl')


if __name__ == '__main__':
    unittest.main()
