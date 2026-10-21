"""Resident, loopback-only M2 reads. No minute jobs, cleanup or scheduler."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from m2.read_transport import ReadTransport

REVISION = "m2-resident-split-reads-v1"
LOGGER = logging.getLogger("vifa.m2")
SPEC = importlib.util.spec_from_file_location("m2_read_entrypoint", Path(__file__).with_name("energy-efficiency-api.py"))
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)

# Only the dashboard projection is reduced. Minute-job reads keep their original schema.
DAY_FIELDS = (
    "station_id", "data_time", "pv_storage_efficiency", "storage_load_efficiency", "pv_load_efficiency",
    "pv_storage_input_kw", "pv_storage_output_kw", "storage_load_input_kw", "storage_load_output_kw",
    "pv_load_input_kw", "pv_load_output_kw",
)
READ_ARGUMENTS = {
    "dashboard": (), "realtime": (), "history": ("date",), "curve": ("date",),
    "events": ("start_date", "end_date"),
}


def query_arguments(query):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=8)
    if any(len(value) != 1 for value in values.values()):
        raise ValueError("duplicate argument")
    operation = values.get("operation", ["dashboard"])[0]
    if operation not in READ_ARGUMENTS:
        raise ValueError("read operation required")
    fields = READ_ARGUMENTS[operation]
    if set(values) - {"operation", "station_id", *fields} or any(field not in values for field in ("station_id", *fields)):
        raise ValueError("invalid argument")
    args = [operation, values["station_id"][0], *(values[field][0] for field in fields)]
    api.parse_request(args)
    return args


class ResidentServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, environ=None, *, executor=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.executor = executor or api.execute
        self.slots = threading.BoundedSemaphore(4)
        self.transport = ReadTransport()
        try:
            super().__init__(address, RequestHandler)
        except Exception:
            self.transport.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.transport.close()

    def handle_error(self, request, client_address):
        LOGGER.warning("m2_http_request_failed")

    def read(self, arguments):
        def reader(error_type):
            def read_json(*args):
                try:
                    return self.transport.get_json(*args)
                except (URLError, TimeoutError, OSError, TypeError, ValueError):
                    raise error_type("数据源读取失败") from None
            return read_json

        source_reader = reader(api.StationEnergyDataError)
        store_reader = reader(api.StationEfficiencyStoreError)
        return self.executor(
            arguments, self.environ,
            fetch_station=lambda station, config: api.fetch_station_source_record(station, config, request_json=source_reader),
            fetch_points=lambda station, start, end, config: api.fetch_minute_points(
                station, start, end, config, request_json=store_reader, fields=DAY_FIELDS,
            ),
            fetch_events=lambda station, start, end, config: api.fetch_dashboard_events(
                station, start, end, config, request_json=store_reader,
            ),
        )


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VIFA-M2"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(40)

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path in {"/health", "/ready"} and not route.query:
            self.reply(200, {"status": "ok", "revision": REVISION})
            return
        if route.path != "/energy-efficiency-api":
            self.reply(404, {"status": "error", "message": "页面暂不可用。"})
            return
        try:
            arguments = query_arguments(route.query)
        except (ValueError, api.EntrypointError):
            self.reply(400, api._error_payload("invalid_arguments", "查询参数不正确。"))
            return
        if not self.server.slots.acquire(blocking=False):
            self.reply(503, api._error_payload("busy", "当前访问较多，请稍后重试。"))
            return
        try:
            payload, code = self.server.read(arguments)
        except Exception:
            payload, code = api._error_payload("internal_error", "数据暂不可用，请稍后重试。"), 1
        finally:
            self.server.slots.release()
        self.reply(200 if code == 0 else 400 if code == 2 else 502, payload)

    def do_POST(self):
        self.reply(405, api._error_payload("invalid_arguments", "操作暂不支持。"))


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if os.environ.get("M2_LISTEN_HOST", "127.0.0.1") != "127.0.0.1":
            raise ValueError("M2 must bind to loopback")
        api.load_runtime_config(os.environ, require_nocobase=True)
        server = ResidentServer(("127.0.0.1", int(os.environ.get("M2_LISTEN_PORT", "8812"))))
    except Exception:
        LOGGER.error("m2_read_api_startup_failed")
        raise SystemExit(1) from None

    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOGGER.info("m2_read_api_started revision=%s", REVISION)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
