# M2 Device Bottleneck Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-device minute samples, evaluate three independent bottleneck rules, save real NocoBase events, expose them to the dashboard, and automate minute and retention jobs through Node-RED.

**Architecture:** Keep the existing real-time calculator unchanged. Add one focused device-source adapter, extend the pure history rules with chain-low-efficiency evaluation, add a pure station-level bottleneck coordinator, and let the existing minute job orchestrate NocoBase reads and idempotent writes. The CLI remains a one-line-JSON process launched by Node-RED.

**Tech Stack:** Python 3 standard library, `unittest`, NocoBase REST API, vanilla HTML/CSS/JavaScript, Node-RED, Playwright-based Node.js E2E test.

**Spec:** `m2/docs/superpowers/specs/2026-08-27-m2-device-bottleneck-automation-design.md`

## Global Constraints

- All business timestamps use `Asia/Shanghai`; persisted timestamps are timezone-aware ISO 8601 values.
- Device samples more than 2 minutes old or later than the calculation instant are invalid for that minute.
- ES02 inverter IDs are exactly `emu1`, `emu2`, `emu3`, `emu4`, `emu5`, `emu21`, `emu22`, `emu23`, and `emu24`; each rated power is 60 kW and actual output is `a35` in kW.
- Battery power comes from each cabinet's `t_emu.battery_power`, converted from W to kW; temperature fields remain nullable until configured.
- Rules are independent: chain efficiency below 85%, inverter low load, or battery temperature rise may each open its own event.
- Chain-low-efficiency triggers after 2 consecutive valid minutes below 85% and recovers after 2 consecutive valid minutes at or above 85%.
- Inverter low-load initial settings are 5 kW minimum running power, 20% threshold, 3 trigger minutes, and 2 recovery minutes.
- Battery temperature initial settings are a 5-minute window, 3℃ rise, 2 trigger minutes, and 2 recovery minutes.
- `t_efficiency_device_points` already exists in NocoBase; implementation must verify it read-only and must not recreate or drop it.
- Device minute rows are retained for 30 days; efficiency points and bottleneck events are never deleted by this cleanup.
- Secrets remain only in `energy_efficiency_local_config.py` or environment variables and must never appear in Git, logs, exception text, URLs, or JSON responses.
- Existing uncommitted user and M2 changes must be preserved. Before Task 1, inspect `git status` and `git diff`; do not reset, checkout, delete, or bulk-stage unrelated files.
- Use TDD for every behavior change: failing focused test, minimal implementation, focused pass, then broader regression.

---

### Task 1: Canonical Per-Device Source Adapter

**Files:**
- Create: `m2/station_efficiency_device_adapter.py`
- Modify: `m2/station_energy_data_adapter.py`
- Create: `m2/tests/test_station_efficiency_device_adapter.py`
- Modify: `m2/tests/test_station_energy_data_adapter.py`

**Interfaces:**
- Consumes: raw `t_emu:list` rows, raw `t_growall:list` rows, one timezone-aware calculation time, and the runtime config dictionary.
- Produces: `fetch_emu_rows(config, request_json=None) -> list[dict]`, `get_station_config(station_id) -> dict`, `fetch_growall_rows(config, request_json=None) -> list[dict]`, `build_inverter_device_points(*, station_id, growall_rows, data_time, config) -> list[dict]`, and `build_battery_device_points(*, station_id, emu_rows, data_time, config) -> list[dict]`.

- [ ] **Step 1: Add failing tests for reusable `t_emu` access**

Add tests proving that one request can return raw rows and that the existing public function still returns the same normalized source:

```python
def test_fetch_emu_rows_returns_validated_raw_rows(self):
    rows = make_es02_rows()
    calls = []

    def request_json(url, token, timeout):
        calls.append((url, token, timeout))
        return {"data": rows}

    actual = self.adapter.fetch_emu_rows(
        {"emu_url": "https://station.example/api/t_emu:list", "emu_token": "secret"},
        request_json=request_json,
    )
    self.assertEqual(actual, rows)
    self.assertEqual(len(calls), 1)

def test_get_station_config_returns_copy(self):
    first = self.adapter.get_station_config("ES02")
    first["cabinet_sns"] = ()
    self.assertEqual(len(self.adapter.get_station_config("ES02")["cabinet_sns"]), 6)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  m2.tests.test_station_energy_data_adapter.StationEnergyDataAdapterTests.test_fetch_emu_rows_returns_validated_raw_rows \
  m2.tests.test_station_energy_data_adapter.StationEnergyDataAdapterTests.test_get_station_config_returns_copy -v
```

Expected: both tests fail because `fetch_emu_rows` and `get_station_config` do not exist.

- [ ] **Step 3: Expose the raw-row and station-config functions**

Refactor `fetch_station_source_record()` to call `fetch_emu_rows()` and then `build_station_source_record()`. Keep request validation and error messages unchanged. Return a copy from `get_station_config()` so callers cannot mutate `STATION_CONFIGS`.

```python
def get_station_config(station_id):
    station_id, station = _station_config(station_id)
    return {"station_id": station_id, **dict(station)}

def fetch_emu_rows(config, request_json=None):
    request_json = request_json or _request_json
    timeout = _number(config.get("timeout_seconds", 10), "timeout_seconds")
    if timeout <= 0:
        raise StationEnergyDataError("timeout_seconds 必须大于 0")
    payload = request_json(
        _config_text(config, "emu_url"),
        _config_text(config, "emu_token"),
        timeout,
    )
    return _payload_list(payload, "t_emu:list")
```

- [ ] **Step 4: Add failing tests for inverter time selection and load rate**

Create `test_station_efficiency_device_adapter.py` with fixed samples:

```python
def inverter_row(sn, timestamp, power):
    return {"sn": sn, "timestamp": timestamp, "a35": power}

def test_builds_nine_inverter_points_and_selects_latest_valid_sample(self):
    rows = [
        inverter_row(sn, "2026-08-27T10:00:30+08:00", 12.0)
        for sn in INVERTER_SNS
    ]
    rows.append(inverter_row("emu1", "2026-08-27T10:00:50+08:00", 9.0))
    points = build_inverter_device_points(
        station_id="ES02",
        growall_rows=rows,
        data_time="2026-08-27T10:01:00+08:00",
        config=CONFIG,
    )
    emu1 = next(point for point in points if point["device_id"] == "emu1")
    self.assertEqual(len(points), 9)
    self.assertEqual(emu1["active_power_kw"], 9.0)
    self.assertEqual(emu1["rated_power_kw"], 60.0)
    self.assertEqual(emu1["load_rate_pct"], 15.0)
    self.assertEqual(emu1["data_time"], "2026-08-27T10:01:00+08:00")
    self.assertEqual(emu1["source_time"], "2026-08-27T10:00:50+08:00")

def test_rejects_future_and_more_than_two_minute_old_inverter_samples(self):
    rows = [
        inverter_row("emu1", "2026-08-27T10:01:01+08:00", 10.0),
        inverter_row("emu2", "2026-08-27T09:58:59+08:00", 10.0),
    ]
    points = build_inverter_device_points(
        station_id="ES02",
        growall_rows=rows,
        data_time="2026-08-27T10:01:00+08:00",
        config=CONFIG,
    )
    self.assertEqual(points, [])
```

- [ ] **Step 5: Add failing tests for battery power and optional temperature**

Use `t_emu` rows with one configured temperature field and one missing value:

```python
def test_builds_battery_points_from_t_emu_and_keeps_missing_temperature_null(self):
    rows = make_es01_rows()
    rows[0]["max_cell_temperature"] = 31.5
    rows[0]["hottest_cluster"] = "cluster-2"
    points = build_battery_device_points(
        station_id="ES01",
        emu_rows=rows,
        data_time=TIMESTAMP,
        config={
            **CONFIG,
            "battery_max_temperature_field": "max_cell_temperature",
            "battery_hot_cluster_field": "hottest_cluster",
        },
    )
    self.assertEqual(points[0]["battery_power_kw"], -18.0)
    self.assertEqual(points[0]["temperature_c"], 31.5)
    self.assertEqual(points[0]["subdevice_id"], "cluster-2")
    self.assertIsNone(points[1]["temperature_c"])
```

- [ ] **Step 6: Run device-adapter tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_device_adapter -v
```

Expected: imports or assertions fail because the new adapter is not implemented.

- [ ] **Step 7: Implement canonical device-point construction**

Use one shape for both device types:

```python
DEVICE_POINT_FIELDS = (
    "station_id", "device_type", "device_id", "device_name", "subdevice_id",
    "data_time", "source_time", "active_power_kw", "rated_power_kw",
    "load_rate_pct", "battery_power_kw", "temperature_c",
)
```

The inverter builder must return no rows for ES01, require the configured nine IDs for ES02, choose the newest valid record for each `sn`, treat `a35` as non-negative kW, and compute `a35 / 60 * 100`. The battery builder must use configured cabinet IDs, convert W to kW, keep temperature nullable, and store a nullable hottest-cluster ID. Invalid or stale individual devices are skipped rather than converted to zero.

- [ ] **Step 8: Implement `t_growall` GET access without leaking credentials**

Mirror the existing safe `urllib.request` pattern. `fetch_growall_rows()` must require `growall_url` and `growall_token`, validate a list response, and raise `StationEfficiencyDeviceDataError` with messages that never interpolate token values.

- [ ] **Step 9: Run focused and adapter regression tests**

Run:

```bash
python3 -m unittest \
  m2.tests.test_station_efficiency_device_adapter \
  m2.tests.test_station_energy_data_adapter -v
```

Expected: all tests pass and the existing station calculation inputs remain unchanged.

- [ ] **Step 10: Commit the adapter unit**

Stage only these paths after preserving pre-existing changes:

```bash
git add m2/station_efficiency_device_adapter.py \
  m2/station_energy_data_adapter.py \
  m2/tests/test_station_efficiency_device_adapter.py \
  m2/tests/test_station_energy_data_adapter.py
git commit -m "feat: adapt per-device energy samples"
```

---

### Task 2: NocoBase Device, Event, and Cleanup Store

**Files:**
- Modify: `m2/station_efficiency_nocobase.py`
- Modify: `m2/tests/test_station_efficiency_nocobase.py`

**Interfaces:**
- Consumes: canonical device points from Task 1 and event dictionaries from the pure rules.
- Produces: `save_device_point(point, config, request_json=None) -> dict`, `fetch_device_points(station_id, start_time, end_time, config, request_json=None) -> list[dict]`, `fetch_active_events(station_id, config, request_json=None) -> list[dict]`, `fetch_dashboard_events(station_id, start_time, end_time, config, request_json=None) -> list[dict]`, and `delete_device_points_before(cutoff, config, request_json=None) -> int`.

- [ ] **Step 1: Add failing Upsert and range-query tests**

Assert the exact resource and composite identity:

```python
def test_device_save_uses_four_field_identity(self):
    calls = []
    save_device_point(DEVICE_POINT, CONFIG, request_json=lambda *args: calls.append(args) or {"data": {"id": 7}})
    parsed = urlsplit(calls[0][0])
    self.assertEqual(parsed.path, "/api/t_efficiency_device_points:updateOrCreate")
    self.assertEqual(
        parse_qs(parsed.query)["filterKeys[]"],
        ["station_id", "device_type", "device_id", "data_time"],
    )

def test_fetch_device_points_filters_station_and_time_range(self):
    rows = fetch_device_points(
        "ES02", "2026-08-27T09:50:00+08:00", "2026-08-27T10:01:00+08:00",
        CONFIG, request_json=lambda *args: {"data": [DEVICE_POINT]},
    )
    self.assertEqual(rows, [DEVICE_POINT])
```

- [ ] **Step 2: Add failing event-query tests**

Test two distinct queries:

```python
def test_fetch_active_events_queries_all_active_events_for_station(self):
    calls = []
    fetch_active_events("ES02", CONFIG, request_json=lambda *args: calls.append(args) or {"data": []})
    query = parse_qs(urlsplit(calls[0][0]).query)
    self.assertEqual(json.loads(query["filter"][0]), {
        "$and": [
            {"station_id": {"$eq": "ES02"}},
            {"status": {"$eq": "active"}},
        ]
    })

def test_dashboard_events_include_active_and_recovered_in_day(self):
    calls = []
    fetch_dashboard_events(
        "ES02", "2026-08-27T00:00:00+08:00", "2026-08-28T00:00:00+08:00",
        CONFIG, request_json=lambda *args: calls.append(args) or {"data": []},
    )
    filter_value = json.loads(parse_qs(urlsplit(calls[0][0]).query)["filter"][0])
    self.assertEqual(filter_value["$and"][0], {"station_id": {"$eq": "ES02"}})
    self.assertIn("$or", filter_value["$and"][1])
```

- [ ] **Step 3: Add a failing cleanup request test**

NocoBase's resource API uses `POST /api/t_efficiency_device_points:destroy` with a URL-encoded `filter` query parameter. Assert that only the device table and an older-than filter are targeted:

```python
def test_cleanup_destroys_only_old_device_points(self):
    calls = []
    deleted = delete_device_points_before(
        "2026-07-28T10:00:00+08:00",
        CONFIG,
        request_json=lambda *args: calls.append(args) or {"data": 123},
    )
    parsed = urlsplit(calls[0][0])
    self.assertEqual(parsed.path, "/api/t_efficiency_device_points:destroy")
    self.assertEqual(json.loads(parse_qs(parsed.query)["filter"][0]), {
        "data_time": {"$lt": "2026-07-28T02:00:00+00:00"}
    })
    self.assertEqual(deleted, 123)
```

- [ ] **Step 4: Run store tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_nocobase -v
```

Expected: new imports fail.

- [ ] **Step 5: Implement collection allowlisting, Upsert, reads, and POST destroy**

Add `t_efficiency_device_points` to `ALLOWED_COLLECTIONS`. Reuse `_canonical_time`, `_base_url`, `_timeout`, and safe token handling. For deletion, add a POST helper that accepts no meaningful body and validates `payload["data"]` as a non-negative integer. Do not add a generic arbitrary-resource method.

- [ ] **Step 6: Run the NocoBase adapter suite**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_nocobase -v
```

Expected: all old and new tests pass.

- [ ] **Step 7: Perform the read-only existing-table check**

Using the already configured local NocoBase connection, query a narrow empty-or-small time window. Print only row count and field names; never print record values or configuration:

```bash
python3 -c "import os, importlib.util; from datetime import datetime, timedelta; from m2.station_efficiency_nocobase import fetch_device_points; p='energy-efficiency-api.py'; s=importlib.util.spec_from_file_location('energy_api', p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); c=m.load_runtime_config(os.environ, require_nocobase=True); e=datetime.now().astimezone(); r=fetch_device_points('ES02',(e-timedelta(minutes=1)).isoformat(),e.isoformat(),c); print({'rows':len(r),'fields':sorted(r[0]) if r else []})"
```

Expected: no authorization/schema error. An empty list is acceptable before the minute job is connected. If the table's `event_type` field is an enum, separately verify in NocoBase UI that `chain_low_efficiency` is allowed.

- [ ] **Step 8: Commit the persistence unit**

```bash
git add m2/station_efficiency_nocobase.py m2/tests/test_station_efficiency_nocobase.py
git commit -m "feat: persist device points and query events"
```

---

### Task 3: Chain-Low-Efficiency Pure Rule and Public Event Contract

**Files:**
- Modify: `m2/station_efficiency_history.py`
- Modify: `m2/tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: chain samples shaped as `{device_id, device_name, data_time, efficiency_pct}`, normalized rule dictionaries, optional active event, and a confirmation-minute device snapshot.
- Produces: `evaluate_chain_low_efficiency(samples, rule, active_event=None, trigger_device_snapshot=None, diagnosed_causes=None) -> dict | None`; `build_calendar_day_dashboard(station_id, timezone_name, as_of, realtime, points, events)` additionally publishes `cause_status`, `diagnosed_causes`, and `trigger_device_snapshot` per event.

- [ ] **Step 1: Extend every rule fixture with the three exact chain settings**

Add to the existing `inverter_rule()` fixture and required-field assertions:

```python
"chain_low_efficiency_threshold_pct": 85,
"chain_low_efficiency_trigger_minutes": 2,
"chain_low_efficiency_recovery_minutes": 2,
```

Validate threshold as a finite number from 0 through 100 and trigger/recovery as positive integers.

- [ ] **Step 2: Add failing chain trigger and pending-cause tests**

```python
def chain_sample(self, minute, efficiency, chain="storage_load"):
    return {
        "device_id": chain,
        "device_name": {"storage_load": "储→用", "pv_storage": "光→储", "pv_load": "光→用"}[chain],
        "data_time": f"2026-08-27T10:{minute:02d}:00+08:00",
        "efficiency_pct": efficiency,
    }

def test_chain_low_efficiency_opens_after_two_minutes_with_snapshot(self):
    snapshot = {"battery_cabinets": [{"device_id": "emu21", "temperature_c": None}]}
    event = evaluate_chain_low_efficiency(
        [self.chain_sample(0, 80), self.chain_sample(1, 82)],
        self.inverter_rule(),
        trigger_device_snapshot=snapshot,
        diagnosed_causes=[],
    )
    self.assertEqual(event["event_type"], "chain_low_efficiency")
    self.assertEqual(event["start_time"], "2026-08-27T10:00:00+08:00")
    self.assertEqual(event["observed_value"], 80.0)
    self.assertEqual(event["evidence"]["cause_status"], "pending")
    self.assertEqual(event["evidence"]["trigger_device_snapshot"], snapshot)
```

- [ ] **Step 3: Add failing recovery, gap, null, and immutability tests**

Cover all four behaviors in named tests:

```python
def test_chain_event_recovers_after_two_minutes_at_or_above_85(self):
    active = evaluate_chain_low_efficiency(
        [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
        trigger_device_snapshot={}, diagnosed_causes=[],
    )
    recovered = evaluate_chain_low_efficiency(
        [self.chain_sample(1, 82), self.chain_sample(2, 85), self.chain_sample(3, 86)],
        self.inverter_rule(), active_event=active,
    )
    self.assertEqual(recovered["status"], "recovered")
    self.assertEqual(recovered["end_time"], "2026-08-27T10:02:00+08:00")

def test_active_chain_event_does_not_overwrite_trigger_snapshot(self):
    original = {"pv_inverters": [{"device_id": "emu1", "load_rate_pct": 10.0}]}
    active = evaluate_chain_low_efficiency(
        [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
        trigger_device_snapshot=original, diagnosed_causes=[],
    )
    updated = evaluate_chain_low_efficiency(
        [self.chain_sample(2, 70)], self.inverter_rule(), active_event=active,
        trigger_device_snapshot={"pv_inverters": []},
    )
    self.assertEqual(updated["evidence"]["trigger_device_snapshot"], original)
```

Also verify that a missing minute resets the trigger run and calling an active event with no valid samples returns it unchanged.

- [ ] **Step 4: Run focused history tests and verify RED**

Run:

```bash
python3 -m unittest \
  m2.tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_chain_low_efficiency_opens_after_two_minutes_with_snapshot \
  m2.tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_chain_event_recovers_after_two_minutes_at_or_above_85 \
  m2.tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_active_chain_event_does_not_overwrite_trigger_snapshot -v
```

Expected: import or missing-function failures.

- [ ] **Step 5: Implement the chain state machine by reusing minute-run helpers**

Add `_normalize_chain_samples()` and extend `_validate_active_event()` with:

```python
threshold_field, observed_unit = {
    "inverter_low_load": ("inverter_low_load_threshold_pct", "%"),
    "battery_temperature_rise": ("temperature_rise_threshold_c", "℃"),
    "chain_low_efficiency": ("chain_low_efficiency_threshold_pct", "%"),
}[expected_event_type]
```

At event creation, copy the snapshot once with JSON-safe primitive values. Use `cause_status="diagnosed"` only when `diagnosed_causes` is non-empty; otherwise use `pending`. Never mutate the caller's snapshot or the active event's nested evidence.

- [ ] **Step 6: Add failing dashboard-contract assertions**

Add a `chain_low_efficiency` event to an existing dashboard test and assert:

```python
self.assertEqual(dashboard["events"][0]["type"], "链路低效率")
self.assertEqual(dashboard["events"][0]["cause_status"], "pending")
self.assertEqual(
    dashboard["events"][0]["trigger_device_snapshot"]["battery_cabinets"][0]["temperature_c"],
    None,
)
```

- [ ] **Step 7: Publish the third label and safe diagnostic fields**

Add `"chain_low_efficiency": "链路低效率"` to `EVENT_LABELS`. In `build_calendar_day_dashboard()`, keep the current short `evidence` text and add copied structured fields with empty defaults for older event types:

```python
"cause_status": event["evidence"].get("cause_status"),
"diagnosed_causes": list(event["evidence"].get("diagnosed_causes") or []),
"trigger_device_snapshot": dict(event["evidence"].get("trigger_device_snapshot") or {}),
```

- [ ] **Step 8: Run the full history suite**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_history -v
```

Expected: all existing inverter, temperature, calendar-day, and new chain tests pass.

- [ ] **Step 9: Commit the rule unit**

```bash
git add m2/station_efficiency_history.py m2/tests/test_station_efficiency_history.py
git commit -m "feat: detect chain low efficiency events"
```

---

### Task 4: Pure Station Bottleneck Coordinator

**Files:**
- Create: `m2/station_efficiency_bottlenecks.py`
- Create: `m2/tests/test_station_efficiency_bottlenecks.py`

**Interfaces:**
- Consumes: recent station minute points, recent canonical device points, existing active events, and one normalized rule.
- Produces: `evaluate_station_bottlenecks(*, station_id, minute_points, device_points, active_events, rule) -> list[dict]`, returning only new or changed events ready for `save_bottleneck_event()`.

- [ ] **Step 1: Add a failing three-rule OR test**

Construct history where storage→use is below 85%, one inverter is low load, and battery temperatures are absent. Assert two independent updates, not one combined event:

```python
updates = evaluate_station_bottlenecks(
    station_id="ES02",
    minute_points=[minute_point(0, storage_load=80), minute_point(1, storage_load=81)],
    device_points=[inverter_point("emu1", 0, 10), inverter_point("emu1", 1, 10), inverter_point("emu1", 2, 10)],
    active_events=[],
    rule=RULE,
)
self.assertEqual(
    {(event["event_type"], event["device_id"]) for event in updates},
    {("chain_low_efficiency", "storage_load"), ("inverter_low_load", "emu1")},
)
```

- [ ] **Step 2: Add failing chain-specific snapshot tests**

Assert exact device inclusion:

```python
self.assertEqual(set(pv_load_snapshot), {"pv_inverters"})
self.assertEqual(set(pv_storage_snapshot), {"pv_inverters", "battery_cabinets"})
self.assertEqual(set(storage_load_snapshot), {"battery_cabinets"})
```

Battery entries with `temperature_c=None` must remain in the snapshot so the frontend can display “温度数据暂未提供”. Snapshot rows must come from the confirmation minute only.

- [ ] **Step 3: Add failing active-event deduplication and update tests**

Assert that duplicate active identities raise `HistoryError`, unchanged events are omitted, and a recovered evaluator result is returned once for persistence.

- [ ] **Step 4: Run coordinator tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_bottlenecks -v
```

Expected: module import fails.

- [ ] **Step 5: Implement grouping, evaluation order, causes, and snapshots**

Implementation order must be deterministic:

1. validate one station and unique active-event keys;
2. group device rows by `(device_type, device_id)`;
3. evaluate inverter and battery events first;
4. build current active diagnosed causes per affected chain;
5. build each confirmation-minute snapshot from relevant device types;
6. evaluate the three chain events;
7. compare normalized outputs with corresponding active inputs and return only changed/new/recovered events.

Use chain metadata with exact IDs and names:

```python
CHAIN_META = {
    "pv_storage": ("光→储", "pv_storage_efficiency", ("pv_inverter", "battery_cabinet")),
    "storage_load": ("储→用", "storage_load_efficiency", ("battery_cabinet",)),
    "pv_load": ("光→用", "pv_load_efficiency", ("pv_inverter",)),
}
```

- [ ] **Step 6: Run coordinator and history suites**

Run:

```bash
python3 -m unittest \
  m2.tests.test_station_efficiency_bottlenecks \
  m2.tests.test_station_efficiency_history -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the coordinator unit**

```bash
git add m2/station_efficiency_bottlenecks.py m2/tests/test_station_efficiency_bottlenecks.py
git commit -m "feat: coordinate station bottleneck rules"
```

---

### Task 5: Minute Job Integration and Partial-Success Semantics

**Files:**
- Modify: `m2/station_efficiency_job.py`
- Modify: `m2/tests/test_station_efficiency_job.py`

**Interfaces:**
- Consumes: Task 1 source functions, Task 2 store functions, and Task 4 pure coordinator.
- Produces: `process_station_minute(station_id, config, source_request_json=None, growall_request_json=None, query_request_json=None, store_request_json=None) -> dict` with keys `status`, `warnings`, `minute_point`, `saved_record`, `device_points_saved`, and `event_updates`; produces `cleanup_device_history(config, now=None, request_json=None) -> dict`.

- [ ] **Step 1: Add a failing single-fetch full-flow test**

Replace the old one-write expectation with a fixture that supplies `t_emu`, `t_growall`, recent histories, and active events. Assert:

```python
self.assertEqual(len(emu_calls), 1)
self.assertEqual(len(growall_calls), 1)
self.assertEqual(output["status"], "ok")
self.assertEqual(output["device_points_saved"], 15)  # 9 inverters + 6 ES02 cabinets
self.assertEqual(output["warnings"], [])
self.assertGreaterEqual(len(output["event_updates"]), 1)
```

Inject request functions separately so tests never use the network.

- [ ] **Step 2: Add failing partial-success tests**

Add named cases for:

- `t_growall` request failure saves the station minute and six battery points, skips inverter evaluation, returns `partial`, and contains no token in warnings;
- one device-point Upsert failure does not undo other rows and returns `partial`;
- one event Upsert failure preserves successful minute/device writes and returns `partial`;
- empty battery temperature fields produce `ok`, not a missing-config error;
- invalid `t_emu` still performs zero writes and raises the existing source error.

- [ ] **Step 3: Run focused job tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_station_efficiency_job -v
```

Expected: new assertions fail against the current one-point-only job.

- [ ] **Step 4: Refactor the job to fetch `t_emu` once and save the station point first**

The function sequence must be explicit and test-injectable:

```python
emu_rows = fetch_emu_rows(config, request_json=source_request_json)
source = build_station_source_record(station_id=station_id, emu_rows=emu_rows)
calculation = calculate_bus(source, config.get("calculation_config"))
minute_point = build_minute_point(
    station_id=station_id,
    data_time=_beijing_time(calculation["data_time"], config, "data_time"),
    chains=_minute_chains(calculation),
    formula_version=FORMULA_VERSION,
    calculated_at=_beijing_time(
        calculation["calculated_at"], config, "calculated_at",
    ),
)
saved_record = save_minute_point(minute_point, config, request_json=store_request_json)
```

The station minute is the hard requirement. Failure before or during this write remains an error rather than `partial`.

- [ ] **Step 5: Add best-effort device fetch and idempotent writes**

Always build battery points from the already fetched `t_emu` rows. Fetch inverters only for configured PV stations. Catch only known device-source/store exceptions, append sanitized warning objects, and continue unrelated devices.

- [ ] **Step 6: Read the bounded history window, merge the current point, and evaluate events**

Compute a lookback that covers the largest configured trigger/recovery/window requirement plus one minute. Merge the current in-memory minute/device points by their logical keys so evaluation does not depend on immediate read-after-write consistency. Fetch active events, call `evaluate_station_bottlenecks()`, and Upsert each returned event independently.

- [ ] **Step 7: Implement 30-day cleanup in the job layer**

```python
def cleanup_device_history(config, now=None, request_json=None):
    current = now or datetime.now(ZoneInfo(config.get("timezone", "Asia/Shanghai")))
    cutoff = current.astimezone(ZoneInfo(config.get("timezone", "Asia/Shanghai"))) - timedelta(days=30)
    deleted = delete_device_points_before(cutoff.isoformat(), config, request_json=request_json)
    return {"cutoff": cutoff.isoformat(), "deleted_count": deleted}
```

Use `device_point_retention_days` from config instead of a literal inside the final function; the snippet shows the required initial value.

- [ ] **Step 8: Run job, adapter, store, and coordinator tests**

Run:

```bash
python3 -m unittest \
  m2.tests.test_station_efficiency_job \
  m2.tests.test_station_efficiency_device_adapter \
  m2.tests.test_station_efficiency_nocobase \
  m2.tests.test_station_efficiency_bottlenecks -v
```

Expected: all pass.

- [ ] **Step 9: Commit the minute orchestration unit**

```bash
git add m2/station_efficiency_job.py m2/tests/test_station_efficiency_job.py
git commit -m "feat: automate per-device bottleneck minutes"
```

---

### Task 6: CLI Configuration, Real Dashboard Events, and Cleanup Command

**Files:**
- Modify: `energy-efficiency-api.py`
- Modify: `m2/tests/test_energy_efficiency_api.py`

**Interfaces:**
- Consumes: enriched minute-job result, NocoBase event reads, and cleanup result.
- Produces: accepted commands `dashboard ES01|ES02`, `minute ES01|ES02`, and `cleanup`; every path writes exactly one final JSON line.

- [ ] **Step 1: Add failing command-parsing and config tests**

Assert exact command forms:

```python
self.assertEqual(energy_api.parse_request(["cleanup"]), ("cleanup", None))
for invalid in (["cleanup", "ES02"], ["minute"], ["dashboard", "ES03"]):
    with self.subTest(invalid=invalid):
        with self.assertRaises(energy_api.EntrypointError):
            energy_api.parse_request(invalid)
```

Add tests for local/environment config values without ever asserting secret text in the returned public payload. ES02 missing Growall config must be handled by the minute job as `partial`, not crash dashboard or ES01.

- [ ] **Step 2: Add failing minute result and cleanup JSON tests**

```python
def test_partial_minute_returns_exit_zero_and_warning_counts(self):
    payload, exit_code = energy_api.execute(
        ["minute", "ES02"], ENVIRONMENT,
        process_minute=lambda station_id, config: {
            "status": "partial", "warnings": [{"code": "growall_unavailable"}],
            "minute_point": {"data_time": "2026-08-27T10:00:00+08:00"},
            "saved_record": {"id": 1}, "device_points_saved": 6, "event_updates": [],
        },
    )
    self.assertEqual(exit_code, 0)
    self.assertEqual(payload["status"], "partial")
    self.assertEqual(payload["data"]["device_points_saved"], 6)

def test_cleanup_returns_deleted_count(self):
    payload, exit_code = energy_api.execute(
        ["cleanup"], ENVIRONMENT,
        cleanup_history=lambda config: {"cutoff": "2026-07-28T10:00:00+08:00", "deleted_count": 12},
    )
    self.assertEqual((payload["status"], exit_code), ("ok", 0))
    self.assertEqual(payload["data"]["deleted_count"], 12)
```

- [ ] **Step 3: Add a failing dashboard real-event fetch test**

Inject `fetch_events` and assert the list reaches `build_calendar_day_dashboard()` rather than `[]`. Include an activity that started yesterday and a recovered event ending today.

- [ ] **Step 4: Run CLI tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api -v
```

Expected: cleanup parsing and event-fetch assertions fail.

- [ ] **Step 5: Load device and rule settings without exposing secrets**

Read local values for Growall URL/token, inverter inventory, rated power, maximum age, retention days, optional battery fields, and rule dictionary. Environment variables may override scalar URL/token/numeric settings. Normalize values into snake_case runtime keys consumed by Tasks 1–5. Never include the config dictionary in returned payloads or exception strings.

- [ ] **Step 6: Implement operation-specific execution**

Use this branch order:

```python
operation, station_id = parse_request(argv)
config = load_runtime_config(environ, require_nocobase=True)
if operation == "cleanup":
    result = cleanup_history(config)
    return {"status": "ok", "data": {"operation": "cleanup", **result}}, 0
if operation == "minute":
    result = process_minute(station_id, config)
    return {"status": result["status"], "data": public_minute_result(result)}, 0
```

For dashboard, fetch `points` and `events` using the same calendar-day bounds. Pass real events into `build_dashboard()`. Keep existing source/calculation/store error codes stable.

- [ ] **Step 7: Run CLI tests and one-line serialization tests**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api -v
python3 energy-efficiency-api.py invalid 2>&1 | python3 -c "import json,sys; lines=sys.stdin.read().splitlines(); assert len(lines)==1; assert json.loads(lines[0])['status']=='error'"
```

Expected: tests pass and invalid input still emits exactly one JSON object.

- [ ] **Step 8: Commit the CLI unit**

```bash
git add energy-efficiency-api.py m2/tests/test_energy_efficiency_api.py
git commit -m "feat: expose bottleneck jobs through energy CLI"
```

---

### Task 7: Dashboard Display for Chain Diagnosis and Device Snapshot

**Files:**
- Modify: `m2/场站三条能效链路能流图.html`
- Modify: `m2/tests/energy_dashboard_e2e.js`

**Interfaces:**
- Consumes: public event fields `event_type`, `type`, `device`, `evidence`, `cause_status`, `diagnosed_causes`, `trigger_device_snapshot`, `impact`, and `status`.
- Produces: event table and chart markers for `chain_low_efficiency`, including an accessible expandable trigger snapshot.

- [ ] **Step 1: Extend the E2E fixture with a cause-pending chain event**

Add an event shaped exactly as the public contract:

```javascript
{
  id: 103,
  event_type: "chain_low_efficiency",
  type: "链路低效率",
  device: "储→用",
  start: "2026-08-25T14:32:00+08:00",
  end: null,
  evidence: "储→用效率最低 80.00%，低于阈值 85.00%",
  cause_status: "pending",
  diagnosed_causes: [],
  trigger_device_snapshot: {
    battery_cabinets: [
      { device_id: "emu21", temperature_c: null, source_time: "2026-08-25T14:33:30+08:00" }
    ]
  },
  impact: ["储→用"],
  status: "持续中"
}
```

- [ ] **Step 2: Add failing browser assertions for the third event and snapshot**

Assert:

```javascript
const chainRow = page.locator("#event-table-body tr", { hasText: "链路低效率" });
assert.strictEqual(await chainRow.count(), 1);
assert.match(await chainRow.innerText(), /原因待判断/);
await chainRow.locator("summary").click();
assert.match(await chainRow.innerText(), /emu21/);
assert.match(await chainRow.innerText(), /温度数据暂未提供/);
assert.strictEqual(
  await page.locator("rect[data-event-type='chain_low_efficiency']").count(),
  1,
);
```

- [ ] **Step 3: Run E2E and verify RED**

Run:

```bash
node m2/tests/energy_dashboard_e2e.js
```

Expected: assertions fail because the table only renders the short evidence string.

- [ ] **Step 4: Add safe snapshot formatting helpers**

Create DOM nodes with `textContent`; never assign event values through `innerHTML`. Format:

- inverter: `<device_id>：<active_power_kw> kW，负载率 <load_rate_pct>%`;
- battery temperature available: `<device_id>：最高电芯温度 <temperature_c>℃`;
- battery temperature missing: `<device_id>：温度数据暂未提供`.

For a pending chain event, append “链路低效率，原因待判断”. For diagnosed causes, list their device/type summaries. Use `<details><summary>查看触发时设备数据</summary>…</details>` inside the判定数据 cell, with compact CSS that does not change the three-chain flow layout.

- [ ] **Step 5: Update chart description and event rendering**

Mention all three event types in the SVG description. Existing chart mapping already accepts arbitrary `event_type`; preserve the active/recovered colors and add no new chart axis or legend.

- [ ] **Step 6: Run E2E twice to catch refresh/state defects**

Run:

```bash
node m2/tests/energy_dashboard_e2e.js
node m2/tests/energy_dashboard_e2e.js
```

Expected: both runs print `energy_dashboard_e2e_ok` and exit 0.

- [ ] **Step 7: Commit the dashboard unit**

```bash
git add 'm2/场站三条能效链路能流图.html' m2/tests/energy_dashboard_e2e.js
git commit -m "feat: show bottleneck device snapshots"
```

---

### Task 8: Node-RED Automation and Deployment Documentation

**Files:**
- Modify: `m2/node-red-energy-efficiency-api-flow.json`
- Modify: `m2/M2线上部署流程图.md`
- Modify: `m2/M2未完成事项.md`

**Interfaces:**
- Consumes: CLI commands from Task 6.
- Produces: one importable Node-RED flow with dashboard HTTP handling, minute-aligned ES01/ES02 jobs, and a daily 30-day cleanup job.

- [ ] **Step 1: Change the minute inject node to a calendar-minute schedule**

Set the existing inject node to:

```json
{
  "repeat": "",
  "crontab": "* * * * *",
  "once": true,
  "onceDelay": "5"
}
```

Keep the two exact commands produced by `构造双电站分钟命令`:

```text
python3 /userdata/holo/pyfiles/energy-efficiency-api.py minute ES01
python3 /userdata/holo/pyfiles/energy-efficiency-api.py minute ES02
```

- [ ] **Step 2: Add the daily cleanup nodes**

Add an inject node scheduled for local server time 02:10, an exec node running:

```text
python3 /userdata/holo/pyfiles/energy-efficiency-api.py cleanup
```

and success/error debug nodes. Add all new IDs to the group `nodes` array and expand the group height without changing existing node IDs.

- [ ] **Step 3: Validate the Node-RED JSON**

Run:

```bash
python3 -m json.tool m2/node-red-energy-efficiency-api-flow.json >/dev/null
```

Expected: exit 0.

- [ ] **Step 4: Rewrite the deployment diagrams to match the actual process model**

Remove stale statements that a Python HTTP service is still required. Document the real flow:

```text
iframe → Node-RED /energy-efficiency-api → exec energy-efficiency-api.py dashboard <station>
Node-RED minute cron → exec minute ES01 and minute ES02
Node-RED daily cron → exec cleanup
```

Add `t_growall`, `t_efficiency_device_points`, three independent rules, real event queries, 2-minute freshness, and 30-day retention to the Mermaid diagrams. State that Node-RED/server timezone must be `Asia/Shanghai` so the daily schedule and minute buckets agree.

- [ ] **Step 5: Update the unfinished-items checklist from current evidence**

Mark the device table as created, mark code items complete only after their tests pass, and leave production upload/Node-RED import/real temperature-field mapping as operational follow-ups. Do not preserve obsolete table names or the removed NocoBase rule-table requirement.

- [ ] **Step 6: Commit automation and docs**

```bash
git add m2/node-red-energy-efficiency-api-flow.json \
  'm2/M2线上部署流程图.md' \
  'm2/M2未完成事项.md'
git commit -m "docs: wire bottleneck automation flow"
```

---

### Task 9: Full Regression and Safe Real-API Handoff

**Files:**
- Verify only; modify the smallest owning file if a verification failure exposes a defect.

**Interfaces:**
- Consumes: all Tasks 1–8.
- Produces: evidence that unit tests, browser tests, JSON parsing, one-line CLI behavior, and read-only real APIs work without exposing secrets.

- [ ] **Step 1: Run the full Python suite**

Run:

```bash
python3 -m unittest discover -s m2/tests -p 'test_*.py' -v
```

Expected: all tests pass with no live network calls.

- [ ] **Step 2: Run the browser E2E suite**

Run:

```bash
node m2/tests/energy_dashboard_e2e.js
```

Expected: `energy_dashboard_e2e_ok`.

- [ ] **Step 3: Verify syntax, JSON, and patch cleanliness**

Run:

```bash
python3 -m py_compile energy-efficiency-api.py m2/*.py
python3 -m json.tool m2/node-red-energy-efficiency-api-flow.json >/dev/null
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Run read-only live dashboard checks for both stations**

Run from the directory containing the ignored local config:

```bash
python3 energy-efficiency-api.py dashboard ES01
python3 energy-efficiency-api.py dashboard ES02
```

Expected: each command prints one JSON line with `status: ok`; no token or password appears. These commands do not write minute/device/event rows.

- [ ] **Step 5: Inspect the final diff and secret scan**

Run:

```bash
git status --short
git diff --stat
rg -n "Bearer eyJ|PASSWORD_PLACEHOLDER|Authorization: Bearer" --glob '!energy_efficiency_local_config.py' .
```

Expected: no credential value appears in tracked code or docs. A literal header name inside safe request-building code is acceptable; hard-coded JWTs and passwords are not.

- [ ] **Step 6: Stop before the first real write and present the exact server checklist**

Report:

- verified `t_efficiency_device_points` access result;
- exact files to upload;
- required local config key names without values;
- Node-RED flow import/redeploy steps;
- the two minute commands and one cleanup command;
- the command that performs the first real ES02 minute write.

Do not execute the first production `minute ES02` or import/redeploy Node-RED without the user's explicit go-ahead at that point.

- [ ] **Step 7: Create a final implementation commit only if uncommitted implementation fixes remain**

Stage only the verified feature paths, review `git diff --cached`, then commit:

```bash
git diff --cached --check
git commit -m "feat: complete M2 bottleneck automation"
```

If nothing remains staged, do not create an empty commit.
