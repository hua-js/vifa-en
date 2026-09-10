# Station Energy Source Records Implementation Plan

> **Status: Superseded on 2026-08-25.** The retained CLI, empty NocoBase connection constants, and embedded-test references below describe the historical implementation only. The current production file contains only importable calculation and data-transformation logic; test fixtures and browser serialization live under `tests`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a strict, API-independent source-record boundary that validates complete station power records and constructs the existing dashboard request from the latest 24-hour window.

**Architecture:** Keep `station_energy_backend.py` as the single zero-dependency CLI and preserve all existing calculation and input modes. Add one strict normalizer for future NocoBase-mapped records and one builder that sorts those records, selects the latest point, extracts an exact 24-hour window, and hands the existing `dashboard_request()` an unchanged contract; actual HTTP access remains outside this implementation because the API is not defined.

**Tech Stack:** Python 3 standard library (`datetime`, `unittest`), existing embedded `unittest` suite, Node.js + Playwright browser regression test.

**Spec:** `docs/m3/specs/2026-08-25-station-energy-source-access-design.md`

## Global Constraints

- Keep the backend in the single file `station_energy_backend.py`; do not add runtime dependencies.
- Do not implement or guess a NocoBase URL, collection, pagination contract, authentication header, or response field name.
- Add the empty code constants `NOCOBASE_BASE_URL`, `NOCOBASE_API_PATH`, `NOCOBASE_API_TOKEN`, and `NOCOBASE_FIELD_MAPPING` exactly as approved.
- A standard source record must contain `bus_id`, `data_time`, and every field in `POWER_FIELDS`; all power values must be finite non-negative JSON numbers.
- In a standard source record, numeric `0` is a real measured zero; missing, `null`, boolean, negative, and non-finite values are mapping errors and must never be converted to zero.
- Preserve legacy behavior for existing stdin, `--input-json`, `--input-file`, `--mock`, and `--self-test` requests.
- Do not add `measurement_quality` or the states `good`, `missing`, `stale`, or `bad`.
- Do not add `--from-nocobase` until a real API contract exists.
- Never include `NOCOBASE_API_TOKEN` in stdout, stderr, exception messages, or error details.
- The workspace is not a Git repository, so commit steps are replaced by test and inspection checkpoints.

## File Structure

- Modify `station_energy_backend.py`: approved empty NocoBase constants, strict source-record normalization, 24-hour dashboard payload construction, and embedded unit tests.
- Read-only regression target `场站三条能效链路能流图.html`: no behavior or layout changes are expected.
- Read-only regression target `tests/energy_dashboard_e2e.js`: existing browser test must remain green without modification.

---

### Task 1: Strict Standard Source Record Normalization

**Files:**
- Modify: `station_energy_backend.py:12-55`
- Modify: `station_energy_backend.py:167-219`
- Test: `station_energy_backend.py:1345-end` (`StationEnergyBackendTests`)

**Interfaces:**
- Consumes: `POWER_FIELDS`, `normalize_sample(raw, config=None)`, `BackendError`.
- Produces: `SOURCE_REQUIRED_FIELDS: tuple[str, ...]` and `normalize_source_record(record: dict) -> dict` containing only normalized `bus_id`, `data_time`, every `POWER_FIELDS` value, `device_status`, and `source_times`.

- [ ] **Step 1: Add failing tests for complete records, real zero values, missing fields, and invalid values**

Add these methods to `StationEnergyBackendTests`:

```python
def test_normalize_source_record_requires_all_power_fields(self):
    record = make_sample()
    del record["grid_export_power"]
    with self.assertRaises(BackendError) as caught:
        normalize_source_record(record)
    self.assertEqual(caught.exception.code, "source_mapping_error")
    self.assertEqual(caught.exception.details["fields"], ["grid_export_power"])

def test_normalize_source_record_preserves_real_zero_values(self):
    record = make_sample(**{field: 0.0 for field in POWER_FIELDS})
    normalized = normalize_source_record(record)
    self.assertTrue(all(normalized[field] == 0.0 for field in POWER_FIELDS))

def test_normalize_source_record_rejects_invalid_power_values(self):
    invalid_values = (None, True, -1, float("nan"), float("inf"))
    for value in invalid_values:
        with self.subTest(value=value):
            record = make_sample(pv_dc_power=value)
            with self.assertRaises(BackendError) as caught:
                normalize_source_record(record)
            self.assertEqual(caught.exception.code, "source_mapping_error")
            self.assertEqual(caught.exception.details["field"], "pv_dc_power")
```

- [ ] **Step 2: Run the three focused tests and verify the red state**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_normalize_source_record_requires_all_power_fields \
  station_energy_backend.StationEnergyBackendTests.test_normalize_source_record_preserves_real_zero_values \
  station_energy_backend.StationEnergyBackendTests.test_normalize_source_record_rejects_invalid_power_values -v
```

Expected: all three tests error because `normalize_source_record` is not defined.

- [ ] **Step 3: Add approved source constants and the minimal strict normalizer**

Immediately after `DEFAULT_CONFIG`, add the intentionally unconfigured constants:

```python
NOCOBASE_BASE_URL = ""
NOCOBASE_API_PATH = ""
NOCOBASE_API_TOKEN = ""
NOCOBASE_FIELD_MAPPING = {}
```

Immediately after `POWER_FIELDS`, add:

```python
SOURCE_REQUIRED_FIELDS = ("bus_id", "data_time", *POWER_FIELDS)
```

Immediately after `normalize_sample`, add the strict adapter-boundary function:

```python
def normalize_source_record(record):
    """Validate one complete API-independent station source record."""
    if not isinstance(record, dict):
        raise BackendError(
            "source_mapping_error",
            "数据源记录必须是对象",
            {"field": "record"},
        )
    missing = sorted(field for field in SOURCE_REQUIRED_FIELDS if field not in record)
    if missing:
        raise BackendError(
            "source_mapping_error",
            "数据源记录缺少必需字段",
            {"fields": missing},
        )
    try:
        normalized = normalize_sample(record)
    except BackendError as exc:
        safe_details = {
            key: value
            for key, value in exc.details.items()
            if key in {"field", "fields", "span_seconds"}
        }
        raise BackendError(
            "source_mapping_error",
            "数据源记录无法转换为标准结构",
            {"cause": exc.code, **safe_details},
        ) from exc
    return {
        "bus_id": normalized["bus_id"],
        "data_time": normalized["data_time"],
        **{field: normalized[field] for field in POWER_FIELDS},
        "device_status": normalized["device_status"],
        "source_times": normalized["source_times"],
    }
```

The returned object intentionally drops unknown source metadata and internal `_data_time` fields. It does not read or expose any NocoBase constant.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the same command from Step 2.

Expected: 3 tests pass.

- [ ] **Step 5: Run the complete embedded backend test suite as the task checkpoint**

Run:

```bash
python3 station_energy_backend.py --self-test
```

Expected: exit 0; stdout is one JSON document with `status: "ok"`, `failures: 0`, and `errors: 0`.

---

### Task 2: Exact 24-Hour Dashboard Payload Construction

**Files:**
- Modify: `station_energy_backend.py:914-965`
- Test: `station_energy_backend.py:1345-end` (`StationEnergyBackendTests`)

**Interfaces:**
- Consumes: `normalize_source_record(record: dict) -> dict`, `parse_data_time(value, field="data_time")`, `normalize_config(raw=None)`, `_validate_dashboard_history(samples, config, current_sample)`.
- Produces: `build_dashboard_payload(records: list[dict], config: dict | None = None) -> dict` with keys `operation`, `current`, `trend_samples`, and `config`, suitable for `dashboard_request()`.

- [ ] **Step 1: Add a helper that creates deterministic complete source history in tests**

Add the helper inside `StationEnergyBackendTests`:

```python
def make_source_history(self, start="2026-08-24T12:00:00+08:00", hours=25):
    base = datetime.fromisoformat(start)
    return [
        make_sample(
            data_time=(base + timedelta(hours=hour)).isoformat(),
            pv_dc_power=190,
            pv_ac_power=183,
            load_power=80,
            cabinet_charge_power=103,
            pcs_charge_power=100,
            bms_charge_power=95,
        )
        for hour in range(hours)
    ]
```

- [ ] **Step 2: Add failing tests for window selection and all edge cases**

Add:

```python
def test_build_dashboard_payload_sorts_and_selects_latest_24_hours(self):
    records = self.make_source_history(hours=27)
    payload = build_dashboard_payload(list(reversed(records)))
    self.assertEqual(payload["operation"], "dashboard")
    self.assertEqual(payload["current"]["data_time"], records[-1]["data_time"])
    self.assertEqual(len(payload["trend_samples"]), 25)
    self.assertEqual(payload["trend_samples"][0]["data_time"], records[2]["data_time"])
    self.assertEqual(payload["trend_samples"][-1]["data_time"], records[-1]["data_time"])
    response = dashboard_request(payload)
    self.assertEqual(response["operation"], "dashboard")

def test_build_dashboard_payload_rejects_insufficient_history(self):
    with self.assertRaises(BackendError) as caught:
        build_dashboard_payload(self.make_source_history(hours=24))
    self.assertEqual(caught.exception.code, "insufficient_history")

def test_build_dashboard_payload_rejects_duplicate_times(self):
    records = self.make_source_history()
    records.append(dict(records[-1]))
    with self.assertRaises(BackendError) as caught:
        build_dashboard_payload(records)
    self.assertEqual(caught.exception.code, "invalid_time_series")

def test_build_dashboard_payload_does_not_fall_back_from_invalid_latest_record(self):
    records = self.make_source_history()
    del records[-1]["pv_dc_power"]
    with self.assertRaises(BackendError) as caught:
        build_dashboard_payload(records)
    self.assertEqual(caught.exception.code, "source_mapping_error")
```

- [ ] **Step 3: Run the focused test and verify the red state**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_build_dashboard_payload_sorts_and_selects_latest_24_hours \
  station_energy_backend.StationEnergyBackendTests.test_build_dashboard_payload_rejects_insufficient_history \
  station_energy_backend.StationEnergyBackendTests.test_build_dashboard_payload_rejects_duplicate_times \
  station_energy_backend.StationEnergyBackendTests.test_build_dashboard_payload_does_not_fall_back_from_invalid_latest_record -v
```

Expected: all four tests error because `build_dashboard_payload` is not defined.

- [ ] **Step 4: Implement the minimal payload builder**

Add immediately before `dashboard_request`:

```python
def build_dashboard_payload(records, config=None):
    """Build the existing dashboard request from complete source records."""
    if not isinstance(records, list) or not records:
        raise BackendError(
            "insufficient_history",
            "数据源没有可用记录",
            {"record_count": 0 if isinstance(records, list) else None},
        )
    normalized = [normalize_source_record(record) for record in records]
    ordered = sorted(normalized, key=lambda item: parse_data_time(item["data_time"]))
    times = [parse_data_time(item["data_time"]) for item in ordered]
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise BackendError(
            "invalid_time_series",
            "数据源记录时间必须唯一",
            {"field": "data_time"},
        )

    current = ordered[-1]
    window_start = times[-1] - timedelta(hours=24)
    trend_samples = [
        item
        for item, item_time in zip(ordered, times)
        if window_start <= item_time <= times[-1]
    ]
    if not trend_samples or parse_data_time(trend_samples[0]["data_time"]) != window_start:
        raise BackendError(
            "insufficient_history",
            "数据源记录无法覆盖完整 24 小时",
            {
                "required_start": window_start.isoformat(),
                "current_time": times[-1].isoformat(),
            },
        )

    normalized_config = normalize_config(config)
    _validate_dashboard_history(
        trend_samples,
        normalized_config,
        normalize_sample(current, normalized_config),
    )
    return {
        "operation": "dashboard",
        "current": current,
        "trend_samples": trend_samples,
        "config": {} if config is None else dict(config),
    }
```

This accepts unordered API output but never silently discards duplicate timestamps, mixed buses, timezone conflicts, or gaps longer than one hour.

- [ ] **Step 5: Run all four focused tests and verify they pass**

Run the same command from Step 3.

Expected: 4 tests pass.

- [ ] **Step 6: Run the complete embedded backend test suite as the task checkpoint**

Run:

```bash
python3 station_energy_backend.py --self-test
```

Expected: exit 0; stdout reports all tests successful, including the seven new source-boundary tests from Tasks 1 and 2.

---

### Task 3: Contract Documentation and Full Regression Verification

**Files:**
- Modify: `docs/m3/specs/2026-08-24-station-energy-backend-design.md`
- Verify: `station_energy_backend.py`
- Verify: `场站三条能效链路能流图.html`
- Verify: `tests/energy_dashboard_e2e.js`

**Interfaces:**
- Consumes: `normalize_source_record(record)`, `build_dashboard_payload(records, config=None)`, existing CLI and dashboard JSON contracts.
- Produces: a discoverable source-record section in the main backend design and verified compatibility with the existing HTML dashboard.

- [ ] **Step 1: Add the source-record function contract to the main backend design**

After the existing request-contract section in `docs/m3/specs/2026-08-24-station-energy-backend-design.md`, add a short subsection containing these exact points:

```markdown
### 标准数据源记录

未来 NocoBase 响应必须先转换为 `2026-08-25-station-energy-source-access-design.md` 定义的完整标准记录。`normalize_source_record(record)` 要求 `bus_id`、带时区的 `data_time` 和全部功率字段；缺失或非法功率返回 `source_mapping_error`，不会补零。`build_dashboard_payload(records)` 接受无序记录，选择最新时刻并构造精确的最近 24 小时 `current + trend_samples` 契约。
```

- [ ] **Step 2: Compile the backend**

Run:

```bash
python3 -m py_compile station_energy_backend.py
```

Expected: exit 0 with no output.

- [ ] **Step 3: Run the complete backend self-test and inspect its JSON summary**

Run:

```bash
python3 station_energy_backend.py --self-test
```

Expected: exit 0; every test is `ok`; stdout JSON contains `"status":"ok"`, zero failures, and zero errors.

- [ ] **Step 4: Run the browser end-to-end regression test**

Run:

```bash
NODE_PATH=/Users/hua/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
/Users/hua/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
tests/energy_dashboard_e2e.js
```

Expected: exit 0 and output `energy_dashboard_e2e_ok`. This command starts a local ephemeral HTTP server and Chromium, so execution may require sandbox approval.

- [ ] **Step 5: Verify the implementation did not add premature API behavior or forbidden quality-state fields**

Run:

```bash
rg -n "urllib|requests|httpx|--from-nocobase|measurement_quality|\bgood\b|\bmissing\b|\bstale\b|\bbad\b" station_energy_backend.py
```

Expected: no matches. Empty `NOCOBASE_*` constants and `NOCOBASE_FIELD_MAPPING` are present, but there is no HTTP implementation, no `--from-nocobase` CLI flag, and no field-level quality-state model.

- [ ] **Step 6: Verify the approved constants and source interfaces exist exactly once**

Run:

```bash
rg -n "^(NOCOBASE_BASE_URL|NOCOBASE_API_PATH|NOCOBASE_API_TOKEN|NOCOBASE_FIELD_MAPPING|SOURCE_REQUIRED_FIELDS)\s*=|^def (normalize_source_record|build_dashboard_payload)\(" station_energy_backend.py
```

Expected: one definition for each approved constant and each new function.

- [ ] **Step 7: Record the non-Git completion checkpoint**

Confirm in the handoff that the workspace is not a Git repository, no commit was created, the exact backend self-test count is reported from Step 3, and the browser regression result from Step 4 is reported.
