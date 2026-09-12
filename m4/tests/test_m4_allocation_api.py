"""M4 publishes station plans; retired cabinet allocation endpoints stay closed."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4
from fastapi.testclient import TestClient
from m4.settings.api import create_app


class AllocationRemovalTests(TestCase):
    def test_retired_endpoints_are_unavailable_and_settings_remain(self):
        with TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp)/'settings.db')) as client:
            for station in ('station-1', 'station-2'):
                prefix = '/m4-api/stations/' + station + '/'
                self.assertEqual(client.get(prefix+'settings').status_code, 200)
                self.assertEqual(client.get(prefix+'allocations').status_code, 404)
                self.assertEqual(client.get(prefix+'allocation-result', params={
                    'allocation_id': str(uuid4())}).status_code, 404)
                self.assertEqual(client.post(prefix+'allocations', json={
                    'request_id':str(uuid4()), 'plan_run_id':str(uuid4())}).status_code, 404)
