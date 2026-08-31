"""Static deployment contracts for the isolated M3 systemd/Node-RED boundary."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import json


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "m3" / "deploy"
GUIDE = ROOT / "m3" / "部署说明.md"
RUNTIME_LOCK = ROOT / "m3" / "requirements.lock.txt"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "compose.yaml"
ENTRYPOINT = DEPLOY / "container-entrypoint.sh"
FLOW = ROOT / "m3" / "node_red" / "m3_dashboard_exec_flow.json"
PAGE_FLOW = ROOT / "m3" / "node_red" / "m3_dashboard_page_flow.json"
NOCOBASE_BLOCK = ROOT / "m3" / "nocobase" / "m3_forecast_iframe_block.js"
PRODUCTION_FLOW = ROOT / "m3" / "node_red" / "m3_production_gateway_flow.json"
PRODUCTION_BLOCK = (
    ROOT / "m3" / "nocobase" / "m3_forecast_iframe_block.production.js"
)
NOCOBASE_MANIFEST = (
    ROOT / "m3" / "nocobase" / "m3_nocobase_iframe_manifest.json"
)
ARCHITECTURE = ROOT / "m3" / "部署架构流程图.md"
RUNBOOK = ROOT / "m3" / "AMD64三域Docker部署手册.md"
HOST_ENTRYPOINT = DEPLOY / "host-entrypoint.sh"
HOST_WORKER_UNIT = DEPLOY / "vifa-m3-worker.service"
HOST_DASHBOARD_UNIT = DEPLOY / "vifa-m3-dashboard.service"
M3_HTML = ROOT / "m3" / "node_red" / "m3_production_gateway_page.html"
FLOW_SYNC = ROOT / "m3" / "node_red" / "sync_production_gateway_flow.py"


CORE_NODE_RED_TYPES = {
    "tab",
    "group",
    "http in",
    "http response",
    "http request",
    "function",
    "template",
    "exec",
    "join",
    "inject",
    "catch",
    "comment",
}


def _environment_keys(path: Path) -> set[str]:
    keys = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keys.add(line.split("=", 1)[0])
    return keys


def _systemd_directives(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise AssertionError(f"invalid systemd directive in {path}: {raw}")
        key, value = line.split("=", 1)
        sections[section].setdefault(key, []).append(value)
    return sections


class DeploymentArtifactTests(unittest.TestCase):
    def test_runtime_requirements_are_hash_locked_and_match_project_pins(self):
        content = RUNTIME_LOCK.read_text(encoding="utf-8")
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertIn("--hash=sha256:", content)
        for requirement in project["dependencies"]:
            self.assertIn(requirement, content)

    def test_root_deployment_guide_matches_the_confirmed_architecture(self):
        guide = GUIDE.read_text(encoding="utf-8")
        required = (
            "/userdata/holo/pyfiles/vifa-m3",
            "AMD64",
            "/userdata/holo/pyfiles/vifa-m3/run/worker.sock",
            "/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock",
            "https://vifa.hlszh.com",
            "https://ems.lvkpower.com",
            "https://opdash.lvkpower.com",
            "普通 iframe",
            "dashboard.sock",
            "不重启 Node-RED",
        )
        for marker in required:
            self.assertIn(marker, guide, marker)
        for obsolete in (
            "python3.12 -m venv",
            ".venv/bin/python",
            "systemctl enable --now vifa-m3.service",
            "/etc/systemd/system/node-red.service.d",
            "/etc/vifa-m3/nodered.env",
            "systemctl restart nodered.service",
        ):
            self.assertNotIn(obsolete, guide)
        self.assertNotIn("postMessage", guide)
        self.assertNotIn("Authorization: Bearer eyJ", guide)
        self.assertNotRegex(guide, re.compile(r"\bpsql\b|postgresql://", re.I))
        self.assertNotIn(
            "M3_NOCOBASE_PAGE_ORIGIN=https://vifa.hlszh.com",
            guide,
        )

    def test_manual_deployment_runbook_is_production_only_and_executable(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        required = (
            "x86_64",
            "linux/amd64",
            "docker load -i",
            "docker image inspect",
            "docker compose up -d --no-build",
            "Deploy Modified Flows",
            "https://opdash.lvkpower.com/ett",
            "https://vifa.hlszh.com/api/t_es_data:list",
            "m3_production_gateway_flow.json",
            "M3_AUTH_MODE=server_token",
            "公开只读",
        )
        for marker in required:
            self.assertIn(marker, runbook, marker)
        self.assertNotIn("docker compose down", runbook)
        self.assertNotIn("systemctl restart nodered.service", runbook)
        self.assertNotIn("测试环境终端", runbook)
        self.assertNotIn("192.168.1.53", runbook)
        self.assertNotIn("linux/arm64", runbook)
        self.assertNotIn(
            "M3_NOCOBASE_PAGE_ORIGIN=https://vifa.hlszh.com",
            runbook,
        )

    def test_amd64_virtualenv_units_run_two_isolated_uds_services(self):
        self.assertFalse((DEPLOY / "vifa-m3.service").exists())
        self.assertTrue(os.access(HOST_ENTRYPOINT, os.X_OK))

        entrypoint = HOST_ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", entrypoint)
        self.assertIn(".venv/bin/python", entrypoint)
        self.assertIn("m3_worker.main:app", entrypoint)
        self.assertIn("m3_worker.persisted_dashboard_app:app", entrypoint)
        self.assertIn("/userdata/holo/pyfiles/vifa-m3/run/worker.sock", entrypoint)
        self.assertIn("/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock", entrypoint)
        self.assertIn("--workers 1", entrypoint)
        self.assertIn('--uds "$socket_path"', entrypoint)
        self.assertIn('test -S "$socket_path"', entrypoint)
        self.assertNotIn("rm -rf", entrypoint)
        self.assertNotIn("--host", entrypoint)
        self.assertNotIn("--port", entrypoint)

        worker = _systemd_directives(HOST_WORKER_UNIT)
        dashboard = _systemd_directives(HOST_DASHBOARD_UNIT)
        for unit, mode, environment_file in (
            (worker, "worker", "/etc/vifa-m3/m3.env"),
            (dashboard, "dashboard", "/etc/vifa-m3/dashboard.env"),
        ):
            service = unit["Service"]
            self.assertEqual(service["User"], ["vifa-m3"])
            self.assertEqual(service["Group"], ["vifa-m3"])
            self.assertEqual(service["EnvironmentFile"], [environment_file])
            self.assertEqual(
                service["ExecStart"],
                [
                    "/userdata/holo/pyfiles/vifa-m3/m3/deploy/"
                    f"host-entrypoint.sh {mode}"
                ],
            )
            self.assertEqual(service["Restart"], ["on-failure"])
            self.assertEqual(service["NoNewPrivileges"], ["true"])
            self.assertEqual(service["PrivateTmp"], ["true"])
            self.assertEqual(service["ProtectSystem"], ["strict"])
            self.assertEqual(service["ProtectHome"], ["true"])
            self.assertEqual(service["CapabilityBoundingSet"], [""])
            self.assertEqual(service["UMask"], ["0007"])
            writable = " ".join(service["ReadWritePaths"])
            self.assertIn("/userdata/holo/pyfiles/vifa-m3/run", writable)
            self.assertIn("/userdata/holo/pyfiles/vifa-m3/cache", writable)
            rendered = HOST_WORKER_UNIT.read_text(encoding="utf-8") + HOST_DASHBOARD_UNIT.read_text(encoding="utf-8")
            self.assertNotIn("docker", rendered.lower())
            self.assertNotIn("--host", rendered)
            self.assertNotIn("--port", rendered)

    def test_image_uses_python_312_hash_lock_and_non_root_runtime(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        ignored = set(DOCKERIGNORE.read_text(encoding="utf-8").splitlines())

        self.assertIn("FROM python:3.12-slim-bookworm\n", dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes", dockerfile)
        self.assertIn("m3/requirements.lock.txt", dockerfile)
        self.assertIn(
            "COPY --chown=10001:10001 pyproject.toml uv.lock /app/",
            dockerfile,
        )
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/vifa-m3-entrypoint"]', dockerfile)
        self.assertNotRegex(dockerfile, re.compile(r"COPY\s+\.\s"))
        for forbidden in ("密钥.txt", ".env", "raw-source.token", "Bearer "):
            self.assertNotIn(forbidden, dockerfile)
        run_instructions = [
            line for line in dockerfile.splitlines() if line.startswith("RUN ")
        ]
        self.assertGreaterEqual(len(run_instructions), 2)
        self.assertTrue(all(
            line.startswith("RUN --mount=type=tmpfs,target=/dev/mqueue ")
            for line in run_instructions
        ))
        for required in (
            ".git",
            ".venv",
            "__pycache__",
            "*.env",
            "*.token",
            "m3_worker/密钥.txt",
            "tests",
            "vifa",
        ):
            self.assertIn(required, ignored)

    def test_compose_renders_two_hardened_services_without_tcp_or_docker_socket(self):
        if shutil.which("docker") is None:
            self.skipTest("Docker Compose is unavailable")
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE), "config",
                "--no-env-resolution", "--format", "json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        services = json.loads(result.stdout)["services"]

        self.assertEqual(set(services), {"vifa-m3-worker", "vifa-m3-dashboard"})
        worker = services["vifa-m3-worker"]
        dashboard = services["vifa-m3-dashboard"]
        self.assertEqual(worker["image"], "vifa-m3:0.1.0")
        self.assertEqual(dashboard["image"], worker["image"])
        self.assertEqual(worker["build"]["network"], "host")
        self.assertEqual(dashboard["build"]["network"], "host")
        self.assertEqual(worker["command"], ["worker"])
        self.assertEqual(dashboard["command"], ["dashboard"])
        for service in (worker, dashboard):
            self.assertNotIn("ports", service)
            self.assertEqual(service["user"], "10001:10001")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            self.assertEqual(service["network_mode"], "host")
            self.assertNotIn("extra_hosts", service)
            self.assertTrue(any(
                value.startswith("/dev/mqueue:") for value in service["tmpfs"]
            ))
            rendered = json.dumps(service, ensure_ascii=False)
            self.assertNotIn("/var/run/docker.sock", rendered)
            self.assertIn("/run/vifa-m3", rendered)
            self.assertIn("/run/secrets/raw-source.token", rendered)
            self.assertIn("healthcheck", service)
        self.assertIn("/etc/vifa-m3/m3.env", json.dumps(worker))
        self.assertNotIn("dashboard.env", json.dumps(worker))
        self.assertIn("/etc/vifa-m3/dashboard.env", json.dumps(dashboard))
        self.assertNotIn("m3.env", json.dumps(dashboard))

    def test_entrypoint_owns_only_its_selected_socket_and_one_uvicorn_worker(self):
        script = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertTrue(os.access(ENTRYPOINT, os.X_OK))
        self.assertIn("set -euo pipefail", script)
        self.assertIn('worker.sock', script)
        self.assertIn('dashboard.sock', script)
        self.assertIn("m3_worker.main:app", script)
        self.assertIn("m3_worker.persisted_dashboard_app:app", script)
        self.assertIn("--workers 1", script)
        self.assertIn('--uds "$socket_path"', script)
        self.assertIn('test -S "$socket_path"', script)
        self.assertNotIn("rm -rf", script)
        for forbidden in ("--reload", "--host", "--port"):
            self.assertNotIn(forbidden, script)

    def test_node_red_public_config_is_flow_scoped_without_systemd_drop_in(self):
        self.assertFalse((DEPLOY / "node-red-vifa-m3-dashboard.conf").exists())
        self.assertFalse((DEPLOY / "nodered.env.example").exists())

        script = (DEPLOY / "prepare-server.sh").read_text(encoding="utf-8")
        self.assertNotIn("nodered.service.d", script)
        self.assertNotIn("nodered.env", script)
        self.assertNotIn("systemctl", script)

    def test_production_import_package_has_exact_domains_routes_and_public_config(self):
        self.assertTrue(PRODUCTION_FLOW.exists(), str(PRODUCTION_FLOW))
        self.assertTrue(NOCOBASE_MANIFEST.exists(), str(NOCOBASE_MANIFEST))
        self.assertTrue(ARCHITECTURE.exists(), str(ARCHITECTURE))

        flow = json.loads(PRODUCTION_FLOW.read_text(encoding="utf-8"))
        manifest = json.loads(NOCOBASE_MANIFEST.read_text(encoding="utf-8"))
        tabs = [node for node in flow if node.get("type") == "tab"]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].get("label"), "M3 负载预测")
        self.assertFalse(tabs[0].get("disabled"))
        flow_env = {
            item["name"]: item["value"] for item in tabs[0].get("env", [])
        }
        self.assertEqual(
            flow_env,
            {
                "M3_AUTH_MODE": "server_token",
                "M3_AUTH_BASE_URL": "https://ems.lvkpower.com",
                "M3_NOCOBASE_PAGE_ORIGIN": "https://ems.lvkpower.com",
                "M3_STATIONS_JSON": (
                    '[{"station_id":"ES01","station_key":"station_1",'
                    '"station_name":"1# 电站"},{"station_id":"ES02",'
                    '"station_key":"station_2","station_name":"2# 电站"}]'
                ),
            },
        )

        self.assertTrue({node["type"] for node in flow} <= CORE_NODE_RED_TYPES)
        routes = sorted(
            (node["url"], node["method"])
            for node in flow
            if node.get("type") == "http in" and not node.get("d", False)
        )
        self.assertEqual(
            routes,
            [
                ("/energy-forecast-api", "get"),
                ("/energy-forecast-api/custom-performance/:station_key", "get"),
                ("/energy-forecast-api/custom-runs/:run_id", "get"),
                ("/energy-forecast-api/custom-runs/:run_id/result", "get"),
                ("/energy-forecast-api/custom-runs/:station_key", "post"),
                ("/ett", "get"),
            ],
        )

        public_gate = next(
            node for node in flow if node.get("id") == "m3_prod_auth_prepare"
        )
        self.assertEqual(public_gate["outputs"], 3)
        self.assertEqual(
            public_gate["wires"][2], ["m3_prod_dashboard_exec"]
        )

        self.assertEqual(manifest["kind"], "vifa-m3-nocobase-iframe-manifest")
        self.assertEqual(manifest["version"], 3)
        self.assertFalse(manifest["nocobase_native_import"]["importable"])
        self.assertEqual(
            manifest["target"]["nocobase_origin"],
            "https://ems.lvkpower.com",
        )
        self.assertEqual(
            manifest["target"]["node_red_origin"],
            "https://opdash.lvkpower.com",
        )
        self.assertEqual(
            manifest["target"]["data_origin"],
            "https://vifa.hlszh.com",
        )
        self.assertEqual(
            manifest["target"]["iframe_url"],
            "https://opdash.lvkpower.com/ett",
        )

        self.assertEqual(manifest["block"]["type"], "iframe_html")
        self.assertEqual(
            manifest["security"]["token_transport"],
            "not exposed to browser",
        )
        rendered = "\n".join(
            (
                json.dumps(flow, ensure_ascii=False),
                json.dumps(manifest, ensure_ascii=False),
            )
        )
        for forbidden in (
            "Authorization: Bearer eyJ",
            "M3_NOCOBASE_API_KEY",
            "M3_DASHBOARD_NOCOBASE_API_KEY",
            "M3_NOCOBASE_IFRAME_TOKEN",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotRegex(rendered, re.compile(r"eyJ[a-zA-Z0-9_-]+\."))

    def test_production_flow_embeds_current_dashboard_html(self):
        flow = json.loads(PRODUCTION_FLOW.read_text(encoding="utf-8"))
        page = next(
            node for node in flow if node.get("id") == "m3_prod_page_template"
        )
        html = M3_HTML.read_text(encoding="utf-8")
        marker = "  <script>\n    (() => {"
        injection = (
            "  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "
            "{{{m3DashboardAuthModeJson}}}; "
            "window.__M3_NOCOBASE_PARENT_ORIGIN__ = "
            "{{{m3NocobaseParentOriginJson}}};</script>\n"
        )
        self.assertEqual(html.count(marker), 1)
        self.assertEqual(html.count("{{{m3DashboardAuthModeJson}}}"), 0)
        self.assertEqual(html.count("{{{m3NocobaseParentOriginJson}}}"), 0)
        self.assertEqual(page["template"], html.replace(marker, injection + marker))
        self.assertEqual(page["template"].count(injection), 1)
        self.assertEqual(page["template"].count("{{{m3DashboardAuthModeJson}}}"), 1)
        self.assertEqual(page["template"].count("{{{m3NocobaseParentOriginJson}}}"), 1)

    def test_production_page_describes_weekly_load_evidence(self):
        html = M3_HTML.read_text(encoding="utf-8")

        for label in (
            "负载周期：7 天",
            "SOC 后处理：最后真实状态锚定",
            "weekly_load_v1",
            "compare-model-label",
            "compare-baseline-label",
            "baseline-note-label",
        ):
            self.assertIn(label, html)
        self.assertIn(
            '<span class="selection-basis">模型选择依据：留出周 WAPE → MAE → 固定模型顺序</span>',
            html,
        )
        self.assertNotIn(
            '<span class="selection-basis">模型选择依据：留出周 WAPE → MAE → MAPE</span>',
            html,
        )

    def test_production_page_rejects_legacy_custom_runs(self):
        html = M3_HTML.read_text(encoding="utf-8")

        self.assertIn("任务版本已失效，请重新预测", html)
        self.assertNotIn("旧任务无同策略可比汇总", html)
        self.assertNotIn("旧版日周期策略", html)
        self.assertNotIn("renderLegacyCustomPolicy", html)

    def test_production_flow_sync_reports_and_repairs_drift(self):
        python = sys.executable
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            page = temp_root / "page.html"
            flow = temp_root / "flow.json"
            shutil.copyfile(M3_HTML, page)
            shutil.copyfile(PRODUCTION_FLOW, flow)
            command = [python, str(FLOW_SYNC), "--page", str(page), "--flow", str(flow)]

            clean = subprocess.run(command + ["--check"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            page.write_text(page.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            stale = subprocess.run(command + ["--check"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(stale.returncode, 1)
            repaired = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(
                subprocess.run(command + ["--check"], cwd=ROOT, capture_output=True, text=True).returncode,
                0,
            )

    def test_iframe_auth_uses_current_user_and_exact_origin_nonce_handshake(self):
        html = M3_HTML.read_text(encoding="utf-8")
        block = NOCOBASE_BLOCK.read_text(encoding="utf-8")
        page_nodes = json.loads(PAGE_FLOW.read_text(encoding="utf-8"))
        page_flow = json.dumps(page_nodes, ensure_ascii=False)

        self.assertIn("window.__M3_NOCOBASE_PARENT_ORIGIN__", html)
        self.assertIn("vifa-m3-auth-ready", html)
        self.assertIn("vifa-m3-auth-token", html)
        self.assertIn("event.source !== window.parent", html)
        self.assertIn("event.origin !== parentOrigin", html)
        self.assertIn("crypto.getRandomValues", html)
        self.assertNotIn("window.__NOCOBASE_API_TOKEN__", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn('postMessage(message, "*")', html)

        self.assertIn('ctx.getVar("ctx.token")', block)
        self.assertIn("event.source !== iframe.contentWindow", block)
        self.assertIn("event.origin !== iframeOrigin", block)
        self.assertIn("vifa-m3-auth-ready", block)
        self.assertIn("vifa-m3-auth-token", block)
        self.assertIn("allow-scripts allow-same-origin", block)
        self.assertIn("no-referrer", block)
        self.assertNotIn("localStorage", block)
        self.assertNotIn("sessionStorage", block)
        self.assertNotIn("?token=", block)
        self.assertNotRegex(block, re.compile(r"postMessage\([^\n]+,\s*[\"']\*[\"']"))

        self.assertIn("M3_NOCOBASE_PAGE_ORIGIN", page_flow)
        self.assertIn("Cache-Control", page_flow)
        self.assertIn("no-store", page_flow)
        page_gate = next(
            node for node in page_nodes if node["id"] == "m3_dashboard_page_origin"
        )
        self.assertIn("originPattern", page_gate["func"])
        self.assertNotIn("new URL", page_gate["func"])
        self.assertNotIn("M3_NOCOBASE_IFRAME_TOKEN", page_flow)
        self.assertNotIn("__NOCOBASE_API_TOKEN__", page_flow)
        self.assertNotRegex(page_flow, re.compile(r"eyJ[a-zA-Z0-9_-]+\."))

    def test_production_iframe_block_keeps_the_tested_handshake_contract(self):
        self.assertTrue(PRODUCTION_BLOCK.exists(), str(PRODUCTION_BLOCK))
        block = PRODUCTION_BLOCK.read_text(encoding="utf-8")

        self.assertIn('ctx.getVar("ctx.token")', block)
        self.assertIn("event.source !== iframe.contentWindow", block)
        self.assertIn("event.origin !== iframeOrigin", block)
        self.assertIn("vifa-m3-auth-ready", block)
        self.assertIn("vifa-m3-auth-token", block)
        self.assertIn("allow-scripts allow-same-origin", block)
        self.assertIn("no-referrer", block)
        self.assertNotIn("localStorage", block)
        self.assertNotIn("sessionStorage", block)
        self.assertNotIn("?token=", block)
        self.assertNotRegex(
            block,
            re.compile(r"postMessage\([^\n]+,\s*[\"']\*[\"']"),
        )

    def test_node_red_dashboard_exec_calls_only_the_fixed_host_dashboard_socket(self):
        nodes = json.loads(FLOW.read_text(encoding="utf-8"))
        by_id = {node["id"]: node for node in nodes}
        command = by_id["m3_dashboard_exec"]["command"]

        self.assertEqual(
            command,
            "/usr/bin/curl --fail --silent --show-error --connect-timeout 2 "
            "--max-time 30 --unix-socket "
            "/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock "
            "http://localhost/dashboard",
        )
        self.assertFalse(by_id["m3_dashboard_exec"]["addpay"])
        self.assertEqual(by_id["m3_dashboard_exec"]["append"], "")
        self.assertEqual(by_id["m3_dashboard_exec"]["timer"], "35")
        for forbidden in ("python", "docker", "m3-forecast-api.py"):
            self.assertNotIn(forbidden, command.lower())

        mapper = by_id["m3_dashboard_response_map"]["func"]
        self.assertIn("parts.rc.code === 28", mapper)
        self.assertIn("504", mapper)
        self.assertIn("502", mapper)
        self.assertNotIn("statusByExit", mapper)

        health = by_id["m3_worker_health_exec"]["command"]
        self.assertIn(
            "--unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock",
            health,
        )
        self.assertNotIn("--unix-socket /run/vifa-m3/worker.sock", health)

    def test_node_red_dashboard_exec_does_not_wait_for_empty_stderr(self):
        nodes = json.loads(FLOW.read_text(encoding="utf-8"))
        by_id = {node["id"]: node for node in nodes}

        self.assertEqual(
            by_id["m3_dashboard_exec"]["wires"],
            [
                ["m3_dashboard_stdout"],
                ["m3_dashboard_stderr"],
                ["m3_dashboard_rc"],
            ],
        )
        self.assertEqual(by_id["m3_dashboard_stderr"]["wires"], [])
        self.assertEqual(by_id["m3_dashboard_join"]["count"], "2")
        self.assertEqual(
            by_id["m3_dashboard_join"]["name"],
            "join stdout and exit code",
        )

    def test_worker_and_dashboard_credentials_are_separate(self):
        worker_keys = _environment_keys(DEPLOY / "m3.env.example")
        dashboard_keys = _environment_keys(DEPLOY / "dashboard.env.example")

        self.assertEqual(worker_keys, {
            "M3_STATIONS_JSON",
            "M3_RAW_SOURCE_URL",
            "M3_RAW_SOURCE_API_TOKEN_FILE",
            "M3_SOURCE_BASE_URL",
            "M3_SOURCE_API_TOKEN",
            "M3_NOCOBASE_BASE_URL",
            "M3_NOCOBASE_API_KEY",
            "M3_ADMIN_API_TOKEN",
            "M3_ACCEPTANCE_ENABLED",
            "M3_TIMEZONE",
        })
        self.assertEqual(dashboard_keys, {
            "M3_STATIONS_JSON",
            "M3_RAW_SOURCE_URL",
            "M3_RAW_SOURCE_API_TOKEN_FILE",
            "M3_NOCOBASE_BASE_URL",
            "M3_DASHBOARD_NOCOBASE_API_KEY",
            "M3_TIMEZONE",
        })
        self.assertNotIn("M3_NOCOBASE_API_KEY", dashboard_keys)
        self.assertNotIn("M3_ADMIN_API_TOKEN", dashboard_keys)
        for path in (DEPLOY / "m3.env.example", DEPLOY / "dashboard.env.example"):
            content = path.read_text(encoding="utf-8")
            self.assertIn(
                "M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/raw-source.token",
                content,
            )
            self.assertIn("REPLACE_", content)
            self.assertNotIn("eyJhbGci", content)

    def test_prepare_script_is_idempotent_and_never_mutates_m2_or_shared_root(self):
        path = DEPLOY / "prepare-server.sh"
        script = path.read_text(encoding="utf-8")

        self.assertTrue(os.access(path, os.X_OK))
        self.assertIn("set -euo pipefail", script)
        self.assertIn("/userdata/holo/pyfiles/vifa-m3", script)
        self.assertIn("/userdata/holo/pyfiles/vifa-m3/run", script)
        self.assertIn("/etc/vifa-m3", script)
        self.assertNotIn("/etc/systemd/system/nodered.service.d", script)
        self.assertNotIn("nodered.env", script)
        self.assertNotIn("systemctl", script)
        self.assertNotIn("/etc/systemd/system/node-red.service.d", script)
        self.assertIn('install -d -o 10001 -g 10001 -m 0750 "$RUN_DIR"', script)
        self.assertNotIn("useradd", script)
        self.assertNotIn("/userdata/holo/pyfiles/m2", script)
        self.assertNotRegex(
            script,
            re.compile(
                r"(?:chown|chmod)\b[^\n]*?/userdata/holo/pyfiles(?:[\s'\"]|$)"
            ),
        )
        self.assertNotRegex(script, re.compile(r"\brm\s+-rf\b"))


if __name__ == "__main__":
    unittest.main()
