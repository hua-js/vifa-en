# Station Efficiency History and Bottlenecks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an API-independent Python history/event module and update the existing HTML to display the current natural-day minute curve and the two configured bottleneck event types.

**Architecture:** Keep `station_energy_backend.py` unchanged as the existing real-time calculator. Create a separate pure-Python module that converts already-calculated chain values into minute records, evaluates bottleneck windows, emits logical Upsert records, and builds the confirmed daily dashboard contract; a future NocoBase adapter will translate those records into the actual REST calls once the API contract is supplied. Update the vanilla HTML/SVG renderer and its Playwright fixture to consume minute timestamps instead of 25 hourly buckets.

**Tech Stack:** Python 3 standard library (`datetime`, `zoneinfo`, `math`, `unittest`), vanilla JavaScript and SVG, Node.js, Playwright.

**Spec:** `场站三条能效链路历史曲线与瓶颈事件设计.md`

## Global Constraints

- Do not modify `station_energy_backend.py` or its existing real-time calculation formulas.
- Put all new production Python logic in one importable file: `station_efficiency_history.py`.
- The new Python file must perform no HTTP, file, database, stdin, stdout, CLI, scheduler, or mock-data I/O.
- Do not invent a NocoBase URL, route, token, pagination format, source collection name, or source-field mapping.
- Save logical minute records at a 60-second bucket; page refresh remains read-only.
- Treat `(station_id, data_time)` as the minute-point Upsert key.
- Treat `(station_id, event_type, device_id, start_time)` as the event Upsert key.
- Never convert a missing efficiency, input, output, event sample, or future minute into zero.
- Do not add `good`, `missing`, `stale`, or `bad` fields or rules.
- Only implement `inverter_low_load` and `battery_temperature_rise` bottlenecks.
- All thresholds, windows, trigger durations, and recovery durations come from the rule record; no business defaults are embedded in Python or HTML.
- Use timezone-aware ISO 8601 timestamps and natural-day range `[00:00, next day 00:00)` in the station timezone.
- The curve breaks when adjacent returned points are more than 120 seconds apart or when a series value is `null`.
- The current workspace is not a Git repository, so each task ends with a focused test and inspection checkpoint instead of a commit.

## File Structure

- Create `station_efficiency_history.py`: minute-record normalization, rule validation, two event evaluators, logical Upsert envelopes, natural-day summary and dashboard response builder.
- Create `tests/test_station_efficiency_history.py`: pure-Python tests for all new history and event behavior.
- Create `tests/station_efficiency_history_test_support.py`: deterministic minute points, rules, device samples, events, and full dashboard fixture used only by tests.
- Modify `场站三条能效链路能流图.html`: consume `range`, `summary_today`, minute `trend`, and persisted event fields; draw natural-day curves and gaps.
- Modify `tests/energy_dashboard_e2e.js`: serve the new fixture and verify minute rendering, gaps, independent nulls, event clipping, empty states, refresh locking, and existing real-time lanes.
- Read-only regression target `tests/test_station_energy_backend.py`: all existing calculator tests must remain green without edits.

---

### Task 1: Minute Record Boundary and Logical Upsert

**Files:**
- Create: `station_efficiency_history.py`
- Create: `tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: already-calculated values in `chains: dict[str, dict]` with the exact keys `pv_storage`, `storage_load`, and `pv_load`.
- Produces: `HistoryError`, `minute_bucket(value: str) -> datetime`, `build_minute_point(station_id, data_time, chains, formula_version, calculated_at) -> dict`, and `build_minute_upsert(point: dict) -> dict`.

- [ ] **Step 1: Write failing tests for timezone enforcement, minute bucketing, independent nulls, and the Upsert key**

Create `tests/test_station_efficiency_history.py` with:

```python
import unittest

from station_efficiency_history import (
    HistoryError,
    build_minute_point,
    build_minute_upsert,
)


class StationEfficiencyHistoryTests(unittest.TestCase):
    def test_build_minute_point_floors_seconds_and_preserves_independent_nulls(self):
        point = build_minute_point(
            station_id="station-1",
            data_time="2026-08-25T14:36:47+08:00",
            chains={
                "pv_storage": {"efficiency": 91.5, "input_kw": 103, "output_kw": 95},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 94.3, "input_kw": 96, "output_kw": 90.528},
            },
            formula_version="energy-chain-v1",
            calculated_at="2026-08-25T14:36:49+08:00",
        )
        self.assertEqual(point["data_time"], "2026-08-25T14:36:00+08:00")
        self.assertEqual(point["pv_storage_efficiency"], 91.5)
        self.assertIsNone(point["storage_load_efficiency"])
        self.assertEqual(point["pv_load_output_kw"], 90.528)

    def test_build_minute_point_rejects_naive_time_and_invalid_numbers(self):
        valid_chains = {
            name: {"efficiency": None, "input_kw": None, "output_kw": None}
            for name in ("pv_storage", "storage_load", "pv_load")
        }
        with self.assertRaises(HistoryError) as caught:
            build_minute_point(
                "station-1", "2026-08-25T14:36:00", valid_chains,
                "energy-chain-v1", "2026-08-25T14:36:01+08:00",
            )
        self.assertEqual(caught.exception.code, "invalid_time")

        valid_chains["pv_storage"]["input_kw"] = -1
        with self.assertRaises(HistoryError) as caught:
            build_minute_point(
                "station-1", "2026-08-25T14:36:00+08:00", valid_chains,
                "energy-chain-v1", "2026-08-25T14:36:01+08:00",
            )
        self.assertEqual(caught.exception.code, "invalid_minute_point")

    def test_build_minute_upsert_uses_station_and_minute_as_key(self):
        point = build_minute_point(
            "station-1",
            "2026-08-25T14:36:00+08:00",
            {
                "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 95, "input_kw": 80, "output_kw": 76},
            },
            "energy-chain-v1",
            "2026-08-25T14:36:02+08:00",
        )
        envelope = build_minute_upsert(point)
        self.assertEqual(envelope["collection"], "station_efficiency_points")
        self.assertEqual(
            envelope["key"],
            {"station_id": "station-1", "data_time": "2026-08-25T14:36:00+08:00"},
        )
        self.assertEqual(envelope["values"], point)
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
```

Expected: import error because `station_efficiency_history.py` does not exist.

- [ ] **Step 3: Implement the error type, time parser, numeric validation, record conversion, and Upsert envelope**

Create `station_efficiency_history.py` with this public boundary and the minimal helpers used by it:

```python
"""Pure minute-history and bottleneck transformations for the energy dashboard."""

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo


CHAIN_COLUMNS = {
    "pv_storage": (
        "pv_storage_efficiency", "pv_storage_input_kw", "pv_storage_output_kw"
    ),
    "storage_load": (
        "storage_load_efficiency", "storage_load_input_kw", "storage_load_output_kw"
    ),
    "pv_load": (
        "pv_load_efficiency", "pv_load_input_kw", "pv_load_output_kw"
    ),
}


class HistoryError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def _parse_time(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HistoryError("invalid_time", f"{field} 不是合法 ISO 8601 时间", {"field": field}) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryError("invalid_time", f"{field} 必须包含时区", {"field": field})
    return parsed


def minute_bucket(value):
    return _parse_time(value, "data_time").replace(second=0, microsecond=0)


def _optional_non_negative(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError("invalid_minute_point", f"{field} 必须是数值或 null", {"field": field})
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise HistoryError("invalid_minute_point", f"{field} 必须是有限非负数", {"field": field})
    return number


def build_minute_point(station_id, data_time, chains, formula_version, calculated_at):
    if not str(station_id).strip():
        raise HistoryError("invalid_minute_point", "station_id 不能为空", {"field": "station_id"})
    if set(chains or {}) != set(CHAIN_COLUMNS):
        raise HistoryError("invalid_minute_point", "chains 必须包含三条链路", {"field": "chains"})
    if not str(formula_version).strip():
        raise HistoryError("invalid_minute_point", "formula_version 不能为空", {"field": "formula_version"})

    point = {
        "station_id": str(station_id),
        "data_time": minute_bucket(data_time).isoformat(),
        "formula_version": str(formula_version),
        "calculated_at": _parse_time(calculated_at, "calculated_at").isoformat(),
    }
    for chain_name, columns in CHAIN_COLUMNS.items():
        chain = chains[chain_name]
        if not isinstance(chain, dict):
            raise HistoryError("invalid_minute_point", "链路结果必须是对象", {"field": chain_name})
        efficiency_column, input_column, output_column = columns
        point[efficiency_column] = _optional_non_negative(chain.get("efficiency"), efficiency_column)
        point[input_column] = _optional_non_negative(chain.get("input_kw"), input_column)
        point[output_column] = _optional_non_negative(chain.get("output_kw"), output_column)
    return point


def build_minute_upsert(point):
    return {
        "collection": "station_efficiency_points",
        "key": {"station_id": point["station_id"], "data_time": point["data_time"]},
        "values": dict(point),
    }
```

- [ ] **Step 4: Run Task 1 tests and inspect the production module for I/O**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
rg -n 'urllib|requests|open\(|print\(|input\(|argparse|__main__' station_efficiency_history.py
```

Expected: 3 tests pass; `rg` returns no matches.

---

### Task 2: Natural-Day Summary and Dashboard Contract

**Files:**
- Modify: `station_efficiency_history.py`
- Modify: `tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: minute point dictionaries from Task 1, persisted event dictionaries from Tasks 3–4, an unchanged `realtime` dictionary, a station timezone name, and an explicit `as_of` timestamp.
- Produces: `summarize_today(points: list[dict]) -> dict`, `build_calendar_day_dashboard(station_id, timezone_name, as_of, realtime, points, events) -> dict`.

- [ ] **Step 1: Add failing tests for filtering, ordering, cumulative efficiency, null independence, and overlapping events**

Append to `StationEfficiencyHistoryTests`:

```python
    def test_calendar_day_dashboard_uses_existing_minutes_only(self):
        points = [
            {
                "station_id": "station-1",
                "data_time": "2026-08-25T00:00:00+08:00",
                "pv_storage_efficiency": 50.0,
                "storage_load_efficiency": None,
                "pv_load_efficiency": 80.0,
                "pv_storage_input_kw": 100.0,
                "pv_storage_output_kw": 50.0,
                "storage_load_input_kw": None,
                "storage_load_output_kw": None,
                "pv_load_input_kw": 100.0,
                "pv_load_output_kw": 80.0,
                "formula_version": "v1",
                "calculated_at": "2026-08-25T00:00:02+08:00",
            },
            {
                "station_id": "station-1",
                "data_time": "2026-08-25T00:02:00+08:00",
                "pv_storage_efficiency": 100.0,
                "storage_load_efficiency": 90.0,
                "pv_load_efficiency": None,
                "pv_storage_input_kw": 300.0,
                "pv_storage_output_kw": 300.0,
                "storage_load_input_kw": 100.0,
                "storage_load_output_kw": 90.0,
                "pv_load_input_kw": None,
                "pv_load_output_kw": None,
                "formula_version": "v1",
                "calculated_at": "2026-08-25T00:02:02+08:00",
            },
        ]
        events = [{
            "id": 7,
            "station_id": "station-1",
            "event_type": "inverter_low_load",
            "device_id": "inv-1",
            "device_name": "1#逆变器",
            "start_time": "2026-08-24T23:50:00+08:00",
            "end_time": None,
            "last_seen_time": "2026-08-25T00:02:00+08:00",
            "status": "active",
            "observed_value": 12.0,
            "threshold_value": 20.0,
            "observed_unit": "%",
            "evidence": {"display_text": "负载率最低 12.0%"},
            "impact_chain": ["光→储", "光→用"],
            "rule_version": 3,
        }]
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {"inputs": {"data_time": "2026-08-25T14:36:20+08:00"}, "result": {}},
            list(reversed(points)), events,
        )
        self.assertEqual(result["range"]["start_time"], "2026-08-25T00:00:00+08:00")
        self.assertEqual(result["range"]["end_time"], "2026-08-26T00:00:00+08:00")
        self.assertEqual(result["range"]["latest_time"], "2026-08-25T00:02:00+08:00")
        self.assertEqual([row["data_time"] for row in result["trend"]], [
            "2026-08-25T00:00:00+08:00", "2026-08-25T00:02:00+08:00"
        ])
        self.assertAlmostEqual(result["summary_today"]["pv_storage_efficiency"], 87.5)
        self.assertAlmostEqual(result["summary_today"]["storage_load_efficiency"], 90.0)
        self.assertAlmostEqual(result["summary_today"]["pv_load_efficiency"], 80.0)
        self.assertEqual(result["events"][0]["status"], "持续中")
        self.assertIsNone(result["events"][0]["end"])

    def test_calendar_day_dashboard_returns_empty_contract_without_points(self):
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, [], [],
        )
        self.assertIsNone(result["range"]["latest_time"])
        self.assertEqual(result["trend"], [])
        self.assertIsNone(result["summary_today"]["pv_storage_efficiency"])
        self.assertEqual(result["events"], [])

    def test_calendar_day_dashboard_rejects_duplicates_and_excludes_future_points(self):
        point = build_minute_point(
            "station-1", "2026-08-25T14:36:00+08:00",
            {
                "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 95, "input_kw": 100, "output_kw": 95},
            },
            "v1", "2026-08-25T14:36:02+08:00",
        )
        with self.assertRaises(HistoryError) as caught:
            build_calendar_day_dashboard(
                "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
                {}, [point, dict(point)], [],
            )
        self.assertEqual(caught.exception.code, "duplicate_minute")

        future = dict(point, data_time="2026-08-25T14:37:00+08:00")
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, [point, future], [],
        )
        self.assertEqual(len(result["trend"]), 1)
```

Add `build_calendar_day_dashboard` to the imports at the top of the test file.

- [ ] **Step 2: Run the two focused tests and verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_calendar_day_dashboard_uses_existing_minutes_only \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_calendar_day_dashboard_returns_empty_contract_without_points -v
```

Expected: import error because `build_calendar_day_dashboard` is not defined.

- [ ] **Step 3: Implement natural-day selection, cumulative ratios, event overlap, and the confirmed public names**

Add to `station_efficiency_history.py`:

```python
EVENT_LABELS = {
    "inverter_low_load": "逆变器低负载",
    "battery_temperature_rise": "电池温升",
}


def _day_bounds(as_of, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise HistoryError("invalid_timezone", "场站时区无效", {"timezone": timezone_name}) from exc
    local = _parse_time(as_of, "as_of").astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _summary_efficiency(points, input_column, output_column):
    pairs = [
        (point.get(input_column), point.get(output_column))
        for point in points
        if point.get(input_column) is not None and point.get(output_column) is not None
    ]
    total_input = sum(pair[0] / 60.0 for pair in pairs)
    total_output = sum(pair[1] / 60.0 for pair in pairs)
    return None if total_input <= 0 else total_output / total_input * 100.0


def summarize_today(points):
    return {
        "pv_storage_efficiency": _summary_efficiency(points, "pv_storage_input_kw", "pv_storage_output_kw"),
        "storage_load_efficiency": _summary_efficiency(points, "storage_load_input_kw", "storage_load_output_kw"),
        "pv_load_efficiency": _summary_efficiency(points, "pv_load_input_kw", "pv_load_output_kw"),
    }


def _event_overlaps(event, start, end):
    event_start = _parse_time(event["start_time"], "start_time")
    event_end = _parse_time(event["end_time"], "end_time") if event.get("end_time") else None
    return event_start < end and (event_end is None or event_end >= start)


def build_calendar_day_dashboard(station_id, timezone_name, as_of, realtime, points, events):
    start, end = _day_bounds(as_of, timezone_name)
    cutoff = minute_bucket(as_of).astimezone(start.tzinfo)
    selected = sorted(
        (
            point for point in points
            if str(point.get("station_id")) == str(station_id)
            and start <= _parse_time(point["data_time"], "data_time").astimezone(start.tzinfo) <= cutoff
        ),
        key=lambda point: _parse_time(point["data_time"], "data_time"),
    )
    keys = [(point["station_id"], point["data_time"]) for point in selected]
    if len(keys) != len(set(keys)):
        raise HistoryError("duplicate_minute", "同一场站同一分钟只能有一个效率点")
    latest = selected[-1]["data_time"] if selected else None
    summary = summarize_today(selected)
    public_events = []
    for event in sorted(events, key=lambda item: _parse_time(item["start_time"], "start_time")):
        if str(event.get("station_id")) != str(station_id) or not _event_overlaps(event, start, end):
            continue
        public_events.append({
            "id": event.get("id"),
            "event_type": event["event_type"],
            "type": EVENT_LABELS[event["event_type"]],
            "device": event["device_name"],
            "start": event["start_time"],
            "end": event.get("end_time"),
            "evidence": event["evidence"]["display_text"],
            "impact": list(event["impact_chain"]),
            "status": "持续中" if event["status"] == "active" else "已恢复",
        })
    return {
        "operation": "dashboard",
        "range": {
            "mode": "calendar_day",
            "timezone": timezone_name,
            "start_time": start.isoformat(),
            "latest_time": latest,
            "end_time": end.isoformat(),
            "interval_seconds": 60,
        },
        "realtime": dict(realtime or {}),
        "summary_today": {
            "start_time": start.isoformat(),
            "end_time": latest,
            **summary,
        },
        "trend": [{
            "data_time": point["data_time"],
            "pvStorage": point["pv_storage_efficiency"],
            "storageLoad": point["storage_load_efficiency"],
            "pvLoad": point["pv_load_efficiency"],
        } for point in selected],
        "events": public_events,
    }
```

- [ ] **Step 4: Run all history tests**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
```

Expected: 6 tests pass.

---

### Task 3: Config Validation and Inverter Low-Load State Machine

**Files:**
- Modify: `station_efficiency_history.py`
- Modify: `tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: one station rule row; one device's minute samples shaped as `{device_id, device_name, data_time, active_power_kw, rated_power_kw}`; an optional persisted active event.
- Produces: `normalize_rule(record: dict) -> dict`, `evaluate_inverter_low_load(samples: list[dict], rule: dict, active_event: dict | None = None) -> dict | None`, and `build_event_upsert(event: dict) -> dict`.

- [ ] **Step 1: Add failing tests for no hardcoded defaults, trigger start time, minimum observed load, recovery, stop behavior, and missing minutes**

Add imports for `normalize_rule`, `evaluate_inverter_low_load`, and `build_event_upsert`, then append:

```python
    def inverter_rule(self):
        return normalize_rule({
            "station_id": "station-1",
            "enabled": True,
            "inverter_min_running_power_kw": 5,
            "inverter_low_load_threshold_pct": 20,
            "inverter_trigger_minutes": 3,
            "inverter_recovery_minutes": 2,
            "temperature_rise_window_minutes": 5,
            "temperature_rise_threshold_c": 3,
            "temperature_trigger_minutes": 2,
            "temperature_recovery_minutes": 2,
            "version": 4,
            "updated_at": "2026-08-25T00:00:00+08:00",
        })

    def inverter_sample(self, minute, power):
        return {
            "device_id": "inv-1",
            "device_name": "1#逆变器",
            "data_time": f"2026-08-25T10:{minute:02d}:00+08:00",
            "active_power_kw": power,
            "rated_power_kw": 100,
        }

    def test_rule_requires_every_configured_value(self):
        record = dict(self.inverter_rule())
        del record["inverter_trigger_minutes"]
        with self.assertRaises(HistoryError) as caught:
            normalize_rule(record)
        self.assertEqual(caught.exception.code, "invalid_rule")

    def test_inverter_event_opens_at_first_qualifying_minute(self):
        samples = [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)]
        event = evaluate_inverter_low_load(samples, self.inverter_rule())
        self.assertEqual(event["start_time"], "2026-08-25T10:00:00+08:00")
        self.assertEqual(event["status"], "active")
        self.assertEqual(event["observed_value"], 10.0)
        self.assertEqual(event["impact_chain"], ["光→储", "光→用"])
        self.assertEqual(build_event_upsert(event)["key"], {
            "station_id": "station-1",
            "event_type": "inverter_low_load",
            "device_id": "inv-1",
            "start_time": "2026-08-25T10:00:00+08:00",
        })

    def test_inverter_event_recovers_when_device_stops_for_configured_duration(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        samples = [self.inverter_sample(3, 0), self.inverter_sample(4, 0)]
        recovered = evaluate_inverter_low_load(samples, self.inverter_rule(), active)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T10:03:00+08:00")

    def test_inverter_gap_does_not_complete_trigger_or_recovery(self):
        samples = [self.inverter_sample(0, 10), self.inverter_sample(2, 10), self.inverter_sample(3, 10)]
        self.assertIsNone(evaluate_inverter_low_load(samples, self.inverter_rule()))
```

- [ ] **Step 2: Run the four focused tests and verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_rule_requires_every_configured_value \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_inverter_event_opens_at_first_qualifying_minute \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_inverter_event_recovers_when_device_stops_for_configured_duration \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_inverter_gap_does_not_complete_trigger_or_recovery -v
```

Expected: import error for the new functions.

- [ ] **Step 3: Implement strict rule validation and trailing contiguous-run helpers**

Add a `RULE_FIELDS` tuple containing every field used in `inverter_rule()`. Implement `normalize_rule()` so it:

```python
RULE_FIELDS = (
    "station_id", "enabled", "inverter_min_running_power_kw",
    "inverter_low_load_threshold_pct", "inverter_trigger_minutes",
    "inverter_recovery_minutes", "temperature_rise_window_minutes",
    "temperature_rise_threshold_c", "temperature_trigger_minutes",
    "temperature_recovery_minutes", "version", "updated_at",
)


def normalize_rule(record):
    missing = [field for field in RULE_FIELDS if field not in record]
    if missing:
        raise HistoryError("invalid_rule", "瓶颈规则缺少字段", {"fields": missing})
    normalized = dict(record)
    for field in (
        "inverter_trigger_minutes", "inverter_recovery_minutes",
        "temperature_rise_window_minutes", "temperature_trigger_minutes",
        "temperature_recovery_minutes", "version",
    ):
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HistoryError("invalid_rule", f"{field} 必须是正整数", {"field": field})
    for field in (
        "inverter_min_running_power_kw", "inverter_low_load_threshold_pct",
        "temperature_rise_threshold_c",
    ):
        try:
            number = _optional_non_negative(record[field], field)
        except HistoryError as exc:
            raise HistoryError("invalid_rule", exc.message, exc.details) from exc
        if number is None:
            raise HistoryError("invalid_rule", f"{field} 不能为空", {"field": field})
        normalized[field] = number
    if not isinstance(record["enabled"], bool):
        raise HistoryError("invalid_rule", "enabled 必须是布尔值", {"field": "enabled"})
    if normalized["inverter_low_load_threshold_pct"] > 100:
        raise HistoryError("invalid_rule", "低负载阈值不能超过 100%", {"field": "inverter_low_load_threshold_pct"})
    normalized["station_id"] = str(record["station_id"]).strip()
    if not normalized["station_id"]:
        raise HistoryError("invalid_rule", "station_id 不能为空", {"field": "station_id"})
    normalized["updated_at"] = _parse_time(record["updated_at"], "updated_at").isoformat()
    return normalized
```

Add these helpers to sort samples, require one device, validate exact one-minute adjacency, and return the trailing contiguous records satisfying a predicate. A gap resets the trailing run; it is never counted as a false sample:

```python
def _ordered_one_device(samples, normalizer):
    if not isinstance(samples, list):
        raise HistoryError("invalid_device_samples", "设备样本必须是数组")
    ordered = sorted((normalizer(sample) for sample in samples), key=lambda item: item["_time"])
    device_ids = {sample["device_id"] for sample in ordered}
    if len(device_ids) > 1:
        raise HistoryError("invalid_device_samples", "一次只能评估一个设备")
    times = [sample["_time"] for sample in ordered]
    if len(times) != len(set(times)):
        raise HistoryError("invalid_device_samples", "设备样本时间不能重复")
    return ordered


def _normalize_inverter_samples(samples):
    def normalize(sample):
        active = _optional_non_negative(sample.get("active_power_kw"), "active_power_kw")
        rated = _optional_non_negative(sample.get("rated_power_kw"), "rated_power_kw")
        if active is None or rated is None or rated <= 0:
            raise HistoryError("invalid_device_samples", "逆变器功率必须完整且额定功率大于零")
        time = minute_bucket(sample.get("data_time"))
        return {
            "device_id": str(sample.get("device_id", "")),
            "device_name": str(sample.get("device_name", "")),
            "data_time": time.isoformat(),
            "active_power_kw": active,
            "rated_power_kw": rated,
            "_time": time,
        }
    return _ordered_one_device(samples, normalize)


def _trailing_minute_run(items, predicate, time_getter=lambda item: item["_time"]):
    run = []
    previous_time = None
    for item in items:
        current_time = time_getter(item)
        if previous_time is not None and current_time - previous_time != timedelta(minutes=1):
            run = []
        if predicate(item):
            run.append(item)
        else:
            run = []
        previous_time = current_time
    return run


def _new_event(rule, event_type, first, last, observed, threshold, unit, impact, text):
    return {
        "station_id": rule["station_id"],
        "event_type": event_type,
        "device_id": first["device_id"],
        "device_name": first["device_name"],
        "start_time": first["data_time"],
        "end_time": None,
        "last_seen_time": last["data_time"],
        "status": "active",
        "observed_value": observed,
        "threshold_value": threshold,
        "observed_unit": unit,
        "evidence": {
            "display_text": text,
            "continuous_minutes": int((last["_time"] - first["_time"]).total_seconds() / 60) + 1,
            "rule": dict(rule),
        },
        "impact_chain": list(impact),
        "rule_version": rule["version"],
    }
```

- [ ] **Step 4: Implement the inverter state transition and event Upsert envelope**

Implement `evaluate_inverter_low_load()` with these exact transitions:

```python
def evaluate_inverter_low_load(samples, rule, active_event=None):
    rule = normalize_rule(rule)
    ordered = _normalize_inverter_samples(samples)
    if not ordered:
        return dict(active_event) if active_event else None
    if not rule["enabled"] and active_event is None:
        return None

    def low_load(sample, threshold, min_power):
        rate = sample["active_power_kw"] / sample["rated_power_kw"] * 100.0
        return sample["active_power_kw"] >= min_power and rate < threshold

    if active_event is None:
        run = _trailing_minute_run(
            ordered,
            lambda sample: low_load(
                sample,
                rule["inverter_low_load_threshold_pct"],
                rule["inverter_min_running_power_kw"],
            ),
        )
        if len(run) < rule["inverter_trigger_minutes"]:
            return None
        rates = [sample["active_power_kw"] / sample["rated_power_kw"] * 100.0 for sample in run]
        first, last = run[0], run[-1]
        return _new_event(
            rule, "inverter_low_load", first, last,
            min(rates), rule["inverter_low_load_threshold_pct"], "%",
            ["光→储", "光→用"],
            f"负载率最低 {min(rates):.2f}%，低于阈值 {rule['inverter_low_load_threshold_pct']:.2f}%",
        )

    event = {**active_event, "evidence": dict(active_event["evidence"])}
    snapshot = event["evidence"]["rule"]
    recovery = _trailing_minute_run(
        ordered,
        lambda sample: not low_load(
            sample,
            snapshot["inverter_low_load_threshold_pct"],
            snapshot["inverter_min_running_power_kw"],
        ),
    )
    valid_rates = [
        sample["active_power_kw"] / sample["rated_power_kw"] * 100.0
        for sample in ordered if sample["active_power_kw"] >= snapshot["inverter_min_running_power_kw"]
    ]
    if valid_rates:
        event["observed_value"] = min(event["observed_value"], min(valid_rates))
        event["evidence"]["display_text"] = (
            f"负载率最低 {event['observed_value']:.2f}%，"
            f"低于阈值 {snapshot['inverter_low_load_threshold_pct']:.2f}%"
        )
    event["last_seen_time"] = ordered[-1]["data_time"]
    if len(recovery) >= snapshot["inverter_recovery_minutes"]:
        event["status"] = "recovered"
        event["end_time"] = recovery[0]["data_time"]
    return event


def build_event_upsert(event):
    return {
        "collection": "efficiency_bottleneck_events",
        "key": {
            "station_id": event["station_id"],
            "event_type": event["event_type"],
            "device_id": event["device_id"],
            "start_time": event["start_time"],
        },
        "values": dict(event),
    }
```

- [ ] **Step 5: Run all history tests**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
```

Expected: 10 tests pass.

---

### Task 4: Battery Temperature-Rise State Machine

**Files:**
- Modify: `station_efficiency_history.py`
- Modify: `tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: one device's exact-minute temperature samples shaped as `{device_id, device_name, data_time, temperature_c}`, a normalized rule, and an optional persisted active event.
- Produces: `evaluate_battery_temperature_rise(samples: list[dict], rule: dict, active_event: dict | None = None) -> dict | None`.

- [ ] **Step 1: Add failing tests for exact rolling windows, trigger, maximum rise, recovery, and incomplete windows**

Add the import and append:

```python
    def temperature_sample(self, minute, temperature):
        return {
            "device_id": "battery-1",
            "device_name": "1#电池簇",
            "data_time": f"2026-08-25T11:{minute:02d}:00+08:00",
            "temperature_c": temperature,
        }

    def test_temperature_event_uses_window_rise_and_opens_at_first_condition(self):
        samples = [
            self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
            self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
            self.temperature_sample(6, 28.5),
        ]
        event = evaluate_battery_temperature_rise(samples, self.inverter_rule())
        self.assertEqual(event["start_time"], "2026-08-25T11:05:00+08:00")
        self.assertEqual(event["event_type"], "battery_temperature_rise")
        self.assertAlmostEqual(event["observed_value"], 3.2)
        self.assertEqual(event["impact_chain"], ["光→储", "储→用"])

    def test_temperature_event_recovers_after_consecutive_below_threshold_windows(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        recovery_samples = [
            self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
            self.temperature_sample(6, 28.5), self.temperature_sample(7, 28.0),
            self.temperature_sample(8, 28.2),
        ]
        recovered = evaluate_battery_temperature_rise(recovery_samples, self.inverter_rule(), active)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T11:07:00+08:00")

    def test_temperature_window_gap_is_not_a_valid_condition(self):
        samples = [
            self.temperature_sample(0, 25.0),
            self.temperature_sample(2, 25.5),
            self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5),
            self.temperature_sample(5, 29.0),
            self.temperature_sample(6, 29.5),
        ]
        self.assertIsNone(evaluate_battery_temperature_rise(samples, self.inverter_rule()))
```

- [ ] **Step 2: Run the three focused tests and verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_temperature_event_uses_window_rise_and_opens_at_first_condition \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_temperature_event_recovers_after_consecutive_below_threshold_windows \
  tests.test_station_efficiency_history.StationEfficiencyHistoryTests.test_temperature_window_gap_is_not_a_valid_condition -v
```

Expected: import error because `evaluate_battery_temperature_rise` is undefined.

- [ ] **Step 3: Implement exact-window observations and temperature transitions**

Add exact sample normalization and build a valid observation only when every minute from `current_time - window_minutes` through `current_time` exists:

```python
def _normalize_temperature_samples(samples):
    def normalize(sample):
        value = sample.get("temperature_c")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HistoryError("invalid_device_samples", "temperature_c 必须是有限数值")
        time = minute_bucket(sample.get("data_time"))
        return {
            "device_id": str(sample.get("device_id", "")),
            "device_name": str(sample.get("device_name", "")),
            "data_time": time.isoformat(),
            "temperature_c": float(value),
            "_time": time,
        }
    return _ordered_one_device(samples, normalize)


def _temperature_observations(samples, window_minutes):
    by_time = {sample["_time"]: sample for sample in samples}
    observations = []
    for sample in samples:
        window_start = sample["_time"] - timedelta(minutes=window_minutes)
        required = [
            window_start + timedelta(minutes=offset)
            for offset in range(window_minutes + 1)
        ]
        if not all(point_time in by_time for point_time in required):
            continue
        observations.append({
            "sample": sample,
            "rise_c": sample["temperature_c"] - by_time[window_start]["temperature_c"],
        })
    return observations


def _trailing_observation_run(observations, predicate):
    return _trailing_minute_run(
        observations,
        predicate,
        time_getter=lambda item: item["sample"]["_time"],
    )
```

Implement the public evaluator with the same active-event snapshot rule as Task 3:

```python
def evaluate_battery_temperature_rise(samples, rule, active_event=None):
    rule = normalize_rule(rule)
    ordered = _normalize_temperature_samples(samples)
    if not ordered:
        return dict(active_event) if active_event else None
    if not rule["enabled"] and active_event is None:
        return None

    snapshot = active_event["evidence"]["rule"] if active_event else rule
    observations = _temperature_observations(
        ordered, snapshot["temperature_rise_window_minutes"]
    )
    if not observations:
        return dict(active_event) if active_event else None

    if active_event is None:
        run = _trailing_observation_run(
            observations,
            lambda item: item["rise_c"] >= rule["temperature_rise_threshold_c"],
        )
        if len(run) < rule["temperature_trigger_minutes"]:
            return None
        maximum = max(item["rise_c"] for item in run)
        return _new_event(
            rule, "battery_temperature_rise", run[0]["sample"], run[-1]["sample"],
            maximum, rule["temperature_rise_threshold_c"], "℃",
            ["光→储", "储→用"],
            f"{rule['temperature_rise_window_minutes']} 分钟最大温升 {maximum:.2f}℃",
        )

    event = {**active_event, "evidence": dict(active_event["evidence"])}
    event["last_seen_time"] = observations[-1]["sample"]["data_time"]
    event["observed_value"] = max(
        event["observed_value"], max(item["rise_c"] for item in observations)
    )
    event["evidence"]["display_text"] = (
        f"{snapshot['temperature_rise_window_minutes']} 分钟最大温升 "
        f"{event['observed_value']:.2f}℃"
    )
    recovery = _trailing_observation_run(
        observations,
        lambda item: item["rise_c"] < snapshot["temperature_rise_threshold_c"],
    )
    if len(recovery) >= snapshot["temperature_recovery_minutes"]:
        event["status"] = "recovered"
        event["end_time"] = recovery[0]["sample"]["data_time"]
    return event
```

- [ ] **Step 4: Run all history tests and verify event dictionaries are JSON-compatible**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
python3 -c 'import json; from tests.test_station_efficiency_history import StationEfficiencyHistoryTests as T; from station_efficiency_history import evaluate_battery_temperature_rise; t=T(); print(json.dumps(evaluate_battery_temperature_rise([t.temperature_sample(i, v) for i, v in enumerate([25,25.3,25.6,26,26.5,28.1,28.5])], t.inverter_rule()), ensure_ascii=False))'
```

Expected: 13 tests pass; the second command prints one JSON object without serialization errors.

---

### Task 5: Deterministic Full Dashboard Fixture

**Files:**
- Create: `tests/station_efficiency_history_test_support.py`
- Modify: `tests/test_station_efficiency_history.py`

**Interfaces:**
- Consumes: `build_calendar_day_dashboard()` and deterministic test records only.
- Produces: `build_history_dashboard_response() -> dict` containing minute rows through 14:36, a deliberate 3-minute gap, an independent `storageLoad=null`, one active low-load event, and one recovered cross-midnight temperature event.

- [ ] **Step 1: Create the test-only fixture builder**

Create `tests/station_efficiency_history_test_support.py`:

```python
from datetime import datetime, timedelta

from station_efficiency_history import build_calendar_day_dashboard, build_minute_point
from tests.station_energy_test_support import build_dashboard_response


def _chains(index):
    return {
        "pv_storage": {"efficiency": 88.0 + index * 0.02, "input_kw": 100.0, "output_kw": 88.0 + index * 0.02},
        "storage_load": {"efficiency": None if index == 3 else 90.0, "input_kw": None if index == 3 else 100.0, "output_kw": None if index == 3 else 90.0},
        "pv_load": {"efficiency": 95.0, "input_kw": 100.0, "output_kw": 95.0},
    }


def build_history_dashboard_response():
    legacy = build_dashboard_response()
    realtime = legacy["realtime"]
    realtime["inputs"]["data_time"] = "2026-08-25T14:36:00+08:00"
    realtime["result"]["data_time"] = "2026-08-25T14:36:00+08:00"
    start = datetime.fromisoformat("2026-08-25T14:27:00+08:00")
    minutes = [0, 1, 2, 6, 7, 8, 9]
    points = [
        build_minute_point(
            "station-1",
            (start + timedelta(minutes=offset)).isoformat(),
            _chains(index),
            "energy-chain-v1",
            (start + timedelta(minutes=offset, seconds=2)).isoformat(),
        )
        for index, offset in enumerate(minutes)
    ]
    events = [
        {
            "id": 1, "station_id": "station-1", "event_type": "inverter_low_load",
            "device_id": "inv-1", "device_name": "1#逆变器",
            "start_time": "2026-08-25T14:30:00+08:00", "end_time": None,
            "last_seen_time": "2026-08-25T14:36:00+08:00", "status": "active",
            "observed_value": 12.4, "threshold_value": 20.0, "observed_unit": "%",
            "evidence": {"display_text": "负载率最低 12.40%"},
            "impact_chain": ["光→储", "光→用"], "rule_version": 4,
        },
        {
            "id": 2, "station_id": "station-1", "event_type": "battery_temperature_rise",
            "device_id": "battery-1", "device_name": "1#电池簇",
            "start_time": "2026-08-24T23:50:00+08:00", "end_time": "2026-08-25T00:10:00+08:00",
            "last_seen_time": "2026-08-25T00:11:00+08:00", "status": "recovered",
            "observed_value": 3.5, "threshold_value": 3.0, "observed_unit": "℃",
            "evidence": {"display_text": "5 分钟最大温升 3.50℃"},
            "impact_chain": ["光→储", "储→用"], "rule_version": 4,
        },
    ]
    return build_calendar_day_dashboard(
        "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
        realtime, points, events,
    )
```

- [ ] **Step 2: Add a fixture contract test**

Append to `tests/test_station_efficiency_history.py`:

```python
    def test_full_fixture_matches_html_contract(self):
        from tests.station_efficiency_history_test_support import build_history_dashboard_response

        payload = build_history_dashboard_response()
        self.assertEqual(payload["operation"], "dashboard")
        self.assertEqual(payload["range"]["latest_time"], "2026-08-25T14:36:00+08:00")
        self.assertEqual(len(payload["trend"]), 7)
        self.assertIsNone(payload["trend"][3]["storageLoad"])
        self.assertEqual([event["status"] for event in payload["events"]], ["已恢复", "持续中"])
```

- [ ] **Step 3: Run the Python tests and serialize the fixture exactly as the browser test will**

Run:

```bash
python3 -m unittest tests.test_station_efficiency_history -v
python3 -c 'import json; from tests.station_efficiency_history_test_support import build_history_dashboard_response; print(json.dumps({"status":"ok","data":build_history_dashboard_response()}, ensure_ascii=False))'
```

Expected: 14 tests pass; the second command prints a JSON response with `data.range.latest_time` equal to `2026-08-25T14:36:00+08:00`.

---

### Task 6: HTML Natural-Day Minute Curve and Event Rendering

**Files:**
- Modify: `场站三条能效链路能流图.html:7`
- Modify: `场站三条能效链路能流图.html:823`
- Modify: `场站三条能效链路能流图.html:1367-1407`
- Modify: `场站三条能效链路能流图.html:1427-1430`
- Modify: `场站三条能效链路能流图.html:1435-1845`
- Modify: `tests/energy_dashboard_e2e.js`

**Interfaces:**
- Consumes: Task 2 contract: `range`, `realtime`, `summary_today`, timestamped `trend`, and public `events`.
- Produces: natural-day SVG rendering with fixed 0/4/8/12/16/20/24 ticks, minute tooltip selection, independent null/gap breaks, and event clipping.

- [ ] **Step 1: Change the browser fixture to the new response and add failing contract assertions**

In `tests/energy_dashboard_e2e.js`, change the Python fixture import to:

```javascript
const fixtureScript = [
  "import json",
  "from tests.station_efficiency_history_test_support import build_history_dashboard_response",
  'print(json.dumps({"status": "ok", "data": build_history_dashboard_response()}))',
].join("; ");
```

Remove the manual replacement of `dashboardPayload.data.events`. After the existing real-time assertions, replace the legacy 24-hour assertions with:

```javascript
assert.strictEqual(await page.locator("#trend-title").innerText(), "今日链路效率");
assert.strictEqual(await page.locator("#summary-pv-storage").innerText(), "88.06%");
assert.strictEqual(await page.locator("#summary-storage-load").innerText(), "90.00%");
assert.strictEqual(await page.locator("#summary-pv-load").innerText(), "95.00%");
assert.strictEqual(
  (await page.locator("#efficiency-readout").innerText()).replace(/　/g, " "),
  "14:36 光储88.83% 储用无运行数据 光用100.00%",
);
assert.strictEqual(
  await page.locator("[data-hour-tick='0']").textContent(),
  "00:00",
);
assert.strictEqual(
  await page.locator("[data-hour-tick='24']").textContent(),
  "24:00",
);
assert.strictEqual(
  await page.locator("#efficiency-trend-chart").getAttribute("data-latest-time"),
  "2026-08-25T14:36:00+08:00",
);
const pvPath = await page.locator("path[data-series='pvStorage']").getAttribute("d");
assert.strictEqual((pvPath.match(/ M /g) || []).length, 2);
const pvXCoordinates = [...pvPath.matchAll(/[ML]\s+([0-9.]+)/g)].map((match) => Number(match[1]));
const chartWidth = Number(
  (await page.locator("#efficiency-trend-chart").getAttribute("viewBox")).split(/\s+/)[2],
);
assert.ok(pvXCoordinates.at(-1) < chartWidth - 18);
const storagePath = await page.locator("path[data-series='storageLoad']").getAttribute("d");
assert.ok((storagePath.match(/ M /g) || []).length >= 2);
assert.ok(Number(await page.locator("rect[aria-label='逆变器低负载']").getAttribute("width")) > 0);
assert.ok(Number(await page.locator("rect[aria-label='电池温升']").getAttribute("width")) > 0);
assert.strictEqual(
  await page.locator("#event-table-body tr").count(),
  2,
);
```

- [ ] **Step 2: Run the browser test and verify the red state**

Run:

```bash
node tests/energy_dashboard_e2e.js
```

Expected: assertion failure because the HTML still reads `summary_24h`, requires 25 trend rows, and draws the old rolling-hour axis.

- [ ] **Step 3: Update titles, accessible labels, summary keys, and empty-event text**

Apply these exact public-copy changes:

```text
24小时链路效率            → 今日链路效率
24h输入、输出电量累计     → 今日输入、输出电量累计
三条链路24小时效率曲线    → 三条链路今日效率曲线
当前时间范围内无质量异常事件 → 当前时间范围内无瓶颈事件
```

In `renderSummary()`, read:

```javascript
setText("summary-pv-storage", formatPercent(summary?.pv_storage_efficiency));
setText("summary-storage-load", formatPercent(summary?.storage_load_efficiency));
setText("summary-pv-load", formatPercent(summary?.pv_load_efficiency));
```

In `renderEvents()`, display `[formatTime(item.start), item.type, item.device, item.evidence, item.impact.join("、"), item.status]`.

- [ ] **Step 4: Replace hourly chart state with timestamp state**

Initialize:

```javascript
let rows = [];
let events = [];
let range = null;
let geometry = null;
```

Add helpers:

```javascript
function timeRatio(value) {
  const point = Date.parse(value);
  const start = Date.parse(range?.start_time);
  const end = Date.parse(range?.end_time);
  if (![point, start, end].every(Number.isFinite) || end <= start) return null;
  return Math.max(0, Math.min(1, (point - start) / (end - start)));
}

function clockFromTimestamp(value) {
  const match = String(value || "").match(/T(\d{2}):(\d{2})/);
  return match ? match[1] + ":" + match[2] : "--:--";
}

function nearestRow(targetTime) {
  if (!rows.length) return null;
  return rows.reduce((best, row) =>
    Math.abs(Date.parse(row.data_time) - targetTime) <
    Math.abs(Date.parse(best.data_time) - targetTime) ? row : best
  );
}
```

In `renderDashboard(data)`, use `data.summary_today`, assign `range = data.range`, accept any array length, and map events with:

```javascript
rows = Array.isArray(data.trend) ? data.trend : [];
range = data.range || null;
const latestEnd = range?.latest_time;
events = rawEvents.map((item) => ({
  startRatio: timeRatio(item.start) ?? 0,
  endRatio: timeRatio(item.end || latestEnd) ?? 0,
  label: item.type || "瓶颈事件",
  shortLabel: item.type || "事件",
  color: item.status === "持续中" ? "var(--red)" : "var(--viz-series-4)",
}));
svg.setAttribute("data-latest-time", latestEnd || "");
```

- [ ] **Step 5: Draw paths by timestamp and break at null or gaps over 120 seconds**

Replace `buildPath()` with:

```javascript
function buildPath(key, x, y) {
  let path = "";
  let previousTime = null;
  rows.forEach((row) => {
    const value = row[key];
    const currentTime = Date.parse(row.data_time);
    const ratio = timeRatio(row.data_time);
    if (value === null || !Number.isFinite(Number(value)) || ratio === null) {
      previousTime = null;
      return;
    }
    const continues = previousTime !== null && currentTime - previousTime <= 120000;
    path += (continues ? " L " : " M ") + x(ratio) + " " + y(Number(value));
    previousTime = currentTime;
  });
  return path;
}
```

Change `x` to accept a 0–1 ratio:

```javascript
const x = (ratio) => margin.left + ratio * plotWidth;
```

Keep the fixed hour ticks, drawing each tick at `x(tick / 24)` and labeling it with `tick === 24 ? "24:00" : String(tick).padStart(2, "0") + ":00"`.

Draw event rectangles from `event.startRatio` to `event.endRatio`, clamping both to the day range:

```javascript
events.forEach((event) => {
  const startRatio = Math.max(0, Math.min(1, event.startRatio));
  const endRatio = Math.max(startRatio, Math.min(1, event.endRatio));
  const startX = x(startRatio);
  const eventWidth = Math.max(6, x(endRatio) - startX);
  svg.appendChild(make("rect", {
    x: startX,
    y: margin.top,
    width: eventWidth,
    height: plotHeight,
    fill: event.color,
    "fill-opacity": 0.1,
    "aria-label": event.label,
  }));
  svg.appendChild(make("text", {
    x: Math.min(startX + 3, width - margin.right - 34),
    y: margin.top + 12,
    fill: event.color,
  }, event.shortLabel));
});
```

- [ ] **Step 6: Select the nearest actual minute for tooltips**

Replace `showTooltip()` with logic that converts pointer position into a target timestamp and finds an existing row:

```javascript
function showTooltip(event) {
  if (!geometry || !range || !rows.length) return;
  const rect = svg.getBoundingClientRect();
  const localX = event.clientX - rect.left;
  const ratio = Math.max(0, Math.min(1,
    (localX - geometry.margin.left) / geometry.plotWidth
  ));
  const targetTime = Date.parse(range.start_time) +
    ratio * (Date.parse(range.end_time) - Date.parse(range.start_time));
  const row = nearestRow(targetTime);
  if (!row) return;
  const summary = clockFromTimestamp(row.data_time) +
    "　光储" + formatValue(row.pvStorage) +
    "　储用" + formatValue(row.storageLoad) +
    "　光用" + formatValue(row.pvLoad);
  readout.textContent = summary;
  tooltip.textContent = summary;
  tooltip.classList.add("is-visible");
  tooltip.setAttribute("aria-hidden", "false");
  tooltip.style.left = Math.max(0, Math.min(wrap.clientWidth - 220, localX + 12)) + "px";
  tooltip.style.top = "36px";
}
```

- [ ] **Step 7: Run the browser test and inspect the generated curve state**

Run:

```bash
node tests/energy_dashboard_e2e.js
```

Expected: stdout ends with `energy_dashboard_e2e_ok`; no console or page errors; the curve has two `M` segments for the deliberate gap, `storageLoad` has its independent null break, and both events are visible.

---

### Task 7: Empty-State Browser Coverage and Full Regression

**Files:**
- Modify: `tests/energy_dashboard_e2e.js`
- Read-only: `station_energy_backend.py`
- Read-only: `tests/test_station_energy_backend.py`
- Read-only: `station_efficiency_history.py`

**Interfaces:**
- Consumes: completed Python contract and HTML renderer.
- Produces: regression evidence for empty history, refresh errors, pure-module behavior, old real-time calculations, and browser rendering.

- [ ] **Step 1: Add an empty successful-response browser case before the existing HTTP-error case**

Add `let useEmptyPayload = false;` beside `apiShouldFail`. In the route handler, when it is true, clone the fixture and replace only the daily history fields:

```javascript
if (useEmptyPayload) {
  const emptyPayload = structuredClone(dashboardPayload);
  emptyPayload.data.range.latest_time = null;
  emptyPayload.data.summary_today.end_time = null;
  emptyPayload.data.summary_today.pv_storage_efficiency = null;
  emptyPayload.data.summary_today.storage_load_efficiency = null;
  emptyPayload.data.summary_today.pv_load_efficiency = null;
  emptyPayload.data.trend = [];
  emptyPayload.data.events = [];
  await route.fulfill({
    status: 200,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(emptyPayload),
  });
  return;
}
```

Dispatch a refresh and assert:

```javascript
useEmptyPayload = true;
await page.evaluate(() => document
  .getElementById("three-energy-flow")
  .dispatchEvent(new Event("energy-dashboard-refresh")));
await page.locator("#three-energy-flow[data-dashboard-state='ready']").waitFor();
assert.strictEqual(await page.locator("path[data-series='pvStorage']").getAttribute("d"), "");
assert.strictEqual(await page.locator("#summary-pv-storage").innerText(), "无运行数据");
assert.strictEqual(await page.locator("#event-table-body").innerText(), "当前时间范围内无瓶颈事件");
useEmptyPayload = false;
```

- [ ] **Step 2: Run both Python suites**

Run:

```bash
python3 -m unittest tests.test_station_energy_backend tests.test_station_efficiency_history -v
```

Expected: all existing backend tests and all 14 new history tests pass.

- [ ] **Step 3: Run the final browser regression**

Run:

```bash
node tests/energy_dashboard_e2e.js
```

Expected: `energy_dashboard_e2e_ok`.

- [ ] **Step 4: Verify the separation boundary and absence of forbidden production I/O**

Run:

```bash
rg -n 'station_efficiency_history' station_energy_backend.py tests/test_station_energy_backend.py
rg -n 'urllib|requests|open\(|print\(|input\(|argparse|__main__|mock' station_efficiency_history.py
python3 -c 'import station_efficiency_history'
```

Expected: the first two `rg` commands return no matches; importing the new module exits 0 without output.

## NocoBase Handoff Boundary

Completion of this plan provides stable logical persistence envelopes but does not perform real writes. Once the NocoBase API contract is supplied, create a separate adapter plan that maps:

- `build_minute_upsert()` to Upsert on `station_efficiency_points` using `(station_id, data_time)`;
- `build_event_upsert()` to Upsert on `efficiency_bottleneck_events` using `(station_id, event_type, device_id, start_time)`;
- the configured one-minute scheduler to raw-device reads, pure calculations, rule reads, and transactional writes;
- the iframe query endpoint to `build_calendar_day_dashboard()`.

That adapter must not copy formulas or state-machine logic out of `station_efficiency_history.py`.
