"""Tests for load-only weekly profile candidates and scoring."""

from datetime import datetime, timedelta
import unittest

import pandas as pd

from m3_worker.domain.custom_load_profiles import (
    LOAD_SELECTION_POLICY,
    MODEL_ORDER,
    LoadCandidateScore,
    eligible_load_models,
    load_candidate_comparison_key,
    load_candidate_score,
    weekly_profile_values,
)
from m3_worker.domain.custom_training_data import CustomTrainingDataset


START = datetime.fromisoformat("2026-08-31T00:00:00+08:00")
UNIQUE_ID = "station_total_load"


def make_dataset(
    values_by_time: dict[datetime, float],
    *,
    imputed_keys: frozenset[tuple[str, datetime]] = frozenset(),
    interval_seconds: int = 86_400,
) -> CustomTrainingDataset:
    frame = pd.DataFrame(
        [
            {"unique_id": UNIQUE_ID, "ds": timestamp, "y": value}
            for timestamp, value in sorted(values_by_time.items())
        ]
    )
    return CustomTrainingDataset(
        frame=frame,
        imputed_keys=imputed_keys,
        source_available_start=min(values_by_time),
        source_available_points=len(frame),
        leading_no_data_points=0,
        invalid_points=0,
        negative_invalid_points=0,
        start=min(values_by_time),
        end=max(values_by_time),
        mode="full",
        interval_seconds=interval_seconds,
        points_per_day=86_400 // interval_seconds,
    )


def make_regime_shift_dataset() -> CustomTrainingDataset:
    values: dict[datetime, float] = {}
    current = START - timedelta(days=14)
    while current < START:
        active = 8 <= current.hour < 18
        recent = current >= START - timedelta(days=3)
        values[current] = 500.0 if active else 9.0 if recent else 15.0
        current += timedelta(hours=1)
    return make_dataset(values, interval_seconds=3600)


class WeeklyProfileTests(unittest.TestCase):
    def test_weekly_naive_uses_exactly_seven_day_lag(self):
        values = {
            START - timedelta(days=7) + timedelta(days=index): 70.0 + index
            for index in range(3)
        }
        dataset = make_dataset(values)

        result = weekly_profile_values(dataset, "WeeklyNaive", origin=START, periods=3)

        self.assertEqual(result, [70.0, 71.0, 72.0])

    def test_weekly_weighted_two_uses_two_thirds_recent_week(self):
        values = {
            START - timedelta(days=7): 90.0,
            START - timedelta(days=14): 30.0,
        }
        dataset = make_dataset(values)

        result = weekly_profile_values(dataset, "WeeklyWeighted2", origin=START, periods=1)

        self.assertAlmostEqual(result[0], 2 / 3 * 90.0 + 1 / 3 * 30.0)

    def test_weekly_median_three_rejects_one_abnormal_week(self):
        values = {
            START - timedelta(days=7): 50.0,
            START - timedelta(days=14): 500.0,
            START - timedelta(days=21): 50.0,
        }
        dataset = make_dataset(values)

        result = weekly_profile_values(dataset, "WeeklyMedian3", origin=START, periods=1)

        self.assertEqual(result, [50.0])

    def test_regime_adjusted_uses_recent_standby_level(self):
        dataset = make_regime_shift_dataset()

        result = weekly_profile_values(
            dataset, "WeeklyRegimeAdjusted", origin=START, periods=24
        )

        self.assertAlmostEqual(result[0], 9.0)

    def test_regime_adjusted_preserves_active_load(self):
        dataset = make_regime_shift_dataset()

        result = weekly_profile_values(
            dataset, "WeeklyRegimeAdjusted", origin=START, periods=24
        )

        self.assertAlmostEqual(result[8], 500.0)

    def test_profile_does_not_read_source_at_or_after_origin(self):
        values = {
            START - timedelta(days=7) + timedelta(days=index): 10.0
            for index in range(8)
        }
        dataset = make_dataset(values)

        with self.assertRaises(ValueError):
            weekly_profile_values(dataset, "WeeklyNaive", origin=START, periods=8)

    def test_profile_does_not_access_future_row_value_for_valid_forecast(self):
        class UnexpectedFutureValue:
            def __float__(self):
                raise AssertionError("future row value was accessed")

        dataset = make_dataset(
            {
                START - timedelta(days=7): 42.0,
                START: UnexpectedFutureValue(),
            }
        )

        result = weekly_profile_values(dataset, "WeeklyNaive", origin=START, periods=1)

        self.assertEqual(result, [42.0])


class EligibilityTests(unittest.TestCase):
    def test_regime_candidate_uses_a_new_selection_policy_version(self):
        self.assertEqual(LOAD_SELECTION_POLICY, "weekly_load_v2")

    def test_high_frequency_models_are_lightweight_only(self):
        self.assertEqual(
            eligible_load_models(4, 30),
            (
                "WeeklyNaive",
                "WeeklyWeighted2",
                "WeeklyRegimeAdjusted",
                "WeeklyMedian3",
            ),
        )

    def test_eligibility_tiers_at_sixty_seconds(self):
        expected = {
            0: (),
            1: ("WeeklyNaive",),
            2: ("WeeklyNaive",),
            3: ("WeeklyNaive", "WeeklyWeighted2", "WeeklyRegimeAdjusted"),
            4: (
                "WeeklyNaive",
                "WeeklyWeighted2",
                "WeeklyRegimeAdjusted",
                "WeeklyMedian3",
            ),
        }
        for usable_weeks, names in expected.items():
            with self.subTest(usable_weeks=usable_weeks):
                self.assertEqual(eligible_load_models(usable_weeks, 60), names)

    def test_eligibility_tiers_at_five_minutes(self):
        expected = {
            0: (),
            1: ("WeeklyNaive",),
            2: ("WeeklyNaive",),
            3: ("WeeklyNaive", "WeeklyWeighted2", "WeeklyRegimeAdjusted"),
            4: (
                "WeeklyNaive",
                "WeeklyWeighted2",
                "WeeklyRegimeAdjusted",
                "WeeklyMedian3",
                "AutoARIMA",
                "MSTL",
            ),
        }
        for usable_weeks, names in expected.items():
            with self.subTest(usable_weeks=usable_weeks):
                self.assertEqual(eligible_load_models(usable_weeks, 300), names)


class LoadMetricTests(unittest.TestCase):
    def test_metrics_use_literal_common_finite_points(self):
        times = [START + timedelta(days=index) for index in range(5)]
        actual = pd.DataFrame(
            {
                "ds": times,
                "y": [100.0, 50.0, 0.0, float("nan"), float("nan")],
            }
        )
        predicted = [90.0, 60.0, 10.0, 20.0, float("inf")]

        score = load_candidate_score("WeeklyNaive", actual, predicted, frozenset())

        self.assertEqual(score.scorable_point_count, 3)
        self.assertAlmostEqual(score.wape_percent, 30 * 100 / 150)
        self.assertAlmostEqual(score.mae, 30 / 3)
        self.assertAlmostEqual(score.mape_percent, ((10 + 20) / 100) * 100 / 2)
        self.assertIsNone(score.skip_reason)

    def test_imputed_target_is_excluded_from_every_metric(self):
        times = [START + timedelta(days=index) for index in range(3)]
        actual = pd.DataFrame({"ds": times, "y": [100.0, 1000.0, 0.0]})
        predicted = [90.0, 0.0, 20.0]
        excluded = frozenset({times[1]})

        score = load_candidate_score("WeeklyNaive", actual, predicted, excluded)

        self.assertEqual(score.scorable_point_count, 2)
        self.assertAlmostEqual(score.wape_percent, 30.0)
        self.assertAlmostEqual(score.mae, 15.0)
        self.assertAlmostEqual(score.mape_percent, 10.0)

    def test_wape_unavailable_uses_mae_tie_break(self):
        actual = pd.DataFrame({"ds": [START, START + timedelta(days=1)], "y": [0.0, 0.0]})
        first = load_candidate_score("WeeklyNaive", actual, [1.0, 1.0], frozenset())
        second = load_candidate_score("WeeklyWeighted2", actual, [2.0, 2.0], frozenset())

        self.assertIsNone(first.wape_percent)
        self.assertIsNone(first.skip_reason)
        self.assertLess(load_candidate_comparison_key(first), load_candidate_comparison_key(second))

    def test_partial_prediction_is_rejected_before_ranking_against_complete_candidate(self):
        """Dropping one non-finite prediction would let a one-point fit win unfairly."""
        actual = pd.DataFrame(
            {
                "ds": [START, START + timedelta(days=1)],
                "y": [100.0, 100.0],
            }
        )
        partial = load_candidate_score(
            "WeeklyNaive", actual, [100.0, float("nan")], frozenset()
        )
        complete = load_candidate_score(
            "WeeklyWeighted2", actual, [120.0, 120.0], frozenset()
        )

        self.assertEqual(partial.scorable_point_count, 2)
        self.assertIsNone(partial.wape_percent)
        self.assertIsNone(partial.mae)
        self.assertIsNone(partial.mape_percent)
        self.assertEqual(partial.skip_reason, "non_finite_prediction")
        self.assertEqual(complete.scorable_point_count, 2)
        self.assertEqual(complete.wape_percent, 20.0)
        self.assertEqual(
            min(
                (score for score in (partial, complete) if score.mae is not None),
                key=load_candidate_comparison_key,
            ).model_name,
            "WeeklyWeighted2",
        )

    def test_fixed_model_order_breaks_exact_metric_ties(self):
        scores = [
            LoadCandidateScore(name, 1.0, 1.0, 1.0, 1, None)
            for name in reversed(MODEL_ORDER)
        ]

        ordered = [score.model_name for score in sorted(scores, key=load_candidate_comparison_key)]

        self.assertEqual(ordered, list(MODEL_ORDER))

    def test_no_finite_common_points_is_skipped(self):
        actual = pd.DataFrame({"ds": [START], "y": [float("nan")]})

        score = load_candidate_score("WeeklyNaive", actual, [1.0], frozenset())

        self.assertEqual(score.scorable_point_count, 0)
        self.assertIsNone(score.wape_percent)
        self.assertIsNone(score.mae)
        self.assertIsNone(score.mape_percent)
        self.assertEqual(score.skip_reason, "no_scorable_points")


if __name__ == "__main__":
    unittest.main()
