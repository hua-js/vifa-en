# M3 Production Import Package Implementation Plan（已废弃）

> 现行实施计划见 `2026-08-28-m3-amd64-three-domain-iframe.md`。本计划中的 JS Block、
> postMessage 和 ARM64 要求不再用于生产交付。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a production architecture flowchart, one importable Node-RED M3 gateway Flow, one NocoBase manual configuration manifest, and one production NocoBase JS block for the confirmed three-domain deployment.

**Architecture:** The M3 Worker reads and writes prediction data through `vifa.hlszh.com`; the NocoBase page and current-user identity live at `ems.lvkpower.com`; Node-RED at `opdash.lvkpower.com` serves the iframe and validates the browser user before calling the read-only Dashboard Unix Socket. Node-RED stores no credential and the NocoBase manifest is explicitly a human-operated checklist rather than a fake page-import format.

**Tech Stack:** Node-RED core nodes and Flow JSON, NocoBase JavaScript Block, browser `postMessage`, Docker Compose, FastAPI over Unix domain sockets, Python `unittest`, Node.js `vm`, Playwright, Markdown/Mermaid.

**Spec:** `docs/m3/specs/2026-08-28-m3-production-import-package-design.md`

## Global Constraints

- Production origins are exactly `https://vifa.hlszh.com`, `https://ems.lvkpower.com`, and `https://opdash.lvkpower.com`, without trailing slashes.
- Do not restart Node-RED, NocoBase, M1, or M2; do not install or upgrade Docker, Node-RED, or NocoBase.
- Do not create or alter the four M3 tables.
- Do not add TCP listeners, Nginx, third-party Node-RED nodes, or fixed browser tokens.
- Current-user authentication uses `M3_AUTH_BASE_URL=https://ems.lvkpower.com`; four-table access continues using Dashboard/Worker `M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com`.
- Browser tokens remain in runtime memory only and use exact-origin, exact-source, nonce-bound `postMessage`.
- The Node-RED Unix Socket path is exactly `/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock`.
- The workspace root is not a Git repository; replace commit steps with explicit verification checkpoints and changed-file listings.
- The first release does not claim or start seven-day acceptance; the Worker must require explicit `M3_ACCEPTANCE_ENABLED=false` while ordinary forecasting and model selection continue.

## File Structure

- `m3/node_red/m3_production_gateway_flow.json`: the only production Node-RED artifact to import; owns `/ett`, `/energy-forecast-api`, and M3 Worker socket health monitoring.
- `docs/m3/nocobase/m3_forecast_iframe_block.production.js`: production-only JS Block source with the fixed `opdash` iframe URL.
- `docs/m3/nocobase/m3_nocobase_iframe_manifest.json`: strict JSON manual installation, security, verification, and rollback contract; explicitly non-importable.
- `m3/部署架构流程图.md`: operator-facing Mermaid diagrams and import/rollback sequence.
- `m3/node_red/m3_dashboard_exec_flow.json`: reusable authentication/API source Flow; change only its auth-origin variable name.
- `m3/deploy/prepare-server.sh`: prepare only M3 Docker/config/socket directories; remove Node-RED systemd directory handling.
- `m3/worker/config.py`: parse the explicit strict acceptance switch.
- `m3/worker/scheduler/runner.py`: skip all acceptance calls while preserving forecast/model-selection schedules.
- `m3/worker/main.py`: skip startup acceptance reconciliation when disabled.
- `m3/deploy/m3.env.example`: default the phase-one deployment to `M3_ACCEPTANCE_ENABLED=false`.
- `m3/deploy/nodered.env.example`: delete the obsolete Node-RED process-level environment example.
- `m3/deploy/node-red-vifa-m3-dashboard.conf`: delete the obsolete systemd drop-in.
- `docs/m3/部署说明.md`: single source of truth; correct the production three-domain responsibilities and point to the new import package.
- `m3/人工部署手册.md`: complete production-only runbook for ARM64 image import, code/config upload, Docker start, manual imports, verification, and production rollback.
- `m3/M3 测试到生产部署操作手册.md`: replace obsolete commands with a short redirect to `m3/人工部署手册.md`.
- `m3/tests/test_m3_deployment_artifacts.py`: static contracts for the new Flow, manifest, JS, diagrams, origins, routes, sockets, and absence of credentials.
- `m3/tests/test_m3_dashboard_auth_modes.js`: executable Node-RED Function-node contract for `M3_AUTH_BASE_URL` and production-only postmessage mode.
- `m3/tests/m3_dashboard_e2e.js`: browser contract that also exercises the production JS block source.

---

### Task 1: Lock the production import contracts with failing tests

**Files:**
- Modify: `m3/tests/test_m3_deployment_artifacts.py`
- Modify: `m3/tests/test_m3_dashboard_auth_modes.js`
- Modify: `m3/tests/m3_dashboard_e2e.js`

**Interfaces:**
- Consumes: existing Node-RED flow parsing helpers, existing browser handshake fixtures, and paths rooted at `ROOT`.
- Produces: exact artifact paths and assertions that later tasks must satisfy.

- [ ] **Step 1: Add production artifact constants and strict static assertions**

Add these constants near the existing Flow paths:

```python
PRODUCTION_FLOW = ROOT / "m3" / "node_red" / "m3_production_gateway_flow.json"
PRODUCTION_BLOCK = ROOT / "m3" / "nocobase" / "m3_forecast_iframe_block.production.js"
NOCOBASE_MANIFEST = ROOT / "m3" / "nocobase" / "m3_nocobase_iframe_manifest.json"
ARCHITECTURE = ROOT / "m3" / "部署架构流程图.md"
```

Add one test that parses both JSON files and asserts:

```python
self.assertEqual(manifest["kind"], "vifa-m3-nocobase-js-block-manifest")
self.assertEqual(manifest["version"], 1)
self.assertFalse(manifest["nocobase_native_import"]["importable"])
self.assertEqual(manifest["target"]["nocobase_origin"], "https://ems.lvkpower.com")
self.assertEqual(manifest["target"]["node_red_origin"], "https://opdash.lvkpower.com")
self.assertEqual(manifest["target"]["data_origin"], "https://vifa.hlszh.com")
self.assertEqual(manifest["target"]["iframe_url"], "https://opdash.lvkpower.com/ett")
```

For the production Flow, assert exactly one tab, the exact Flow environment map, exactly one enabled GET route for each fixed path, no third-party node types, and no credential markers:

```python
allowed = {
    "tab", "group", "http in", "http response", "http request", "function",
    "template", "exec", "join", "inject", "catch", "comment",
}
self.assertTrue({node["type"] for node in flow} <= allowed)
self.assertEqual(routes, [("/energy-forecast-api", "get"), ("/ett", "get")])
self.assertEqual(flow_env, {
    "M3_AUTH_MODE": "postmessage",
    "M3_AUTH_BASE_URL": "https://ems.lvkpower.com",
    "M3_NOCOBASE_PAGE_ORIGIN": "https://ems.lvkpower.com",
})
```

Scan Flow, manifest, JS, and architecture text for `Authorization: Bearer eyJ`, JWT-shaped values, `M3_NOCOBASE_API_KEY`, `M3_DASHBOARD_NOCOBASE_API_KEY`, and wildcard postMessage targets. The safe URL validation expression `iframeUrl.password` is allowed and must not be treated as a credential.

- [ ] **Step 2: Change the executable Function-node test to the new auth variable**

In `m3/tests/test_m3_dashboard_auth_modes.js`, pass:

```javascript
{
  M3_AUTH_MODE: "postmessage",
  M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
}
```

and assert `request.url === "https://ems.lvkpower.com/api/auth:check"`. Add invalid-origin cases for a trailing slash, path, query, fragment, credentials, and port greater than 65535; each must produce `503 auth_unavailable`.

- [ ] **Step 3: Point the browser wrapper test at the production JS source**

Read `docs/m3/nocobase/m3_forecast_iframe_block.production.js` and replace its exact production URL with the local fixture URL:

```javascript
const productionBlockPath = path.join(
  ROOT, "m3", "nocobase", "m3_forecast_iframe_block.production.js",
);
const nocobaseBlockText = fs.readFileSync(productionBlockPath, "utf8");
const testBlockText = nocobaseBlockText.replace(
  'const M3_IFRAME_URL = "https://opdash.lvkpower.com/ett";',
  `const M3_IFRAME_URL = ${JSON.stringify(`${origin}/ett`)};`,
);
assert.notStrictEqual(testBlockText, nocobaseBlockText);
```

- [ ] **Step 4: Run focused tests and verify they fail for missing artifacts/old variable**

Run:

```bash
.venv/bin/python m3/tests/test_m3_deployment_artifacts.py
node m3/tests/test_m3_dashboard_auth_modes.js
node m3/tests/m3_dashboard_e2e.js
```

Expected: the new static/browser tests fail because the three new artifacts do not exist, and the auth-mode test fails because the Flow still reads `M3_NOCOBASE_BASE_URL`.

- [ ] **Step 5: Record checkpoint**

Run `find tests -maxdepth 1 -type f -name '*m3*' -newer docs/m3/specs/2026-08-28-m3-production-import-package-design.md -print` and retain the output in the turn log. No Git commit is possible in this workspace.

### Task 2: Create the production NocoBase manual package

**Files:**
- Create: `docs/m3/nocobase/m3_forecast_iframe_block.production.js`
- Create: `docs/m3/nocobase/m3_nocobase_iframe_manifest.json`

**Interfaces:**
- Consumes: the existing tested handshake in `docs/m3/nocobase/m3_forecast_iframe_block.js` and the manifest assertions from Task 1.
- Produces: a paste-ready production JS Block and a strict operator manifest referenced by documentation.

- [ ] **Step 1: Create the production JS Block with one environment-specific change**

Copy the current tested block behavior exactly and set only:

```javascript
// NocoBase JS 区块：生产环境。固定域名，禁止拼接用户输入。
const M3_IFRAME_URL = "https://opdash.lvkpower.com/ett";
```

Preserve `ctx.getVar("ctx.token")`, exact `event.source`, exact origin, strict message shape, 32-hex nonce, cleanup, sandbox, no-referrer, and memory-only Token handling.

- [ ] **Step 2: Create the strict manifest**

Create a JSON object with these top-level keys in order:

```json
{
  "kind": "vifa-m3-nocobase-js-block-manifest",
  "version": 1,
  "nocobase_native_import": {
    "importable": false,
    "reason": "NocoBase 页面配置没有通用 JSON 导入格式；本文件是人工配置和验收清单"
  },
  "target": {},
  "access": {},
  "block": {},
  "security": {},
  "preflight": [],
  "installation": [],
  "verification": [],
  "rollback": []
}
```

Use exact target values from the spec. Installation must say to create one JavaScript Block, paste the named production JS source, save the block, and open the page as a normal authenticated user. Preflight must include the positive-integer `data.id` auth check and the unique Node-RED route check. Rollback must remove/disable only the M3 block.

- [ ] **Step 3: Run NocoBase-focused tests**

Run:

```bash
.venv/bin/python m3/tests/test_m3_deployment_artifacts.py
node m3/tests/m3_dashboard_e2e.js
```

Expected: manifest and production JS assertions pass; Flow/auth-variable assertions may still fail until Task 3.

- [ ] **Step 4: Record checkpoint**

Run `shasum -a 256 docs/m3/nocobase/m3_forecast_iframe_block.production.js docs/m3/nocobase/m3_nocobase_iframe_manifest.json` and retain the hashes in the turn log.

### Task 3: Build the importable production Node-RED Flow

**Files:**
- Modify: `m3/node_red/m3_dashboard_exec_flow.json`
- Modify: `m3/worker/config.py`
- Modify: `m3/worker/scheduler/runner.py`
- Modify: `m3/worker/main.py`
- Modify: `m3/deploy/m3.env.example`
- Modify: `m3/deploy/prepare-server.sh`
- Delete: `m3/deploy/nodered.env.example`
- Delete: `m3/deploy/node-red-vifa-m3-dashboard.conf`
- Create: `m3/node_red/m3_production_gateway_flow.json`
- Modify: `m3/tests/test_m3_dashboard_auth_modes.js`

**Interfaces:**
- Consumes: the existing page template node, API/Unix Socket nodes, and Task 1 exact Flow contract.
- Produces: one self-contained import artifact using only Node-RED core nodes.

- [ ] **Step 1: Rename the auth-origin variable at its source**

In the `m3_dashboard_auth_prepare` Function node, replace only:

```javascript
const configuredBase = env.get('M3_NOCOBASE_BASE_URL');
```

with:

```javascript
const configuredBase = env.get('M3_AUTH_BASE_URL');
```

Delete the obsolete `nodered.env.example` and systemd drop-in. In `prepare-server.sh`, delete `NODE_RED_DROP_IN_DIR` and its `install -d` command so the script never changes Node-RED process configuration. Add a static test that both obsolete files are absent and the preparation script contains neither `nodered.service.d` nor `systemctl`.

- [ ] **Step 2: Assemble one production Flow**

Create one tab `m3_production_gateway_tab` labeled `M3 负载预测` with Flow environment entries:

```json
[
  {"name":"M3_AUTH_MODE","value":"postmessage","type":"str"},
  {"name":"M3_AUTH_BASE_URL","value":"https://ems.lvkpower.com","type":"str"},
  {"name":"M3_NOCOBASE_PAGE_ORIGIN","value":"https://ems.lvkpower.com","type":"str"}
]
```

Move the page, API, Catch, and health nodes into that tab; retain their existing Function code, command strings, timeout limits, stdout bound, and wires. Use three visual groups: `M3 看板页面`、`M3 当前用户鉴权与 API`、`M3 Worker 健康检查`. Ensure all IDs are unique and prefixed `m3_prod_` except the tab ID.

- [ ] **Step 3: Make production mode fail closed**

The production Flow must not route a `server_token` branch. Its auth prepare Function accepts only `postmessage`; missing or any other mode returns `503 auth_unavailable`. The `/ett` gate likewise accepts only `postmessage`. No fixed Token environment variable or fallback path may remain in the production artifact.

- [ ] **Step 4: Validate the Flow behavior**

Run:

```bash
node m3/tests/test_m3_dashboard_auth_modes.js
.venv/bin/python m3/tests/test_m3_deployment_artifacts.py
python3 -m json.tool m3/node_red/m3_production_gateway_flow.json >/dev/null
```

Expected: all commands succeed; the Flow exposes exactly the two fixed GET routes and reads only `M3_AUTH_BASE_URL` for current-user validation.

- [ ] **Step 5: Record checkpoint**

Run `shasum -a 256 m3/node_red/m3_production_gateway_flow.json` and retain the hash in the turn log.

- [ ] **Step 6: Implement and verify the phase-one acceptance switch**

Require exact lower-case `M3_ACCEPTANCE_ENABLED=true|false` in `Settings.from_env`. Pass it to `SchedulerRunner`; when false, skip 01:02 baselines, per-forecast acceptance backfill, and startup acceptance reconciliation. Keep ordinary forecasts and model selection unchanged. Set the deployment example to false and run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_scheduler.SchedulerTests.test_acceptance_disabled_skips_baseline_and_backfill_but_keeps_forecasts \
  m3.tests.test_m3_contracts.M3ContractTests.test_settings_require_an_explicit_strict_acceptance_switch \
  m3.tests.test_m3_worker_api.ResourceTests.test_recover_skips_acceptance_reconciliation_when_phase_one_is_disabled
```

Expected: all three tests pass.

### Task 4: Publish the architecture diagrams and executable deployment runbook

**Files:**
- Create: `m3/部署架构流程图.md`
- Modify: `docs/m3/部署说明.md`
- Modify: `m3/人工部署手册.md`
- Modify: `m3/M3 测试到生产部署操作手册.md`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Consumes: exact file names, domains, variables, routes, security rules, and rollback behavior from Tasks 2–3.
- Produces: a technical source-of-truth guide, an independently executable cutover runbook, and a standalone diagram document.

- [ ] **Step 1: Write four Mermaid diagrams**

The architecture document must include:

1. three-domain deployment architecture;
2. Worker pull → StatsForecast → four-table write data flow;
3. NocoBase JS Block → iframe nonce handshake → Node-RED auth → Dashboard Socket sequence;
4. external confirmation that test Docker is stopped → import production image → start production Docker → import → verify → production rollback decision flow.

Label Phase 1 acceptance as disabled/not claimed and keep M1/M2 outside the modified boundary.

- [ ] **Step 2: Correct the main deployment guide**

Replace production instructions that currently set both values to `vifa.hlszh.com` with:

```text
M3_AUTH_MODE=postmessage
M3_AUTH_BASE_URL=https://ems.lvkpower.com
M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com
```

Keep Worker and Dashboard `M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com`. Add the new production Flow, manifest, production JS, and architecture document to the upload/import list. Explicitly prohibit importing `energy_forecast_flow.json`.

- [ ] **Step 3: Complete the production-only manual deployment runbook**

Keep `docs/m3/部署说明.md` as the technical source of truth, but make `m3/人工部署手册.md` independently executable. Treat “test Docker already stopped by the user” as a checkbox only; do not include test-host login, stop, start, IP, or rollback commands. Include exact production commands and checkpoints for:

```bash
# production: verify and import the supplied ARM64 image archive
sha256sum /tmp/vifa-m3-image.tar
docker load -i /tmp/vifa-m3-image.tar

# production: upload/prepare code and config
cd /userdata/holo/pyfiles/vifa-m3
docker compose config --quiet

# production: start both M3 services
docker compose up -d --no-build vifa-m3-worker vifa-m3-dashboard
```

The runbook must label every command block `生产环境终端`, prohibit `docker compose down`, verify the loaded image is ARM64 and has the exact expected tag, verify one production Worker, and replace every claim that production page/auth origin is `vifa.hlszh.com`. Replace the older test-to-production manual with a redirect so operators cannot follow conflicting instructions.

- [ ] **Step 4: Add documentation assertions**

Assert the main guide contains all three domains, `M3_AUTH_BASE_URL`, the four delivery file names, `Modified Nodes`, `不重启 Node-RED`, and the positive-integer auth check. Assert it does not contain the obsolete production pair:

```python
self.assertNotIn(
    "M3_NOCOBASE_PAGE_ORIGIN=https://vifa.hlszh.com",
    guide,
)
```

Assert the runbook contains `docker load -i`, `docker compose up -d --no-build vifa-m3-worker vifa-m3-dashboard`, both production domains, Flow/JS artifact names, explicit `测试机器 M3 Docker 已停止` prerequisite, and a prohibition on `docker compose down`. Assert it contains no test IP, `测试环境终端`, test-host SSH, or test-container start/stop commands.

- [ ] **Step 5: Run documentation/static tests**

Run:

```bash
.venv/bin/python m3/tests/test_m3_deployment_artifacts.py
```

Expected: all deployment artifact tests pass.

- [ ] **Step 6: Record checkpoint**

Run `shasum -a 256 m3/部署架构流程图.md docs/m3/部署说明.md m3/人工部署手册.md` and retain the hashes in the turn log.

### Task 5: Complete focused and regression verification

**Files:**
- Verify only; no planned production or server writes.

**Interfaces:**
- Consumes: all artifacts from Tasks 1–4.
- Produces: evidence that the package is syntactically valid, secure by static contract, and browser-compatible.

- [ ] **Step 1: Validate JSON syntax and secret absence**

Run:

```bash
python3 -m json.tool m3/node_red/m3_production_gateway_flow.json >/dev/null
python3 -m json.tool docs/m3/nocobase/m3_nocobase_iframe_manifest.json >/dev/null
rg -n 'eyJ[a-zA-Z0-9_-]+\.|Bearer [A-Za-z0-9_-]{20,}|M3_NOCOBASE_API_KEY|M3_DASHBOARD_NOCOBASE_API_KEY' \
  m3/node_red/m3_production_gateway_flow.json \
  docs/m3/nocobase/m3_nocobase_iframe_manifest.json \
  docs/m3/nocobase/m3_forecast_iframe_block.production.js \
  m3/部署架构流程图.md
```

Expected: both JSON commands succeed and `rg` returns no matches.

- [ ] **Step 2: Run all focused contracts**

Run:

```bash
.venv/bin/python m3/tests/test_m3_deployment_artifacts.py
node m3/tests/test_m3_dashboard_auth_modes.js
node m3/tests/m3_dashboard_e2e.js
```

Expected: all focused tests pass.

- [ ] **Step 3: Run adjacent M3 Dashboard tests**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_dashboard_cli \
  m3.tests.test_m3_persisted_dashboard \
  m3.tests.test_m3_persisted_dashboard_app \
  m3.tests.test_m3_live_dashboard
```

Expected: all tests pass without network access or production writes.

- [ ] **Step 4: Self-review against the spec**

Check every section in `docs/m3/specs/2026-08-28-m3-production-import-package-design.md` against at least one test or document section. Search the plan and artifacts for `TBD`, `TODO`, `implement later`, wildcard postMessage, old production origins, duplicate routes, and secret markers; correct any finding before completion.

- [ ] **Step 5: Produce the final operator handoff**

List the architecture, Node-RED Flow, NocoBase manifest, production JS, and executable manual deployment runbook with clickable absolute paths; include the tests run and their exact pass results. State the remaining production gate: a successful real-user `ems/api/auth:check`. Confirm the deployment config keeps `M3_ACCEPTANCE_ENABLED=false`, and state explicitly that no production import, Node-RED restart, NocoBase change, or test-machine operation was performed.
