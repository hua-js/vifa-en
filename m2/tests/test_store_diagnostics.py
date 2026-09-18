import io
import json
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from m2 import station_efficiency_nocobase as store


class StoreDiagnosticTests(unittest.TestCase):
    def test_single_record_upsert_array_is_accepted_and_silent(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(store, 'urlopen', return_value=io.StringIO('{"data":[{"id":1}]}')):
            payload = store._request_json('https://example.invalid/api/t_efficiency_points:updateOrCreate', 'secret', 1, {})
            self.assertEqual(store._saved_record(payload), {'id': 1})
        self.assertEqual(output.getvalue(), '')

    def test_invalid_upsert_arrays_remain_failures(self):
        for data in ([], [{'id': 1}, {'id': 2}], [None], [True], ['PRIVATE'], None):
            with self.subTest(data=data):
                output = io.StringIO()
                with redirect_stderr(output), patch.object(store, 'urlopen', return_value=io.StringIO(json.dumps({'data': data}))):
                    payload = store._request_json('https://example.invalid/api/t_efficiency_points:updateOrCreate', 'secret', 1, {})
                    with self.assertRaises(store.StationEfficiencyStoreError):
                        store._saved_record(payload)
                self.assertEqual(json.loads(output.getvalue())['error_type'], 'invalid_response')
                self.assertNotIn('PRIVATE', output.getvalue())

    def test_valid_delete_count_is_silent(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(store, 'urlopen', return_value=io.StringIO('{"data":2}')):
            self.assertEqual(store._request_post_json('https://example.invalid/api/t_efficiency_device_points:destroy', 'secret', 1), {'data': 2})
        self.assertEqual(output.getvalue(), '')

    def test_transport_failures_emit_only_safe_metadata(self):
        secret = 'DO-NOT-LOG-THIS'
        url = 'https://example.invalid/api/t_efficiency_bottleneck_events:list?token=' + secret
        for error, kind, status in [
            (HTTPError(url, 403, secret, {}, None), 'http_error', 403),
            (URLError(TimeoutError(secret)), 'timeout', None),
            (URLError(secret), 'network_error', None),
        ]:
            with self.subTest(kind=kind):
                output = io.StringIO()
                with redirect_stderr(output), patch.object(store, 'urlopen', side_effect=error):
                    with self.assertRaises(store.StationEfficiencyStoreError):
                        store._request_get_json(url, secret, 1)
                diagnostic = json.loads(output.getvalue())
                self.assertEqual(diagnostic['source'], 'm2_store_diagnostic')
                self.assertEqual(diagnostic['collection'], 't_efficiency_bottleneck_events')
                self.assertEqual(diagnostic['operation'], 'list')
                self.assertEqual(diagnostic['error_type'], kind)
                self.assertEqual(diagnostic['http_status'], status)
                self.assertNotIn(secret, output.getvalue())

    def test_post_response_error_is_reported_without_body(self):
        response = io.StringIO('{"errors":[{"message":"PRIVATE"}]}')
        output = io.StringIO()
        with redirect_stderr(output), patch.object(store, 'urlopen', return_value=response):
            payload = store._request_json(
                'https://example.invalid/api/t_efficiency_bottleneck_events:updateOrCreate',
                'PRIVATE', 1, {'evidence': 'PRIVATE'},
            )
        self.assertIn('errors', payload)
        self.assertEqual(json.loads(output.getvalue())['error_type'], 'response_error')
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_success_is_silent(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(store, 'urlopen', return_value=io.StringIO('{"data":[]}')):
            self.assertEqual(store._request_get_json('https://example.invalid/api/t_efficiency_points:list', 'secret', 1), {'data': []})
        self.assertEqual(output.getvalue(), '')
