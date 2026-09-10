# Node-RED Energy Efficiency API Entrypoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a safe `m2/energy-efficiency-api.py` command-line entrypoint that Node-RED can call to obtain one station's real-time dashboard JSON.

**Architecture:** The root-level script validates the fixed `dashboard ES01|ES02` command, loads secrets only from environment variables, delegates source conversion and calculation to existing `m2` modules, and emits exactly one compact JSON line. The first version builds a real-time calendar-day dashboard with empty `trend` and `events`, and never writes to NocoBase.

**Tech Stack:** Python standard library, `unittest`, existing M2 pure-Python modules, Node-RED Exec node.

**Spec:** `docs/m2/specs/2026-08-26-node-red-energy-efficiency-api-design.md`

## Global Constraints

- Create the deployable entrypoint at the M2 module as `m2/energy-efficiency-api.py`.
- Support exactly `dashboard ES01` and `dashboard ES02`.
- Read `VIFA_EMU_URL` and `VIFA_EMU_TOKEN` only from server environment variables.
- Default `M2_TIMEZONE` to `Asia/Shanghai` and `M2_REQUEST_TIMEOUT_SECONDS` to `15`.
- Never accept tokens or upstream URLs as command arguments.
- Standard output must contain exactly one compact JSON line and no traceback.
- First-version `trend` and `events` must be empty arrays and real-time data must come from `t_emu:list`.
- Do not write to NocoBase and do not add third-party Python dependencies.

---

### Task 1: Command and environment validation

**Files:**
- Create: `m2/energy-efficiency-api.py`
- Create: `m2/tests/test_energy_efficiency_api.py`

**Interfaces:**
- Produces: `parse_request(argv: list[str]) -> tuple[str, str]`
- Produces: `load_runtime_config(environ: Mapping[str, str]) -> dict`
- Raises: `EntrypointError(code: str, message: str, exit_code: int)` for safe expected failures.

- [ ] **Step 1: Write failing validation tests**

Load the hyphenated entrypoint by file path and assert literal behavior:

```python
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


ENTRYPOINT = Path(__file__).resolve().parents[2] / "m2/energy-efficiency-api.py"


def load_entrypoint():
    spec = spec_from_file_location("energy_efficiency_api_entrypoint", ENTRYPOINT)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnergyEfficiencyEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_entrypoint()

    def test_parse_request_accepts_only_dashboard_and_known_station(self):
        self.assertEqual(
            self.api.parse_request(["dashboard", "ES02"]),
            ("dashboard", "ES02"),
        )
        with self.assertRaisesRegex(self.api.EntrypointError, "参数不正确"):
            self.api.parse_request(["save", "ES02"])
        with self.assertRaisesRegex(self.api.EntrypointError, "参数不正确"):
            self.api.parse_request(["dashboard", "ES03"])

    def test_runtime_config_requires_server_environment_without_leaking_token(self):
        with self.assertRaisesRegex(self.api.EntrypointError, "服务器缺少运行配置"):
            self.api.load_runtime_config({})

        config = self.api.load_runtime_config({
            "VIFA_EMU_URL": "https://station.example/api/t_emu:list?filter=%7B%7D",
            "VIFA_EMU_TOKEN": "secret-token",
        })
        self.assertEqual(config["emu_token"], "secret-token")
        self.assertEqual(config["timeout_seconds"], 15.0)
        self.assertEqual(config["timezone"], "Asia/Shanghai")
```

- [ ] **Step 2: Run the validation tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api
```

Expected: FAIL because `m2/energy-efficiency-api.py` does not exist.

- [ ] **Step 3: Implement minimal parsing and configuration validation**

Create `m2/energy-efficiency-api.py` with:

```python
#!/usr/bin/env python3

import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class EntrypointError(ValueError):
    def __init__(self, code, message, exit_code):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def parse_request(argv):
    values = list(argv)
    if values not in (["dashboard", "ES01"], ["dashboard", "ES02"]):
        raise EntrypointError("invalid_arguments", "参数不正确", 2)
    return values[0], values[1]


def load_runtime_config(environ):
    url = str(environ.get("VIFA_EMU_URL", "")).strip()
    token = str(environ.get("VIFA_EMU_TOKEN", "")).strip()
    if not url or not token:
        raise EntrypointError("missing_config", "服务器缺少运行配置", 3)
    try:
        timeout = float(environ.get("M2_REQUEST_TIMEOUT_SECONDS", "15"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3)
    timezone_name = str(environ.get("M2_TIMEZONE", "Asia/Shanghai")).strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EntrypointError("missing_config", "服务器运行配置不正确", 3) from exc
    return {
        "emu_url": url,
        "emu_token": token,
        "timeout_seconds": timeout,
        "timezone": timezone_name,
    }
```

- [ ] **Step 4: Run validation tests and verify GREEN**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api
```

Expected: both validation tests PASS.

- [ ] **Step 5: Commit the validation contract**

```bash
git add m2/energy-efficiency-api.py m2/tests/test_energy_efficiency_api.py
git commit -m "feat: validate Node-RED energy API invocation"
```

---

### Task 2: Real-time dashboard orchestration and safe errors

**Files:**
- Modify: `m2/energy-efficiency-api.py`
- Modify: `m2/tests/test_energy_efficiency_api.py`

**Interfaces:**
- Consumes: `fetch_station_source_record(station_id, config)` from `m2.station_energy_data_adapter`.
- Consumes: `calculate_bus(source)` from `m2.station_energy_backend`.
- Consumes: `build_calendar_day_dashboard(station_id, timezone_name, as_of, realtime, points, events)` from `m2.station_efficiency_history`.
- Produces: `execute(argv, environ, fetch_station=None, calculate=None, build_dashboard=None) -> tuple[dict, int]`.

- [ ] **Step 1: Write failing orchestration tests**

Add literal fakes that exercise the real entrypoint logic without network access:

```python
    def valid_environment(self):
        return {
            "VIFA_EMU_URL": "https://station.example/api/t_emu:list?filter=%7B%7D",
            "VIFA_EMU_TOKEN": "secret-token",
        }

    def test_execute_builds_realtime_dashboard_with_empty_history(self):
        source = {"bus_id": "ES02", "data_time": "2026-08-26T15:30:00+08:00"}
        calculation = {"bus_id": "ES02", "data_time": source["data_time"]}
        calls = []

        def fetch_station(station_id, config):
            calls.append(("fetch", station_id, config["emu_url"]))
            return source

        def calculate(value):
            calls.append(("calculate", value))
            return calculation

        def build_dashboard(station_id, timezone_name, as_of, realtime, points, events):
            calls.append(("dashboard", station_id, timezone_name, as_of, points, events))
            return {
                "operation": "dashboard",
                "realtime": realtime,
                "trend": [],
                "events": [],
            }

        payload, exit_code = self.api.execute(
            ["dashboard", "ES02"],
            self.valid_environment(),
            fetch_station=fetch_station,
            calculate=calculate,
            build_dashboard=build_dashboard,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["realtime"], {
            "inputs": source,
            "result": calculation,
        })
        self.assertEqual(calls[-1], (
            "dashboard", "ES02", "Asia/Shanghai",
            "2026-08-26T15:30:00+08:00", [], [],
        ))

    def test_execute_returns_safe_source_error_without_token(self):
        def fail_source(station_id, config):
            raise self.api.StationEnergyDataError(
                "upstream failed with secret-token"
            )

        payload, exit_code = self.api.execute(
            ["dashboard", "ES02"],
            self.valid_environment(),
            fetch_station=fail_source,
        )

        self.assertEqual(exit_code, 4)
        self.assertEqual(payload, {
            "status": "error",
            "error": {
                "code": "source_error",
                "message": "场站实时数据读取失败",
            },
        })
        self.assertNotIn("secret-token", repr(payload))
```

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api
```

Expected: FAIL because `execute()` is not defined.

- [ ] **Step 3: Implement minimal orchestration**

Import the existing modules and add:

```python
from m2.station_efficiency_history import HistoryError, build_calendar_day_dashboard
from m2.station_energy_backend import BackendError, calculate_bus
from m2.station_energy_data_adapter import (
    StationEnergyDataError,
    fetch_station_source_record,
)


def _error_payload(code, message):
    return {"status": "error", "error": {"code": code, "message": message}}


def execute(
    argv,
    environ,
    fetch_station=fetch_station_source_record,
    calculate=calculate_bus,
    build_dashboard=build_calendar_day_dashboard,
):
    try:
        _, station_id = parse_request(argv)
        config = load_runtime_config(environ)
        source = fetch_station(station_id, config)
        calculation = calculate(source)
        data = build_dashboard(
            station_id,
            config["timezone"],
            source["data_time"],
            {"inputs": source, "result": calculation},
            [],
            [],
        )
        return {"status": "ok", "data": data}, 0
    except EntrypointError as exc:
        return _error_payload(exc.code, exc.message), exc.exit_code
    except StationEnergyDataError:
        return _error_payload("source_error", "场站实时数据读取失败"), 4
    except (BackendError, HistoryError):
        return _error_payload("calculation_error", "场站能效计算失败"), 5
    except Exception:
        return _error_payload("internal_error", "服务器内部错误"), 1
```

- [ ] **Step 4: Run orchestration tests and verify GREEN**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api
```

Expected: all entrypoint tests PASS and the token does not appear in output.

- [ ] **Step 5: Commit dashboard orchestration**

```bash
git add m2/energy-efficiency-api.py m2/tests/test_energy_efficiency_api.py
git commit -m "feat: add Node-RED realtime dashboard entrypoint"
```

---

### Task 3: One-line CLI output and full regression verification

**Files:**
- Modify: `m2/energy-efficiency-api.py`
- Modify: `m2/tests/test_energy_efficiency_api.py`

**Interfaces:**
- Consumes: `execute(argv, environ) -> tuple[dict, int]`.
- Produces: `main(argv=None, environ=None, stdout=None) -> int`.
- Produces: exactly one UTF-8 JSON line on standard output.

- [ ] **Step 1: Write failing output tests**

```python
from io import StringIO


    def test_main_prints_one_compact_json_line_for_invalid_arguments(self):
        output = StringIO()
        exit_code = self.api.main(
            argv=["dashboard", "ES03"],
            environ={},
            stdout=output,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(
            output.getvalue(),
            '{"status":"error","error":{"code":"invalid_arguments",'
            '"message":"参数不正确"}}\n',
        )
```

- [ ] **Step 2: Run output test and verify RED**

Run:

```bash
python3 -m unittest \
  m2.tests.test_energy_efficiency_api.EnergyEfficiencyEntrypointTests.test_main_prints_one_compact_json_line_for_invalid_arguments
```

Expected: FAIL because `main()` is not defined.

- [ ] **Step 3: Implement compact JSON output and process exit**

```python
import json
import os
import sys


def main(argv=None, environ=None, stdout=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    payload, exit_code = execute(argv, environ)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    stdout.write(text + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run entrypoint tests and direct CLI smoke test**

Run:

```bash
python3 -m unittest m2.tests.test_energy_efficiency_api
python3 m2/energy-efficiency-api.py dashboard ES03
```

Expected: tests PASS; direct command prints one error JSON line and exits with code `2`.

- [ ] **Step 5: Run complete Python and browser regression checks**

Run:

```bash
python3 -m unittest discover -s m2/tests -p 'test_*.py'
python3 -m py_compile m2/energy-efficiency-api.py m2/*.py
node m2/tests/energy_dashboard_e2e.js
git diff --check
```

Expected: all Python tests PASS, compilation exits `0`, browser test prints `energy_dashboard_e2e_ok`, and `git diff --check` has no output.

- [ ] **Step 6: Perform a read-only real-data smoke test**

On the server or an approved network-enabled shell, set the environment variables without printing them, then run:

```bash
python3 /userdata/holo/pyfiles/energy-efficiency-api.py dashboard ES02
```

Expected: one JSON line with `status: ok`, `data.operation: dashboard`, real `data.realtime`, and empty `data.trend` and `data.events`. Confirm no requests are sent to `t_efficiency_points` or `t_efficiency_bottleneck_events`.

- [ ] **Step 7: Commit verified entrypoint**

```bash
git add m2/energy-efficiency-api.py m2/tests/test_energy_efficiency_api.py
git commit -m "test: verify Node-RED energy API output"
```
