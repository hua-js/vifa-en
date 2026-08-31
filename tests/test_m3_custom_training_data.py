"""Behavior tests for aligned usable-week evidence in custom training data."""

from datetime import datetime, timedelta
import unittest

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomObservationPoint,
)
from m3_worker.domain.custom_training_data import (
    CustomWeekSummary,
    build_custom_training_dataset,
)


HISTORY_END = datetime.fromisoformat("2026-08-31T00:00:00+08:00")
UNIQUE_ID = "station_total_load"


def make_config(history_start: datetime) -> CustomForecastConfig:
    history_days = (HISTORY_END - history_start).days
    return CustomForecastConfig(
        history_start=history_start,
        history_end=HISTORY_END,
        history_days=history_days,
        forecast_start=HISTORY_END,
        forecast_end=HISTORY_END + timedelta(days=1),
        forecast_days=1,
        interval_seconds=900,
        points_per_day=96,
        expected_points_per_series=96,
        model_policy=("seasonal_naive_only" if history_days < 28 else "full_selection"),
    )


def make_points(
    history_start: datetime,
    history_end: datetime,
    *,
    imputed_times: set[datetime] | None = None,
) -> list[CustomObservationPoint]:
    imputed_times = imputed_times or set()
    points = []
    current = history_start
    while current < history_end:
        if current in imputed_times:
            points.append(
                CustomObservationPoint(
                    unique_id=UNIQUE_ID,
                    ds=current,
                    y=None,
                    quality="invalid",
                    source_revision=1,
                )
            )
        else:
            points.append(
                CustomObservationPoint(
                    unique_id=UNIQUE_ID,
                    ds=current,
                    y=800.0,
                    quality="valid",
                    source_revision=1,
                )
            )
        current += timedelta(minutes=15)
    return points


def build_dataset_with_week_imputation(imputed_points: int):
    history_start = HISTORY_END - timedelta(days=14)
    imputed_times = {
        HISTORY_END - timedelta(days=7) + timedelta(minutes=15 * index)
        for index in range(imputed_points)
    }
    return build_custom_training_dataset(
        make_points(
            history_start + timedelta(minutes=15),
            HISTORY_END,
            imputed_times=imputed_times,
        ),
        UNIQUE_ID,
        make_config(history_start),
    )


def build_dataset_with_two_weeks(latest_imputed_points: int):
    history_start = HISTORY_END - timedelta(days=14)
    imputed_times = {
        HISTORY_END - timedelta(days=7) + timedelta(minutes=15 * index)
        for index in range(latest_imputed_points)
    }
    return build_custom_training_dataset(
        make_points(history_start, HISTORY_END, imputed_times=imputed_times),
        UNIQUE_ID,
        make_config(history_start),
    )


def build_dataset_starting_at(start: str):
    history_start = datetime.fromisoformat("2026-08-06T00:00:00+08:00")
    actual_start = datetime.fromisoformat(start)
    return build_custom_training_dataset(
        make_points(actual_start, HISTORY_END), UNIQUE_ID, make_config(history_start)
    )


class CustomTrainingDataUsableWeekTests(unittest.TestCase):
    def test_latest_aligned_week_accepts_largest_integer_below_five_percent(self):
        dataset = build_dataset_with_week_imputation(imputed_points=33)
        self.assertEqual(len(dataset.usable_weeks), 1)
        week = dataset.usable_weeks[0]
        self.assertIsInstance(week, CustomWeekSummary)
        self.assertEqual(week.point_count, 672)
        self.assertEqual(week.imputed_point_count, 33)
        self.assertAlmostEqual(week.imputation_ratio, 33 / 672)

    def test_week_above_five_percent_stops_older_week_discovery(self):
        dataset = build_dataset_with_two_weeks(latest_imputed_points=34)
        self.assertEqual(dataset.usable_weeks, ())

    def test_partial_old_prefix_does_not_remove_three_complete_recent_weeks(self):
        dataset = build_dataset_starting_at("2026-08-06T08:00:00+08:00")
        self.assertEqual(len(dataset.usable_weeks), 3)
        self.assertEqual(
            dataset.usable_weeks[-1].end.isoformat(),
            "2026-08-31T00:00:00+08:00",
        )


if __name__ == "__main__":
    unittest.main()
