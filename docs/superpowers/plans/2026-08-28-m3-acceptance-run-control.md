# M3 Acceptance Run Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect M3 formal seven-day acceptance directly to the NocoBase `energy_forecast_acceptance_runs` task table so operators create runs manually and the Worker maintains progress and final outcome summaries.

**Architecture:** Add the fifth collection and least-privilege Worker permissions to the machine contract. Introduce a focused `AcceptanceRunService` over the existing fixed `NocoBaseApiClient`; `AcceptanceService` asks it for each station's active run, writes immutable detail evidence through the existing sink, and synchronizes only the task summary fields. Production Node-RED remains unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, httpx, FastAPI lifecycle resources, NocoBase Resource API, PostgreSQL constraints documented in the machine contract, Docker Compose, JSON.

**Spec:** `docs/superpowers/specs/2026-08-28-m3-acceptance-run-control-design.md`

## Global Constraints

- Production remains AMD64 Docker with separate Worker and Dashboard containers.
- `energy_forecast_acceptance_runs` already exists in production with the approved fields, PostgreSQL checks, unique constraint, and one-active-run partial unique index.
- Operators alone write `station_id`, `acceptance_run_id`, `window_start`, `window_end`, and `control_state` in NocoBase.
- Worker may list task rows and update only `completed_days`, `result_state`, and `calculated_at` by numeric `id`.
- Existing four detail collections and the public Dashboard JSON contract do not change.
- `m3_production_gateway_flow.json` and all other Node-RED flows do not change.
- `M3_ACCEPTANCE_ENABLED` remains `false` in templates and is enabled in production only after the new image and an active run are ready.
- Per the user's explicit instruction, do not add or run automated tests. Use only JSON, Python import/compile, Compose, and Git static verification.

---

### Task 1: Add the acceptance-run collection to the machine contract

**Files:**
- Modify: `m3/contracts/nocobase_collections.json`

**Interfaces:**
- Consumes: the already-created NocoBase fields and PostgreSQL constraints from the approved spec.
- Produces: an exact collection/permission contract consumed by deployment review and by the fixed field lists in `AcceptanceRunService`.

- [ ] **Step 1: Bump the contract version and document the NocoBase system-field exception**

Change the top-level version from `1` to `2`. Keep `lowercase_snake_case` for M3-owned identifiers and add this exact exception under `database_policy`:

```json
"identifier_exceptions": {
  "nocobase_system_timestamps": ["createdAt", "updatedAt"]
}
```

- [ ] **Step 2: Add the fifth collection after `energy_forecast_latest`**

Add `energy_forecast_acceptance_runs` with these exact fields:

```json
{
  "name": "energy_forecast_acceptance_runs",
  "fields": {
    "id": {
      "type": "bigint",
      "nullable": false,
      "identity": true,
      "primary_key": true
    },
    "station_id": {"type": "text", "nullable": false},
    "acceptance_run_id": {"type": "text", "nullable": false},
    "window_start": {"type": "timestamptz", "nullable": false},
    "window_end": {"type": "timestamptz", "nullable": false},
    "control_state": {"type": "text", "nullable": false},
    "completed_days": {"type": "smallint", "nullable": false, "default": 0},
    "result_state": {"type": "text", "nullable": false, "default": "pending"},
    "calculated_at": {"type": "timestamptz", "nullable": true},
    "createdAt": {
      "type": "timestamptz",
      "nullable": false,
      "client_writable": false,
      "nocobase_system_field": true
    },
    "updatedAt": {
      "type": "timestamptz",
      "nullable": false,
      "client_writable": false,
      "nocobase_system_field": true
    }
  },
  "unique_constraints": [
    {
      "name": "energy_forecast_acceptance_runs_station_run_key",
      "fields": ["station_id", "acceptance_run_id"]
    }
  ],
  "foreign_keys": [],
  "indexes": [
    {
      "name": "energy_forecast_acceptance_runs_station_run_key",
      "fields": ["station_id", "acceptance_run_id"],
      "unique": true,
      "provided_by": "unique_constraint",
      "access_pattern": {
        "equality_prefix": ["station_id", "acceptance_run_id"],
        "range_suffix": []
      }
    },
    {
      "name": "energy_forecast_acceptance_runs_one_active_per_station_idx",
      "fields": ["station_id"],
      "unique": true,
      "provided_by": "partial_unique_index",
      "predicate": {"control_state": "active"},
      "access_pattern": {
        "equality_prefix": ["station_id", "control_state"],
        "range_suffix": []
      }
    },
    {
      "name": "energy_forecast_acceptance_runs_state_station_idx",
      "fields": ["control_state", "station_id"],
      "unique": false,
      "provided_by": "explicit_index",
      "access_pattern": {
        "equality_prefix": ["control_state", "station_id"],
        "range_suffix": []
      }
    }
  ],
  "check_constraints": [
    {
      "name": "energy_forecast_acceptance_runs_station_id_check",
      "kind": "non_empty_text",
      "field": "station_id"
    },
    {
      "name": "energy_forecast_acceptance_runs_run_id_check",
      "kind": "regex",
      "field": "acceptance_run_id",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    },
    {
      "name": "energy_forecast_acceptance_runs_window_check",
      "kind": "timestamp_interval",
      "start_field": "window_start",
      "end_field": "window_end",
      "interval_days": 7
    },
    {
      "name": "energy_forecast_acceptance_runs_window_alignment_check",
      "kind": "timezone_wall_clock",
      "fields": ["window_start", "window_end"],
      "timezone": "Asia/Shanghai",
      "hour": 1,
      "minute": 0,
      "second": 0
    },
    {
      "name": "energy_forecast_acceptance_runs_control_state_check",
      "kind": "enum",
      "field": "control_state",
      "allowed_values": ["active", "completed", "cancelled"]
    },
    {
      "name": "energy_forecast_acceptance_runs_result_state_check",
      "kind": "enum",
      "field": "result_state",
      "allowed_values": ["pending", "in_progress", "passed", "failed", "insufficient_data"]
    },
    {
      "name": "energy_forecast_acceptance_runs_completed_days_check",
      "kind": "range",
      "field": "completed_days",
      "range": {"minimum": 0, "maximum": 7}
    },
    {
      "name": "energy_forecast_acceptance_runs_progress_check",
      "kind": "acceptance_summary_state",
      "state_field": "result_state",
      "days_field": "completed_days",
      "calculated_at_field": "calculated_at"
    },
    {
      "name": "energy_forecast_acceptance_runs_completion_check",
      "kind": "conditional_enum",
      "when": {"field": "control_state", "equals": "completed"},
      "field": "result_state",
      "allowed_values": ["passed", "failed", "insufficient_data"]
    },
    {
      "name": "energy_forecast_acceptance_runs_audit_time_check",
      "kind": "field_comparison",
      "left_field": "updatedAt",
      "operator": ">=",
      "right_field": "createdAt"
    }
  ],
  "application_invariants": [
    "operators_write_identity_window_and_control_state_only",
    "worker_updates_completed_days_result_state_and_calculated_at_only",
    "pending_requires_zero_days_and_null_calculated_at",
    "in_progress_requires_one_to_seven_days_and_null_calculated_at",
    "terminal_result_requires_seven_days_and_calculated_at_not_before_window_end",
    "completed_control_state_requires_a_terminal_result",
    "summary_is_recoverable_from_complete_batches_and_evaluations"
  ]
}
```

- [ ] **Step 3: Extend the optional station relation scope**

Add `energy_forecast_acceptance_runs` to `optional_station_relation.when_configured.affected_collections` because it owns a `station_id` identity.

- [ ] **Step 4: Grant the Worker least privilege on the new collection**

Add this entry under `worker_role.collections`:

```json
"energy_forecast_acceptance_runs": {
  "allowed_actions": ["list", "update"],
  "denied_actions": ["get", "create", "updateOrCreate", "firstOrCreate", "destroy", "delete", "export", "import"],
  "fields_by_action": {
    "list": {
      "read": ["id", "station_id", "acceptance_run_id", "window_start", "window_end", "control_state", "completed_days", "result_state", "calculated_at"],
      "filter": ["station_id", "acceptance_run_id", "control_state"],
      "sort": [],
      "write": [],
      "record_key": []
    },
    "update": {
      "read": ["id", "station_id", "acceptance_run_id", "window_start", "window_end", "control_state", "completed_days", "result_state", "calculated_at"],
      "filter": [],
      "sort": [],
      "write": ["completed_days", "result_state", "calculated_at"],
      "record_key": ["id"]
    }
  },
  "protected_fields": ["id", "station_id", "acceptance_run_id", "window_start", "window_end", "control_state", "createdAt", "updatedAt"]
}
```

Do not add the collection to `dashboard_role.collections`.

- [ ] **Step 5: Perform static contract validation**

Run:

```bash
python3 -m json.tool m3/contracts/nocobase_collections.json >/dev/null
python3 - <<'PY'
import json
from pathlib import Path

contract = json.loads(Path("m3/contracts/nocobase_collections.json").read_text())
collections = {item["name"]: item for item in contract["collections"]}
assert contract["version"] == 2
assert "energy_forecast_acceptance_runs" in collections
worker = contract["worker_role"]["collections"]["energy_forecast_acceptance_runs"]
assert worker["allowed_actions"] == ["list", "update"]
assert set(worker["fields_by_action"]["update"]["write"]) == {
    "completed_days", "result_state", "calculated_at"
}
assert "energy_forecast_acceptance_runs" not in contract["dashboard_role"]["collections"]
PY
git diff --check
```

Expected: all commands exit 0 with no output.

- [ ] **Step 6: Commit the machine contract**

```bash
git add m3/contracts/nocobase_collections.json
git commit -m "feat(m3): 定义七日验收任务表合同"
```

---

### Task 2: Implement the fixed acceptance-run repository

**Files:**
- Create: `m3_worker/services/acceptance_run_service.py`

**Interfaces:**
- Consumes: `NocoBaseApiClient.list_records()` and `NocoBaseApiClient.update_record()`.
- Produces: `AcceptanceRunRecord`, `AcceptanceRunService.active_run()`, `sync_progress()`, `sync_result()`, and `reconcile_active()` for `AcceptanceService`.

- [ ] **Step 1: Define the exact record and constants**

Create a frozen dataclass and fixed field sets:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Literal

from m3_worker.contracts import validate_shanghai_timestamp
from m3_worker.errors import M3Error

CONTROL_STATES = frozenset({"active", "completed", "cancelled"})
RESULT_STATES = frozenset(
    {"pending", "in_progress", "passed", "failed", "insufficient_data"}
)
TERMINAL_RESULTS = frozenset({"passed", "failed", "insufficient_data"})
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
RUN_FIELDS = (
    "id", "station_id", "acceptance_run_id", "window_start", "window_end",
    "control_state", "completed_days", "result_state", "calculated_at",
)
RUN_COLLECTION = "energy_forecast_acceptance_runs"

@dataclass(frozen=True)
class AcceptanceRunRecord:
    id: int
    station_id: str
    acceptance_run_id: str
    window_start: datetime
    window_end: datetime
    control_state: Literal["active", "completed", "cancelled"]
    completed_days: int
    result_state: Literal[
        "pending", "in_progress", "passed", "failed", "insufficient_data"
    ]
    calculated_at: datetime | None
```

- [ ] **Step 2: Implement strict NocoBase row validation**

Implement private helpers that:

- Require `type(raw) is dict` and `set(raw) == set(RUN_FIELDS)`.
- Require `id` and `completed_days` to be exact integers, never booleans.
- Require exact strings and the run-ID regex.
- Parse timestamps with `datetime.fromisoformat(value.replace("Z", "+00:00"))` only when the origin is an exact string or datetime.
- Call `validate_shanghai_timestamp(..., quarter_hour=True)` for both window fields.
- Require both wall-clock times to be exactly 01:00:00 and `window_end - window_start == timedelta(days=7)`.
- Validate `pending`, `in_progress`, and terminal summary invariants exactly as specified.
- Raise `M3Error("acceptance_context_invalid", "Acceptance run record is invalid")` for identity/window/origin failures.
- Raise `M3Error("acceptance_summary_invalid", "Acceptance run summary is invalid")` for invalid progress/result combinations.

Expose one private method:

```python
def _run_record(raw: object) -> AcceptanceRunRecord:
    ...
```

- [ ] **Step 3: Implement active and identity reads**

Implement:

```python
class AcceptanceRunService:
    def __init__(self, api) -> None:
        self._api = api

    def active_run(self, station_id: str) -> AcceptanceRunRecord | None:
        rows = self._api.list_records(
            RUN_COLLECTION,
            filter={"station_id": station_id, "control_state": "active"},
            fields=list(RUN_FIELDS),
        )
        if len(rows) > 1:
            raise M3Error(
                "acceptance_context_invalid",
                "Station has multiple active acceptance runs",
            )
        return None if not rows else _run_record(rows[0])

    def _identity_run(
        self, station_id: str, acceptance_run_id: str
    ) -> AcceptanceRunRecord:
        rows = self._api.list_records(
            RUN_COLLECTION,
            filter={
                "station_id": station_id,
                "acceptance_run_id": acceptance_run_id,
            },
            fields=list(RUN_FIELDS),
        )
        if len(rows) != 1:
            raise M3Error(
                "acceptance_context_invalid",
                "Acceptance run identity is unavailable",
            )
        return _run_record(rows[0])
```

After parsing, verify the returned row exactly matches the requested station and run. `active_run()` must also verify `control_state == "active"` even though the filter requested it.

- [ ] **Step 4: Derive completed-day progress from immutable batches**

Implement a fixed batch query for `write_state=complete`, `station_id`, and `acceptance_run_id`. Read exactly:

```python
BATCH_FIELDS = (
    "station_id", "acceptance_run_id", "issued_at",
    "forecast_start_time", "forecast_end_time", "write_state",
)
```

For every exact row:

- Identity must match.
- `write_state` must be `complete`.
- `issued_at` must be Shanghai 01:02:00.
- `forecast_start_time` must be the same date at 01:00.
- `forecast_end_time == forecast_start_time + timedelta(days=1)`.
- The complete daily window must be inside the run window.
- Daily `forecast_start_time` values must be unique.
- Count must be 0–7.

Expose:

```python
def completed_days(self, run: AcceptanceRunRecord) -> int:
    ...
```

Raise `acceptance_summary_invalid` for malformed or impossible batch topology.

- [ ] **Step 5: Implement limited summary updates**

Implement one private method that re-reads the row by identity, rejects a changed identity/window/control state, writes only the three summary fields by numeric `id`, and strictly validates the update response:

```python
def _update_summary(
    self,
    run: AcceptanceRunRecord,
    *,
    completed_days: int,
    result_state: str,
    calculated_at: datetime | None,
) -> AcceptanceRunRecord:
    ...
```

Use this exact payload:

```python
values = {
    "completed_days": completed_days,
    "result_state": result_state,
    "calculated_at": (
        None if calculated_at is None else calculated_at.isoformat()
    ),
}
```

Require the `update_record()` response to have exactly the `RUN_FIELDS` set, parse it through `_run_record()`, and compare identity, window, control state, and every requested summary value. Raise `acceptance_run_update_incomplete` for mismatch.

- [ ] **Step 6: Implement progress, final result, and recovery entry points**

Implement:

```python
def sync_progress(
    self, station_id: str, acceptance_run_id: str
) -> AcceptanceRunRecord:
    run = self._identity_run(station_id, acceptance_run_id)
    days = self.completed_days(run)
    state = "pending" if days == 0 else "in_progress"
    return self._update_summary(
        run,
        completed_days=days,
        result_state=state,
        calculated_at=None,
    )

def sync_result(
    self,
    station_id: str,
    acceptance_run_id: str,
    outcome: str,
    calculated_at: datetime,
) -> AcceptanceRunRecord:
    ...

def reconcile_active(self, station_id: str) -> AcceptanceRunRecord | None:
    ...
```

`sync_result()` must require a terminal outcome, exactly seven complete days, an explicit Shanghai timestamp not before `window_end`, and then update the terminal summary.

`reconcile_active()` must:

1. Return `None` when no active row exists.
2. Compute complete days.
3. Query `energy_forecast_evaluations` for `evaluation_key=overall`, matching station/run, reading `station_id`, `acceptance_run_id`, `evaluation_key`, `outcome`, `calculated_at`.
4. Reject more than one overall row.
5. With no overall row, synchronize pending/in-progress progress.
6. With one exact terminal overall row, call `sync_result()`.

- [ ] **Step 7: Perform Python static validation**

Run:

```bash
python3 -m py_compile m3_worker/services/acceptance_run_service.py
python3 - <<'PY'
from m3_worker.services.acceptance_run_service import (
    AcceptanceRunRecord,
    AcceptanceRunService,
    RUN_FIELDS,
)
assert len(RUN_FIELDS) == 9
assert AcceptanceRunRecord.__dataclass_params__.frozen
assert callable(AcceptanceRunService.active_run)
assert callable(AcceptanceRunService.sync_progress)
assert callable(AcceptanceRunService.sync_result)
assert callable(AcceptanceRunService.reconcile_active)
PY
git diff --check
```

Expected: exit 0 with no errors.

- [ ] **Step 8: Commit the fixed repository**

```bash
git add m3_worker/services/acceptance_run_service.py
git commit -m "feat(m3): 接入七日验收任务仓储"
```

---

### Task 3: Integrate task-backed runs into acceptance orchestration

**Files:**
- Modify: `m3_worker/services/acceptance_service.py`
- Modify: `m3_worker/main.py`

**Interfaces:**
- Consumes: `AcceptanceRunService.active_run()`, `sync_progress()`, `sync_result()`, and `reconcile_active()` from Task 2.
- Produces: station-independent scheduled no-op behavior, persisted run summaries, and startup reconciliation.

- [ ] **Step 1: Replace the external context dependency**

Change `AcceptanceService.__init__` from `context_source` to `run_service`:

```python
def __init__(
    self,
    *,
    run_service,
    observation_source,
    api,
    sink,
    forecast_service,
    now,
    evaluator=None,
):
    self._runs = run_service
    ...
```

Replace `_context()` with a method that asks `active_run(station_id)`. Return `AcceptanceContext(active=False)` when no row exists; otherwise construct the active context only from the validated run identity/window. Keep the existing seven-day context invariant as defense in depth.

- [ ] **Step 2: Make an inactive station a scheduled no-op**

Change `run_baseline()` to return `dict[str, Any] | None`. After validating the exact 01:02 slot and taking the station lock:

```python
context = self._context(station_id, require_active=False)
if not context.active:
    return None
acceptance_run_id, window_start, window_end = self._active_window(context)
```

Do this before forecasting or writing any acceptance evidence. Keep out-of-window active tasks as `acceptance_window_invalid`; they indicate operator configuration errors.

- [ ] **Step 3: Synchronize progress only after a complete baseline**

Preserve the existing baseline publish sequence. After `publish_acceptance()` returns successfully, call:

```python
self._runs.sync_progress(station_id, acceptance_run_id)
```

Return the original published batch response. If summary synchronization fails, propagate the stable error so the scheduler emits a station-scoped alert; the immutable complete batch remains recoverable.

- [ ] **Step 4: Synchronize the final result only after all evaluations persist**

In `_recalculate_locked()`, preserve the existing sequence that writes and validates both series plus overall evaluation rows. After all three responses are validated, call:

```python
self._runs.sync_result(
    station_id,
    acceptance_run_id,
    overall["outcome"],
    datetime.fromisoformat(calculated_at),
)
```

The call must occur before returning `results`, so the public operation cannot report successful reconciliation while the task summary is stale.

- [ ] **Step 5: Add active-run summary recovery**

Add:

```python
def reconcile_run_summaries(self, station_ids) -> int:
    reconciled = 0
    for station_id in station_ids:
        with self._station_lock(station_id):
            if self._runs.reconcile_active(station_id) is not None:
                reconciled += 1
    return reconciled
```

Call this only after `reconcile_writing_batches()` completes, so `writing` batches recover before progress is counted.

- [ ] **Step 6: Wire the repository in Worker resource construction**

In `m3_worker/main.py`:

- Import `AcceptanceRunService`.
- Stop constructing `SourceApiClient` as `context_source`; do not remove `NodeRedAlertClient` or its existing `M3_SOURCE_*` settings.
- Construct `run_service = AcceptanceRunService(api)` immediately after `NocoBaseApiClient`.
- Pass `run_service=run_service` into `AcceptanceService`.
- In `WorkerResources.recover()`, after `reconcile_writing_batches()`, call:

```python
self.acceptance_service.reconcile_run_summaries(self.settings.station_ids)
```

Keep the entire recovery block behind `acceptance_enabled`; a summary permission/configuration failure must leave startup health unready and emit the existing `acceptance_reconcile` alert.

- [ ] **Step 7: Perform static integration validation**

Run:

```bash
python3 -m py_compile \
  m3_worker/services/acceptance_run_service.py \
  m3_worker/services/acceptance_service.py \
  m3_worker/main.py
python3 - <<'PY'
import inspect
from m3_worker.services.acceptance_service import AcceptanceService

parameters = inspect.signature(AcceptanceService).parameters
assert "run_service" in parameters
assert "context_source" not in parameters
assert hasattr(AcceptanceService, "reconcile_run_summaries")
PY
python3 -m json.tool m3/contracts/nocobase_collections.json >/dev/null
git diff --check
```

Expected: exit 0 with no errors.

- [ ] **Step 8: Commit orchestration integration**

```bash
git add m3_worker/services/acceptance_service.py m3_worker/main.py
git commit -m "feat(m3): 启用按站七日验收任务"
```

---

### Task 4: Update production documentation for the fifth table

**Files:**
- Modify: `m3/人工部署手册.md`
- Modify: `m3/部署说明.md`
- Modify: `m3/AMD64三域Docker部署手册.md`
- Modify: `m3/M3 场站未来能耗预测设计.md`

**Interfaces:**
- Consumes: the final Worker behavior and permissions from Tasks 1–3.
- Produces: one unambiguous production procedure that leaves Node-RED unchanged and enables acceptance only after task-table probes succeed.

- [ ] **Step 1: Replace four-table descriptions with the five-table boundary**

Where the documents describe “M3 四表”, distinguish:

- Four existing result/detail collections.
- One new control/summary collection, `energy_forecast_acceptance_runs`.

State that detailed evidence remains in batches/points/evaluations and that the task table does not duplicate metrics.

- [ ] **Step 2: Replace the old production non-scope statement**

Remove statements that continuous seven-day acceptance is out of scope or permanently disabled. Preserve `M3_ACCEPTANCE_ENABLED=false` as the safe template/default, and document that production changes it to `true` only for the new image after an active task exists.

- [ ] **Step 3: Document the NocoBase task creation fields**

Add this operator input example, using a clearly labeled example rather than production IDs:

```text
station_id:          ES01-FULL-ID-EXAMPLE
acceptance_run_id:   acceptance-20260829-station1
window_start:        2026-08-29T01:00:00+08:00
window_end:          2026-09-05T01:00:00+08:00
control_state:       active
completed_days:      0
result_state:        pending
calculated_at:       留空
```

Explain that the dates are an example; every real run starts at Shanghai 01:00, ends exactly seven days later, and is created before the first day's 01:02 scheduler slot.

- [ ] **Step 4: Document least-privilege probes without exposing credentials**

Provide commands that prompt for a token and query only permitted fields. The read probe must filter by station and `control_state=active`; the update permission must be verified through the NocoBase role UI rather than mutating a production task during deployment.

Include:

```bash
read -rsp 'Worker NocoBase token: ' M3_PROBE_TOKEN
echo
read -rp 'Full station ID: ' M3_STATION_ID
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${M3_PROBE_TOKEN}" \
  --get 'https://vifa.hlszh.com/api/energy_forecast_acceptance_runs:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"control_state\":\"active\"}" \
  --data-urlencode 'fields=id,station_id,acceptance_run_id,window_start,window_end,control_state,completed_days,result_state,calculated_at' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
unset M3_PROBE_TOKEN M3_STATION_ID
```

- [ ] **Step 5: Document deployment and activation order**

Use this exact order:

1. Verify table constraints and Worker role permissions.
2. Deploy the new image while `M3_ACCEPTANCE_ENABLED=false`.
3. Create one active task per desired station.
4. Verify each active/empty station query.
5. Set `M3_ACCEPTANCE_ENABLED=true` in `/etc/vifa-m3/m3.env`.
6. Recreate only `vifa-m3-worker` before the first 01:02 slot.
7. Check the first complete batch and task summary after 01:02.
8. Keep `control_state=active` through final actual backfill and evaluation.
9. Mark the task `completed` only after the Dashboard shows the terminal 7/7 result.

State explicitly that `m3_production_gateway_flow.json` does not need to be re-imported for this change.

- [ ] **Step 6: Perform documentation consistency checks**

Run:

```bash
rg -n "四表|五表|M3_ACCEPTANCE_ENABLED|energy_forecast_acceptance_runs|energy_forecast_flow.json" \
  m3/人工部署手册.md \
  m3/部署说明.md \
  m3/AMD64三域Docker部署手册.md \
  'm3/M3 场站未来能耗预测设计.md'
git diff --check
```

Review every remaining “四表” occurrence and keep it only when it explicitly means the four detail/result collections.

- [ ] **Step 7: Commit production documentation**

```bash
git add \
  m3/人工部署手册.md \
  m3/部署说明.md \
  m3/AMD64三域Docker部署手册.md \
  'm3/M3 场站未来能耗预测设计.md'
git commit -m "docs(m3): 增加七日验收启用流程"
```

---

### Task 5: Run final static verification and prepare the release handoff

**Files:**
- Verify only: all files changed by Tasks 1–4

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: evidence that static contracts parse, Python imports, Compose configuration, and Git scope are clean; plus exact production activation commands.

- [ ] **Step 1: Verify JSON and Python syntax/imports**

Run:

```bash
python3 -m json.tool m3/contracts/nocobase_collections.json >/dev/null
python3 -m compileall -q m3_worker
python3 - <<'PY'
from m3_worker.main import build_resources
from m3_worker.services.acceptance_run_service import AcceptanceRunService
from m3_worker.services.acceptance_service import AcceptanceService
assert callable(build_resources)
assert callable(AcceptanceRunService.active_run)
assert hasattr(AcceptanceService, "reconcile_run_summaries")
PY
```

Expected: exit 0 with no errors.

- [ ] **Step 2: Verify Compose and release scope**

Run:

```bash
docker compose config -q
git diff --check
git status --short
git log -7 --oneline
```

Expected: Compose exits 0, no whitespace errors, and no unintended files are modified.

- [ ] **Step 3: Review requirements line by line**

Confirm all of the following against the actual diff:

- Fifth collection and Worker permissions are in the machine contract.
- Dashboard has no task-table permission.
- One inactive station is a normal no-op.
- Daily complete batches update progress.
- Final validated evaluations update terminal summary.
- Startup recovery reconciles task summaries.
- Node-RED files are unchanged.
- Templates still default to acceptance disabled.
- Production docs require table/permission probes before activation.
- No automated test files were added or changed.

- [ ] **Step 4: Produce the production commands in the handoff**

The final handoff must include, but not execute, these commands:

```bash
cd /userdata/holo/pyfiles/vifa-m3
docker pull ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:0.1.0
docker tag \
  ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:0.1.0 \
  vifa-m3:0.1.0
sudoedit /etc/vifa-m3/m3.env
# Set M3_ACCEPTANCE_ENABLED=true only after creating and probing the active run.
docker compose up -d --no-build --force-recreate vifa-m3-worker
docker compose ps
docker compose logs --since=10m vifa-m3-worker
```

Also state that no Node-RED import is required.
