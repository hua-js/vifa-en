import math
import unittest

from m3.worker.domain.evaluation import evaluate_series, overall_outcome


class M3EvaluationTests(unittest.TestCase):
    def test_zero_actual_is_excluded_and_counted(self):
        """Dropping zero actuals from scoring but counting them must preserve MAPE."""
        result = evaluate_series(
            [0.0, 100.0], [50.0, 90.0], ["valid", "valid"], minimum_valid=1
        )

        self.assertEqual(result.zero_actual_count, 1)
        self.assertEqual(result.valid_count, 1)
        self.assertAlmostEqual(result.mape_percent, 10.0)

    def test_metrics_use_standard_mape_mae_and_smape(self):
        """Metric formulas must use absolute errors and the standard SMAPE denominator."""
        result = evaluate_series(
            [100.0, 50.0], [90.0, 40.0], ["valid", "valid"], minimum_valid=1
        )

        self.assertAlmostEqual(result.mape_percent, 15.0)
        self.assertAlmostEqual(result.mae, 10.0)
        self.assertAlmostEqual(result.smape_percent, 200 * (10 / 190 + 10 / 90) / 2)

    def test_backtest_distribution_metrics_use_scored_nonzero_actuals(self):
        """WAPE, median APE, and P90 APE must expose scale and tail error without zero dilution."""
        result = evaluate_series(
            [0.0, 100.0, 50.0, 25.0],
            [999.0, 90.0, 40.0, 20.0],
            ["valid", "valid", "valid", "valid"],
            minimum_valid=1,
        )

        self.assertAlmostEqual(result.wape_percent, 100 * 25 / 175)
        self.assertAlmostEqual(result.median_ape_percent, 20.0)
        self.assertAlmostEqual(result.p90_ape_percent, 20.0)

    def test_invalid_and_nonfinite_pairs_are_excluded(self):
        """Invalid quality and nonfinite values must not contribute to any metric."""
        result = evaluate_series(
            [100.0, 100.0, None, math.nan, math.inf],
            [90.0, 80.0, 90.0, 90.0, 90.0],
            ["valid", "invalid", "valid", "valid", "valid"],
            minimum_valid=1,
        )

        self.assertEqual(result.expected_count, 5)
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.zero_actual_count, 0)
        self.assertAlmostEqual(result.mape_percent, 10.0)

    def test_604_is_insufficient_and_605_can_pass(self):
        """The acceptance minimum is inclusive at 605 scored nonzero actuals."""
        insufficient = evaluate_series([100.0] * 604, [90.0] * 604, ["valid"] * 604)
        passing = evaluate_series([100.0] * 605, [90.0] * 605, ["valid"] * 605)

        self.assertEqual(insufficient.outcome, "insufficient_data")
        self.assertEqual(passing.outcome, "passed")

    def test_mape_boundary_is_passing_only_at_or_below_thirty(self):
        """MAPE exactly 30 passes while a larger MAPE fails."""
        boundary = evaluate_series([100.0], [70.0], ["valid"], minimum_valid=1)
        failing = evaluate_series([100.0], [69.0], ["valid"], minimum_valid=1)

        self.assertEqual(boundary.outcome, "passed")
        self.assertEqual(failing.outcome, "failed")

    def test_one_failed_series_fails_overall(self):
        """Any failed series must fail a complete three-series acceptance result."""
        passed = evaluate_series([100.0] * 605, [90.0] * 605, ["valid"] * 605)
        failed = evaluate_series([100.0] * 605, [60.0] * 605, ["valid"] * 605)

        self.assertEqual(overall_outcome([passed, passed, failed]), "failed")

    def test_insufficient_series_dominates_overall_failure(self):
        """An insufficient series must take precedence over another series' failure."""
        insufficient = evaluate_series([100.0], [60.0], ["valid"], minimum_valid=2)
        failed = evaluate_series([100.0], [60.0], ["valid"], minimum_valid=1)

        self.assertEqual(overall_outcome([failed, insufficient]), "insufficient_data")

    def test_mismatched_metric_inputs_are_rejected(self):
        """Aligned sequences are required so no observation can be scored against the wrong forecast."""
        with self.assertRaisesRegex(ValueError, "metric inputs must have equal length"):
            evaluate_series([1.0], [1.0, 2.0], ["valid"])


if __name__ == "__main__":
    unittest.main()
