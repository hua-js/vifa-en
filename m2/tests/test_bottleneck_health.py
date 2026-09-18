import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from m2.scripts import check_bottleneck_health as health
from m2.tests.test_station_efficiency_job import RULE


class BottleneckHealthTests(unittest.TestCase):
    def test_missing_outbox_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            self.assertEqual(health.read_pending(path, 'ES02')['status'], 'missing')
            self.assertFalse(path.exists())

    def test_outbox_check_preserves_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.sqlite3'
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE event_outbox (station_id TEXT, payload_json TEXT)')
            connection.execute('INSERT INTO event_outbox VALUES (?, ?)', ('ES02', '{}'))
            connection.commit()
            connection.close()
            before = path.read_bytes()
            result = health.read_pending(path, 'ES02')
            self.assertEqual(result['pending'], 1)
            self.assertEqual(result['invalid_payloads'], 1)
            self.assertEqual(path.read_bytes(), before)

    def test_health_uses_get_only_and_does_not_expose_credentials(self):
        requests = []

        def open_request(request, timeout):
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            requests.append(request)
            response = io.StringIO('{"data":[],"meta":{"totalPage":0}}')
            response.status = 200
            return response

        config = {
            'nocobase_base_url': 'https://private.invalid',
            'nocobase_token': 'PRIVATE', 'timeout_seconds': 1,
            'event_outbox_path': '/does-not-exist/queue.sqlite3',
            'bottleneck_rules': {'ES02': RULE},
        }
        output = io.StringIO()
        with redirect_stdout(output), patch.object(health, 'urlopen', side_effect=open_request):
            health.inspect_station(config, 'ES02')
        self.assertEqual(len(requests), 3)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn('private.invalid', output.getvalue())
        last = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(last['stage'], 'evaluation_only')
        self.assertEqual(last['written'], 0)
