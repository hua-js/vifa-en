"""Resident M1 API; request credentials remain isolated and sources stay fresh."""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
from typing import Any, Callable, cast
from urllib.parse import urlsplit

from m1 import dashboard_energy_api as api

REVISION = "m1-pv-controller-summary-v2"
LOGGER = logging.getLogger("vifa.m1")


@dataclass(frozen=True)
class RuntimeSettings:
    base_url: str
    energy_base_url: str
    growatt_url: str
    energy_token: str = field(repr=False)
    history_db: Path
    max_requests: int = 4

    def __post_init__(self) -> None:
        for url in (self.base_url, self.energy_base_url, self.growatt_url):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid M1 source URL")
        if not self.energy_token or any(character.isspace() for character in self.energy_token):
            raise ValueError("invalid M1 energy credential")
        if not 1 <= self.max_requests <= 8:
            raise ValueError("invalid M1 request capacity")


def load_settings() -> RuntimeSettings:
    project_path = Path(os.environ.get("M1_PROJECT_CONFIG", "/config/project.json"))
    credential_path = Path(os.environ.get("M1_ENERGY_CONFIG", "/app/.local/energy_efficiency_local_config.py"))
    project = json.loads(project_path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("m1_private_energy_configuration", credential_path)
    if spec is None or spec.loader is None:
        raise ValueError("M1 energy configuration is unavailable")
    credentials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credentials)
    sources = project["sources"]
    return RuntimeSettings(
        # Preserve the production launcher's explicit EMS override.
        base_url=os.environ.get("M1_BASE_URL", "https://ems.lvkpower.com/api"),
        energy_base_url=sources["m1_energy_base_url"].rstrip("/"),
        growatt_url=sources["m1_growatt_url"] + "?pageSize=100&sort%5B%5D=-timestamp&filter=%7B%7D",
        energy_token=credentials.EMU_TOKEN,
        history_db=Path(os.environ.get("M1_ALERT_HISTORY_DB", "/state/alert_history.sqlite3")),
        max_requests=int(os.environ.get("M1_MAX_REQUESTS", "4")),
    )


def configure_sources(settings: RuntimeSettings) -> None:
    """Apply instance-wide, administrator-owned settings once before serving."""
    api.BASE = settings.base_url.rstrip("/")
    api.ENERGY_SOURCE_BASE = settings.energy_base_url.rstrip("/")
    api.STATION_LOAD_URL = api.ENERGY_SOURCE_BASE + "/t_es_data:list"
    api.CABINET_POWER_URL = api.ENERGY_SOURCE_BASE + "/t_emu:list"
    api.GROWATT_URL = settings.growatt_url


class ResidentServer(ThreadingHTTPServer):
    # Drain accepted reads on shutdown; no schedulers or prediction workers.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address: tuple[str, int], settings: RuntimeSettings, collector: Callable[..., dict[str, Any]]):
        self.settings = settings
        self.collector = collector
        self.slots = threading.BoundedSemaphore(settings.max_requests)
        super().__init__(address, RequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        LOGGER.error("m1_http_request_failed")


class RequestHandler(BaseHTTPRequestHandler):
    @property
    def resident(self) -> ResidentServer:
        return cast(ResidentServer, self.server)

    protocol_version = "HTTP/1.1"
    server_version = "VIFA-M1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(120)

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request targets, credentials, cookies or business payloads.
        pass

    def reply(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route in {"/health", "/ready"}:
            self.reply(200, {"status": "ok", "revision": REVISION, "pid": os.getpid()})
            return
        if route != "/dashboard_energy_api":
            self.reply(404, {"status": "error", "message": "页面暂不可用。"})
            return
        headers = self.headers.get_all("Authorization", [])
        match = re.fullmatch(r"Bearer\s+([A-Za-z0-9._~-]{1,4096})", headers[0], re.IGNORECASE) if len(headers) == 1 else None
        if match is None:
            self.reply(401, {"status": "error", "message": "请先登录后再查看。"})
            return
        if not self.resident.slots.acquire(blocking=False):
            self.reply(503, {"status": "error", "message": "当前访问较多，请稍后重试。"})
            return
        try:
            result = self.resident.collector(
                match.group(1), self.resident.settings.energy_token,
                growatt_token=self.resident.settings.energy_token,
                history_db=self.resident.settings.history_db,
                use_connection_pool=True,
            )
        except api.SourceAuthorizationError:
            result = {"status": "error", "message": "登录状态已失效或无权访问，请重新打开页面。"}
        except Exception as error:
            LOGGER.warning("m1_source_read_failed kind=%s", type(error).__name__)
            result = {"status": "error", "message": "数据暂不可用，请稍后重试。"}
        finally:
            self.resident.slots.release()
        # Keep the public gateway's existing business-envelope convention.
        self.reply(200, result)

    def do_POST(self) -> None:
        self.reply(405, {"status": "error", "message": "操作暂不支持。"})


def make_server(
    address: tuple[str, int],
    settings: RuntimeSettings,
    *,
    collector: Callable[..., dict[str, Any]] = api.collect_dashboard,
) -> ResidentServer:
    return ResidentServer(address, settings, collector)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # SDK INFO/DEBUG messages include request URLs and query values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
        configure_sources(settings)
        host = os.environ.get("M1_LISTEN_HOST", "127.0.0.1")
        if host != "127.0.0.1":
            raise ValueError("M1 must bind to loopback")
        server = make_server((host, int(os.environ.get("M1_LISTEN_PORT", "8811"))), settings)
    except Exception as error:
        LOGGER.error("m1_startup_failed kind=%s", type(error).__name__)
        raise SystemExit(1) from None

    def shutdown(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, name="m1-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOGGER.info("m1_resident_started revision=%s", REVISION)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
