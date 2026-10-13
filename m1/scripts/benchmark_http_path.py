"""Local M1 HTTP-path A/B fixture; never a production latency benchmark."""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import statistics
import subprocess
import time
import types
from unittest.mock import patch

from m1 import dashboard_energy_api as api
from m1.tests.test_dashboard_http_optimization import ResidentTransportIntegrationTests

BASELINE_COMMIT = "9bcacf5"


class FixedClock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 12, 12, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def run(samples: int) -> dict:
    root = Path(__file__).resolve().parents[2]
    # Trusted, fixed repository baseline executed only against synthetic local inputs.
    baseline = types.ModuleType("m1_http_baseline")
    baseline.__file__ = api.__file__
    source = subprocess.check_output(
        ["git", "show", BASELINE_COMMIT + ":m1/dashboard_energy_api.py"], cwd=root, text=True,
    )
    exec(compile(source, api.__file__, "exec"), baseline.__dict__)
    setattr(baseline, "datetime", FixedClock)
    case = ResidentTransportIntegrationTests(
        "test_resident_collector_reuses_connections_without_reusing_business_responses",
    )
    case.setUp()
    rows = {"baseline": [], "candidate": []}
    try:
        case.source.full_business = True
        case.source.connection_delay = 0.04
        case.source.response_delay = 0.03
        for name in ("BASE", "STATION_LOAD_URL", "CABINET_POWER_URL", "GROWATT_URL"):
            setattr(baseline, name, getattr(api, name))
        expected = json.loads((root / "m1/tests/fixtures/dashboard_http_baseline.json").read_text())["payload"]

        def old_collector(token, energy_token, **kwargs):
            kwargs.pop("use_connection_pool", None)
            return baseline.collect_dashboard(token, energy_token, **kwargs)

        def measure(label, collector, save):
            case.server.collector = collector
            case.source.accepted = 0
            case.source.calls.clear()
            started = time.perf_counter()
            value = case.read()
            elapsed = time.perf_counter() - started
            if value != expected:
                raise AssertionError("business JSON differs from the baseline fixture")
            row = {"seconds": elapsed, "new_connections": case.source.accepted,
                   "source_requests": len(case.source.calls), "business_json_equal": True}
            if row["source_requests"] != 8:
                raise AssertionError("source request count changed")
            if save:
                rows[label].append(row)

        with patch.object(api, "datetime", FixedClock):
            measure("baseline", old_collector, False)
            measure("candidate", api.collect_dashboard, False)
            for index in range(samples):
                # Alternate ordering so a consistently first/second run does not win.
                sequence = (("baseline", old_collector), ("candidate", api.collect_dashboard))
                if index % 2:
                    sequence = tuple(reversed(sequence))
                for label, collector in sequence:
                    measure(label, collector, True)
        summary = {}
        for label, values in rows.items():
            summary[label] = {
                "median_seconds": statistics.median(row["seconds"] for row in values),
                "median_new_connections": statistics.median(row["new_connections"] for row in values),
            }
        before = summary["baseline"]["median_seconds"]
        after = summary["candidate"]["median_seconds"]
        return {
            "scope": "local real-HTTP fixture with artificial delays; no real TLS, upstream or production timing",
            "baseline_commit": BASELINE_COMMIT,
            "new_connection_fixture_delay_ms": 40,
            "response_fixture_delay_ms": 30,
            "samples_per_variant": samples,
            "warmups_per_variant": 1,
            "summary": summary,
            "fixture_median_saved_seconds": before - after,
            "fixture_median_saved_percent": (before - after) / before * 100,
            "rows": rows,
        }
    finally:
        case.doCleanups()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.samples <= 30:
        parser.error("samples must be between 1 and 30")
    print(json.dumps(run(args.samples), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
