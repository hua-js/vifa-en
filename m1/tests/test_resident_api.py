import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from m1 import resident_api


class ResidentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.result = {"status": "ok", "data": {"marker": "real-collector-fixture"}}
        self.config = resident_api.RuntimeSettings(
            base_url="https://ems.example.invalid/api",
            energy_base_url="https://energy.example.invalid/api",
            growatt_url="https://growatt.example.invalid/api/list",
            energy_token="private-energy-fixture",
            history_db=Path(self.temp.name) / "history.sqlite3",
            max_requests=1,
        )

        def collect(token, energy_token, **kwargs):
            self.calls.append((token, energy_token, kwargs))
            return self.result

        self.server = resident_api.make_server(("127.0.0.1", 0), self.config, collector=collect)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def read(self, path, token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        request = urllib.request.Request(self.url + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_does_not_read_sources_or_write_history(self):
        status, body = self.read("/ready")
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"], resident_api.REVISION)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.config.history_db.exists())

    def test_no_user_token_has_no_default_fallback(self):
        status, body = self.read("/dashboard_energy_api")
        self.assertEqual(status, 401)
        self.assertEqual(body["status"], "error")
        self.assertEqual(self.calls, [])

    def test_repeated_reads_use_same_process_but_never_share_business_responses(self):
        for token in ("user-a-fixture", "user-b-fixture"):
            status, body = self.read("/dashboard_energy_api", token)
            self.assertEqual(status, 200)
            self.assertEqual(body, self.result)
        self.assertEqual([call[0] for call in self.calls], ["user-a-fixture", "user-b-fixture"])
        self.assertTrue(all(call[1] == "private-energy-fixture" for call in self.calls))
        self.assertTrue(all(call[2]["history_db"] == self.config.history_db for call in self.calls))
        self.assertNotIn("private-energy-fixture", repr(self.config))

    def test_busy_server_fails_closed_without_starting_another_read(self):
        self.server.slots.acquire()
        try:
            status, body = self.read("/dashboard_energy_api", "user-fixture")
        finally:
            self.server.slots.release()
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "error")
        self.assertEqual(self.calls, [])

    def test_transport_error_does_not_echo_credentials(self):
        def failure(*args, **kwargs):
            raise RuntimeError("private-energy-fixture")
        self.server.collector = failure
        status, body = self.read("/dashboard_energy_api", "user-fixture")
        self.assertEqual(status, 200)  # Preserve the established gateway business envelope.
        self.assertEqual(body["status"], "error")
        self.assertNotIn("private-energy-fixture", json.dumps(body))
        self.assertEqual(self.server.slots._value, 1)

    def test_bad_authorization_and_unknown_route_do_not_read_sources(self):
        self.assertEqual(self.read("/dashboard_energy_api", "invalid token")[0], 401)
        self.assertEqual(self.read("/missing", "user-fixture")[0], 404)
        self.assertEqual(self.calls, [])

    def test_credentials_are_not_accepted_from_internal_query_strings(self):
        self.assertEqual(self.read("/dashboard_energy_api?token=user-fixture")[0], 401)
        self.assertEqual(self.calls, [])
