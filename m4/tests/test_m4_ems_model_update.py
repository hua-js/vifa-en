"""Offline-only transport cases. No production credentials or endpoints."""
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from m4.settings.ems_model_update import EMSModelUpdateAdapter, ModelUpdateError
from shared.project import get_project


class ModelUpdateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 14, 7, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.start = self.now.replace(minute=15)
        self.row = dict(id=7, es_sn=['ES02'], start_time='00:00:00', end_time='08:00:00',
            type='charge', kw=100, repeat='每天重复', updatedAt='2026-09-17T05:00:00Z')
        self.reader = Mock()
        self.reader._read_table.return_value = [self.row]
        self.opener = Mock()
        self.adapter = EMSModelUpdateAdapter(self.reader, opener=self.opener)
        self.config = SimpleNamespace(station_id='station-2', version='v1',
            parameters=SimpleNamespace(max_charge_kw=600, max_discharge_kw=540))
        self.payload = dict(schema_version='m4-rolling-plan-v1', station_id='station-2',
            run_id='333f7478-faf0-4b46-aa08-dbd05e37dec1', date='2026-09-17',
            configuration_version='v1', usage='remaining_day_advice_only', dispatch_status='not_dispatched',
            effective_at=self.start.isoformat(), valid_until=(self.start+timedelta(minutes=15)).isoformat(),
            finished_at=self.now.isoformat(),
            request=dict(station_id='station-2', capability=dict(available=True, max_charge_kw=600, max_discharge_kw=540,
                    initial_soc_pct=80, energy_capacity_kwh=2000, charge_efficiency=0.95, discharge_efficiency=0.95),
                constraints=dict(soc_min_pct=2, soc_max_pct=98, preferred_soc_min_pct=5, preferred_soc_max_pct=95,
                    terminal_soc_tolerance_pct=100, demand_limit_kw=2000, grid_import_limit_kw=2000,
                    grid_export_enabled=False, grid_export_limit_kw=0, cycle_cost_per_kwh=0),
                points=[dict(timestamp=self.start.isoformat(), load_forecast_kw=800, pv_forecast_kw=0)],
                source_versions=dict(configuration='v1', project_configuration=get_project().fingerprint)),
            plan=[dict(timestamp=self.start.isoformat(), mode='discharge', target_power_kw=540)])

    def preview(self):
        return self.adapter.preview('station-2', self.payload, self.config, now=self.now)

    def test_preview_binds_only_id7_and_station_and_version(self):
        result = self.preview()
        request = result['request']
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['path'], 't_model:update')
        self.assertEqual(json.loads(request['query']['filter']), {'$and': [
            {'id': {'$eq': 7}}, {'es_sn': {'$eq': ['ES02']}},
            {'updatedAt': {'$eq': self.row['updatedAt']}}]})
        self.assertEqual(request['body'], dict(start_time='14:15:00', end_time='14:30:00',
            type='discharge', kw=90, repeat='每天重复', es_sn=['ES02']))
        self.assertFalse(result['network_write_performed'])
        self.opener.open.assert_not_called()

    def test_idle_skips_record_read_and_post(self):
        self.payload['plan'][0].update(mode='idle', target_power_kw=0)
        result = self.preview()
        self.assertEqual(result['status'], 'skipped')
        self.assertIsNone(result['request'])
        self.reader._read_table.assert_not_called()
        self.opener.open.assert_not_called()

    def test_wrong_or_shared_ownership_and_missing_record_rejected(self):
        for owners in (['ES01'], ['ES01', 'ES02'], 'ES02'):
            self.row['es_sn'] = owners
            with self.assertRaises(ModelUpdateError):
                self.preview()
        self.reader._read_table.return_value = []
        with self.assertRaises(ModelUpdateError):
            self.preview()

    def test_station1_without_binding_rejected(self):
        with self.assertRaises(ModelUpdateError):
            self.adapter.preview('station-1', self.payload, self.config, now=self.now)
        self.opener.open.assert_not_called()

    def test_expired_and_excessive_power_rejected(self):
        with self.assertRaises(ModelUpdateError):
            self.adapter.preview('station-2', self.payload, self.config, now=self.start)
        self.payload['plan'][0]['target_power_kw'] = 600
        with self.assertRaises(ModelUpdateError):
            self.preview()

    def test_default_write_gate_prevents_all_io(self):
        with self.assertRaisesRegex(ModelUpdateError, '未启用'):
            self.adapter.submit('station-2', self.payload, self.config)
        self.reader._read_table.assert_not_called()
        self.opener.open.assert_not_called()

    def test_transport_timeout_is_uncertain_and_never_retried(self):
        self.opener.open.side_effect = TimeoutError('simulated network timeout')
        result = self.adapter.submit('station-2', self.payload, self.config,
            explicitly_enabled=True, write_token='dummy-test-only', now=self.now)
        self.assertEqual(result['status'], 'update_unconfirmed')
        self.assertEqual(result['device_execution_status'], 'unverified')
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        predicate = json.loads(parse_qs(urlsplit(request.full_url).query)['filter'][0])
        self.assertEqual(predicate['$and'][0], {'id': {'$eq': 7}})
        self.assertNotIn('dummy-test-only', json.dumps(result))

    def test_success_requires_readback_and_does_not_claim_execution(self):
        after = deepcopy(self.row)
        after.update(self.preview()['request']['body'], updatedAt='2026-09-17T06:07:01Z')
        self.reader._read_table.side_effect = [[self.row], [after]]
        response = io.BytesIO(b'{"data": []}')
        response.status = 200
        self.opener.open.return_value = response
        result = self.adapter.submit('station-2', self.payload, self.config,
            explicitly_enabled=True, write_token='dummy-test-only', now=self.now)
        self.assertEqual(result['status'], 'plan_update_readback_verified')
        self.assertEqual(result['device_execution_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
