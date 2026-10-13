import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from typing import Any, cast

from m1 import dashboard_energy_api as api


def topology_fixture():
    site = int(api.TARGET_SITE_ID)
    return {
        "site": {"data": [{"id": site, "status": "enabled", "name": "fixture-site"}]},
        "es": {"data": [
            {"id": 101, "fk_site": site, "sn": "fixture-ES01", "status": "enabled"},
            {"id": 102, "fk_site": site, "sn": "fixture-ES02", "status": "enabled"},
            {"id": 999, "fk_site": site + 1, "sn": "foreign-ES01"},
        ]},
        "en": {"data": [
            {"id": 201, "fk_es": 101, "sn": "load-one", "node_type": "load"},
            {"id": 202, "fk_es": 102, "sn": "load-two", "node_type": "load"},
            {"id": 999, "fk_es": 999, "sn": "foreign"},
        ]},
        "en_realtime": {"data": [
            {"fk_en": 201, "power": 20, "timestamp": "2026-10-12T12:00:00+08:00"},
            {"fk_en": 202, "power": 30, "timestamp": "2026-10-12T12:00:00+08:00"},
            {"fk_en": 999, "power": 999},
        ]},
    }


class TopologyConcurrencyTests(unittest.TestCase):
    def test_site_and_station_reads_overlap_before_dependent_queries(self):
        fixture = topology_fixture()
        started = set()
        lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()
        output = []
        failures = []
        calls = []

        def read(path, token):
            resource = path.split(":", 1)[0].strip("/")
            with lock:
                calls.append((resource, token))
                if resource in {"site", "es"}:
                    started.add(resource)
                    if len(started) == 2:
                        both_started.set()
            if resource in {"site", "es"}:
                if not release.wait(3):
                    raise AssertionError("topology fixture release timed out")
            else:
                self.assertTrue(release.is_set(), "dependent reads started before topology completed")
            return copy.deepcopy(fixture[resource])

        def run():
            try:
                output.append(api.fetch_raw_data("current-user-fixture"))
            except Exception as error:
                failures.append(error)

        with patch.object(api, "api", side_effect=read):
            worker = threading.Thread(target=run)
            worker.start()
            overlap = both_started.wait(1)
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(overlap, "site and es should run concurrently with the current user's credentials")
        self.assertEqual({token for _, token in calls}, {"current-user-fixture"})
        self.assertEqual([resource for resource, _ in calls[2:]], ["en", "en_realtime"])
        self.assertEqual([item["id"] for item in output[0]["data"]["es_list"]], [101, 102])
        self.assertEqual([item["id"] for item in output[0]["data"]["nodes"]], [201, 202])
        self.assertEqual([item["fk_en"] for item in output[0]["data"]["realtime"]], [201, 202])


def business_topology_fixture():
    fixture = topology_fixture()
    nodes = []
    for index, station in enumerate(api.TARGET_STATIONS):
        es_id = 101 + index
        base = 1000 + index * 100
        fixture["es"]["data"][index].update(installed_power=1000, power_storage=600)
        nodes.extend([
            {"id": base, "fk_es": es_id, "sn": station["code"], "node_type": "load", "is_aggregate": True},
            {"id": base + 1, "fk_es": es_id, "sn": "fixture-pv", "node_type": "pv_grid_ac", "is_aggregate": True},
            {"id": base + 2, "fk_es": es_id, "sn": "fixture-meter", "node_type": "meter", "parentId": base + 1},
            {"id": base + 3, "fk_es": es_id, "sn": "fixture-storage", "node_type": "ess", "is_aggregate": True},
        ])
        for offset, sn in enumerate(station["cabinet_sns"]):
            nodes.append({"id": base + 10 + offset, "fk_es": es_id, "sn": sn, "node_type": "ess", "parentId": base + 3})
    nodes.append({"id": 1300, "fk_es": 102, "sn": "fixture-grid", "node_type": "meter"})
    fixture["en"] = {"data": nodes + [{"id": 999, "fk_es": 999, "sn": "foreign"}]}
    fixture["en_realtime"] = {"data": [
        {"fk_en": node["id"], "power": 5, "soc": 50, "run_status": "standby",
         "timestamp": "2026-10-12T12:00:00+08:00"} for node in nodes
    ] + [{"fk_en": 999, "power": 999}]}
    return fixture


class FixtureSourceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.accepted = 0
        self.calls = []
        self.lock = threading.Lock()
        self.denied = {}
        self.set_cookie = False
        self.full_business = False
        self.connection_delay = 0.0
        self.response_delay = 0.0
        super().__init__(("127.0.0.1", 0), FixtureSourceHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            self.accepted += 1
        return connection, address


class FixtureSourceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def source(self) -> FixtureSourceServer:
        return cast(FixtureSourceServer, self.server)

    def setup(self):
        super().setup()
        if self.source.connection_delay:
            time.sleep(self.source.connection_delay)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self):
        resource = urlsplit(self.path).path.strip("/").split(":", 1)[0]
        authorization = self.headers.get("Authorization")
        with self.source.lock:
            self.source.calls.append({"resource": resource, "authorization": authorization,
                                      "cookie": self.headers.get("Cookie")})
        if self.source.response_delay:
            time.sleep(self.source.response_delay)
        fixture = business_topology_fixture() if self.source.full_business else topology_fixture()
        status = self.source.denied.get(resource, 200)
        if status != 200:
            value = {"error": "fixture denied"}
        elif resource in fixture:
            value = fixture[resource]
            if resource == "site":
                value["data"][0]["name"] = authorization
        elif resource == "t_es_data":
            sn = json.loads(parse_qs(urlsplit(self.path).query)["filter"][0])["es_sn"]["$eq"]
            value = {"data": [{"es_sn": sn, "load_power": 20, "timestamp": "2026-10-12T12:00:00+08:00"}]}
        elif resource == "t_emu":
            value = {"data": [
                {"f_es_sn": station["code"], "emu_sn": sn, "latest_power": 5,
                 "max_temp": 30, "last_time_iso": "2026-10-12T12:00:00+08:00"}
                for station in api.TARGET_STATIONS for sn in station["cabinet_sns"]
            ] + [{"f_es_sn": "ES02", "emu_sn": "emu27", "last_time_iso": "2026-10-12T12:00:00+08:00"}]}
        elif resource == "t_growall":
            rows = [{"sn": sn, "timestamp": "2026-10-12T12:00:00+08:00",
                     "a0": 1, "a1": 10, "a35": 9} for sn in api.GROWATT_INVERTER_SNS]
            rows.extend([{"sn": "foreign-inverter", "a0": 1, "a1": 100, "a35": 100},
                         {"sn": api.GROWATT_INVERTER_SNS[0], "timestamp": "2026-10-12T11:00:00+08:00",
                          "a0": 1, "a1": 10, "a35": 7}])
            value = {"data": rows if self.source.full_business else []}
        else:
            value = {"error": "fixture unknown route"}
            status = 404
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.source.set_cookie:
            self.send_header("Set-Cookie", "fixture_session=must-not-replay; Path=/")
        self.end_headers()
        self.wfile.write(body)


class ResidentTransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        from m1 import resident_api
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = FixtureSourceServer()
        self.source_thread = threading.Thread(target=self.source.serve_forever)
        self.source_thread.start()
        self.addCleanup(self.close_source)
        self.source_url = "http://127.0.0.1:%d" % self.source.server_address[1]
        changes = {
            "BASE": self.source_url,
            "STATION_LOAD_URL": self.source_url + "/t_es_data:list",
            "CABINET_POWER_URL": self.source_url + "/t_emu:list",
            "GROWATT_URL": self.source_url + "/t_growall:list?pageSize=100&sort%5B%5D=-timestamp&filter=%7B%7D",
        }
        for name, value in changes.items():
            patcher = patch.object(api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        settings = resident_api.RuntimeSettings(
            base_url=self.source_url, energy_base_url=self.source_url,
            growatt_url=self.source_url + "/t_growall:list",
            energy_token="energy-fixture", history_db=Path(self.temp.name) / "history.sqlite3",
            max_requests=2,
        )
        self.server = resident_api.make_server(("127.0.0.1", 0), settings)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_resident)
        self.url = "http://127.0.0.1:%d/dashboard_energy_api" % self.server.server_address[1]

    def close_resident(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def close_source(self):
        self.source.shutdown()
        self.source.server_close()
        self.source_thread.join(3)

    def read(self, token="current-user-fixture"):
        request = urllib.request.Request(self.url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_resident_collector_reuses_connections_without_reusing_business_responses(self):
        self.source.set_cookie = True
        first = self.read("user-a-fixture")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["data"]["sites"][0]["name"], "Bearer user-a-fixture")
        connections = self.source.accepted
        self.assertEqual(len(self.source.calls), 8)
        self.assertLess(connections, 8, "resident source reads should reuse request-scoped HTTP connections")
        second = self.read("user-b-fixture")
        self.assertEqual(second["data"]["sites"][0]["name"], "Bearer user-b-fixture")
        self.assertEqual(len(self.source.calls), 16)
        self.assertGreater(self.source.accepted, connections, "a new user request must get a fresh transport scope")
        for call in self.source.calls:
            self.assertIsNone(call["cookie"], "upstream session cookies must not be replayed")
            if call["resource"] not in {"site", "es", "en", "en_realtime"}:
                self.assertEqual(call["authorization"], "Bearer energy-fixture")

    def test_full_business_json_matches_pre_optimization_baseline(self):
        import datetime
        self.source.full_business = True
        expected = json.loads((Path(__file__).parent / "fixtures/dashboard_http_baseline.json").read_text())

        class FixedClock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 10, 12, 12, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        with patch.object(api, "datetime", FixedClock):
            result = self.read()
        self.assertEqual(result, expected["payload"])
        self.assertEqual(len(result["data"]["nodes"]), 17)
        self.assertEqual(len(result["data"]["pv_inverters"]), 9)
        self.assertEqual(result["data"]["alert_history"]["status"], "ok")
        self.assertTrue(self.server.settings.history_db.exists())

    def test_each_topology_auth_failure_blocks_private_sources_and_history(self):
        for resource in ("site", "es", "en", "en_realtime"):
            for status in (401, 403):
                with self.subTest(resource=resource, status=status):
                    self.source.denied = {resource: status}
                    self.source.calls.clear()
                    result = self.read()
                    self.assertEqual(result["status"], "error")
                    self.assertIn("登录状态已失效", result["message"])
                    self.assertTrue(all(call["resource"] in {"site", "es", "en", "en_realtime"}
                                        for call in self.source.calls))
                    self.assertTrue(all(call["authorization"] == "Bearer current-user-fixture"
                                        for call in self.source.calls))
                    self.assertFalse(self.server.settings.history_db.exists())

    def test_simultaneous_user_requests_never_mix_credentials_or_response_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        self.source.set_cookie = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.read, token) for token in ("user-a-fixture", "user-b-fixture")]
            output = [future.result(6) for future in futures]
        self.assertEqual([body["data"]["sites"][0]["name"] for body in output],
                         ["Bearer user-a-fixture", "Bearer user-b-fixture"])
        self.assertEqual(len(self.source.calls), 16)
        self.assertTrue(all(call["cookie"] is None for call in self.source.calls))
        auth_counts = {}
        for call in self.source.calls:
            auth = call["authorization"]
            auth_counts[auth] = auth_counts.get(auth, 0) + 1
        self.assertEqual(auth_counts, {"Bearer user-a-fixture": 4,
                                       "Bearer user-b-fixture": 4, "Bearer energy-fixture": 8})

    def test_pool_is_closed_and_context_restored_after_success_or_denial(self):
        from m1 import http_reader
        readers = []
        original = http_reader.RequestReader

        def create():
            reader = original()
            readers.append(reader)
            return reader

        with patch.object(http_reader, "RequestReader", side_effect=create):
            self.assertEqual(self.read()["status"], "ok")
            self.source.denied = {"site": 403}
            self.assertEqual(self.read()["status"], "error")
        self.assertEqual(len(readers), 2)
        self.assertTrue(all(reader.client.is_closed for reader in readers))
        self.assertIsNone(http_reader._active_reader.get())


class DirectCLICompatibilityTests(unittest.TestCase):
    def test_direct_script_help_does_not_require_package_installation(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, api.__file__, "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--history-db", result.stdout)


class TransportPolicyTests(unittest.TestCase):
    def test_https_upgrade_cannot_redirect_back_to_plain_http(self):
        import httpx
        from m1 import http_reader
        calls = []
        original_client = httpx.Client

        def handler(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(302, headers={"Location": "https://ems.fixture.invalid/final"})
            if len(calls) == 2:
                return httpx.Response(302, headers={"Location": "http://ems.fixture.invalid/unsafe"})
            return httpx.Response(200, json={"data": []})

        def create_client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(http_reader.httpx, "Client", side_effect=create_client):
            with http_reader.RequestReader() as reader:
                with self.assertRaisesRegex(ValueError, "重定向目标"):
                    reader.get_json("http://ems.fixture.invalid/start", "user-fixture")
        self.assertEqual(calls, ["http://ems.fixture.invalid/start", "https://ems.fixture.invalid/final"])

    def test_cross_origin_redirect_is_rejected_before_forwarding_authorization(self):
        import httpx
        from m1 import http_reader
        calls = []
        original_client = httpx.Client

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://other.fixture.invalid/data"})

        with patch.object(http_reader.httpx, "Client", side_effect=lambda **kwargs:
                          original_client(transport=httpx.MockTransport(handler), **kwargs)):
            with http_reader.RequestReader() as reader:
                with self.assertRaisesRegex(ValueError, "重定向目标"):
                    reader.get_json("https://ems.fixture.invalid/start", "user-fixture")
        self.assertEqual(calls, ["https://ems.fixture.invalid/start"])

    def test_same_origin_redirect_retains_auth_but_never_replays_set_cookie(self):
        import httpx
        from m1 import http_reader
        calls = []
        original_client = httpx.Client

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(302, headers={"Location": "/final", "Set-Cookie": "session=fixture; Path=/"})
            return httpx.Response(200, json={"data": [1]})

        with patch.object(http_reader.httpx, "Client", side_effect=lambda **kwargs:
                          original_client(transport=httpx.MockTransport(handler), **kwargs)):
            with http_reader.RequestReader() as reader:
                self.assertEqual(reader.get_json("https://ems.fixture.invalid/start", "user-fixture"), {"data": [1]})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(request.headers["Authorization"] == "Bearer user-fixture" for request in calls))
        self.assertTrue(all("Cookie" not in request.headers for request in calls))

    def test_empty_sdk_timeout_is_a_nonempty_source_failure(self):
        import httpx
        from m1 import http_reader
        original_client = httpx.Client

        def handler(request):
            raise httpx.ReadTimeout("", request=request)

        with patch.object(http_reader.httpx, "Client", side_effect=lambda **kwargs:
                          original_client(transport=httpx.MockTransport(handler), **kwargs)):
            with http_reader.RequestReader() as reader:
                with self.assertRaisesRegex(TimeoutError, "请求超时"):
                    reader.get_json("https://ems.fixture.invalid/start", "user-fixture")


class LoggingIsolationTests(unittest.TestCase):
    def test_resident_logging_disables_library_request_url_logging(self):
        import logging
        from m1 import resident_api
        names = ("httpx", "httpcore")
        old_levels = {name: logging.getLogger(name).level for name in names}
        self.addCleanup(lambda: [logging.getLogger(name).setLevel(level) for name, level in old_levels.items()])
        with patch.object(resident_api.logging, "basicConfig"):
            resident_api.configure_logging()
        for name in names:
            self.assertFalse(logging.getLogger(name).isEnabledFor(logging.INFO))
