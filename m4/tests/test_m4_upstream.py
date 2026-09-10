import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from m4.settings.upstream import NocoBaseClient, SourceReadError


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, payload, url, *, final_url=None):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.url = final_url or url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, rows=None, *, metadata='totalPage', payload=None, error=None, final_url=None):
        self.rows = rows if rows is not None else [{'timestamp': index} for index in range(5)]
        self.metadata = metadata
        self.payload = payload
        self.error = error
        self.final_url = final_url
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        query = parse_qs(urlsplit(request.full_url).query)
        page, size = int(query['page'][0]), int(query['pageSize'][0])
        batch = self.rows[(page - 1) * size:page * size]
        meta = {'page': page, 'pageSize': size}
        if self.metadata == 'totalPage':
            meta['totalPage'] = (len(self.rows) + size - 1) // size
        elif self.metadata == 'hasNext':
            meta['hasNext'] = page * size < len(self.rows)
        elif self.metadata == 'count':
            meta['count'] = len(self.rows)
        payload = self.payload if self.payload is not None else {'data': batch, 'meta': meta}
        return FakeResponse(payload, request.full_url, final_url=self.final_url)


class UpstreamTests(unittest.TestCase):
    def read(self, *, opener=None, table='t_es_data', **kwargs):
        opener = opener or FakeOpener()
        with patch('m4.settings.upstream.build_opener', return_value=opener):
            result = NocoBaseClient('test-secret').list_rows(table, fields='timestamp', **kwargs)
        return result, opener

    def test_all_supported_pagination_metadata_reads_complete_rows(self):
        for metadata in ('hasNext', 'totalPage', 'count', 'none'):
            with self.subTest(metadata=metadata):
                rows, opener = self.read(opener=FakeOpener(metadata=metadata), page_size=2)
                self.assertEqual(rows, [{'timestamp': index} for index in range(5)])
                self.assertEqual(len(opener.calls), 3)

    def test_empty_table_and_exact_full_page_without_metadata(self):
        rows, _ = self.read(opener=FakeOpener(rows=[]), page_size=2)
        self.assertEqual(rows, [])
        rows, opener = self.read(opener=FakeOpener(rows=[{'timestamp': 1}, {'timestamp': 2}], metadata='none'), page_size=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(opener.calls), 2)

    def test_limit_one_uses_one_small_page_and_intentionally_stops(self):
        rows, opener = self.read(limit=1)
        self.assertEqual(rows, [{'timestamp': 0}])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(parse_qs(urlsplit(opener.calls[0][0].full_url).query)['pageSize'], ['1'])

    def test_limit_across_pages_returns_only_requested_rows(self):
        rows, opener = self.read(page_size=2, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(opener.calls), 2)

    def test_request_is_fixed_origin_get_and_encoded_filters(self):
        filters = {'es_sn': 'ES02', 'timestamp': {'$gte': '2026-09-01T00:00:00+08:00'}}
        _, opener = self.read(filters=filters, sort='-timestamp', limit=1)
        request, timeout = opener.calls[0]
        parts = urlsplit(request.full_url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ('https', 'vifa.hlszh.com', '/api/t_es_data:list'))
        self.assertEqual(json.loads(parse_qs(parts.query)['filter'][0]), filters)
        self.assertEqual(parse_qs(parts.query)['sort'], ['-timestamp'])
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')
        self.assertEqual(timeout, 8)
        self.assertNotIn('test-secret', request.full_url)

    def test_all_allowed_tables_and_large_history_pages(self):
        for table in ('t_emu', 't_es_data', 't_es_stat', 't_es_count_stat', 't_peak_diy', 't_rate', 'energy_forecast_latest', 'energy_forecast_manual_runs', 'energy_forecast_manual_points'):
            with self.subTest(table=table):
                rows, _ = self.read(table=table, page_size=2000)
                self.assertEqual(len(rows), 5)

    def test_invalid_request_options_fail_before_io(self):
        invalid = [dict(page_size=value) for value in (0, -1, True, 2001, '100')]
        invalid += [dict(max_pages=value) for value in (0, 101, True)]
        invalid += [dict(limit=value) for value in (0, -1, True, '1')]
        invalid += [dict(filters=[]), dict(filters={'x': float('nan')}), dict(sort='../../elsewhere')]
        for kwargs in invalid:
            opener = FakeOpener()
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceReadError):
                self.read(opener=opener, **kwargs)
            self.assertFalse(opener.calls)
        for table in ('t_need', '../t_emu', 'https://evil.example/', None):
            opener = FakeOpener()
            with self.subTest(table=table), self.assertRaises(SourceReadError):
                self.read(opener=opener, table=table)
            self.assertFalse(opener.calls)

    def test_empty_token_does_not_block_construction_but_blocks_read(self):
        for token in ('', ' ', None, 'unsafe\nsecret'):
            with self.subTest(token=token), patch('m4.settings.upstream.build_opener', return_value=FakeOpener()) as builder:
                client = NocoBaseClient(token)
                with self.assertRaises(SourceReadError):
                    client.list_rows('t_emu', fields='emu_sn')
                self.assertFalse(builder.return_value.calls)

    def test_invalid_fields_fail_before_io(self):
        for fields in ('', '*', '../path', 'emu_sn&filter=secret', None):
            with self.subTest(fields=fields), patch('m4.settings.upstream.build_opener', return_value=FakeOpener()) as builder:
                with self.assertRaises(SourceReadError):
                    NocoBaseClient('test-secret').list_rows('t_emu', fields=fields)
                self.assertFalse(builder.return_value.calls)

    def test_max_pages_never_returns_truncated_results(self):
        with self.assertRaises(SourceReadError):
            self.read(page_size=2, max_pages=2)

    def test_malformed_responses_and_inconsistent_pagination_fail(self):
        for payload in (b'invalid-json test-secret', [], {'data': {}}, {'data': [1]},
                        {'data': [], 'meta': {'hasNext': True}},
                        {'data': [{'timestamp': 1}], 'meta': {'count': 5, 'hasNext': False}},
                        {'data': [{'timestamp': 1}], 'meta': {'totalPage': 0}},
                        {'data': [{'timestamp': 1}], 'meta': {'totalPage': -1}},
                        {'data': [{'timestamp': 1}], 'meta': {'page': 0}},
                        {'data': [], 'meta': {'hasNext': 'yes'}}):
            with self.subTest(payload=payload), self.assertRaises(SourceReadError) as caught:
                self.read(opener=FakeOpener(payload=payload))
            self.assertNotIn('test-secret', str(caught.exception))

    def test_transport_errors_are_sanitized(self):
        for error in (TimeoutError('test-secret'), URLError('test-secret'),
                      HTTPError('https://evil.example/test-secret', 302, 'test-secret', {}, None)):
            with self.subTest(error=type(error).__name__), self.assertRaises(SourceReadError) as caught:
                self.read(opener=FakeOpener(error=error))
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertNotIn('evil.example', str(caught.exception))

    def test_redirects_and_foreign_final_origin_are_rejected(self):
        with patch('m4.settings.upstream.build_opener', return_value=FakeOpener()) as builder:
            NocoBaseClient('test-secret')
        handler = builder.call_args.args[0]
        from urllib.request import Request
        self.assertIsNone(handler.redirect_request(Request('https://vifa.hlszh.com/api/t_emu:list'), None, 302, '', {}, 'https://evil.example/'))
        with self.assertRaises(SourceReadError):
            self.read(opener=FakeOpener(final_url='https://evil.example/'))

    def test_oversize_response_fails(self):
        with self.assertRaises(SourceReadError):
            self.read(opener=FakeOpener(payload=b' ' * (4 * 1024 * 1024 + 1)))

    def test_whole_read_deadline_is_enforced(self):
        opener = FakeOpener()
        with patch('m4.settings.upstream.monotonic', side_effect=[0, 0, 31]):
            with self.assertRaises(SourceReadError):
                self.read(opener=opener, page_size=2)
        self.assertLessEqual(len(opener.calls), 1)


if __name__ == '__main__':
    unittest.main()
