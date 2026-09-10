# M3 Docker Dual-UDS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing M3 Worker and a new read-only Dashboard API as two ARM64-compatible Python 3.12 containers reached from host Node-RED through Unix sockets only.

**Architecture:** One immutable image is reused by `vifa-m3-worker` and `vifa-m3-dashboard`. Each service has its own environment and socket; host `nodered.service` authenticates the browser user and calls only the Dashboard socket with fixed curl arguments.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, httpx, StatsForecast 2.1.1, Docker 27, Docker Compose v2, Unix domain sockets, Node-RED.

**Spec:** `docs/m3/specs/2026-08-27-m3-docker-dual-uds-design.md`

## Global Constraints

- Test machine is Ubuntu 20.04 ARM64 with Docker 27.3.1 and Compose 2.29.7.
- Host Python 3.8.10 must not run M3 code.
- Do not publish a new TCP port or mount `/var/run/docker.sock`.
- Do not modify forecasting algorithms, model selection, evaluation formulas, NocoBase schema, or iframe HTML.
- Tests cover only changed Dashboard API, Docker, deployment, and Node-RED boundary code.
- Do not add or run Node-RED automated runtime tests; perform static Flow validation locally and manual integration on the test machine.
- The workspace root is not a Git repository, so commit steps are omitted; preserve all unrelated files.
- Never print, copy into source, or return plaintext credentials.

---

### Task 1: Read-only persisted Dashboard FastAPI application

**Files:**
- Create: `m3/worker/persisted_dashboard_app.py`
- Create: `m3/tests/test_m3_persisted_dashboard_app.py`
- Reuse: `m3/worker/dashboard_cli.py`
- Reuse: `m3/worker/config.py`

**Interfaces:**
- Consumes: `DashboardSettings.from_env(environ)` and `build_dashboard(settings) -> DashboardEnvelope`.
- Produces: `create_persisted_dashboard_app(settings_factory, builder) -> FastAPI` and module-level `app`.
- HTTP: `GET /health -> {"status":"ok"}` and `GET /dashboard -> DashboardEnvelope`.

- [ ] **Step 1: Write failing API tests**

Cover exact successful payloads, disabled OpenAPI/docs, rejection of query/body, source/NocoBase/contract/internal error mapping, builder execution in a threadpool, and builder output validation.

- [ ] **Step 2: Run the focused test and confirm failure**

Run:

```bash
.venv/bin/python -m unittest m3.tests.test_m3_persisted_dashboard_app -v
```

Expected: import failure because `m3.worker.persisted_dashboard_app` does not exist.

- [ ] **Step 3: Implement the minimal FastAPI app**

The app must parse settings once during lifespan, expose only the two fixed routes, call the existing read-only builder through `run_in_threadpool`, validate the result as `DashboardEnvelope`, and return bounded public errors without traceback, URL, or credential content.

- [ ] **Step 4: Run focused tests**

Run the same unittest command. Expected: all tests pass.

---

### Task 2: ARM64-compatible immutable container image and Compose services

**Files:**
- Create: `m3/deploy/Dockerfile`
- Create: `.dockerignore`
- Create: `m3/deploy/compose.yaml`
- Create: `m3/deploy/container-entrypoint.sh`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Produces one image named `vifa-m3:0.1.0` with entrypoint modes `worker` and `dashboard`.
- Worker socket: `/run/vifa-m3/worker.sock`.
- Dashboard socket: `/run/vifa-m3/dashboard.sock`.
- Host bind directory: `/userdata/holo/pyfiles/vifa-m3/run`.

- [ ] **Step 1: Add failing static deployment tests**

Assert Python 3.12 slim base, hash-locked install, non-root UID/GID 10001, no secret copy, no `ports`, no Docker socket, distinct env files, read-only filesystem, dropped capabilities, exact service commands, health checks, `host-gateway`, and exact socket paths.

- [ ] **Step 2: Run the focused deployment test and confirm failure**

```bash
.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v
```

Expected: missing Docker artifacts.

- [ ] **Step 3: Implement Docker artifacts**

The entrypoint accepts only `worker` or `dashboard`, refuses a non-socket collision at its exact target, removes only its own stale socket, and execs one Uvicorn worker. Compose reuses the image, loads `/etc/vifa-m3/m3.env` only for Worker and `/etc/vifa-m3/dashboard.env` only for Dashboard, mounts `raw-source.token` read-only, and publishes no ports.

- [ ] **Step 4: Run focused deployment tests and local syntax checks**

```bash
.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v
bash -n m3/deploy/container-entrypoint.sh
docker compose config --quiet
```

If local Docker is unavailable, the Compose render is deferred to the test machine; the Python/static and shell checks must still pass locally.

---

### Task 3: Host Node-RED fixed Socket call and minimum environment

**Files:**
- Modify: `m3/node_red/m3_dashboard_exec_flow.json`
- Modify: `m3/deploy/node-red-vifa-m3-dashboard.conf`
- Create: `m3/deploy/nodered.env.example`
- Modify: `m3/deploy/prepare-server.sh`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Node-RED unit: `nodered.service`.
- Node-RED environment: only `M3_NOCOBASE_BASE_URL`.
- Fixed command: `/usr/bin/curl --fail --silent --show-error --connect-timeout 2 --max-time 30 --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock http://localhost/dashboard`.

- [ ] **Step 1: Add failing static boundary assertions**

Assert no Python or Docker command in the Dashboard Exec node, no payload append, exact Socket curl, curl timeout mapping, Worker health Socket host path, corrected systemd drop-in path in the preparation script, and no Dashboard or raw source key in Node-RED environment.

- [ ] **Step 2: Run focused tests and confirm failure**

```bash
.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v
```

- [ ] **Step 3: Update Flow and deployment configuration**

Preserve current-user NocoBase `auth:check`. Replace only the post-auth Python CLI execution with fixed Dashboard Socket curl; map curl timeout exit code 28 to HTTP 504 and other transport/non-2xx failures to HTTP 502. Update health curl to the host Worker Socket. Keep stderr out of the browser response.

- [ ] **Step 4: Validate changed artifacts**

```bash
.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v
.venv/bin/python -m json.tool m3/node_red/m3_dashboard_exec_flow.json
bash -n m3/deploy/prepare-server.sh
```

---

### Task 4: Docker-first deployment documentation

**Files:**
- Modify: `docs/m3/部署说明.md`
- Modify: `docs/m3/specs/2026-08-27-m3-docker-dual-uds-design.md`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Produces the exact fresh-install commands for `/userdata/holo/pyfiles/vifa-m3` on the inspected ARM64 test machine.

- [ ] **Step 1: Add failing documentation markers**

Require Docker/Compose versions, ARM64 image behavior, no host venv, two services, no ports, two sockets, `nodered.service`, upload list, credential permissions, startup, health checks, manual Node-RED integration, and rollback.

- [ ] **Step 2: Rewrite the deployment guide around Compose**

Remove host Python/venv and systemd Worker as the primary path. Retain NocoBase field/index guidance and explicitly gate formal seven-day acceptance on the context/alert routes.

- [ ] **Step 3: Run documentation/deployment tests**

```bash
.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v
```

---

### Task 5: Focused local verification

**Files:**
- Test only files changed in Tasks 1–4.

**Interfaces:**
- Produces evidence that the modified boundary is ready for ARM64 server build.

- [ ] **Step 1: Run focused Python tests**

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_persisted_dashboard_app \
  m3.tests.test_m3_dashboard_cli \
  m3.tests.test_m3_persisted_dashboard \
  m3.tests.test_m3_deployment_artifacts -v
```

- [ ] **Step 2: Run syntax and artifact validation**

```bash
.venv/bin/python -m compileall -q m3/worker/persisted_dashboard_app.py
.venv/bin/python -m json.tool m3/node_red/m3_dashboard_exec_flow.json
bash -n m3/deploy/container-entrypoint.sh
bash -n m3/deploy/prepare-server.sh
```

- [ ] **Step 3: Inspect the final diff by file scope**

Confirm no model, selection, evaluation, NocoBase schema, iframe HTML, M1, or M2 file changed.

---

### Task 6: Fresh test-machine deployment and smoke integration

**Files/targets:**
- Create remotely: `/userdata/holo/pyfiles/vifa-m3`
- Create remotely: `/etc/vifa-m3`
- Create remotely: `/etc/systemd/system/nodered.service.d/vifa-m3-dashboard.conf`
- Preserve: NocoBase containers, PostgreSQL data, existing Node-RED Flow backup.

**Interfaces:**
- Consumes the tested local source archive and existing secret file without printing it.
- Produces two healthy containers and working host Socket calls.

- [ ] **Step 1: Upload a secret-free source archive**

Exclude `.venv`, `.git`, caches, `m3/worker/密钥.txt`, HTML, M1/M2, and all real env files. Transfer the archive to the user-authorized test machine, verify its file list, create the target, and extract it.

- [ ] **Step 2: Prepare directories and install credentials**

Run the revised preparation script. Install the raw token separately with mode 0640 and numeric group 10001. Create `m3.env`, `dashboard.env`, and `nodered.env` with mode 0600 without printing their values. Validate that no `REPLACE_` marker remains.

- [ ] **Step 3: Build and start both services**

```bash
cd /userdata/holo/pyfiles/vifa-m3
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
```

Expected: both services start and become healthy; no new host TCP listener appears.

- [ ] **Step 4: Verify both Unix Sockets**

Use host curl against Worker `/health`, Dashboard `/health`, and Dashboard `/dashboard`. Confirm StatsForecast 2.1.1, two station entries, and no credential or traceback in responses.

- [ ] **Step 5: Install Node-RED minimum environment and import/update Flow**

Back up current Node-RED Flow first. Install the corrected `nodered.service` drop-in, reload systemd, restart only Node-RED, verify it returns to active state, then import the M3 Flow through the approved Node-RED mechanism.

- [ ] **Step 6: Perform manual integration checks**

Verify valid NocoBase current-user authentication, rejection of missing/invalid bearer, successful `/energy-forecast-api`, timeout/transport mapping, two-station response, and preserved existing Node-RED functionality. Do not run Node-RED automated tests.

- [ ] **Step 7: Record residual gates**

Report whether dedicated Worker/Dashboard credentials and acceptance-context/alert endpoints are available. Ordinary prediction/dashboard may be marked integrated only after real raw/NocoBase HTTP succeeds; formal seven-day acceptance remains gated until its context/alert dependencies pass.
