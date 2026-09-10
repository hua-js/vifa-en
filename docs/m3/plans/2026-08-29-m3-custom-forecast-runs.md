# M3 Custom Forecast Runs Implementation Plan

> **For agentic workers:** Implement inline in the current task. Do not delegate. Track each checkbox as it is completed.

**Goal:** Turn the M3 task controls into a persistent custom prediction workflow supporting 7–90 days of history, 1–7 forecast days, and 30-second to 1-hour output intervals without changing the current fixed operational forecast.

**Architecture:** Add a versioned custom source/model/result contract beside the existing fixed 15-minute contract. Persist run identity/configuration first, write result points idempotently, then expose protected run and result APIs. Connect the dashboard through the authenticated same-origin gateway only after the Worker boundary is complete.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Pandas, StatsForecast, NocoBase Resource API/PostgreSQL, Node-RED, vanilla HTML/CSS/JavaScript.

**Spec:** `docs/m3/specs/2026-08-29-m3-custom-forecast-runs-design.md`

**Verification constraint:** Per the user's explicit instruction, do not add, modify, or run automated tests. Use JSON validation, Python compile/import checks, JavaScript parse checks, exact HTML-template comparison, HTTP interface smoke checks, and `git diff --check` only.

## Task 1: Add the three persistent custom-run collections

**Files:**

- Modify: `m3/contracts/nocobase_collections.json`
- Modify: deployment/permission documentation located by `rg "energy_forecast_latest|worker_role" m3 docs`

- [x] Add `energy_forecast_manual_runs` with bigint identity PK, stable external `run_id`, idempotency constraint, immutable request fields, bounded status fields, manifests and audit timestamps.
- [x] Add `energy_forecast_manual_points` with indexed FK, `(run_pk, unique_id, target_time)` uniqueness, forecast/actual fields and interval-independent horizon bounds.
- [x] Add `energy_forecast_manual_evaluations` with one row per task/evaluation key and comparable MAPE/baseline fields.
- [x] Add exact Worker least-privilege actions and fields; deny delete/import/export and arbitrary collection access.
- [x] Validate JSON and inspect every FK/index/check/access-pattern pair.

## Task 2: Introduce interval-independent domain contracts

**Files:**

- Add: `m3/worker/custom_forecast_contracts.py`
- Add: `m3/worker/domain/custom_training_data.py`
- Add: `m3/worker/domain/custom_forecasting.py`
- Keep unchanged: `m3/worker/contracts.py`, `m3/worker/domain/training_data.py`, `m3/worker/domain/forecasting.py`

- [x] Define strict request/config/run/point/evaluation models and the allowed interval set `{3600, 1800, 900, 300, 60, 30}`.
- [x] Derive points per day, horizon, day/week season lengths and the short/full model policy from validated input.
- [x] Parameterize resampling, gap handling, model factories, cross-validation and forecasting in the new modules.
- [x] Preserve load clipping to non-negative and SOC clipping to `0..100`; keep explicit fallback reasons.
- [x] Compile the new modules and run bounded fixture-free import/constructor smoke checks.

## Task 3: Add the configurable raw observation source

**Files:**

- Modify: `m3/worker/clients/raw_energy_api.py`
- Keep unchanged: `m3/worker/clients/source_api.py`, `m3/node_red/energy_forecast_flow.json`

- [x] Confirm the active Worker path reads the fixed NocoBase raw source directly; do not add an unused Node-RED v2 path.
- [x] Add a separate typed `list_custom_observations` method whose `interval_seconds` is validated against the allowed set.
- [x] Pass interval explicitly; keep the existing 15-minute `list_observations` behavior unchanged.
- [x] Aggregate load by mean and SOC by last valid value using coverage derived from the source sampling cadence.
- [x] Preserve seven-day request chunking, stable ordering, duplicate rejection, response-size bounds and configured-station allowlisting.
- [x] Compile the client and run a bounded in-memory raw aggregation smoke check.

## Task 4: Persist and recover custom tasks

**Files:**

- Add: `m3/worker/services/custom_forecast_repository.py`
- Add: `m3/worker/services/custom_forecast_service.py`
- Modify: `m3/worker/clients/nocobase_api.py`
- Modify: `m3/worker/main.py`

- [x] Create the run row before executor submission and return the existing row for the same station/idempotency key.
- [x] Pull the selected history range in bounded chunks, construct both datasets, select allowed models and persist source/model manifests.
- [x] Write points with NocoBase's documented array `create` in bounded batches, with paginated immutable replay validation and a final content hash/count check.
- [x] Mark status only after persistent evidence is complete; store stable errors on failure and leave fully persisted runs recoverable if only the final status write fails.
- [x] Recover `queued/running` rows at startup without creating duplicate runs or points.
- [x] Keep a separate bounded concurrency path instead of changing `JobService`, so manual 30-second jobs do not block scheduled operational forecasts.

## Task 5: Expose protected task and result APIs

**Files:**

- Modify: `m3/worker/api/models.py`
- Modify: `m3/worker/api/routes.py`
- Modify: `m3/worker/api/dependencies.py` only if the existing station authorization boundary needs a typed user identity

- [x] Add strict custom-run request and response models with no coercion of dates, integers or interval values.
- [x] Add POST run creation and GET state/result/performance routes from the design.
- [x] Return `202` for submission, `200` for state/result reads, `404` for unknown authorized IDs, and stable bounded error codes.
- [x] Never return internal numeric PKs, service tokens, upstream bodies or stack traces.
- [x] Exercise local request/response smoke checks without running the automated test suite.

## Task 6: Backfill actuals and calculate comparable MAPE

**Files:**

- Add: `m3/worker/services/custom_forecast_evaluation_service.py`
- Modify: scheduler/resource wiring located by `rg "AcceptanceService|run_forecast" m3/worker/main.py m3/worker/services`

- [x] Find succeeded custom runs whose complete target window has elapsed and fetch actuals at the stored interval.
- [x] Keep immutable raw observations as actual-value truth and overlay them at read/evaluation time; never mutate persisted forecast fields.
- [x] Calculate per-series metrics and SeasonalNaive baseline on the same target points.
- [x] Persist three evaluation rows and expose only comparable-key rolling seven-day aggregates.
- [x] Mark runs `evaluated` only after forecast-point and evaluation persistence verifies completely.

## Task 7: Connect the same-origin gateway and dashboard button

**Files:**

- Modify: `m3/node_red/m3_production_gateway_flow.json`
- Modify: `shared/场站未来能耗预测.html`

- [x] Add fixed POST/GET proxy routes that validate the current NocoBase user before calling Worker with server-side credentials.
- [x] Forward only the fixed request fields and apply method, content-type, body-size, timeout and response-size limits.
- [x] Enable the button only when controls and auth are valid; send one idempotent request and show queued/running/failed states.
- [x] Poll the submitted run, render its actual and forecast points on one target-time axis, and restore the most recent run per station.
- [x] Render missing MAPE/baseline as `—`; do not substitute demo numbers.
- [x] Synchronize the exact formal HTML into the Node-RED template and compare them byte-for-byte.

## Task 8: Final static and interface verification

**Files:** all changed files from Tasks 1–7.

- [x] Validate all JSON files with `python -m json.tool` or `jq empty`.
- [x] Compile/import changed Python modules without invoking the automated test suite.
- [x] Parse the dashboard script and verify there are no external assets, unsafe HTML insertion or accidental secrets.
- [x] Smoke-check create/status/result flows and one validation failure for each input bound.
- [x] Run `git diff --check`, review status to exclude unrelated user files, and commit only the custom forecast implementation.
