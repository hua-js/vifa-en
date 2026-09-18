#!/usr/bin/env python3
"""Read-only diagnosis using the deployed M2 configuration; never runs minute jobs."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from m2.station_efficiency_bottlenecks import evaluate_station_bottlenecks
from m2.station_efficiency_history import build_event_upsert, normalize_rule
from m2.station_efficiency_nocobase import (
    fetch_active_events, fetch_device_points, fetch_minute_points,
)


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def read_pending(path, station):
    """Do not use the outbox helper: it creates schema and changes journal mode."""
    database = Path(path).resolve()
    if not database.is_file():
        return {"status": "missing", "pending": None}
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        count = connection.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE station_id=?", (station,),
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT payload_json FROM event_outbox WHERE station_id=? LIMIT 20", (station,),
        ).fetchall()
        invalid = 0
        for (payload,) in rows:
            try:
                build_event_upsert(json.loads(payload))
            except (ValueError, TypeError, KeyError):
                invalid += 1
        return {"status": "ok", "pending": count, "checked": len(rows), "invalid_payloads": invalid}
    finally:
        connection.close()


def inspect_station(config, station):
    def read(url, token, timeout):
        started = time.monotonic()
        resource = url.split("/api/")[-1].split("?")[0]
        # URLs are produced solely by the three fixed GET readers below.
        details = {"station_id": station, "stage": resource}
        try:
            request = Request(url, headers={"Authorization": "Bearer " + token}, method="GET")
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
                data = payload.get("data") if isinstance(payload, dict) else None
                meta = payload.get("meta") if isinstance(payload, dict) else None
                emit(**details, status="received", http_status=response.status,
                     elapsed_ms=round((time.monotonic() - started) * 1000),
                     data_type=type(data).__name__,
                     rows=len(data) if isinstance(data, list) else None,
                     pagination_valid=isinstance(meta, dict) and type(meta.get("totalPage")) is int)
                return payload
        except Exception as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            emit(**details, status="failed", http_status=exc.code if isinstance(exc, HTTPError) else None,
                 error_type="timeout" if isinstance(reason, TimeoutError) else type(exc).__name__,
                 elapsed_ms=round((time.monotonic() - started) * 1000))
            raise

    rule = config.get("bottleneck_rules", {}).get(station)
    try:
        rule = normalize_rule(rule)
        emit(station_id=station, stage="rule", status="ok", enabled=rule["enabled"])
    except Exception as exc:
        rule = None
        emit(station_id=station, stage="rule", status="failed", error_type=type(exc).__name__)
    try:
        emit(station_id=station, stage="outbox", **read_pending(config["event_outbox_path"], station))
    except Exception as exc:
        emit(station_id=station, stage="outbox", status="failed", error_type=type(exc).__name__)

    window_fields = (
        "inverter_trigger_minutes", "inverter_recovery_minutes", "temperature_rise_window_minutes",
        "temperature_trigger_minutes", "temperature_recovery_minutes",
        "chain_low_efficiency_trigger_minutes", "chain_low_efficiency_recovery_minutes",
    )
    minutes = max(rule[field] for field in window_fields) + 2 if rule else 7
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    start = end - timedelta(minutes=minutes)
    results = {}
    for name, function, args in (
        ("minute_points", fetch_minute_points, (station, start.isoformat(), end.isoformat(), config)),
        ("device_points", fetch_device_points, (station, start.isoformat(), end.isoformat(), config)),
        ("active_events", fetch_active_events, (station, config)),
    ):
        try:
            results[name] = function(*args, request_json=read)
            emit(station_id=station, stage=name, status="ok", rows=len(results[name]))
        except Exception as exc:
            emit(station_id=station, stage=name, status="failed", error_type=type(exc).__name__)
    if rule is not None and len(results) == 3:
        try:
            events = evaluate_station_bottlenecks(station_id=station, rule=rule, **results)
            emit(station_id=station, stage="evaluation_only", status="ok", proposed_updates=len(events), written=0)
        except Exception as exc:
            emit(station_id=station, stage="evaluation_only", status="failed", error_type=type(exc).__name__)


def main():
    entry = ROOT / "energy-efficiency-api.py"
    if not entry.is_file():
        entry = ROOT / "m2" / "energy-efficiency-api.py"
    try:
        spec = importlib.util.spec_from_file_location("m2_health_entry", entry)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        config = api.load_runtime_config(os.environ, require_nocobase=True, require_source=False)
    except Exception as exc:
        emit(stage="load_config", status="failed", error_type=type(exc).__name__)
        return 1
    emit(stage="mode", read_only=True, writes_tested=False)
    for station in ("ES01", "ES02"):
        inspect_station(config, station)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
