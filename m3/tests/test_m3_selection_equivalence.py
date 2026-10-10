"""Selection equivalence guard: the performance refactor (schedule memoization
and loop-invariant hoisting) must reproduce the pre-refactor outputs exactly.

The baseline JSON was captured from the unmodified implementation
(2026-10-10).  If a *deliberate* semantic change ever makes this test fail,
regenerate m3/tests/fixtures/selection_equivalence_baseline.json with
m3/scripts/capture-selection-baseline.py and document the behaviour change in
the commit message.
"""

import json
import unittest
from pathlib import Path

from m3.tests.selection_fixtures import all_cases, run_case

BASELINE_PATH = (
    Path(__file__).parent / "fixtures" / "selection_equivalence_baseline.json"
)


class SelectionEquivalenceTests(unittest.TestCase):
    def test_selection_and_forecast_match_baseline(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        for name, dataset, config in all_cases():
            with self.subTest(case=name):
                self.assertEqual(run_case(dataset, config), baseline[name])


if __name__ == "__main__":
    unittest.main()
