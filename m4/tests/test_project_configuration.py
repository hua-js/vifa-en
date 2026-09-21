"""Offline process-isolated configuration acceptance tests (no services)."""
import os
import sys
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ProjectIntegrationTests(unittest.TestCase):
    def run_example(self, code):
        env = dict(os.environ, VIFA_PROJECT_CONFIG=str(ROOT/'config/projects/example.json'))
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_third_station_policy_and_archive_identity(self):
        self.run_example('''
from shared.project import get_project
from m4.settings.models import StationConfiguration
from m4.settings.roster import STATION_CABINETS
from m4.settings.forecast_source import STATIONS
from m4.settings.objectives import get_daily_profiles
from m4.settings.daily_policy import daily_policy_version
from m4.settings.runtime_config import runtime_parameters
from m4.selection.decision_chain import validate_identity
from uuid import uuid4
assert len(STATION_CABINETS) == 3
assert STATIONS['warehouse'] == 'WH09'
assert StationConfiguration(station_id='warehouse').station_id == 'warehouse'
assert runtime_parameters('warehouse')['grid_import_limit_kw'] == 800.0
assert get_daily_profiles('warehouse')[0].objective_order[0].terms == {'energy_cost': 1.0}
assert daily_policy_version('solar-yard') == 'm4-daily-operating-floor-v1'
validate_identity('warehouse', str(uuid4()))
for invalid in ['station-1', '../warehouse', 'WH09']:
    try: StationConfiguration(station_id=invalid)
    except ValueError: pass
    else: raise AssertionError('unknown station accepted')
''')

    def test_persistence_rejects_cross_project_and_legacy_adoption(self):
        self.run_example('''
import json, sqlite3, tempfile
from pathlib import Path
from m4.settings.project_storage import bind_database, bind_root
from m4.settings.store import SettingsStore
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    db = root/'settings.db'
    old = sqlite3.connect(db)
    old.execute('CREATE TABLE m4_station_settings (station_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
    old.execute('INSERT INTO m4_station_settings VALUES (?,?)', ('warehouse', '{}'))
    old.commit(); old.close()
    try: SettingsStore(db).get('warehouse')
    except ValueError: pass
    else: raise AssertionError('adopted legacy data')
    db2 = root/'new.db'
    assert SettingsStore(db2).get('warehouse').station_id == 'warehouse'
    con = sqlite3.connect(db2)
    con.execute("UPDATE m4_project_identity SET project_id='other'")
    con.commit(); con.close()
    try: SettingsStore(db2).get('warehouse')
    except ValueError: pass
    else: raise AssertionError('cross project settings read')
    archive = root/'archive'; archive.mkdir()
    (archive/'warehouse.json').write_text('{}')
    try: bind_root(archive)
    except ValueError: pass
    else: raise AssertionError('adopted old archive')
    clean = root/'clean'; bind_root(clean)
    (clean/'.project.json').write_text(json.dumps({'schema_version': 1, 'project_id': 'other'}))
    try: bind_root(clean)
    except ValueError: pass
    else: raise AssertionError('cross project history read')
''')

    def test_source_mapping_and_route_safety(self):
        self.run_example('''
from datetime import datetime
from unittest.mock import Mock, patch
from m4.settings.m3_current_result import M3CurrentResult
from m4.settings.pv_forecast_source import load_pv_forecast, ForecastRefreshRequired
from m4.settings.control_sources import ControlSourceReader, ControlSourceError
from m4.settings.upstream import NocoBaseClient, SourceReadError
assert M3CurrentResult._route('/v1/stations/WH09/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1').startswith('warehouse/')
for path in ['/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1',
             '/v1/stations/%2fetc/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1',
             'https://other.invalid/', '/v1/stations/WH09/../../secrets']:
    with patch('m4.settings.m3_current_result.build_opener') as network:
        try: M3CurrentResult().get(path)
        except ValueError: pass
        else: raise AssertionError('unsafe path accepted')
        network.assert_not_called()
client = Mock(); client.list_rows.return_value = []
try: load_pv_forecast(client, station_id='solar-yard', plan_start_at=datetime.fromisoformat('2026-09-16T00:00:00+08:00'), now=datetime.fromisoformat('2026-09-16T08:00:00+08:00'))
except ForecastRefreshRequired: pass
assert client.list_rows.call_args.kwargs['filters']['es_sn'] == {'$eq':'PV03'}
try: ControlSourceReader('test-token', base_url='https://other.invalid/api')
except ControlSourceError: pass
else: raise AssertionError('untrusted origin accepted')
response = Mock(status=200)
response.geturl.return_value='https://other.invalid/api/t_emu:list'
response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
reader=NocoBaseClient('test-token'); reader._opener=Mock(); reader._opener.open.return_value=response
try: reader.list_rows('t_emu', fields='emu_sn')
except SourceReadError: pass
else: raise AssertionError('cross origin response accepted')
''')

    def test_third_station_optimizer_and_metadata_api(self):
        self.run_example('''
import os, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from m4.settings.api import create_app
from m4.tests.m4_optimizer_test_support import make_request
from m4.settings.objectives import get_daily_profiles
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.service import M4Optimizer
from m4.settings.daily_policy import physical_grid_policy
from shared.project import get_project
request = make_request(station_id='warehouse', load_kw=0.0)
request.constraints.grid_import_limit_kw=800.0
request.source_versions.update(economic_policy='station-1-cost-first-v1', physical_grid_policy=physical_grid_policy('warehouse'), project_configuration=get_project().fingerprint)
request.profiles=get_daily_profiles('warehouse')
for point in request.points: point.pv_forecast_kw=0.0
request=OptimizationRequest.model_validate(request.model_dump())
result=M4Optimizer(model_version='project-offline-test').optimize(request)
assert len(result.candidates)==3
assert all(c.status in ('optimal','feasible') for c in result.candidates)
with tempfile.TemporaryDirectory() as tmp:
    os.environ['M4_DECISION_RESULTS_DIR']=str(Path(tmp)/'solver-decisions')
    os.environ['M4_AUTO_PLAN_ENABLED']='0'
    client=TestClient(create_app(settings_path=Path(tmp)/'settings.db'))
    metadata=client.get('/m4-api/project')
    assert metadata.status_code==200 and metadata.json()==get_project().public_metadata()
    assert client.get('/m4-api/stations/warehouse/settings').status_code==200
    assert client.get('/m4-api/stations/station-1/settings').status_code==404
''')

    def test_configuration_fingerprint_invalidates_current_plan(self):
        import copy
        from unittest.mock import patch
        from shared.project import get_project
        from m4.settings.daily_policy import matches_current_daily_policy
        from m4.settings.runtime_config import effective_configuration
        from m4.tests.test_m4_settings import parameters
        from m4.settings.models import StationConfiguration
        # Same station IDs, different inventory/configuration digest.
        original = get_project()
        changed = copy.copy(original)
        object.__setattr__(changed, 'fingerprint', 'different-project-configuration')
        configuration = StationConfiguration(station_id='station-1', version='settings-v1', parameters=parameters())
        first = effective_configuration(configuration)
        with patch('m4.settings.runtime_config.get_project', return_value=changed):
            self.assertNotEqual(first.version, effective_configuration(configuration).version)
        self.assertFalse(matches_current_daily_policy('station-1', {'source_versions': {'project_configuration': 'old'}}))

    def test_valid_m3_socket_and_gateway_transports_remain_read_only(self):
        import json
        from unittest.mock import Mock, patch
        from m4.settings.m3_current_result import M3CurrentResult
        path = '/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1'
        response = Mock(status=200)
        response.read.return_value = b'{"ok":true}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.dict(os.environ, {'M4_M3_TRANSPORT': 'socket'}), patch(
                'm4.settings.m3_current_result.Path.read_text', return_value='offline-test-token'), patch(
                'm4.settings.m3_current_result.SocketConnection', return_value=connection):
            self.assertEqual(M3CurrentResult().get(path), {'ok': True})
            self.assertEqual(connection.request.call_args.args, ('GET', path))
            connection.close.assert_called_once()
        response.read.return_value = json.dumps({'status': 'ok', 'data': {'run_id': 'offline'}}).encode()
        response.geturl.return_value = 'https://opdash.lvkpower.com/energy-forecast-api/custom-runs/station_2/latest?interval_seconds=900&forecast_days=1'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'M4_M3_TRANSPORT': 'platform_gateway'}), patch(
                'm4.settings.m3_current_result.build_opener', return_value=opener):
            self.assertEqual(M3CurrentResult(gateway_token='offline-test-token').get(path), {'run_id': 'offline'})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, response.geturl.return_value)

    def test_legacy_default_database_is_preserved(self):
        import json
        import sqlite3
        import tempfile
        from m4.settings.store import SettingsStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'legacy.db'
            document = json.dumps({'station_id': 'station-1'})
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE m4_station_settings (station_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
                db.execute('INSERT INTO m4_station_settings VALUES (?, ?)', ('station-1', document))
            self.assertEqual(SettingsStore(path).get('station-1').station_id, 'station-1')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT document FROM m4_station_settings').fetchone()[0], document)
                self.assertEqual(db.execute('SELECT project_id FROM m4_project_identity').fetchone()[0], 'vifa')

    def test_metadata_contract(self):
        from shared.project import get_project
        data = get_project().public_metadata()
        self.assertEqual(data['configuration_version'], get_project().fingerprint)
        self.assertEqual(data['stations'][0]['policy'], 'cost_first')
        self.assertEqual(data['stations'][0]['grid_import_limit_kw'], 550.0)


if __name__ == '__main__':
    unittest.main()
