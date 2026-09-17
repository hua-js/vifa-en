"""Mock-only commissioning writes; never contacts an upstream service."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

from m4.settings.ems_remaining_plan import EMSRemainingPlanAdapter
from m4.settings.ems_table_writer import EMSTableWriter
from m4.tests import test_m4_ems_remaining_plan as remaining_fixtures
from m4.tests import test_m4_ems_model_update as fixtures


class TableWriterTests(unittest.TestCase):
    setUp = fixtures.ModelUpdateTests.setUp
    full_day = remaining_fixtures.RemainingPlanTests.full_day

    def prepare(self, root):
        self.full_day()
        self.payload['plan'][0].update(mode='discharge', target_power_kw=540)
        self.rows = [dict(self.row), dict(self.row, id=8, start_time='16:00:00', end_time='17:00:00')]
        self.reader._read_table.side_effect = lambda _: [dict(row) for row in self.rows]
        self.transport = Mock()
        def post(request, **kwargs):
            action = urlsplit(request.full_url).path.split(':')[-1]
            body = json.loads(request.data)
            identifier = int(parse_qs(urlsplit(request.full_url).query).get('filterByTk', ['0'])[0])
            if action == 'destroy':
                self.rows[:] = [r for r in self.rows if r['id'] != identifier]
                data = {}
            elif action == 'create':
                data = dict(body, id=10, updatedAt='2026-09-17T06:07:01Z')
                self.rows.append(data)
            else:
                row = next(r for r in self.rows if r['id'] == identifier)
                row.update(body, updatedAt='2026-09-17T06:07:01Z')
                data = row
            response = io.BytesIO(json.dumps(dict(data=data)).encode())
            response.status = 200
            return response
        self.transport.open.side_effect = post
        return EMSTableWriter(EMSRemainingPlanAdapter(self.reader), root, 'dummy-test-only',
            enabled=True, opener=self.transport)

    def test_disabled_and_other_station_never_reads_or_writes(self):
        writer = EMSTableWriter(None, '/unused', '', enabled=False)
        self.assertEqual(writer.submit('station-2', {}, None)['status'], 'disabled')
        writer.enabled = True
        self.assertEqual(writer.submit('station-1', {}, None)['status'], 'disabled')

    def test_success_readback_deduplicated_across_writer_restart(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'plan_table_readback_verified')
            self.assertEqual(result['completed_operations'], 2)
            self.assertEqual(result['device_execution_status'], 'unverified')
            self.assertFalse((Path(root)/'station-2.hold').exists())
            again = EMSTableWriter(writer.adapter, root, 'dummy-test-only', enabled=True, opener=self.transport)
            self.assertEqual(again.submit('station-2', self.payload, self.config, now=self.now), result)
            self.assertEqual(self.transport.open.call_count, 2)

    def test_timeout_after_partial_write_holds_station_across_runs(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            original = self.transport.open.side_effect
            count = 0
            def fail_second(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise TimeoutError('secret must not be returned')
                return original(*args, **kwargs)
            self.transport.open.side_effect = fail_second
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'table_write_unconfirmed')
            self.assertEqual(result['completed_operations'], 1)
            self.assertEqual(result['pending_operation']['action'], 'create')
            self.assertEqual(result['verified_operations'], [dict(action='destroy', record_id=8)])
            self.assertEqual(json.loads((Path(root)/(self.payload['run_id']+'.json')).read_text()), result)
            self.payload['run_id'] = '433f7478-faf0-4b46-aa08-dbd05e37dec1'
            again = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(again['status'], 'blocked')
            self.assertEqual(count, 2)
            self.assertNotIn('secret must', json.dumps(result))

    def test_existing_crash_hold_prevents_post(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            (Path(root)/'station-2.hold').write_text('previous run')
            self.assertEqual(writer.submit('station-2', self.payload, self.config, now=self.now)['status'], 'blocked')
            self.transport.open.assert_not_called()

    def test_read_crossing_cutover_prevents_post(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            prepared = writer.adapter.preview('station-2', self.payload, self.config, now=self.now)
            writer.adapter.preview = Mock(return_value=prepared)
            with patch('m4.settings.ems_table_writer.datetime') as clock:
                clock.now.side_effect = [self.now, self.start]
                result = writer.submit('station-2', self.payload, self.config)
            self.assertEqual(result['status'], 'table_write_unconfirmed')
            self.assertFalse(result['network_write_performed'])
            self.transport.open.assert_not_called()
