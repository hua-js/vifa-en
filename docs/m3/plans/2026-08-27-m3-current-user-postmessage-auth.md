# M3 Current-user PostMessage Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Keep M3 service credentials server-side while requiring the NocoBase current user to authenticate every browser request to the Node-RED dashboard API.

**Architecture:** A NocoBase JS block reads `ctx.token`, creates the cross-origin Node-RED iframe, and transfers the current-user token through an exact-origin, nonce-bound `postMessage` handshake. Node-RED validates that token against the current NocoBase instance, removes it, and calls the existing Dashboard Unix Socket; the Dashboard container retains its separate read-only key.

**Tech Stack:** NocoBase JS Block/RunJS, browser `postMessage`, Node-RED 4, HTML/JavaScript, Docker Compose, Unix domain socket.

**Spec:** `docs/m3/specs/2026-08-27-m3-current-user-postmessage-auth-design.md`

## Constraints

- Do not modify forecasting models, Worker persistence, NocoBase schema, M1, or M2.
- Never put a service token in source, HTML, Node-RED environment, URL, logs, or test output.
- Use exact HTTP(S) origins and one-time nonce; never use wildcard `postMessage` targets.
- The workspace root is not a Git repository, so work directly and use explicit remote backups.
- Node-RED runtime verification is limited to HTTP/systemd smoke checks on the authorized test machine.

### Task 1: Lock the browser authentication contract with tests

**Files:**
- Modify: `m3/tests/m3_dashboard_e2e.js`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

- [ ] Point the dashboard E2E at `shared/场站未来能耗预测.html`.
- [ ] Assert that no API request starts before authentication.
- [ ] Assert wrong source, origin, nonce, message shape, and token are ignored.
- [ ] Assert the valid nonce-bound current-user token starts one fixed same-origin GET.
- [ ] Assert the token is absent from globals, storage, query, cookie, and referrer.
- [ ] Add static contracts for the NocoBase wrapper and two-variable Node-RED environment.
- [ ] Run the focused tests and confirm the new assertions fail before implementation.

### Task 2: Implement the iframe child and NocoBase parent

**Files:**
- Modify: `shared/场站未来能耗预测.html`
- Create: `docs/m3/nocobase/m3_forecast_iframe_block.js`
- Modify: `m3/deploy/nodered.env.example`

- [ ] Replace the global fixed-token bootstrap with the exact-origin nonce handshake.
- [ ] Keep the received token only in a closure and clear it on page hide.
- [ ] Preserve the existing fixed GET, timeout, last-good rendering, and 60-second polling behavior.
- [ ] Create the NocoBase JS block using `ctx.getVar("ctx.token")`, iframe sandbox, exact origin/source checks, and cleanup on rerun.
- [ ] Add only `M3_NOCOBASE_PAGE_ORIGIN` to the existing Node-RED environment contract.

### Task 3: Update deployment artifacts and documentation

**Files:**
- Create: `m3/node_red/m3_dashboard_page_flow.json`
- Modify: `docs/m3/部署说明.md`
- Modify: `m3/M3 测试到生产部署操作手册.md`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

- [ ] Store the `/ett` page flow without any token and inject only the configured parent origin.
- [ ] Document exact test and production origin settings and the NocoBase JS Block paste/import step.
- [ ] Remove instructions that inject `window.__NOCOBASE_API_TOKEN__` or share a fixed iframe token.
- [ ] Validate JSON and all focused tests.

### Task 4: Apply the change to the authorized test machine

**Remote targets:**
- `/root/.node-red/flows.json`
- `/etc/vifa-m3/nodered.env`

- [ ] Back up both remote files with a new timestamped directory.
- [ ] Stop Node-RED, replace the temporary token injector with a strict parent-origin injector, and update `/ett` HTML.
- [ ] Set current-user auth to local test NocoBase and configure the test NocoBase page origin.
- [ ] Remove `M3_NOCOBASE_IFRAME_TOKEN` and restart `nodered.service`.

### Task 5: Verify security and integration

- [ ] Confirm `/ett` returns `Cache-Control: no-store`, contains the handshake, and contains no JWT/fixed Token.
- [ ] Confirm `/energy-forecast-api` without Authorization returns 401.
- [ ] Confirm a valid NocoBase user token returns HTTP 200 within the HTML timeout and contains two station results.
- [ ] Confirm Node-RED is active and both M3 containers remain healthy.
- [ ] Confirm the server environment no longer contains `M3_NOCOBASE_IFRAME_TOKEN`.
- [ ] Record the one remaining NocoBase UI action if the JS block cannot be updated through the available signed-in browser.
