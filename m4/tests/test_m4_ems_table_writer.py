"""Mock-only commissioning writes; never contacts an upstream service."""
import io
import json
import fcntl
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
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
        self.rows = [dict(self.row), dict(self.row, id=8, start_time='16:00:00', end_time='17:00:00', m4_run_id=self.payload['run_id'])]
        self.reader._read_table.side_effect = lambda _: [dict(row) for row in self.rows]
        self.transport = Mock()
        def post(request, **kwargs):
            action = urlsplit(request.full_url).path.split(':')[-1]
            body = json.loads(request.data)
            identifier = int(parse_qs(urlsplit(request.full_url).query).get('filterByTk', ['0'])[0])
            if action != 'create':
                predicate = json.loads(parse_qs(urlsplit(request.full_url).query)['filter'][0])['$and']
                # Reproduce the deployed collection: array $eq silently matches
                # no rows even though the endpoint returns HTTP 200.
                if any(isinstance(value['$eq'], list) for clause in predicate for value in clause.values()):
                    response = io.BytesIO(b'{"data": []}')
                    response.status = 200
                    return response
                self.assertEqual({key for clause in predicate for key in clause}, ({'id', 'updatedAt', 'm4_run_id'} if action == 'destroy' else {'id', 'updatedAt'}))
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

    def test_small_charge_is_removed_and_not_confirmed_as_charging(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.payload['plan'][0].update(mode='charge', target_power_kw=0.000132683)
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'plan_table_readback_verified')
            self.assertEqual(result['confirmed_plan'][0]['mode'], 'idle')
            self.assertEqual(result['confirmed_plan'][0]['target_power_kw'], 0)
            self.assertFalse(result['operations']['create'])
            self.assertEqual([r['id'] for r in self.rows], [7])

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

    def test_timeout_reconciles_absent_create_before_fresh_run(self):
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
            again = writer.submit('station-2', self.payload, self.config,
                                  now=self.now+timedelta(seconds=61))
            self.assertEqual(again['status'], 'plan_table_readback_verified')
            self.assertEqual(count, 3)
            archived = list((Path(root)/'reconciled').glob('*.json'))
            self.assertEqual(len(archived), 1)
            proof = json.loads(archived[0].read_text())['reconciliation']
            self.assertEqual(proof['operations'][-1]['result'], 'not_applied')
            self.assertFalse(proof['replayed'])

    def test_500_after_create_is_read_back_without_duplicate_create(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            original = self.transport.open.side_effect
            def lost_response(request, **kwargs):
                response = original(request, **kwargs)
                if request.full_url.endswith('t_model:create'):
                    raise HTTPError(request.full_url, 500, 'failed', {}, None)
                return response
            self.transport.open.side_effect = lost_response
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'table_write_unconfirmed')
            count = self.transport.open.call_count
            result = writer.reconcile('station-2', now=self.now+timedelta(seconds=61))
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(self.transport.open.call_count, count)
            self.assertFalse((Path(root)/'station-2.hold').exists())
            self.payload['run_id'] = '433f7478-faf0-4b46-aa08-dbd05e37dec1'
            self.transport.open.side_effect = original
            result = writer.submit('station-2', self.payload, self.config,
                                   now=self.now+timedelta(seconds=61))
            self.assertEqual(result['status'], 'plan_table_readback_verified')
            self.assertFalse(result['operations']['create'])

    def test_recovery_preserves_expired_record_without_post(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            original = self.transport.open.side_effect
            def lost_response(request, **kwargs):
                response = original(request, **kwargs)
                if request.full_url.endswith('t_model:create'):
                    raise TimeoutError()
                return response
            self.transport.open.side_effect = lost_response
            writer.submit('station-2', self.payload, self.config, now=self.now)
            self.rows[-1]['repeat'] = '已过期'
            self.transport.open.reset_mock()
            result = writer.reconcile('station-2', now=self.now+timedelta(days=1))
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(self.rows[-1]['repeat'], '已过期')
            self.transport.open.assert_not_called()
            with self.assertRaises(ValueError):
                writer.submit('station-2', self.payload, self.config, now=self.now+timedelta(days=1))
            self.transport.open.assert_not_called()

    def test_expired_future_row_is_not_recreated_by_same_daily_run(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            prepared = writer.adapter.preview('station-2', self.payload, self.config, now=self.now)
            expired = dict(prepared['operations']['create'][0]['body'], id=99,
                           repeat='已过期', updatedAt='2026-09-17T06:07:01Z')
            self.rows.append(expired)
            with self.assertRaisesRegex(ValueError, '已过期'):
                writer.submit('station-2', self.payload, self.config, now=self.now)
            self.transport.open.assert_not_called()

    def test_preview_failure_has_no_intent_to_replay(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.reader._read_table.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(writer.reconcile('station-2', now=self.now,
                run_id=self.payload['run_id'])['status'], 'ready')
            self.transport.open.assert_not_called()

    def test_read_failure_and_duplicate_keep_hold_without_post(self):
        for conflict in ('unavailable', 'duplicate', 'changed'):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as root:
                writer = self.prepare(root)
                original = self.transport.open.side_effect
                def lost_response(request, **kwargs):
                    response = original(request, **kwargs)
                    if request.full_url.endswith('t_model:create'):
                        raise TimeoutError()
                    return response
                self.transport.open.side_effect = lost_response
                writer.submit('station-2', self.payload, self.config, now=self.now)
                self.transport.open.reset_mock()
                if conflict == 'unavailable':
                    self.reader._read_table.side_effect = TimeoutError()
                elif conflict == 'duplicate':
                    self.rows.append(dict(self.rows[-1], id=11))
                else:
                    self.rows[-1]['kw'] = 123
                result = writer.reconcile('station-2', now=self.now+timedelta(seconds=61))
                self.assertEqual(result['status'], 'blocked')
                self.assertTrue((Path(root)/'station-2.hold').exists())
                self.transport.open.assert_not_called()

    def test_live_writer_mutex_blocks_reconciliation_and_submit(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            with (Path(root)/'station-2.write.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(writer.reconcile('station-2', now=self.now)['status'], 'blocked')
                self.assertEqual(writer.submit('station-2', self.payload, self.config,
                                              now=self.now)['status'], 'blocked')
            self.transport.open.assert_not_called()

    def test_existing_crash_hold_prevents_post(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            (Path(root)/'station-2.hold').write_text('previous run')
            self.assertEqual(writer.submit('station-2', self.payload, self.config, now=self.now)['status'], 'blocked')
            self.transport.open.assert_not_called()

    def test_changed_station_or_version_before_post_blocks_write(self):
        for changed in ({'es_sn': ['ES01']}, {'es_sn': ['ES02', 'ES01']},
                        {'updatedAt': '2026-09-18T00:00:00Z'}, {'m4_run_id': ''}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root:
                writer = self.prepare(root)
                prepared = writer.adapter.preview('station-2', self.payload, self.config, now=self.now)
                writer.adapter.preview = Mock(return_value=prepared)
                self.rows[1].update(changed)
                result = writer.submit('station-2', self.payload, self.config, now=self.now)
                self.assertEqual(result['status'], 'table_write_unconfirmed')
                self.assertEqual(result['failure_stage'], 'read_before_write')
                self.assertFalse(result['network_write_performed'])
                self.transport.open.assert_not_called()

    def test_read_crossing_cutover_prevents_post(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            prepared = writer.adapter.preview('station-2', self.payload, self.config, now=self.now)
            writer.adapter.preview = Mock(return_value=prepared)
            with patch('m4.settings.ems_table_writer.datetime') as clock:
                clock.now.side_effect = [self.now, self.start, self.start]
                result = writer.submit('station-2', self.payload, self.config)
            self.assertEqual(result['status'], 'table_write_unconfirmed')
            self.assertFalse(result['network_write_performed'])
            self.transport.open.assert_not_called()

    def test_fixed_cabinet_power_changes_only_outgoing_kw(self):
        from copy import deepcopy
        for mode, reference_power, cabinet_kw in [('charge',120,600),('discharge',533.39706,540)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                writer = self.prepare(root)
                self.payload['plan'][0].update(mode=mode,target_power_kw=reference_power)
                # Original charge 120 stays under 1000; mapped 600 would not.
                self.payload['request']['constraints']['demand_limit_kw'] = 1000
                original = deepcopy(self.payload)
                result = writer.submit('station-2', self.payload, self.config, now=self.now)
                self.assertEqual(result['status'],'plan_table_readback_verified')
                row = next(r for r in self.rows if r['id']==10)
                self.assertEqual((row['type'],row['kw'],row['start_time'],row['end_time']),
                    (mode,cabinet_kw,'14:15:00','14:30:00'))
                self.assertEqual(self.payload,original)

    def test_http_failure_records_status_and_stage_without_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.transport.open.side_effect = HTTPError('https://example.invalid/private',403,
                'dummy-test-only',{},None)
            result = writer.submit('station-2',self.payload,self.config,now=self.now)
            self.assertEqual(result['http_status'],403)
            self.assertEqual(result['failure_stage'],'http_request')
            self.assertNotIn('dummy-test-only',json.dumps(result))
            self.assertNotIn('example.invalid',json.dumps(result))
            self.transport.open.assert_called_once()

    def test_new_fields_are_written_and_confirmed_plan_is_archived(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            created = next(row for row in self.rows if row['id'] == 10)
            self.assertEqual(created['m4_run_id'], self.payload['run_id'])
            self.assertEqual(created['m4_plan_date'], self.payload['date'])
            self.assertEqual(created['explain'], '储能供应部分负荷，减少本时段电网购电。')
            self.assertEqual(result['confirmed_schedule'][0]['record_id'], created['id'])
            self.assertEqual(result['confirmed_schedule'][0]['explain'], created['explain'])
            self.assertEqual(result['execution_basis'], 'ems_plan_table_readback_v1')
            self.assertEqual(len(result['confirmed_plan']), len(self.payload['plan']))

    def test_running_record_is_carried_without_rewriting_execution_fields(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.rows[1].update(start_time='14:00:00', end_time='14:30:00', type='discharge', kw=540)
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'plan_table_readback_verified')
            self.assertEqual(result['completed_operations'], 1)
            request = self.transport.open.call_args.args[0]
            self.assertTrue(urlsplit(request.full_url).path.endswith(':update'))
            self.assertEqual(set(json.loads(request.data)), {'m4_run_id', 'm4_plan_date', 'repeat', 'explain'})
            self.assertEqual((self.rows[1]['start_time'], self.rows[1]['end_time'], self.rows[1]['kw']),
                             ('14:00:00', '14:30:00', 540))

    def test_changed_running_record_is_cut_only_after_future_readback(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.rows[1].update(start_time='14:00:00', end_time='16:00:00', type='charge', kw=100)
            original = self.transport.open.side_effect
            def check_order(request, **kwargs):
                action = urlsplit(request.full_url).path.split(':')[-1]
                if action == 'create':
                    self.assertEqual(self.rows[1]['end_time'], '16:00:00')
                if action == 'update':
                    self.assertTrue(any(r['id'] == 10 for r in self.rows))
                    self.assertEqual(json.loads(request.data), {'end_time': '14:15:00', 'repeat': '今日有效'})
                return original(request, **kwargs)
            self.transport.open.side_effect = check_order
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'plan_table_readback_verified')
            self.assertEqual([{k: op[k] for k in ('action', 'record_id')}
                              for op in result['verified_operations']],
                             [dict(action='create', record_id=10), dict(action='update', record_id=8)])
            self.assertEqual(result['verified_operations'][1]['before']['end_time'], '16:00:00')
            self.assertEqual(self.rows[1]['start_time'], '14:00:00')
            self.assertEqual(self.rows[1]['kw'], 100)

    def test_future_create_failure_leaves_running_end_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            writer = self.prepare(root)
            self.rows[1].update(start_time='14:00:00', end_time='16:00:00')
            self.transport.open.side_effect = TimeoutError('no response')
            result = writer.submit('station-2', self.payload, self.config, now=self.now)
            self.assertEqual(result['status'], 'table_write_unconfirmed')
            self.assertEqual(result['pending_operation']['action'], 'create')
            self.assertEqual(self.rows[1]['end_time'], '16:00:00')
            self.assertTrue((Path(root)/'station-2.hold').exists())

    def test_missing_new_field_in_readback_keeps_hold(self):
        for field in ('m4_plan_date', 'explain'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                writer = self.prepare(root)
                original = self.transport.open.side_effect
                def strip_date(request, **kwargs):
                    response = original(request, **kwargs)
                    for row in self.rows:
                        row.pop(field, None)
                    return response
                self.transport.open.side_effect = strip_date
                result = writer.submit('station-2', self.payload, self.config, now=self.now)
                self.assertEqual(result['status'], 'table_write_unconfirmed')
                self.assertEqual(result['failure_stage'], 'readback')
                self.assertNotIn('confirmed_plan', result)
                self.assertTrue((Path(root)/'station-2.hold').exists())
