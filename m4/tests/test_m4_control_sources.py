import copy
import io
import json
import unittest
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from m4.settings.control_sources import ControlSourceError, ControlSourceReader


def source_rows():
    updated = '2026-09-07T00:00:00Z'
    return {
        't_es': [dict(id=1, sn='ES01', es_power_storage='500', updatedAt=updated),
                 dict(id=2, sn='ES02', es_power_storage=1500, updatedAt=updated)],
        't_need': [
            dict(id=1, f_es_sn='ES01', need_kw='1062.5', reserved_kw=50,
                 rated_capacity=1250, load_rate=.85, updatedAt=updated),
            dict(id=2, f_es_sn='ES02', need_kw=504, reserved_kw=5,
                 rated_capacity=630, load_rate=.8, updatedAt=updated),
        ],
        're_flow': [
            dict(id=1, fk_es_sn='ES01', re_kw=40, updatedAt=updated),
            dict(id=2, fk_es_sn='ES02', re_kw='40', updatedAt=updated),
        ],
        't_model': [
            dict(id=13, es_sn=['ES01'], start_time='00:00:00', end_time='08:00:00',
                 type='charge', kw=100, repeat='每天重复', updatedAt=updated),
            dict(id=7, es_sn=['ES02'], start_time='00:00:00', end_time='08:00:00',
                 type='charge', kw=100, repeat='每天重复', updatedAt=updated),
            dict(id=14, es_sn=['ES01'], start_time='08:00:00', end_time='12:00:00',
                 type='discharge', kw=60, repeat='每天重复', updatedAt=updated),
            dict(id=8, es_sn=['ES02'], start_time='08:00:00', end_time='12:00:00',
                 type='discharge', kw=90, repeat='每天重复', updatedAt=updated),
        ],
    }


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, payload, url):
        super().__init__(json.dumps(payload).encode())
        self.url = url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, rows, page_size=None, error=None, payload=None, table_errors=None):
        self.rows = rows
        self.page_size = page_size
        self.error = error
        self.payload = payload
        self.table_errors = table_errors or {}
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        parts = urlsplit(request.full_url)
        table = parts.path.rsplit('/', 1)[1].split(':')[0]
        if table in self.table_errors:
            raise self.table_errors[table]
        query = parse_qs(parts.query)
        page = int(query['page'][0])
        size = self.page_size or int(query['pageSize'][0])
        rows = self.rows[table]
        payload = self.payload if self.payload is not None else {
            'data': rows[(page - 1) * size:page * size],
            'meta': {'page': page, 'pageSize': size,
                     'totalPage': max(1, (len(rows) + size - 1) // size)},
        }
        return FakeResponse(payload, request.full_url)


class ControlSourceTests(unittest.TestCase):
    def fetch(self, rows=None, station='station-1', **opener_options):
        opener = FakeOpener(rows if rows is not None else source_rows(), **opener_options)
        with patch('m4.settings.control_sources.build_opener', return_value=opener):
            result = ControlSourceReader('test-secret').fetch(station)
        return result, opener

    def test_capacity_is_required_and_cannot_fall_back_to_manual_settings(self):
        for capacity in ([], [dict(sn='ES01', es_power_storage=0, updatedAt='2026-09-07T00:00:00Z')],
                         [dict(sn='ES01', es_power_storage=-1, updatedAt='2026-09-07T00:00:00Z')]):
            rows=source_rows();rows['t_es']=capacity
            with self.subTest(capacity=capacity),self.assertRaises(ControlSourceError):
                self.fetch(rows)
        rows=source_rows();rows['t_es'].append(copy.deepcopy(rows['t_es'][0]))
        with self.assertRaises(ControlSourceError):self.fetch(rows)

    def test_station_mapping_numbers_and_daily_schedule_with_reverse_control_inactive(self):
        one, opener = self.fetch()
        two, _ = self.fetch(station='station-2')
        self.assertEqual(one['source_station_id'], 'ES01')
        self.assertEqual(one['demand']['need_kw'], 1062.5)
        self.assertEqual(one['demand']['reserved_kw'], 50)
        self.assertEqual(two['demand']['need_kw'], 504)
        self.assertEqual(two['reverse_flow']['re_kw'], 40)
        self.assertEqual(one['schedule'][1]['power_kw'], 60)
        self.assertEqual(two['schedule'][1]['power_kw'], 90)
        self.assertEqual(one['schedule'][0]['repeat'], 'daily')
        self.assertEqual(one['status'], 'ready')
        self.assertEqual(one['storage_capacity']['energy_capacity_kwh'], 500)
        self.assertEqual(two['storage_capacity']['energy_capacity_kwh'], 1500)
        self.assertEqual(one['configured_cabinet_count'], 2)
        self.assertEqual(one['power_scope'], 'cabinet')
        self.assertEqual(one['source_health'], {'demand': 'ready', 'reverse_flow': 'ready', 'schedule': 'ready'})
        self.assertEqual(one['control_policy_version'], 'm4-control-policy-v5-need-import-limit')
        self.assertFalse(one['reverse_flow']['enabled'])
        self.assertIsNone(one['reverse_flow']['effective_limit_kw'])
        self.assertEqual(one['warnings'], [])
        self.assertEqual(len(one['issues']), 1)
        self.assertIn('不参与', one['issues'][0])
        self.assertNotIn('待确认', one['issues'][0])
        self.assertIsNotNone(datetime.fromisoformat(one['fetched_at']).utcoffset())
        self.assertNotIn('constraints', one)
        for request, timeout in opener.calls:
            self.assertEqual(timeout, 8)
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')
            self.assertIn('fields', parse_qs(urlsplit(request.full_url).query))

    def test_array_station_membership_is_exact_and_shared_rows_are_supported(self):
        rows = source_rows()
        rows['t_model'][0]['es_sn'] = ['ES01', 'ES02']
        rows['t_model'][1]['es_sn'] = ['ES010']
        one, _ = self.fetch(rows)
        two, _ = self.fetch(rows, station='station-2')
        self.assertEqual([x['id'] for x in one['schedule']], [13, 14])
        self.assertEqual([x['id'] for x in two['schedule']], [13, 8])

    def test_pagination_loads_all_rows_before_station_filtering(self):
        result, opener = self.fetch(station='station-2', page_size=1)
        self.assertEqual(len(result['schedule']), 2)
        self.assertEqual(len(opener.calls), 10)

    def test_missing_and_duplicate_demand_controls_fail(self):
        for duplicate in (False, True):
            rows = source_rows()
            rows['t_need'] = [rows['t_need'][0]] * 2 if duplicate else rows['t_need'][1:]
            with self.subTest(duplicate=duplicate), self.assertRaises(ControlSourceError):
                self.fetch(rows)

    def test_missing_duplicate_and_invalid_reverse_records_do_not_block_demand(self):
        for variant in ('missing', 'duplicate', 'number', 'station', 'timestamp'):
            rows = source_rows()
            if variant == 'missing':
                rows['re_flow'] = []
            elif variant == 'duplicate':
                rows['re_flow'].append(copy.deepcopy(rows['re_flow'][0]))
            elif variant == 'number':
                rows['re_flow'][0]['re_kw'] = 'NaN'
            elif variant == 'station':
                rows['re_flow'][0]['fk_es_sn'] = None
            else:
                rows['re_flow'][0]['updatedAt'] = 'invalid'
            with self.subTest(variant=variant):
                result, _ = self.fetch(rows)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['demand']['need_kw'], 1062.5)
                self.assertIsNone(result['reverse_flow'])
                self.assertEqual(result['source_health']['reverse_flow'], 'unavailable')
                self.assertEqual(result['source_health']['schedule'], 'ready')
                self.assertTrue(result['warnings'])

    def test_empty_schedule_is_reported_without_inventing_idle_points(self):
        rows = source_rows()
        rows['t_model'] = []
        result, _ = self.fetch(rows)
        self.assertEqual(result['schedule'], [])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['source_health']['schedule'], 'ready')
        self.assertEqual(result['warnings'], [])
        self.assertTrue(any('未配置' in issue for issue in result['issues']))

    def test_invalid_numbers_are_rejected(self):
        for value in (None, True, -1, 'NaN', 'Infinity', 'not-a-number'):
            rows = source_rows()
            rows['t_need'][0]['need_kw'] = value
            with self.subTest(value=value), self.assertRaises(ControlSourceError):
                self.fetch(rows)

    def test_invalid_schedule_shape_mode_repeat_and_time_are_isolated(self):
        for change in ({'es_sn': 'ES01'}, {'type': 'idle'}, {'repeat': '每周重复'},
                       {'repeat': None}, {'start_time': '8:00'}, {'end_time': '24:01:00'},
                       {'kw': True}, {'id': None}):
            rows = source_rows()
            rows['t_model'][0].update(change)
            with self.subTest(change=change):
                result, _ = self.fetch(rows)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['schedule'], [])
                self.assertEqual(result['source_health']['schedule'], 'unavailable')
                self.assertEqual(result['source_health']['reverse_flow'], 'ready')
                self.assertTrue(result['warnings'])
                self.assertFalse(any('未配置' in issue for issue in result['issues']))

    def test_duplicate_schedule_identifiers_are_isolated(self):
        rows = source_rows()
        rows['t_model'].append(copy.deepcopy(rows['t_model'][0]))
        result, _ = self.fetch(rows)
        self.assertEqual(result['schedule'], [])
        self.assertEqual(result['source_health']['schedule'], 'unavailable')
        self.assertTrue(result['warnings'])

    def test_optional_source_transport_failures_are_isolated_and_sanitized(self):
        for failed_tables in (('re_flow',), ('t_model',), ('re_flow', 't_model')):
            with self.subTest(failed_tables=failed_tables):
                result, _ = self.fetch(table_errors={table: URLError('test-secret') for table in failed_tables})
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['source_health']['demand'], 'ready')
                self.assertEqual(result['demand']['need_kw'], 1062.5)
                self.assertEqual(len(result['warnings']), len(failed_tables))
                self.assertNotIn('test-secret', json.dumps(result))
                for table in failed_tables:
                    key = 'reverse_flow' if table == 're_flow' else 'schedule'
                    self.assertEqual(result['source_health'][key], 'unavailable')
                if 't_model' in failed_tables:
                    self.assertEqual(result['schedule'], [])
                    self.assertFalse(any('未配置' in issue for issue in result['issues']))

    def test_demand_transport_failure_still_blocks_result(self):
        with self.assertRaises(ControlSourceError):
            self.fetch(table_errors={'t_need': TimeoutError('test-secret')})

    def test_version_is_stable_across_row_order_other_station_and_fetch_time(self):
        rows = source_rows()
        first, _ = self.fetch(rows)
        rows['t_need'][1]['need_kw'] = 499
        for entries in rows.values():
            entries.reverse()
            for row in entries:
                row['updatedAt'] = '2026-09-07T01:00:00Z'
        second, _ = self.fetch(rows)
        self.assertEqual(first['version'], second['version'])
        for row in rows['t_need']:
            if row['f_es_sn'] == 'ES01':
                row['need_kw'] = 1000
        third, _ = self.fetch(rows)
        self.assertNotEqual(first['version'], third['version'])

    def test_control_policy_version_changes_source_version(self):
        first, _ = self.fetch()
        with patch('m4.settings.control_sources.CONTROL_POLICY_VERSION', 'different-confirmed-policy'):
            second, _ = self.fetch()
        self.assertNotEqual(first['version'], second['version'])

    def test_transport_errors_never_expose_response_or_credentials(self):
        errors = [URLError('test-secret'), TimeoutError('test-secret'),
                  HTTPError('https://evil.example/test-secret', 302, 'test-secret', {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(ControlSourceError) as caught:
                    self.fetch(error=error)
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertNotIn('evil.example', str(caught.exception))

    def test_malformed_payload_and_pagination_fail(self):
        for payload in ([], {'data': {}}, {'data': [{}], 'meta': {'hasNext': 'yes'}},
                        {'data': [], 'meta': {'totalPage': -1}},
                        {'data': [{}], 'meta': {'totalPage': 0}},
                        {'data': [{}], 'meta': {'page': 1, 'hasNext': False, 'totalPage': 2}},
                        {'data': [{}], 'meta': {'page': 0}},
                        {'data': [], 'meta': {'hasNext': True}}):
            with self.subTest(payload=payload), self.assertRaises(ControlSourceError):
                self.fetch(payload=payload)

    def test_endless_pagination_is_bounded(self):
        with self.assertRaises(ControlSourceError) as caught:
            self.fetch(payload={'data': [{}], 'meta': {'hasNext': True}})
        self.assertIn('上限', str(caught.exception))

    def test_missing_token_invalid_station_and_non_https_origins_fail_before_io(self):
        with patch('m4.settings.control_sources.build_opener') as opener:
            for token in ('', ' ', None, 'unsafe\nsecret'):
                with self.subTest(token=token), self.assertRaises(ControlSourceError):
                    ControlSourceReader(token)
            for url in ('http://vifa.hlszh.com/api/', 'https://user:secret@vifa.hlszh.com/',
                        'https://vifa.hlszh.com/api/?next=evil', 'https://vifa.hlszh.com/api/#fragment'):
                with self.subTest(url=url), self.assertRaises(ControlSourceError):
                    ControlSourceReader('test-secret', base_url=url)
            opener.assert_not_called()
        with patch('m4.settings.control_sources.build_opener', return_value=FakeOpener(source_rows())) as opener:
            reader = ControlSourceReader('test-secret')
            with self.assertRaises(ControlSourceError):
                reader.fetch('ES01')
            self.assertFalse(opener.return_value.calls)

    def test_redirect_handler_refuses_every_redirect(self):
        with patch('m4.settings.control_sources.build_opener', return_value=FakeOpener(source_rows())) as builder:
            ControlSourceReader('test-secret')
        handler = builder.call_args.args[0]
        from urllib.request import Request
        self.assertIsNone(handler.redirect_request(
            Request('https://vifa.hlszh.com/api/t_need:list'), None, 302,
            'redirect', {}, 'https://evil.example/'))


class ExpiredScheduleTests(unittest.TestCase):
    def test_expired_records_do_not_enter_current_schedule(self):
        from m4.settings.control_sources import _schedule
        active = source_rows()['t_model'][0]
        expired = dict(active, id=101, repeat='已过期')
        self.assertEqual(_schedule([active, expired]), _schedule([active]))
        self.assertEqual(_schedule([expired]), [])

if __name__ == '__main__':
    unittest.main()
