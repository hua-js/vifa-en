"""Capture the current selection behaviour as the equivalence baseline.

Usage: .venv/bin/python m3/scripts/capture-selection-baseline.py

Regenerate m3/tests/fixtures/selection_equivalence_baseline.json ONLY when a
deliberate selection-semantics change makes test_m3_selection_equivalence fail;
document the behaviour change in the same commit.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from m3.tests.selection_fixtures import all_cases, run_case  # noqa: E402

OUT = ROOT / "m3/tests/fixtures/selection_equivalence_baseline.json"

baseline = {}
timings = {}
for name, dataset, config in all_cases():
    start = time.perf_counter()
    baseline[name] = run_case(dataset, config)
    timings[name] = round(time.perf_counter() - start, 3)
    print(f"{name}: {timings[name]}s "
          f"champion={baseline[name]['champion']['model_name']}", flush=True)

OUT.write_text(json.dumps(baseline, indent=1, sort_keys=True), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
print(json.dumps(timings, indent=1))
