"""Offline release contract; no Docker daemon, registry or production calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ProductReleaseTests(unittest.TestCase):
    def test_arm64_package_contains_project_runtime_and_matching_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'release'
            result = subprocess.run([
                sys.executable, str(ROOT / 'm4/deploy/build_package.py'),
                '--output', str(target), '--platform', 'linux/arm64', '--include-m1',
                '--revision', 'a' * 40,
                '--image', 'registry.example.test/vifa/m4:revision-arm64',
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((target / 'release.json').read_text())
            self.assertEqual(manifest['platform'], 'linux/arm64')
            self.assertEqual(manifest['schema_version'], 1)
            self.assertEqual(manifest['api_contract'], 'm4-project-v1')
            self.assertEqual(manifest['modules'], ['m1', 'm4'])
            self.assertTrue((target / 'backend/app/m1/dashboard_energy_api.py').is_file())
            self.assertEqual((target / 'node_red/m1_dashboard_template.html').read_bytes(),
                             (ROOT / 'm1/web/dashboard_energy.html').read_bytes())
            self.assertTrue((target / 'backend/app/shared/project.py').is_file())
            self.assertTrue((target / 'backend/app/config/projects/vifa.json').is_file())
            self.assertTrue((target / 'config/project.json').is_file())
            self.assertIn('M4_PLATFORM=linux/arm64', (target / 'backend/.env.example').read_text())
            html = (target / 'node_red/m4_customer_template.html').read_text()
            flow = json.loads((target / 'node_red/m4_customer_flow.json').read_text())
            self.assertEqual(next(n['template'] for n in flow if n['id'] == 'm4-page-template'), html)
            self.assertTrue(any(n.get('url') == '/m4-api/project' for n in flow))
            self.assertEqual(manifest['html_sha256'], hashlib.sha256(html.encode()).hexdigest())
            for line in (target / 'SHA256SUMS').read_text().splitlines():
                digest, name = line.split('  ', 1)
                self.assertEqual(hashlib.sha256((target / name).read_bytes()).hexdigest(), digest)
            self.assertFalse(any(p.name.endswith('.sqlite3') for p in target.rglob('*')))
            # Exercise only packaged sources from outside the repository. No
            # business reads, credentials, scheduler or sockets are started.
            code = '''
from pathlib import Path
from fastapi.testclient import TestClient
from shared.project import get_project
from m1 import dashboard_energy_api
from m4.settings.api import create_app
project = get_project()
assert project.id == 'example'
assert len(dashboard_energy_api.TARGET_STATIONS) == 3
client = TestClient(create_app(settings_path=Path('isolated.sqlite3')))
assert client.get('/m4-api/project').json() == project.public_metadata()
for station in project.stations:
    reply = client.get('/m4-api/stations/' + station.id + '/settings')
    assert reply.status_code == 200, reply.text
    assert reply.json()['station_id'] == station.id
assert client.get('/m4-api/stations/not-configured/settings').status_code == 404
'''
            environment = {**os.environ, 'PYTHONPATH': str(target / 'backend/app'),
                           'VIFA_PROJECT_CONFIG': str(target / 'config/example.json'),
                           'M4_NOCOBASE_TOKEN': '', 'M4_AUTO_PLAN_ENABLED': '0',
                           'M4_DECISION_RESULTS_DIR': str(Path(temp) / 'history')}
            check = subprocess.run([sys.executable, '-c', code], cwd=temp,
                                   env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_unsupported_platform_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'release'
            result = subprocess.run([sys.executable, str(ROOT / 'm4/deploy/build_package.py'),
                '--output', str(target), '--platform', 'linux/arm/v7'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
