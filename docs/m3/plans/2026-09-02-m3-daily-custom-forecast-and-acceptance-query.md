# M3 Daily Custom Forecast and Acceptance Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one successful user forecast configuration run automatically every day per station and time granularity, while fixing NocoBase acceptance point reads and preventing one scheduled stage from blocking the others.

**Architecture:** Add a focused `DailyCustomForecastService` that discovers the latest completed configuration for each allowed interval and submits rolled requests through the existing custom forecast service. Refactor formal acceptance reads into a two-step direct lookup (`energy_forecast_batches` then `energy_forecast_points.batch_id`) and split scheduled forecast stages into independently alerted operations.

**Tech Stack:** Python 3.12, FastAPI 0.141.1, Pydantic 2.13.4, stdlib `unittest`, NocoBase Resource API, Docker Compose.

**Spec:** `docs/m3/specs/2026-09-02-m3-daily-custom-forecast-and-acceptance-query-design.md`

## Global Constraints

- All scheduling and rolled request boundaries use `Asia/Shanghai`; daily takeover starts at 00:17.
- The template key is station plus `interval_seconds`; only the newest `succeeded` or `evaluated` run can be a template.
- Copy `history_days`, `forecast_days`, and `interval_seconds`; derive all other forecast config through the existing request contract.
- A successful task whose `forecast_start` is today or later already covers the day and must not be duplicated.
- Never mutate, delete, archive, or overwrite an old custom forecast task or its points/evaluations.
- Keep at most three failed automatic attempts per station, date, and interval; retain every failed attempt.
- Do not issue any `batch.*` NocoBase point filter. Query complete batches directly, then query points by direct `batch_id` equality.
- `forecast`, `acceptance_backfill`, `custom_evaluation`, and `daily_custom_forecast` failures are isolated and use their own alert task names.
- `M3_ACCEPTANCE_ENABLED=false` skips only formal acceptance baseline/backfill; it must not disable rolling forecasts, custom evaluation, or daily automatic forecasts.
- Do not change the public custom forecast API, Dashboard JSON contracts, forecast model algorithms, M1, M2, M4, or Node-RED flows.

---

### Task 1: Query formal acceptance points through complete batch IDs

**Files:**
- Modify: `m3/worker/services/acceptance_service.py:45-70,426-520,724-825`
- Modify: `m3/tests/test_m3_acceptance_service.py:135-175,760-810,1015-1050`

**Interfaces:**
- Produces: immutable `CompleteAcceptanceBatch(record_id: int, issued_at: datetime, forecast_start: datetime, forecast_end: datetime)`.
- Produces: `AcceptanceService._complete_batches(station_id: str, context: AcceptanceContext) -> tuple[CompleteAcceptanceBatch, ...]`.
- Consumes: `NocoBaseApiClient.list_records(collection, filter, fields, sort)` using direct collection fields only.

- [ ] **Step 1: Extend the acceptance fake API and write failing direct-filter tests**

Add complete batch rows to `FakeApi`, route batch queries separately from point queries, and make point rows selectable by `batch_id`. Add assertions equivalent to:

```python
def test_backfill_lists_complete_batches_then_points_by_direct_batch_id(self):
    api = FakeApi(
        complete_batches=[complete_batch_row(record_id=41)],
        backfill_rows=[stored_point("station_total_load", START, batch_id=41)],
    )
    service, _, api, _, _ = make_service(api=api)

    service.backfill_actuals(
        "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
    )

    batch_call = next(call for call in api.list_calls
                      if call.collection == "energy_forecast_batches")
    self.assertEqual(batch_call.filter, {
        "station_id": "station-1",
        "acceptance_run_id": "run-20260825",
        "write_state": "complete",
    })
    point_calls = [call for call in api.list_calls
                   if call.collection == "energy_forecast_points"]
    self.assertTrue(point_calls)
    self.assertTrue(all(call.filter["batch_id"] == 41 for call in point_calls))
    self.assertTrue(all(not any("." in key for key in call.filter)
                        for call in point_calls))
```

Also add cases rejecting duplicate batch IDs, foreign station/run IDs, non-24-hour windows, overlapping dates, and a point returned under the wrong batch ID.

- [ ] **Step 2: Write a failing baseline test proving backfill cannot block publication**

Use a fake API that raises `sink_http_failed` if a point list is attempted, call `run_baseline()` at exact 01:02, and assert `publish_acceptance()` still receives 192 points and no point list call occurs before publication.

- [ ] **Step 3: Run the focused acceptance tests and confirm the old relation filters fail them**

Run:

```bash
python -m unittest m3.tests.test_m3_acceptance_service -v
```

Expected: FAIL because `_list_backfill_points()` and `_series_evaluation()` still emit `batch.*`, and `run_baseline()` still calls `_backfill_locked()` before publishing.

- [ ] **Step 4: Implement strict complete-batch parsing and direct point queries**

Add the immutable value type and fixed fields:

```python
@dataclass(frozen=True)
class CompleteAcceptanceBatch:
    record_id: int
    issued_at: datetime
    forecast_start: datetime
    forecast_end: datetime


COMPLETE_BATCH_FIELDS = (
    "id", "station_id", "acceptance_run_id", "issued_at",
    "forecast_start_time", "forecast_end_time", "write_state",
)
```

Implement `_complete_batches()` with this fixed request and strict identity/window validation:

```python
rows = self._api.list_records(
    "energy_forecast_batches",
    filter={
        "station_id": station_id,
        "acceptance_run_id": acceptance_run_id,
        "write_state": "complete",
    },
    fields=list(COMPLETE_BATCH_FIELDS),
    sort=["issued_at"],
)
```

Refactor both backfill and evaluation to iterate returned batches and query each series with direct filters:

```python
filter={
    "batch_id": batch.record_id,
    "unique_id": unique_id,
    "data_time": {
        "$gte": lower.isoformat(),
        "$lt": upper.isoformat(),
    },
}
```

Include `batch_id` in `BACKFILL_FIELDS` and `EVALUATION_FIELDS` so the response can be checked against the requested batch. Preserve the existing global duplicate, ordering, point-count, physical-bound, and source-revision checks after merging batches.

- [ ] **Step 5: Remove baseline-before-backfill coupling**

Delete the `_backfill_locked(station_id, as_of, context)` call from `run_baseline()`. Keep this order: validate slot and active window, generate snapshot, build payload, `publish_acceptance()`, then `sync_progress()`.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
python -m unittest m3.tests.test_m3_acceptance_service -v
git diff --check
```

Expected: all acceptance service tests PASS.

Commit:

```bash
git add m3/worker/services/acceptance_service.py m3/tests/test_m3_acceptance_service.py
git commit -m "fix(m3): query acceptance points by batch id"
```

---

### Task 2: Align the NocoBase machine contract with direct acceptance filters

**Files:**
- Modify: `m3/contracts/nocobase_collections.json:1103-1145,1184-1255`
- Modify: `m3/tests/test_m3_collection_contract.py:420-535,650-690,1200-1270,1490-1520`

**Interfaces:**
- Consumes: `COMPLETE_BATCH_FIELDS`, `BACKFILL_FIELDS`, and `EVALUATION_FIELDS` from Task 1.
- Produces: exact Worker list permissions for batches and points without relation-filter requirements.

- [ ] **Step 1: Replace relation-filter assertions with failing direct-filter assertions**

Update the contract test fake so `AcceptanceService` receives one complete batch and verifies calls like:

```python
self.assertEqual(batch_call["filter"], {
    "station_id": "station-1",
    "acceptance_run_id": "run-20260825",
    "write_state": "complete",
})
self.assertEqual(point_call["filter"]["batch_id"], 1)
self.assertEqual(
    [key for key in point_call["filter"] if "." in key],
    [],
)
```

Assert the Worker point-list filter allowlist is exactly
`["batch_id", "unique_id", "data_time"]` and that the batch list still allows
`station_id`, `acceptance_run_id`, `write_state`, sorted by `issued_at`.

- [ ] **Step 2: Run the collection contract test and confirm it fails on dotted permissions**

Run:

```bash
python -m unittest m3.tests.test_m3_collection_contract -v
```

Expected: FAIL because the JSON contract still requires the three `batch.*` filters.

- [ ] **Step 3: Update the machine-readable contract**

Keep the physical belongs-to relationship, because NocoBase and the foreign key still use it, but make it non-required for Worker reads:

```json
"required_dotted_filters": [],
"installed_api_documentation": {
  "gate": "hard_pre_production",
  "evidence_required": [
    "association_alias_batch_uses_existing_batch_id_foreign_key",
    "direct_batch_list_filter_probe",
    "direct_point_batch_id_filter_probe"
  ],
  "if_unsupported": "stop_update_machine_contract_and_tests_then_rereview",
  "alternate_query_path": "direct_batch_then_point_batch_id"
}
```

Change the Worker point list filter permission to:

```json
"read": [
  "id", "batch_id", "unique_id", "data_time", "target_time",
  "horizon_step", "model_name", "raw_forecast", "forecast_value",
  "is_clipped", "actual_value", "actual_quality", "actual_source_revision"
],
"filter": ["batch_id", "unique_id", "data_time"]
```

Replace the installed API documentation evidence marker
`points_batch_dotted_relation_filter_permissions` with direct batch and point filter evidence. Do not remove the physical `batch_id` foreign key, index, or NocoBase association metadata.

- [ ] **Step 4: Run contract and acceptance tests, then commit**

Run:

```bash
python -m unittest \
  m3.tests.test_m3_collection_contract \
  m3.tests.test_m3_acceptance_service -v
git diff --check
```

Expected: both modules PASS and no active assertion requires a dotted point filter.

Commit:

```bash
git add m3/contracts/nocobase_collections.json m3/tests/test_m3_collection_contract.py
git commit -m "fix(m3): allow direct acceptance point reads"
```

---

### Task 3: Add bounded repository queries for daily forecast templates

**Files:**
- Modify: `m3/worker/services/custom_forecast_repository.py:225-405`
- Modify: `m3/tests/test_m3_custom_forecast_repository.py:115-220`
- Modify: `m3/contracts/nocobase_collections.json:1290-1332`
- Modify: `m3/tests/test_m3_collection_contract.py:560-620,700-735`

**Interfaces:**
- Produces: `CustomForecastRepository.latest_completed_template(station_id: str, *, interval_seconds: int) -> StoredCustomRun | None`.
- Produces: `CustomForecastRepository.list_daily_runs(station_id: str, *, forecast_start: datetime) -> list[StoredCustomRun]`.
- Produces: constant `DAILY_REQUESTED_BY = "m3_daily_scheduler"` in the repository module for one shared persisted identity.

- [ ] **Step 1: Write failing repository tests for latest-per-interval selection**

Add a fake that validates this exact request:

```python
filter={
    "station_id": "ES01",
    "interval_seconds": 900,
    "status": {"$in": ["succeeded", "evaluated"]},
},
fields=RUN_FIELDS,
sort=["-completed_at", "-createdAt"],
```

Return a newer valid evaluated row and an older succeeded row; assert the newer row is returned. Add cases for no rows, malformed newest row, and one interval query not selecting another interval.

- [ ] **Step 2: Write failing repository tests for today's automatic runs**

Assert `list_daily_runs()` sends:

```python
filter={
    "station_id": "ES01",
    "forecast_start": "2026-09-02T00:00:00+08:00",
    "requested_by": "m3_daily_scheduler",
},
fields=RUN_FIELDS,
sort=["createdAt"],
```

Reject naive/non-Shanghai/non-midnight `forecast_start` before issuing an API request.

- [ ] **Step 3: Run repository tests and verify the methods are absent**

Run:

```bash
python -m unittest m3.tests.test_m3_custom_forecast_repository -v
```

Expected: FAIL with missing `latest_completed_template` and `list_daily_runs`.

- [ ] **Step 4: Implement the two fixed repository methods**

Use `ALLOWED_INTERVAL_SECONDS` and `validate_shanghai_timestamp` to reject invalid arguments. `latest_completed_template()` uses one first-page sorted query and returns only `rows[0]` after strict parsing. `list_daily_runs()` uses a complete paginated query because it must see up to eighteen retained attempts:

```python
def latest_completed_template(self, station_id, *, interval_seconds):
    rows = self._api.list_records(
        RUNS,
        filter={
            "station_id": station_id,
            "interval_seconds": interval_seconds,
            "status": {"$in": ["succeeded", "evaluated"]},
        },
        fields=RUN_FIELDS,
        sort=["-completed_at", "-createdAt"],
    )
    return None if not rows else self._parse_run(rows[0])
```

Require each row from `list_daily_runs()` to have `requested_by == DAILY_REQUESTED_BY` and `config.forecast_start == forecast_start`; otherwise raise `sink_contract_invalid`.

- [ ] **Step 5: Expand only the required manual-run list permissions**

Change `energy_forecast_manual_runs.fields_by_action.list` to include:

```json
"filter": [
  "run_id", "station_id", "idempotency_key", "status",
  "interval_seconds", "forecast_start", "requested_by"
],
"sort": ["createdAt", "completed_at"]
```

Update exact contract tests. Do not grant create, delete, arbitrary update, or additional write fields.

- [ ] **Step 6: Run repository and contract tests, then commit**

Run:

```bash
python -m unittest \
  m3.tests.test_m3_custom_forecast_repository \
  m3.tests.test_m3_collection_contract -v
git diff --check
```

Expected: PASS.

Commit:

```bash
git add \
  m3/worker/services/custom_forecast_repository.py \
  m3/tests/test_m3_custom_forecast_repository.py \
  m3/contracts/nocobase_collections.json \
  m3/tests/test_m3_collection_contract.py
git commit -m "feat(m3): discover completed forecast templates"
```

---

### Task 4: Implement the daily custom forecast coordinator

**Files:**
- Create: `m3/worker/services/daily_custom_forecast_service.py`
- Create: `m3/tests/test_m3_daily_custom_forecast_service.py`
- Modify: `m3/worker/services/__init__.py`

**Interfaces:**
- Consumes: `CustomForecastRepository.latest_completed_template()` and `list_daily_runs()` from Task 3.
- Consumes: `CustomForecastService.submit(station_id, request, requested_by=...) -> StoredCustomRun`.
- Produces: `DailyCustomForecastService.run_station(station_id: str, now: datetime) -> int`, returning the number of submit/reschedule calls attempted in that sweep.
- Produces: stable `M3Error` code `daily_custom_forecast_failed` after three retained failed attempts.

- [ ] **Step 1: Create test helpers and a failing test for copying the full latest configuration**

Build two completed templates for ES01 with 900-second and 3600-second intervals. Assert a 2026-09-02 00:17 sweep submits two requests and preserves each template independently:

```python
self.assertEqual(request_900.interval_seconds, 900)
self.assertEqual(request_900.forecast_days, template_900.config.forecast_days)
self.assertEqual(request_900.history_end, DAY)
self.assertEqual(
    request_900.history_start,
    DAY - timedelta(days=template_900.config.history_days),
)
```

Also assert `requested_by == "m3_daily_scheduler"` and the existing `CustomForecastRequest` derives the same `model_policy` from the preserved history length.

- [ ] **Step 2: Add failing schedule-boundary and same-day coverage tests**

Cover these exact cases:

- 00:02 returns 0 without repository or submit calls.
- 00:17 creates missing tasks.
- A latest completed manual template with `forecast_start == DAY` returns 0 for that interval.
- A latest template with `forecast_start > DAY` also returns 0.
- No completed template is a normal empty state and emits no error.

- [ ] **Step 3: Add failing idempotency, queue recovery, and retry tests**

Assert:

- `running`, `succeeded`, or `evaluated` automatic rows do not submit a duplicate.
- A `queued` automatic row is passed back through `submit()` with its original idempotency key, allowing executor-capacity recovery.
- One or two `failed` rows create the next attempt with the same rolled config and a new deterministic key.
- Three failed rows raise `M3Error(code="daily_custom_forecast_failed")` and create no fourth row.
- A failure for 30-second ES01 still allows 900-second ES01 and ES02 to be attempted by their own service calls.
- Reconstructing the service after a simulated restart produces the same idempotency key for the same station/date/config/attempt.

- [ ] **Step 4: Run the new test module and verify the service is absent**

Run:

```bash
python -m unittest m3.tests.test_m3_daily_custom_forecast_service -v
```

Expected: FAIL because `DailyCustomForecastService` does not exist.

- [ ] **Step 5: Implement deterministic request rolling and attempt keys**

Use fixed constants and SHA-256; never place a raw long station/run ID in the 128-character key:

```python
DAILY_START = time(hour=0, minute=17)
MAX_DAILY_ATTEMPTS = 3

def _roll_request(config, day, key):
    return CustomForecastRequest(
        history_start=day - timedelta(days=config.history_days),
        history_end=day,
        forecast_days=config.forecast_days,
        interval_seconds=config.interval_seconds,
        idempotency_key=key,
    )

def _attempt_key(station_id, day, config, attempt):
    identity = (
        f"{station_id}|{day.date().isoformat()}|{config.interval_seconds}|"
        f"{config.history_days}|{config.forecast_days}"
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"m3-daily:{day:%Y%m%d}:{config.interval_seconds}:{digest}:{attempt}"
```

Strictly validate Shanghai-aware `now`, canonicalize `day = now.replace(hour=0, minute=0, second=0, microsecond=0)`, and protect the completed-date cache with a lock.

- [ ] **Step 6: Implement per-interval orchestration without modifying old rows**

Loop `ALLOWED_INTERVAL_SECONDS`. For each interval:

1. Read the latest completed template and today's automatic rows.
2. Treat a completed template whose `forecast_start >= day` as already covered.
3. If a matching auto run is `running/succeeded/evaluated`, do nothing.
4. If it is `queued`, rebuild its exact request and call `submit()` using the same key.
5. If only failed attempts exist, rebuild from the first attempt's persisted config and create attempt `len(failed)+1`.
6. If no daily row exists, roll from the completed template and create attempt 1.
7. Catch one interval's error, continue remaining intervals, then raise the first safe `M3Error` so the scheduler emits one station-stage alert.

Cache a station/date only when every discovered interval is in `succeeded/evaluated` or already covered by a completed manual task. Do not cache the empty-template case, so a user completing their first task later that day is discovered without a restart; same-day coverage prevents a duplicate automatic run.

- [ ] **Step 7: Run the daily service and existing custom forecast tests, then commit**

Run:

```bash
python -m unittest \
  m3.tests.test_m3_daily_custom_forecast_service \
  m3.tests.test_m3_custom_forecast_service \
  m3.tests.test_m3_custom_forecast_repository -v
git diff --check
```

Expected: PASS.

Commit:

```bash
git add \
  m3/worker/services/daily_custom_forecast_service.py \
  m3/worker/services/__init__.py \
  m3/tests/test_m3_daily_custom_forecast_service.py
git commit -m "feat(m3): repeat completed forecasts daily"
```

---

### Task 5: Isolate scheduled forecast stages and attach daily execution

**Files:**
- Modify: `m3/worker/scheduler/runner.py:12-40,55-110,180-260`
- Modify: `m3/tests/test_m3_scheduler.py:15-180`

**Interfaces:**
- Consumes: `DailyCustomForecastService.run_station()` from Task 4.
- Preserves: `SchedulerRunner.run_manual()` error propagation for manual rolling/model-selection jobs.
- Produces: scheduled stage alert names `forecast`, `acceptance_backfill`, `custom_evaluation`, and `daily_custom_forecast`.

- [ ] **Step 1: Write failing tests for stage isolation**

Add recording evaluation and daily services. Configure acceptance backfill to raise
`M3Error("sink_http_failed", "NocoBase action failed")`, tick at 01:17, and assert:

```python
self.assertEqual(forecast.calls, [("forecast", "station-1", T0117)])
self.assertEqual(evaluations.calls, [("evaluate", "station-1", T0117)])
self.assertEqual(daily.calls, [("daily", "station-1", T0117)])
self.assertEqual(
    alerts,
    [("station-1", "acceptance_backfill", "sink_http_failed", T0117)],
)
```

Add symmetrical cases showing a rolling forecast failure still allows the other three stages, custom evaluation failure still allows daily submission, and one station's failures do not block station two.

- [ ] **Step 2: Write failing acceptance-disabled and manual-error tests**

Assert `acceptance_enabled=False` skips baseline and backfill while still calling evaluation and daily services. Assert `run_manual("station-1", "forecast", now)` still raises a primary forecast failure instead of silently converting it into an alert-only success.

- [ ] **Step 3: Run scheduler tests and verify the serialized body fails them**

Run:

```bash
python -m unittest m3.tests.test_m3_scheduler -v
```

Expected: FAIL because an acceptance exception currently prevents evaluation and no daily service is attached.

- [ ] **Step 4: Add a scheduled-only stage runner**

Add an optional constructor dependency `daily_custom_forecast_service=None`. Keep the existing manual operation body intact, but make scheduled `forecast` slots use a new method:

```python
def _run_scheduled_forecast(self, station_id, at):
    stages = [("forecast", lambda: self._forecast.run_forecast(station_id, at))]
    if self._acceptance_enabled:
        stages.append((
            "acceptance_backfill",
            lambda: self._acceptance.backfill_actuals(station_id, at),
        ))
    if self._custom_evaluations is not None:
        stages.append((
            "custom_evaluation",
            lambda: self._custom_evaluations.evaluate_station(station_id, at),
        ))
    if self._daily_custom_forecasts is not None:
        stages.append((
            "daily_custom_forecast",
            lambda: self._daily_custom_forecasts.run_station(station_id, at),
        ))
    for task, operation in stages:
        try:
            operation()
        except Exception as error:
            self._safe_alert(station_id, task, _safe_error_code(error), at)
```

In `tick()`, reserve the same `(station, "forecast", minute)` key, acquire the station lock once, and call `_run_scheduled_forecast()`. Other slots continue through `_run_locked()` so baseline/model-selection semantics remain unchanged.

- [ ] **Step 5: Run scheduler tests and commit**

Run:

```bash
python -m unittest m3.tests.test_m3_scheduler -v
git diff --check
```

Expected: PASS, including exact minute deduplication and lifecycle tests.

Commit:

```bash
git add m3/worker/scheduler/runner.py m3/tests/test_m3_scheduler.py
git commit -m "fix(m3): isolate scheduled forecast stages"
```

---

### Task 6: Wire startup catch-up and resource ownership

**Files:**
- Modify: `m3/worker/main.py:20-40,105-245,295-365`
- Modify: `m3/tests/test_m3_worker_api.py:65-125,500-620,610-820`

**Interfaces:**
- Consumes: `DailyCustomForecastService` from Task 4.
- Produces: `WorkerResources.daily_custom_forecasts`.
- Produces: startup catch-up after custom queued/running recovery and before scheduler thread start.

- [ ] **Step 1: Write failing resource assembly tests**

Patch `DailyCustomForecastService` during `build_resources()` and assert it receives the exact `CustomForecastRepository` and `CustomForecastService` instances. Assert the constructed scheduler receives that daily service.

- [ ] **Step 2: Write failing startup catch-up tests**

Extend the fake resources with a recording daily service. At a clock time after 00:17, assert `recover()` calls:

```python
[
    ("custom_recover",),
    ("daily", "ES01", NOW),
    ("daily", "ES02", NOW),
]
```

Assert one station's `daily_custom_forecast_failed` emits a
`daily_custom_forecast` alert, still attempts the other station, and makes recovery readiness false without undoing other recovered state. Add a before-00:17 case; the service itself returns 0 and no task is created.

- [ ] **Step 3: Run Worker API tests and confirm assembly is missing**

Run:

```bash
python -m unittest m3.tests.test_m3_worker_api -v
```

Expected: FAIL because `WorkerResources` and `build_resources()` do not own a daily service.

- [ ] **Step 4: Add resource construction and recovery calls**

Import and construct:

```python
daily_custom_forecasts = DailyCustomForecastService(
    custom_repository,
    custom_forecasts,
)
```

Pass it to `SchedulerRunner`, store it on `WorkerResources`, and after
`custom_forecasts.recover()` loop all configured station IDs:

```python
for station_id in self.settings.station_ids:
    try:
        self.daily_custom_forecasts.run_station(station_id, raw_now)
    except Exception as error:
        failed = True
        self._alert(station_id, "daily_custom_forecast", error, recovery_at)
```

Do not add it to `close()` because the coordinator owns no thread, socket, or executor.

- [ ] **Step 5: Run assembly, scheduler, and daily tests, then commit**

Run:

```bash
python -m unittest \
  m3.tests.test_m3_worker_api \
  m3.tests.test_m3_scheduler \
  m3.tests.test_m3_daily_custom_forecast_service -v
git diff --check
```

Expected: PASS.

Commit:

```bash
git add m3/worker/main.py m3/tests/test_m3_worker_api.py
git commit -m "feat(m3): recover daily forecasts on startup"
```

---

### Task 7: Add the production rollout runbook and verify the complete M3 change

**Files:**
- Modify: `docs/m3/deploy/production-env/README.md`
- Modify: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Consumes: direct batch and point filter permissions from Tasks 1-3.
- Produces: operator sequence for deploying with acceptance disabled, probing direct filters, replacing unrecoverable formal acceptance windows, and re-enabling acceptance.

- [ ] **Step 1: Add a focused deployment-artifact test for required rollout markers**

Assert the README contains all of these exact operational concepts:

```python
for marker in (
    "M3_ACCEPTANCE_ENABLED=false",
    "energy_forecast_batches",
    "batch_id",
    "不得使用 `batch.*`",
    "m3_daily_scheduler",
    "M3_ACCEPTANCE_ENABLED=true",
    "新建七日验收任务",
):
    self.assertIn(marker, guide)
```

- [ ] **Step 2: Run the deployment artifact test and confirm the runbook is absent**

Run:

```bash
python -m unittest m3.tests.test_m3_deployment_artifacts -v
```

Expected: FAIL on the new rollout markers.

- [ ] **Step 3: Document the exact safe production order**

Add a section that tells the operator to:

1. Deploy with `M3_ACCEPTANCE_ENABLED=false` and verify daily auto tasks plus custom evaluation.
2. Grant only the direct batch and point filter fields from the machine contract.
3. Probe `energy_forecast_batches` using direct `station_id`, `acceptance_run_id`, `write_state`.
4. Probe `energy_forecast_points` using direct `batch_id`, `unique_id`, `data_time`; explicitly forbid `batch.*`.
5. Mark the incomplete `acceptance-20260829-station1/2` runs cancelled without deleting evidence.
6. Create new seven-day tasks beginning at a future Shanghai 01:00.
7. Set `M3_ACCEPTANCE_ENABLED=true`, rebuild only the Worker, and monitor stage-specific alerts.
8. Verify each first 01:02 baseline has one complete batch and 96 points per series.

Do not include API keys, tokens, production secrets, or commands that delete records.

- [ ] **Step 4: Run the full Python M3 suite in a Python 3.12 dependency environment**

Run:

```bash
python -m unittest discover -s tests -p 'test_m3_*.py' -v
```

Expected: all M3 Python tests PASS. If the host still lacks locked dependencies, build the existing image and mount the checkout read-only for the test command:

```bash
docker build --platform linux/amd64 -t vifa-m3:test .
docker run --rm --platform linux/amd64 \
  --volume "$PWD:/workspace:ro" \
  --workdir /workspace \
  --entrypoint python \
  vifa-m3:test \
  -m unittest discover -s tests -p 'test_m3_*.py' -v
```

- [ ] **Step 5: Run JavaScript contracts, syntax checks, and diff checks**

Run:

```bash
node tests/test_m3_node_red_contract.js
node m3/tests/test_m3_dashboard_auth_modes.js
python -m compileall -q m3/worker
python -m json.tool m3/contracts/nocobase_collections.json >/dev/null
git diff --check
git status --short
```

Expected: both Node tests exit 0, Python compilation and JSON parsing exit 0, diff check is clean, and status contains only intended files before the final commit.

- [ ] **Step 6: Commit the rollout documentation**

```bash
git add docs/m3/deploy/production-env/README.md m3/tests/test_m3_deployment_artifacts.py
git commit -m "docs(m3): add automatic forecast rollout checks"
```

- [ ] **Step 7: Review the final branch diff and record production commands for handoff**

Run:

```bash
git log --oneline --decorate origin/feat/m3-weekly-load-forecast..HEAD
git diff --stat origin/feat/m3-weekly-load-forecast...HEAD
git diff --check origin/feat/m3-weekly-load-forecast...HEAD
```

Expected: the diff contains only M3 Worker, M3 contracts/tests, the approved spec/plan, and the M3 production runbook; no M1/M2/M4 or unrelated user files appear.
