# M3 Custom Forecast Runs Implementation Plan

> **For agentic workers:** Implement inline in the current task. Do not delegate. Track each checkbox as it is completed.

**Goal:** Turn the M3 task controls into a persistent custom prediction workflow supporting 7–90 days of history, 1–7 forecast days, and 30-second to 1-hour output intervals without changing the current fixed operational forecast.

**Architecture:** Add a versioned custom source/model/result contract beside the existing fixed 15-minute contract. Persist run identity/configuration first, write result points idempotently, then expose protected run and result APIs. Connect the dashboard through the authenticated same-origin gateway only after the Worker boundary is complete.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Pandas, StatsForecast, NocoBase Resource API/PostgreSQL, Node-RED, vanilla HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-08-29-m3-custom-forecast-runs-design.md`

**Verification constraint:** Per the user's explicit instruction, do not add, modify, or run automated tests. Use JSON validation, Python compile/import checks, JavaScript parse checks, exact HTML-template comparison, HTTP interface smoke checks, and `git diff --check` only.

## Task 1: Add the three persistent custom-run collections

**Files:**

- Modify: `m3/contracts/nocobase_collections.json`
- Modify: deployment/permission documentation located by `rg "energy_forecast_latest|worker_role" m3 docs`

- [ ] Add `energy_forecast_manual_runs` with bigint identity PK, stable external `run_id`, idempotency constraint, immutable request fields, bounded status fields, manifests and audit timestamps.
- [ ] Add `energy_forecast_manual_points` with indexed FK, `(run_pk, unique_id, target_time)` uniqueness, forecast/actual fields and interval-independent horizon bounds.
- [ ] Add `energy_forecast_manual_evaluations` with one row per task/evaluation key and comparable MAPE/baseline fields.
- [ ] Add exact Worker least-privilege actions and fields; deny delete/import/export and arbitrary collection access.
- [ ] Validate JSON and inspect every FK/index/check/access-pattern pair.

## Task 2: Introduce interval-independent domain contracts

**Files:**

- Add: `m3_worker/custom_forecast_contracts.py`
- Add: `m3_worker/domain/custom_training_data.py`
- Add: `m3_worker/domain/custom_forecasting.py`
- Keep unchanged: `m3_worker/contracts.py`, `m3_worker/domain/training_data.py`, `m3_worker/domain/forecasting.py`

- [ ] Define strict request/config/run/point/evaluation models and the allowed interval set `{3600, 1800, 900, 300, 60, 30}`.
- [ ] Derive points per day, horizon, day/week season lengths and the short/full model policy from validated input.
- [ ] Parameterize resampling, gap handling, model factories, cross-validation and forecasting in the new modules.
- [ ] Preserve load clipping to non-negative and SOC clipping to `0..100`; keep explicit fallback reasons.
- [ ] Compile the new modules and run bounded fixture-free import/constructor smoke checks.

## Task 3: Add the v2 configurable observation source

**Files:**

- Modify: `m3_worker/clients/source_api.py`
- Modify: `m3_worker/clients/raw_energy_api.py`
- Modify: `m3/node_red/energy_forecast_flow.json`

- [ ] Add a separate typed v2 response whose `interval_seconds` is validated against the allowed set.
- [ ] Pass interval explicitly in the request; keep v1 paths and 15-minute behavior unchanged.
- [ ] Aggregate load by mean and SOC by last valid value using coverage derived from the source sampling cadence.
- [ ] Preserve seven-day request chunking, stable ordering, duplicate rejection, response-size bounds and configured-station allowlisting.
- [ ] Validate Node-RED JSON and use direct HTTP smoke requests against the v2 route when the local flow is available.

## Task 4: Persist and recover custom tasks

**Files:**

- Add: `m3_worker/services/custom_forecast_repository.py`
- Add: `m3_worker/services/custom_forecast_service.py`
- Modify: `m3_worker/services/job_service.py`
- Modify: `m3_worker/main.py`

- [ ] Create the run row before executor submission and return the existing row for the same station/idempotency key.
- [ ] Pull the selected history range in bounded chunks, construct both datasets, select allowed models and persist source/model manifests.
- [ ] Write points in bounded batches with immutable replay validation and a final content hash/count check.
- [ ] Mark status only after persistent evidence is complete; store stable errors on failure.
- [ ] Recover `queued/running` rows at startup without creating duplicate runs or points.
- [ ] Keep a separate bounded concurrency path so manual 30-second jobs do not block scheduled operational forecasts.

## Task 5: Expose protected task and result APIs

**Files:**

- Modify: `m3_worker/api/models.py`
- Modify: `m3_worker/api/routes.py`
- Modify: `m3_worker/api/dependencies.py` only if the existing station authorization boundary needs a typed user identity

- [ ] Add strict custom-run request and response models with no coercion of dates, integers or interval values.
- [ ] Add POST run creation and GET state/result/performance routes from the design.
- [ ] Return `202` for queued/running, `200` for terminal reads, `404` for unknown authorized IDs, and stable bounded error codes.
- [ ] Never return internal numeric PKs, service tokens, upstream bodies or stack traces.
- [ ] Exercise local request/response smoke checks without running the automated test suite.

## Task 6: Backfill actuals and calculate comparable MAPE

**Files:**

- Add: `m3_worker/services/custom_forecast_evaluation_service.py`
- Modify: scheduler/resource wiring located by `rg "AcceptanceService|run_forecast" m3_worker/main.py m3_worker/services`

- [ ] Find succeeded custom runs whose target times have elapsed and fetch actuals at the stored interval.
- [ ] Update actual fields only when source revision is newer; never mutate the persisted forecast fields.
- [ ] Calculate per-series metrics and SeasonalNaive baseline on the same target points.
- [ ] Persist three evaluation rows and expose only comparable-key rolling seven-day aggregates.
- [ ] Mark runs `evaluated` only after point and evaluation persistence verifies completely.

## Task 7: Connect the same-origin gateway and dashboard button

**Files:**

- Modify: `m3/node_red/m3_production_gateway_flow.json`
- Modify: `front/场站未来能耗预测.html`

- [ ] Add fixed POST/GET proxy routes that validate the current NocoBase user before calling Worker with server-side credentials.
- [ ] Forward only the fixed request fields and apply method, content-type, body-size, timeout and response-size limits.
- [ ] Enable the button only when controls and auth are valid; send one idempotent request and show queued/running/failed states.
- [ ] Poll the submitted run, render its actual and forecast points on one target-time axis, and restore the most recent run per station.
- [ ] Render missing MAPE/baseline as `—`; do not substitute demo numbers.
- [ ] Synchronize the exact formal HTML into the Node-RED template and compare them byte-for-byte.

## Task 8: Final static and interface verification

**Files:** all changed files from Tasks 1–7.

- [ ] Validate all JSON files with `python -m json.tool` or `jq empty`.
- [ ] Compile/import changed Python modules without invoking the automated test suite.
- [ ] Parse the dashboard script and verify there are no external assets, unsafe HTML insertion or accidental secrets.
- [ ] Smoke-check create/status/result flows and one validation failure for each input bound.
- [ ] Run `git diff --check`, review status to exclude unrelated user files, and commit only the custom forecast implementation.
