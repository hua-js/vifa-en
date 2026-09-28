"""Behavior tests for aligned usable-week evidence in custom training data."""

from datetime import datetime, timedelta
import unittest

from m3.worker.domain.production_schedule import ProductionSchedule, use_schedule

from m3.worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomObservationPoint,
)
from m3.worker.domain.custom_training_data import (
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
                    source_state="coverage",
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
                    source_state="valid",
                    source_revision=1,
                )
            )
        current += timedelta(minutes=15)
    return points


def build_dataset_with_week_imputation(imputed_points: int):
    history_start = HISTORY_END - timedelta(days=14)
    imputed_times = {
        HISTORY_END - timedelta(days=6) + timedelta(minutes=15 * index)
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
        HISTORY_END - timedelta(days=6) + timedelta(minutes=15 * index)
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

    def test_leading_no_row_buckets_do_not_count_as_source_available_history(self):
        history_start = HISTORY_END - timedelta(days=28)
        source_start = history_start + timedelta(days=2, hours=8)
        points = []
        current = history_start
        while current < HISTORY_END:
            leading = current < source_start
            points.append(
                CustomObservationPoint(
                    unique_id=UNIQUE_ID,
                    ds=current,
                    y=None if leading else 800.0,
                    quality="invalid" if leading else "valid",
                    source_state="no_rows" if leading else "valid",
                    source_revision=1,
                )
            )
            current += timedelta(minutes=15)

        dataset = build_custom_training_dataset(
            points, UNIQUE_ID, make_config(history_start)
        )

        self.assertEqual(dataset.source_available_start, source_start)
        self.assertEqual(dataset.leading_no_data_points, 224)
        self.assertEqual(dataset.source_available_points, 2464)
        self.assertEqual(dataset.invalid_points, 0)
        self.assertEqual(dataset.negative_invalid_points, 0)

    def test_negative_load_after_source_start_remains_invalid_evidence(self):
        history_start = HISTORY_END - timedelta(days=8)
        points = make_points(history_start, HISTORY_END)
        negative_time = history_start + timedelta(days=2)
        negative_index = next(
            index for index, point in enumerate(points) if point.ds == negative_time
        )
        points[negative_index] = CustomObservationPoint(
            unique_id=UNIQUE_ID,
            ds=negative_time,
            y=None,
            quality="invalid",
            source_state="negative",
            source_revision=1,
        )

        dataset = build_custom_training_dataset(
            points, UNIQUE_ID, make_config(history_start)
        )

        self.assertEqual(dataset.leading_no_data_points, 0)
        self.assertEqual(dataset.invalid_points, 1)
        self.assertEqual(dataset.negative_invalid_points, 1)
        self.assertIn((UNIQUE_ID, negative_time), dataset.imputed_keys)

    def test_leading_no_row_exclusion_does_not_change_soc_policy(self):
        history_start = HISTORY_END - timedelta(days=8)
        source_start = history_start + timedelta(days=1)
        points = []
        current = history_start
        while current < HISTORY_END:
            leading = current < source_start
            points.append(
                CustomObservationPoint(
                    unique_id="storage_soc",
                    ds=current,
                    y=None if leading else 55.0,
                    quality="invalid" if leading else "valid",
                    source_state="no_rows" if leading else "valid",
                    source_revision=1,
                )
            )
            current += timedelta(minutes=15)

        dataset = build_custom_training_dataset(
            points, "storage_soc", make_config(history_start)
        )

        self.assertEqual(dataset.source_available_start, history_start)
        self.assertEqual(dataset.leading_no_data_points, 0)



class LoadRestGapRecoveryTests(unittest.TestCase):
    def build(self, *, gap_count=17, donor_days=5, donor_valid=True):
        end = datetime.fromisoformat("2026-09-28T00:00:00+08:00")
        start = end - timedelta(days=41)
        target = end - timedelta(days=3) + timedelta(hours=6, minutes=30)
        config = make_config(HISTORY_END - timedelta(days=41)).model_copy(update={
            "history_start": start, "history_end": end,
            "forecast_start": end, "forecast_end": end + timedelta(days=1),
        })
        donor_day = (target - timedelta(days=donor_days)).date()
        statuses = {(start + timedelta(days=i)).date(): "WORK" for i in range(42)}
        statuses[target.date()] = "REST"
        statuses[donor_day] = "REST"
        calendar = ProductionSchedule("ES01", "station_1", "86", start.date(),
            end.date(), end.date(), statuses, {}, "{}")
        gaps = {target + timedelta(minutes=15*i) for i in range(gap_count)}
        points = make_points(start, end)
        for i, point in enumerate(points):
            if point.ds in gaps:
                points[i] = point.model_copy(update={"y": None, "quality": "invalid", "source_state": "negative"})
            elif point.ds.date() == donor_day:
                points[i] = point.model_copy(update={
                    "y": 120.0 if donor_valid else None,
                    "quality": "valid" if donor_valid else "invalid",
                    "source_state": "valid" if donor_valid else "coverage",
                })
        with use_schedule(calendar):
            dataset = build_custom_training_dataset(points, UNIQUE_ID, config)
        return dataset, target, gaps

    def test_rest_day_negative_gap_recovers_from_earlier_real_rest_day(self):
        dataset, target, gaps = self.build()
        self.assertEqual(len(dataset.usable_weeks), 5)
        self.assertEqual(dataset.negative_invalid_points, 17)
        self.assertTrue(all((UNIQUE_ID, t) in dataset.imputed_keys for t in gaps))
        at_eight = target.replace(hour=8, minute=0)
        self.assertEqual(dataset.frame.set_index("ds").loc[at_eight, "y"], 120.0)
        self.assertIn((at_eight, at_eight - timedelta(days=5)), dataset.load_fallback_donors)

    def test_does_not_use_donors_beyond_28_days(self):
        dataset, target, _ = self.build(donor_days=29)
        self.assertEqual(dataset.usable_weeks, ())
        self.assertEqual(dataset.start, target + timedelta(minutes=15*17))

    def test_does_not_chain_imputed_rest_day_values(self):
        dataset, target, _ = self.build(donor_valid=False)
        self.assertEqual(dataset.usable_weeks, ())
        self.assertEqual(dataset.start, target + timedelta(minutes=15*17))

    def test_extended_fallback_does_not_exceed_weekly_imputation_budget(self):
        dataset, _, _ = self.build(gap_count=40)
        self.assertEqual(dataset.usable_weeks, ())


if __name__ == "__main__":
    unittest.main()
