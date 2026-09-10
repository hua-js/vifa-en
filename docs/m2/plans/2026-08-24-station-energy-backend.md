# Station Energy Backend Implementation Plan

> **Status: Superseded on 2026-08-25.** The CLI, Node-RED process invocation, stdout JSON, mock data, and embedded-test steps below describe the historical implementation only. The current production file is an import-only calculation/data-transformation module; see the linked design spec for the current boundary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one zero-dependency Python CLI that calculates the station-level PV→storage, storage→load, and PV→load efficiency chains and returns Node-RED-safe JSON on stdout.

**Architecture:** Node-RED owns HTTP, real-data normalization, and iframe proxying, then invokes `station_energy_backend.py` as a short-lived process. The script separates request parsing, normalized calculation functions, quality evaluation, multi-bus/energy aggregation, and CLI presentation inside one file because single-file delivery is an explicit requirement.

**Tech Stack:** Python 3 standard library (`argparse`, `datetime`, `json`, `math`, `pathlib`, `sys`, `unittest`)

**Spec:** `docs/m3/specs/2026-08-24-station-energy-backend-design.md`

## Global Constraints

- Deliver exactly one runtime file: `station_energy_backend.py`.
- Use only the Python 3 standard library; do not add a requirements file.
- Emit exactly one JSON document to stdout for every CLI invocation.
- Emit diagnostics and unittest progress only to stderr.
- Never embed or echo NocoBase tokens, passwords, or service URLs.
- Input/output powers are kW, integrated values are kWh, and efficiencies are percentages.
- Directional input fields are already normalized and must be finite non-negative numbers.
- The current directory is not a Git repository, so task commit steps are replaced by syntax and focused-test checkpoints.

## File Structure

- Create `station_energy_backend.py`: input contracts, pure calculation functions, quality rules, multi-bus and time-series aggregation, built-in mock scenarios, CLI, and embedded `unittest` suite.
- Read-only reference `场站级三条能效链路后端计算表.md`: source formulas and examples A–D.
- Read-only reference `skills/nocobase-dashboard-builder-SKILL.md`: Node-RED exec/stdout JSON integration contract.

---

### Task 1: Input Contract, Configuration, and Structured Errors

**Files:**
- Create: `station_energy_backend.py`
- Test: embedded `StationEnergyBackendTests` in `station_energy_backend.py`

**Interfaces:**
- Produces: `BackendError(code: str, message: str, details: dict | None)`
- Produces: `normalize_config(raw: object) -> dict[str, float]`
- Produces: `parse_data_time(value: object, field: str = "data_time") -> datetime`
- Produces: `normalize_sample(raw: object, config: dict[str, float]) -> dict[str, object]`
- Produces: `make_sample(**overrides: object) -> dict[str, object]` for embedded tests and mock scenarios

- [ ] **Step 1: Create the module skeleton and failing validation tests**

Start the module with standard-library imports, constants, and the embedded test class. Use these exact constants:

```python
DEFAULT_CONFIG = {
    "min_power_kw": 1.0,
    "balance_error_limit_percent": 5.0,
    "max_efficiency_percent": 105.0,
    "time_tolerance_seconds": 30.0,
}

POWER_FIELDS = (
    "pv_dc_power",
    "pv_ac_power",
    "load_power",
    "cabinet_charge_power",
    "cabinet_discharge_power",
    "pcs_charge_power",
    "pcs_discharge_power",
    "bms_charge_power",
    "bms_discharge_power",
    "grid_import_power",
    "grid_export_power",
    "storage_aux_power",
)
```

Add tests with these observable assertions:

```python
class StationEnergyBackendTests(unittest.TestCase):
    def test_normalize_sample_fills_missing_power_with_zero(self):
        sample = normalize_sample(
            {"data_time": "2026-08-24T12:00:00+08:00", "pv_ac_power": 96},
            DEFAULT_CONFIG,
        )
        self.assertEqual(sample["pv_ac_power"], 96.0)
        self.assertEqual(sample["grid_export_power"], 0.0)

    def test_normalize_sample_rejects_negative_boolean_and_non_finite_power(self):
        for bad_value in (-1, True, float("nan"), float("inf")):
            with self.subTest(value=bad_value):
                with self.assertRaises(BackendError) as caught:
                    normalize_sample(
                        {
                            "data_time": "2026-08-24T12:00:00+08:00",
                            "pv_ac_power": bad_value,
                        },
                        DEFAULT_CONFIG,
                    )
                self.assertEqual(caught.exception.code, "invalid_input")

    def test_data_time_requires_an_explicit_timezone(self):
        with self.assertRaises(BackendError) as caught:
            parse_data_time("2026-08-24T12:00:00")
        self.assertEqual(caught.exception.code, "invalid_time")
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_normalize_sample_fills_missing_power_with_zero \
  station_energy_backend.StationEnergyBackendTests.test_normalize_sample_rejects_negative_boolean_and_non_finite_power \
  station_energy_backend.StationEnergyBackendTests.test_data_time_requires_an_explicit_timezone -v
```

Expected: FAIL because `normalize_sample`, `BackendError`, and `parse_data_time` are not implemented.

- [ ] **Step 3: Implement strict normalization**

Implement `BackendError` with public `code`, `message`, and `details` attributes plus `to_dict()`. Implement the functions with these rules:

```python
class BackendError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def parse_data_time(value, field="data_time"):
    if not isinstance(value, str) or not value.strip():
        raise BackendError("invalid_time", f"字段 {field} 必须是带时区的 ISO 8601 字符串", {"field": field})
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BackendError("invalid_time", f"字段 {field} 不是有效的 ISO 8601 时间", {"field": field}) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackendError("invalid_time", f"字段 {field} 必须包含时区", {"field": field})
    return parsed
```

`normalize_config()` must reject non-dictionaries, unknown keys, booleans, non-finite numbers, negative numbers, and a zero `max_efficiency_percent`. `normalize_sample()` must reject non-dictionaries, require `data_time`, preserve `bus_id` as a string defaulting to `default`, parse `source_times` as a string-to-time dictionary, preserve `device_status` as a dictionary/list/string, and default every missing power field to `0.0`.

Add `make_sample()` as a deterministic helper:

```python
def make_sample(**overrides):
    sample = {field: 0.0 for field in POWER_FIELDS}
    sample.update({
        "bus_id": "bus-1",
        "data_time": "2026-08-24T12:00:00+08:00",
        "device_status": {},
        "source_times": {},
    })
    sample.update(overrides)
    return sample
```

- [ ] **Step 4: Run the focused tests and syntax checkpoint**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_normalize_sample_fills_missing_power_with_zero \
  station_energy_backend.StationEnergyBackendTests.test_normalize_sample_rejects_negative_boolean_and_non_finite_power \
  station_energy_backend.StationEnergyBackendTests.test_data_time_requires_an_explicit_timezone -v
python3 -m py_compile station_energy_backend.py
```

Expected: 3 tests PASS and compilation exits 0.

---

### Task 2: Real-Time Energy Allocation and Efficiency Chains

**Files:**
- Modify: `station_energy_backend.py`
- Test: embedded `StationEnergyBackendTests`

**Interfaces:**
- Consumes: normalized samples from `normalize_sample()` and validated config from `normalize_config()`
- Produces: `evaluate_efficiency(numerator: float, denominator: float, config: dict) -> tuple[float | None, list[str]]`
- Produces: `calculate_bus(raw_sample: object, raw_config: object = None) -> dict[str, object]`
- Produces: result keys named exactly as section 17 of the source calculation table plus `pv_storage_dc_efficiency`, `pv_load_dc_efficiency`, `quality_codes`, `quality_by_metric`, and `intermediate`

- [ ] **Step 1: Add failing tests for source examples A–D**

Add four tests:

```python
def test_example_a_pv_to_storage(self):
    result = calculate_bus(make_sample(
        pv_ac_power=183,
        load_power=80,
        cabinet_charge_power=103,
        pcs_charge_power=100,
        bms_charge_power=95,
    ))
    self.assertEqual(result["pv_to_storage_power"], 103.0)
    self.assertEqual(result["grid_to_storage_power"], 0.0)
    self.assertAlmostEqual(result["pcs_charge_efficiency"], 95.0, places=6)
    self.assertAlmostEqual(result["pv_storage_efficiency"], 95 / 103 * 100, places=6)

def test_example_b_storage_to_load_includes_full_path_loss(self):
    result = calculate_bus(make_sample(
        load_power=90,
        cabinet_discharge_power=92,
        pcs_discharge_power=95,
        bms_discharge_power=100,
    ))
    self.assertEqual(result["storage_to_load_power"], 90.0)
    self.assertAlmostEqual(result["pcs_discharge_efficiency"], 95.0, places=6)
    self.assertAlmostEqual(result["cabinet_discharge_efficiency"], 92.0, places=6)
    self.assertAlmostEqual(result["storage_load_efficiency"], 90.0, places=6)

def test_example_c_pv_to_load_tracks_ac_and_dc_input(self):
    result = calculate_bus(make_sample(
        pv_dc_power=100,
        pv_ac_power=96,
        load_power=94,
    ))
    self.assertAlmostEqual(result["pv_inverter_efficiency"], 96.0, places=6)
    self.assertAlmostEqual(result["pv_load_efficiency"], 94 / 96 * 100, places=6)
    self.assertAlmostEqual(result["pv_load_dc_efficiency"], 94.0, places=6)

def test_example_d_allocates_three_load_sources(self):
    result = calculate_bus(make_sample(
        pv_ac_power=80,
        load_power=150,
        cabinet_discharge_power=50,
        grid_import_power=20,
    ))
    self.assertEqual(result["pv_to_load_power"], 80.0)
    self.assertEqual(result["storage_to_load_power"], 50.0)
    self.assertEqual(result["grid_to_load_power"], 20.0)
    self.assertEqual(result["calculation_mode"], "estimated")
```

- [ ] **Step 2: Run examples A–D and verify failure**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_example_a_pv_to_storage \
  station_energy_backend.StationEnergyBackendTests.test_example_b_storage_to_load_includes_full_path_loss \
  station_energy_backend.StationEnergyBackendTests.test_example_c_pv_to_load_tracks_ac_and_dc_input \
  station_energy_backend.StationEnergyBackendTests.test_example_d_allocates_three_load_sources -v
```

Expected: FAIL because `calculate_bus` does not exist.

- [ ] **Step 3: Implement allocation and intermediate chain powers**

Use normalized names `p = sample` and implement these exact allocation steps:

```python
supply = p["pv_ac_power"] + p["grid_import_power"] + p["cabinet_discharge_power"]
demand = (
    p["load_power"] + p["cabinet_charge_power"]
    + p["grid_export_power"] + p["storage_aux_power"]
)
balance_delta = supply - demand
unmetered_loss = max(balance_delta, 0.0)
load_input_target = p["load_power"] + unmetered_loss

pv_to_load_input = min(p["pv_ac_power"], load_input_target)
remain_input = max(load_input_target - pv_to_load_input, 0.0)
storage_to_load_input = min(p["cabinet_discharge_power"], remain_input)
grid_to_load_input = max(remain_input - storage_to_load_input, 0.0)

pv_to_load = min(pv_to_load_input, p["load_power"])
remain_load = max(p["load_power"] - pv_to_load, 0.0)
storage_to_load = min(storage_to_load_input, remain_load)
grid_to_load = max(remain_load - storage_to_load, 0.0)

pv_surplus = max(p["pv_ac_power"] - p["load_power"] - p["storage_aux_power"], 0.0)
pv_to_storage = min(pv_surplus, p["cabinet_charge_power"])
grid_to_storage = max(p["cabinet_charge_power"] - pv_to_storage, 0.0)
```

Derive chain inputs with guarded ratios:

```python
bms_pv = (
    p["bms_charge_power"] * pv_to_storage / p["cabinet_charge_power"]
    if p["cabinet_charge_power"] > 0 else 0.0
)
bms_load = (
    p["bms_discharge_power"] * storage_to_load_input / p["cabinet_discharge_power"]
    if p["cabinet_discharge_power"] > 0 else 0.0
)
pv_dc_to_storage = (
    p["pv_dc_power"] * pv_to_storage / p["pv_ac_power"]
    if p["pv_ac_power"] > 0 else 0.0
)
pv_dc_to_load = (
    p["pv_dc_power"] * pv_to_load_input / p["pv_ac_power"]
    if p["pv_ac_power"] > 0 else 0.0
)
```

Store each efficiency's exact numerator and denominator under `intermediate`, for example:

```python
"storage_load_efficiency": {
    "numerator_kw": storage_to_load,
    "denominator_kw": bms_load,
}
```

`evaluate_efficiency()` returns `(None, ["zero_denominator"])` for zero, `(None, ["low_power"])` below `min_power_kw`, `(None, ["invalid_efficiency"])` below 0% or above `max_efficiency_percent`, otherwise `(numerator / denominator * 100, [])`.

Set `calculation_mode` to `estimated` when at least two positive sources serve load or both PV and grid charge storage; otherwise use `measured`. Include `estimated` in top-level `quality_codes` only when the mode is estimated.

Return every public field from calculation-table section 17 at the result top level. Set `balance_delta_power` to `balance_delta`, `power_balance_error` to the percentage value, preserve the normalized `bus_id` and ISO `data_time`, and set `calculated_at` with `datetime.now().astimezone().isoformat(timespec="seconds")`.

- [ ] **Step 4: Run examples A–D and compile**

Run the command from Step 2 followed by:

```bash
python3 -m py_compile station_energy_backend.py
```

Expected: 4 tests PASS and compilation exits 0.

---

### Task 3: Quality Codes and Metric Invalidation

**Files:**
- Modify: `station_energy_backend.py`
- Test: embedded `StationEnergyBackendTests`

**Interfaces:**
- Consumes: `calculate_bus()` result and normalized `device_status` / `source_times`
- Produces: `has_abnormal_device(status: object) -> bool`
- Produces: `detect_quality(sample: dict, result: dict, config: dict) -> tuple[list[str], dict[str, list[str]]]`
- Modifies: `calculate_bus()` so only affected metrics are invalidated

- [ ] **Step 1: Add failing quality-rule tests**

```python
def test_zero_and_low_denominators_return_null(self):
    zero = calculate_bus(make_sample())
    self.assertIsNone(zero["pv_inverter_efficiency"])
    self.assertIn("zero_denominator", zero["quality_by_metric"]["pv_inverter_efficiency"])
    low = calculate_bus(make_sample(pv_dc_power=0.5, pv_ac_power=0.45, load_power=0.45))
    self.assertIsNone(low["pv_inverter_efficiency"])
    self.assertIn("low_power", low["quality_by_metric"]["pv_inverter_efficiency"])

def test_power_balance_error_invalidates_chain_efficiency(self):
    result = calculate_bus(make_sample(pv_dc_power=100, pv_ac_power=100, load_power=10))
    self.assertIn("power_balance_error", result["quality_codes"])
    self.assertIsNone(result["pv_load_efficiency"])
    self.assertAlmostEqual(result["pv_inverter_efficiency"], 100.0, places=6)

def test_time_misalignment_invalidates_all_chain_efficiencies(self):
    result = calculate_bus(make_sample(
        pv_dc_power=100,
        pv_ac_power=96,
        load_power=94,
        source_times={
            "pv": "2026-08-24T12:00:00+08:00",
            "load": "2026-08-24T12:00:31+08:00",
        },
    ))
    self.assertIn("time_misaligned", result["quality_codes"])
    self.assertIsNone(result["pv_load_efficiency"])

def test_storage_direction_conflict_does_not_hide_pv_inverter_efficiency(self):
    result = calculate_bus(make_sample(
        pv_dc_power=100,
        pv_ac_power=96,
        load_power=96,
        pcs_charge_power=10,
        bms_discharge_power=10,
    ))
    self.assertIn("direction_conflict", result["quality_codes"])
    self.assertAlmostEqual(result["pv_inverter_efficiency"], 96.0, places=6)
    self.assertIsNone(result["storage_load_efficiency"])

def test_abnormal_status_is_detected_recursively(self):
    result = calculate_bus(make_sample(
        pv_dc_power=100,
        pv_ac_power=96,
        load_power=96,
        device_status={"pcs": {"status": "offline"}},
    ))
    self.assertIn("device_abnormal", result["quality_codes"])
```

- [ ] **Step 2: Run quality tests and verify failure**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_zero_and_low_denominators_return_null \
  station_energy_backend.StationEnergyBackendTests.test_power_balance_error_invalidates_chain_efficiency \
  station_energy_backend.StationEnergyBackendTests.test_time_misalignment_invalidates_all_chain_efficiencies \
  station_energy_backend.StationEnergyBackendTests.test_storage_direction_conflict_does_not_hide_pv_inverter_efficiency \
  station_energy_backend.StationEnergyBackendTests.test_abnormal_status_is_detected_recursively -v
```

Expected: at least the power balance, time, direction, and device-status assertions FAIL.

- [ ] **Step 3: Implement quality detection and targeted blockers**

Calculate:

```python
balance_error = abs(balance_delta) / max(supply, config["min_power_kw"]) * 100.0
```

Trigger `power_balance_error` when it is strictly greater than the configured limit. Parse `source_times` together with `data_time`; trigger `time_misaligned` when newest minus oldest exceeds `time_tolerance_seconds`.

Recursively scan dictionaries, lists, and strings for device states. Treat the case-insensitive values `fault`, `offline`, `alarm`, `communication_error`, `comm_error`, and boolean `abnormal=True` as abnormal.

Direction rules use `min_power_kw` as the active threshold:

```python
bms_mixed = bms_charge >= threshold and bms_discharge >= threshold
pcs_mixed = pcs_charge >= threshold and pcs_discharge >= threshold
charge_disagree = pcs_charge >= threshold and bms_discharge >= threshold
discharge_disagree = pcs_discharge >= threshold and bms_charge >= threshold
```

- `bms_mixed` produces `mixed_battery_direction`.
- `pcs_mixed`, `charge_disagree`, or `discharge_disagree` produces `direction_conflict`.
- `time_misaligned` and `power_balance_error` invalidate the five chain metrics only.
- `direction_conflict`, `mixed_battery_direction`, and `device_abnormal` invalidate storage-chain and PCS/cabinet metrics; PV inverter efficiency stays available unless its own inputs are invalid.
- Every invalidated metric receives the blocker in `quality_by_metric` and has value `None`.
- Top-level `quality_codes` is the sorted union of all metric codes and global codes.

- [ ] **Step 4: Run quality tests and the full current suite**

Run:

```bash
python3 -m unittest station_energy_backend.StationEnergyBackendTests -v
python3 -m py_compile station_energy_backend.py
```

Expected: all Task 1–3 tests PASS and compilation exits 0.

---

### Task 4: Multi-Bus and Time-Series Energy Aggregation

**Files:**
- Modify: `station_energy_backend.py`
- Test: embedded `StationEnergyBackendTests`

**Interfaces:**
- Consumes: `calculate_bus()` results and `intermediate` numerator/denominator pairs
- Produces: `combine_bus_results(results: list[dict], config: dict) -> dict`
- Produces: `calculate_request(payload: dict) -> dict`
- Produces: `aggregate_samples(samples: list[object], raw_config: object = None) -> dict`
- Produces: `aggregate_request(payload: dict) -> dict`
- Produces: `dispatch_request(payload: object) -> dict`

- [ ] **Step 1: Add failing multi-bus and energy-weighting tests**

```python
def test_multi_bus_efficiency_uses_summed_numerator_and_denominator(self):
    response = calculate_request({
        "operation": "calculate",
        "buses": [
            make_sample(bus_id="a", pv_dc_power=100, pv_ac_power=100, load_power=100),
            make_sample(bus_id="b", pv_dc_power=300, pv_ac_power=150, load_power=150),
        ],
    })
    self.assertEqual(len(response["buses"]), 2)
    self.assertAlmostEqual(response["station"]["pv_load_dc_efficiency"], 62.5, places=6)

def test_time_series_integrates_energy_instead_of_averaging_efficiency(self):
    samples = [
        make_sample(data_time="2026-08-24T00:00:00+08:00", pv_dc_power=100, pv_ac_power=100, load_power=100),
        make_sample(data_time="2026-08-24T01:00:00+08:00", pv_dc_power=100, pv_ac_power=50, load_power=50),
        make_sample(data_time="2026-08-24T03:00:00+08:00", pv_dc_power=100, pv_ac_power=50, load_power=50),
    ]
    result = aggregate_samples(samples)
    self.assertAlmostEqual(result["pv_load_dc_input_energy"], 300.0, places=6)
    self.assertAlmostEqual(result["pv_to_load_energy"], 200.0, places=6)
    self.assertAlmostEqual(result["pv_load_dc_efficiency"], 200 / 300 * 100, places=6)

def test_time_series_rejects_non_increasing_timestamps(self):
    repeated = [
        make_sample(data_time="2026-08-24T00:00:00+08:00"),
        make_sample(data_time="2026-08-24T00:00:00+08:00"),
    ]
    with self.assertRaises(BackendError) as caught:
        aggregate_samples(repeated)
    self.assertEqual(caught.exception.code, "invalid_time_series")
```

- [ ] **Step 2: Run aggregation tests and verify failure**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_multi_bus_efficiency_uses_summed_numerator_and_denominator \
  station_energy_backend.StationEnergyBackendTests.test_time_series_integrates_energy_instead_of_averaging_efficiency \
  station_energy_backend.StationEnergyBackendTests.test_time_series_rejects_non_increasing_timestamps -v
```

Expected: FAIL because request and aggregation functions do not exist.

- [ ] **Step 3: Implement multi-bus combination**

`calculate_request()` must accept exactly one of `sample` or non-empty `buses`. Calculate each bus independently. `combine_bus_results()` must:

- sum the five allocation powers and `balance_delta_power`;
- sum each valid metric's `intermediate.numerator_kw` and `denominator_kw`;
- recompute station efficiencies from those sums;
- never average bus efficiency percentages;
- return `buses` plus `station` for multi-bus input and a single `result` for single-sample input;
- set station mode to `estimated` when any bus is estimated;
- union bus quality codes without allowing one invalid bus to contaminate the valid energy of another; expose `excluded_buses_by_metric` for transparency.

- [ ] **Step 4: Implement chronological energy integration**

`aggregate_samples()` must require at least two samples and preserve caller order. For every interval `i`:

```python
hours = (time_i_plus_1 - time_i).total_seconds() / 3600.0
numerator_kwh += intermediate[metric]["numerator_kw"] * hours
denominator_kwh += intermediate[metric]["denominator_kw"] * hours
```

Only include an interval for a metric when that metric's instantaneous efficiency is not `None`. Report excluded interval indexes under `excluded_intervals_by_metric`. The final sample supplies the ending timestamp only and contributes no standalone energy.

Map accumulated chain energies to these public fields:

- `pv_to_storage_energy` and `pv_storage_input_energy`;
- `storage_to_load_energy` and `bms_load_discharge_energy`;
- `pv_to_load_energy`, `pv_load_ac_input_energy`, and `pv_load_dc_input_energy`.

`aggregate_request()` accepts either `samples` for one bus or `buses`, where every bus entry contains `bus_id` and `samples`. For multiple bus series, sum valid per-bus numerator and denominator energies and recompute station efficiency. `dispatch_request()` routes only `calculate` and `aggregate`; any other operation raises `BackendError("unsupported_operation", "不支持的 operation", {"operation": operation})`.

- [ ] **Step 5: Run aggregation tests and the full suite**

Run:

```bash
python3 -m unittest station_energy_backend.StationEnergyBackendTests -v
python3 -m py_compile station_energy_backend.py
```

Expected: all Task 1–4 tests PASS and compilation exits 0.

---

### Task 5: Node-RED CLI, Mock Scenarios, and Self-Test Output

**Files:**
- Modify: `station_energy_backend.py`
- Test: embedded `StationEnergyBackendTests`

**Interfaces:**
- Consumes: `dispatch_request()`, `make_sample()`, and all embedded tests
- Produces: `build_mock_request(name: str) -> dict`
- Produces: `read_request(args: argparse.Namespace) -> dict`
- Produces: `run_self_tests() -> tuple[bool, dict]`
- Produces: `main(argv: list[str] | None = None) -> int`
- Produces CLI options: `--input-json`, `--input-file`, `--mock`, `--self-test`, `--token`, `--pretty`

- [ ] **Step 1: Add failing CLI contract tests**

Use `subprocess` only inside the embedded tests and add:

```python
def test_cli_mock_emits_one_json_document(self):
    completed = subprocess.run(
        [sys.executable, __file__, "--mock", "pv_storage"],
        text=True,
        capture_output=True,
        check=False,
    )
    self.assertEqual(completed.returncode, 0, completed.stderr)
    parsed = json.loads(completed.stdout)
    self.assertEqual(parsed["status"], "ok")
    self.assertIn("pv_storage_efficiency", parsed["data"]["result"])

def test_cli_stdin_calculates_request(self):
    request = {
        "operation": "calculate",
        "sample": make_sample(pv_dc_power=100, pv_ac_power=96, load_power=94),
    }
    completed = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )
    self.assertEqual(completed.returncode, 0, completed.stderr)
    self.assertAlmostEqual(
        json.loads(completed.stdout)["data"]["result"]["pv_load_dc_efficiency"],
        94.0,
        places=6,
    )

def test_cli_invalid_json_returns_structured_error(self):
    completed = subprocess.run(
        [sys.executable, __file__, "--input-json", "{"],
        text=True,
        capture_output=True,
        check=False,
    )
    self.assertEqual(completed.returncode, 2)
    parsed = json.loads(completed.stdout)
    self.assertEqual(parsed["status"], "error")
    self.assertEqual(parsed["error"]["code"], "invalid_json")
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
python3 -m unittest \
  station_energy_backend.StationEnergyBackendTests.test_cli_mock_emits_one_json_document \
  station_energy_backend.StationEnergyBackendTests.test_cli_stdin_calculates_request \
  station_energy_backend.StationEnergyBackendTests.test_cli_invalid_json_returns_structured_error -v
```

Expected: FAIL because the CLI parser and main entry point are incomplete.

- [ ] **Step 3: Implement mock payloads and mutually exclusive input modes**

`build_mock_request()` must provide the exact A–D samples used in Task 2 under the names `pv_storage`, `storage_load`, `pv_load`, and `mixed`. `--mock all` returns all four calculated results in one `data.scenarios` object.

Create an `argparse` mutually exclusive group for `--input-json`, `--input-file`, `--mock`, and `--self-test`. Accept `--token` without using or serializing it. `read_request()` must:

- decode the selected JSON string as UTF-8 text;
- read an explicit file through `Path.read_text(encoding="utf-8")`;
- otherwise read stdin;
- reject empty stdin with `BackendError("missing_input", "未提供 JSON 输入")`;
- translate `json.JSONDecodeError` to `BackendError("invalid_json", "输入不是有效 JSON", {"line": exc.lineno, "column": exc.colno})`.

- [ ] **Step 4: Implement one-document stdout and self-test mode**

Use this control flow:

```python
def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.self_test:
            passed, summary = run_self_tests()
            write_json({"status": "ok" if passed else "error", "data": summary}, args.pretty)
            return 0 if passed else 1
        if args.mock:
            data = calculate_mock(args.mock)
        else:
            data = dispatch_request(read_request(args))
        write_json({"status": "ok", "data": data}, args.pretty)
        return 0
    except BackendError as exc:
        write_json({"status": "error", "error": exc.to_dict()}, args.pretty)
        return 2
```

`run_self_tests()` must use `unittest.TextTestRunner(stream=sys.stderr, verbosity=2)` so stdout remains clean. Return a summary containing `tests_run`, `failures`, `errors`, and `successful`. End the file with:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run CLI tests and full verification**

Run:

```bash
python3 -m unittest station_energy_backend.StationEnergyBackendTests -v
python3 -m py_compile station_energy_backend.py
python3 station_energy_backend.py --self-test
python3 station_energy_backend.py --mock all
printf '%s' '{"operation":"calculate","sample":{"data_time":"2026-08-24T12:00:00+08:00","pv_dc_power":100,"pv_ac_power":96,"load_power":94}}' | python3 station_energy_backend.py
```

Expected:

- unittest suite reports all tests PASS;
- compilation exits 0;
- `--self-test` exits 0 and stdout parses as one JSON object with `status: ok`;
- `--mock all` exits 0 and contains all four scenarios;
- stdin calculation exits 0 and returns `pv_load_dc_efficiency: 94.0`.

---

### Task 6: Final Contract Audit Against the Calculation Table

**Files:**
- Verify: `station_energy_backend.py`
- Read: `场站级三条能效链路后端计算表.md`
- Read: `docs/m3/specs/2026-08-24-station-energy-backend-design.md`

**Interfaces:**
- Consumes: the completed runtime file and all public output names
- Produces: a verified one-file deliverable whose fields match the approved spec

- [ ] **Step 1: Audit required output fields programmatically**

Run the PV→storage mock and assert that the parsed result contains this exact field set:

```python
required = {
    "pv_to_storage_power", "grid_to_storage_power",
    "pv_to_load_power", "storage_to_load_power", "grid_to_load_power",
    "pv_storage_efficiency", "storage_load_efficiency", "pv_load_efficiency",
    "pv_inverter_efficiency", "pcs_charge_efficiency", "pcs_discharge_efficiency",
    "cabinet_charge_efficiency", "cabinet_discharge_efficiency",
    "power_balance_error", "calculation_mode", "quality_codes",
    "quality_by_metric", "calculated_at", "intermediate",
}
```

Use an inline `python3 -c` reader or an equivalent read-only validation command; do not write a second runtime file.

- [ ] **Step 2: Re-run examples A–D and confirm documented values**

Run `python3 station_energy_backend.py --mock all`, parse stdout, and confirm:

- A: PV→storage AC efficiency rounds to 92.23%;
- B: storage→load efficiency rounds to 90.00%;
- C: PV→load AC/DC efficiencies round to 97.92% and 94.00%;
- D: PV/storage/grid load allocation is 80/50/20 kW.

- [ ] **Step 3: Verify secret and dependency constraints**

Run:

```bash
rg -n 'eyJ|Bearer |http://|https://|requests|fastapi|flask|pydantic' station_energy_backend.py
```

Expected: no embedded tokens, URLs, Authorization headers, or third-party imports. The command may match explanatory test strings only if they contain no credential or endpoint.

- [ ] **Step 4: Run the final verification suite**

Run:

```bash
python3 -m py_compile station_energy_backend.py
python3 station_energy_backend.py --self-test
python3 station_energy_backend.py --mock all
```

Expected: every command exits 0; self-test reports no failures/errors; mock output is a single parseable JSON document.
