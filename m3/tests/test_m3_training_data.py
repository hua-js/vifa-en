from datetime import timedelta
import unittest
import warnings

from m3.worker.domain.training_data import (
    OPERATIONAL_HISTORY_DAYS,
    build_training_dataset,
)
from m3.tests.m3_test_support import make_quarter_hour_points


class TrainingDataTests(unittest.TestCase):
    def test_operational_training_expands_with_available_history_up_to_ready_window(self):
        """Keeping the cold-start 15-day cap would prevent readiness forever."""
        points = make_quarter_hour_points(35)

        dataset = build_training_dataset(
            points,
            "station_total_load",
            history_days=OPERATIONAL_HISTORY_DAYS,
        )

        self.assertEqual(OPERATIONAL_HISTORY_DAYS, 28)
        self.assertEqual(len(dataset.frame), 28 * 96)
        self.assertEqual(dataset.start, points[-28 * 96].ds)
        self.assertEqual(dataset.end, points[-1].ds)
        self.assertEqual(dataset.mode, "full")

    def test_operational_training_fills_a_long_gap_from_previous_day_only_in_copy(self):
        """Discarding all history before a 3.5-hour gap would leave only a few points."""
        points = make_quarter_hour_points(15)
        gap_indexes = range(1300, 1314)
        expected = [points[index - 96].y for index in gap_indexes]
        for index in gap_indexes:
            points[index] = points[index].model_copy(
                update={"quality": "invalid", "y": None}
            )

        dataset = build_training_dataset(
            points,
            "station_total_load",
            history_days=15,
        )

        filled = dataset.frame.set_index("ds").loc[
            [points[index].ds for index in gap_indexes], "y"
        ].tolist()
        self.assertEqual(filled, expected)
        self.assertEqual(
            dataset.imputed_keys,
            frozenset(
                ("station_total_load", points[index].ds)
                for index in gap_indexes
            ),
        )
        self.assertTrue(
            all(points[index].quality == "invalid" and points[index].y is None for index in gap_indexes)
        )
        self.assertEqual(dataset.start, points[0].ds)
        self.assertEqual(dataset.mode, "warming_up")

    def test_operational_training_requires_seven_days_of_real_observations(self):
        """Seasonal filling must not turn fewer than seven real days into usable history."""
        points = make_quarter_hour_points(15)
        for index in range(9 * 96):
            points[index] = points[index].model_copy(
                update={"quality": "invalid", "y": None}
            )

        dataset = build_training_dataset(
            points,
            "station_total_load",
            history_days=15,
        )

        self.assertEqual(len(dataset.frame), 6 * 96)
        self.assertEqual(dataset.start, points[9 * 96].ds)
        self.assertEqual(dataset.mode, "insufficient")

    def test_operational_training_falls_back_to_previous_week_when_yesterday_is_missing(self):
        """Using only yesterday would still discard history when both periods have gaps."""
        points = make_quarter_hour_points(15)
        gap_indexes = range(1300, 1314)
        previous_day_indexes = range(1300 - 96, 1314 - 96)
        expected = [points[index - 672].y for index in gap_indexes]
        for index in (*previous_day_indexes, *gap_indexes):
            points[index] = points[index].model_copy(
                update={"quality": "invalid", "y": None}
            )

        dataset = build_training_dataset(
            points,
            "station_total_load",
            history_days=15,
        )

        filled = dataset.frame.set_index("ds").loc[
            [points[index].ds for index in gap_indexes], "y"
        ].tolist()
        self.assertEqual(filled, expected)
        self.assertEqual(dataset.start, points[0].ds)
        self.assertEqual(dataset.mode, "warming_up")

    def test_all_invalid_operational_history_is_rejected_without_pandas_warning(self):
        """An all-null series must fail cleanly instead of relying on object interpolation."""
        points = make_quarter_hour_points(15)
        points = [
            point.model_copy(update={"quality": "invalid", "y": None})
            for point in points
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            with self.assertRaisesRegex(ValueError, "no continuous observations"):
                build_training_dataset(
                    points,
                    "station_total_load",
                    history_days=15,
                )

    def test_one_bucket_gap_is_imputed_only_in_training_copy(self):
        """Treating one invalid bucket as a long gap must fail this test."""
        points = make_quarter_hour_points(28)
        original_value = points[100].y
        points[100] = points[100].model_copy(update={"quality": "invalid", "y": None})

        dataset = build_training_dataset(points, "station_total_load")

        self.assertEqual(dataset.imputed_keys, frozenset({("station_total_load", points[100].ds)}))
        self.assertEqual(points[100].quality, "invalid")
        self.assertIsNone(points[100].y)
        self.assertIsNotNone(original_value)
        self.assertEqual(dataset.mode, "full")

    def test_two_bucket_gap_is_imputed_only_in_training_copy(self):
        """Limiting interpolation below two consecutive buckets must fail this test."""
        points = make_quarter_hour_points(28)
        original = [point.model_copy(deep=True) for point in points]
        points[100] = points[100].model_copy(update={"quality": "invalid", "y": None})
        points[101] = points[101].model_copy(update={"quality": "invalid", "y": None})

        dataset = build_training_dataset(points, "station_total_load")

        self.assertEqual(len(dataset.imputed_keys), 2)
        self.assertEqual(points[100].quality, "invalid")
        self.assertIsNone(points[100].y)
        self.assertIsNotNone(original[100].y)
        self.assertEqual(dataset.mode, "full")

    def test_three_bucket_gap_selects_continuous_tail(self):
        """Keeping data before a three-bucket gap must fail this test."""
        points = make_quarter_hour_points(35)
        for index in (600, 601, 602):
            points[index] = points[index].model_copy(update={"quality": "invalid", "y": None})

        dataset = build_training_dataset(points, "station_total_load")

        self.assertEqual(dataset.start, points[603].ds)
        self.assertEqual(dataset.end, points[-1].ds)
        self.assertEqual(dataset.imputed_keys, frozenset())

    def test_history_modes_change_at_seven_and_twenty_eight_days(self):
        """Changing either cold-start threshold must fail this boundary table."""
        cases = ((6, "insufficient"), (7, "warming_up"), (27, "warming_up"), (28, "full"))

        for days, expected_mode in cases:
            with self.subTest(days=days):
                dataset = build_training_dataset(
                    make_quarter_hour_points(days), "station_total_load"
                )
                self.assertEqual(dataset.mode, expected_mode)

    def test_empty_selected_series_is_rejected(self):
        """Returning an empty dataset for a missing configured series must fail this test."""
        with self.assertRaisesRegex(ValueError, "no observations for storage_soc"):
            build_training_dataset([], "storage_soc")

    def test_duplicate_selected_timestamps_are_rejected(self):
        """Silently accepting duplicate observation times must fail this test."""
        points = make_quarter_hour_points(7)
        points.append(points[10].model_copy())

        with self.assertRaisesRegex(ValueError, "duplicate observation times"):
            build_training_dataset(points, "station_total_load")
