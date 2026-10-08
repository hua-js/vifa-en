"""Calendar-confirmed resumption without modifying invalid/raw observations."""
from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from types import SimpleNamespace

import pandas as pd

from m3.worker.services.forecast_service import ForecastService
from m3.worker.contracts import ObservationPoint
from m3.worker.custom_forecast_contracts import CustomForecastConfig, CustomObservationPoint
from m3.worker.domain.custom_forecasting import forecast_custom_series, select_custom_champion
from m3.worker.domain.custom_training_data import build_custom_training_dataset
from m3.worker.domain.forecasting import forecast_one
from m3.worker.domain.load_history import (
    POST_HOLIDAY_LOAD_MODEL, POST_HOLIDAY_LOAD_REASON, post_holiday_load_reference,
)
from m3.worker.domain.production_schedule import ProductionSchedule, use_schedule
from m3.worker.domain.training_data import build_training_dataset

ORIGIN = datetime.fromisoformat("2026-10-08T00:00:00+08:00")
START = ORIGIN - timedelta(days=28)
GRID = timedelta(minutes=15)
LOAD = "station_total_load"


def calendar():
    statuses = {}
    for index in range(36):
        day = (START + timedelta(days=index)).date()
        statuses[day] = "REST" if day.weekday() == 6 else "WORK"
        if day.month == 10 and 1 <= day.day <= 7:
            statuses[day] = "HOLIDAY"
    return ProductionSchedule("ES01", "station_1", "86", START.date(),
        (ORIGIN + timedelta(days=7)).date(), ORIGIN.date(), statuses, {}, "{}")


def observed(*, holiday_invalid=True):
    slots = pd.date_range(START, end=ORIGIN - GRID, freq=GRID)
    # Distinguishable pre-holiday days make it possible to detect holiday/future
    # leakage, rather than simply asserting a positive output.
    return pd.Series([
        float("nan") if holiday_invalid and slot.month == 10 and 8 <= slot.hour < 18
        else 1.0 if slot.month == 10 else float(slot.day * 10 + slot.hour)
        for slot in slots
    ], index=slots, name="y").rename_axis("ds")


class PostHolidayLoadTests(unittest.TestCase):
    def test_selection_calendar_covers_reference_horizon_across_midnight(self):
        calls = []
        service = object.__new__(ForecastService)
        service._cache = lambda station: None
        def load(station, start, end, *, forecast_start):
            calls.append((end, forecast_start))
            return calendar()
        service._schedule_provider = SimpleNamespace(load=load)
        service._calendar("ES01", ORIGIN + timedelta(hours=14), selection=True)
        self.assertEqual(calls, [((ORIGIN + timedelta(days=1)).date(),
                                 (ORIGIN + timedelta(days=1)).date())])

    def reference(self, values=None, schedule=None, origin=ORIGIN, periods=96):
        with use_schedule(schedule if schedule is not None else calendar()):
            return post_holiday_load_reference(
                observed() if values is None else values, history_end=ORIGIN,
                origin=origin, interval=GRID, periods=periods,
            )

    def test_complete_pre_holiday_days_only_and_no_input_changes(self):
        values = observed()
        before = values.copy()
        reference = self.reference(values)
        self.assertIsNotNone(reference)
        self.assertEqual(len(reference.values), 96)
        # Sep 30, 29, 28: median 290 at midnight, 300 at 10:00.
        self.assertEqual(reference.values[0], 290.0)
        self.assertEqual(reference.values[40], 300.0)
        self.assertTrue(all(source < datetime.fromisoformat("2026-10-01T00:00:00+08:00")
                            for _, sources in reference.donors for source in sources))
        reconstructed = []
        manifest = reference.manifest()
        for segment in manifest["segments"]:
            target = datetime.fromisoformat(segment["target_start"])
            sources = tuple(datetime.fromisoformat(value) for value in segment["source_starts"])
            for step in range(segment["point_count"]):
                reconstructed.append((target + step * GRID, tuple(source + step * GRID for source in sources)))
        self.assertEqual(tuple(reconstructed), reference.donors)
        pd.testing.assert_series_equal(values, before)

    def test_partial_negative_day_and_future_observations_are_excluded(self):
        values = observed()
        values.loc[ORIGIN - timedelta(days=8) + timedelta(hours=10)] = -1.0  # Sep 30
        values.loc[ORIGIN] = 999999.0
        reference = self.reference(values)
        # Skip the entire invalid Sep 30; Sep 29/28/26 are the latest workdays.
        self.assertEqual(reference.values[0], 280.0)
        self.assertTrue(all(source < ORIGIN for _, sources in reference.donors for source in sources))

    def test_no_confirmed_long_holiday_means_no_reference(self):
        base = calendar()
        for status in ("REST", "SHUTDOWN", "WORK", None):
            statuses = {day: (status if value == "HOLIDAY" else value)
                        for day, value in base.statuses.items()}
            if status is None:
                statuses = {day: value for day, value in statuses.items() if value is not None}
            with self.subTest(status=status):
                self.assertIsNone(self.reference(schedule=replace(base, statuses=statuses)))
        with use_schedule(None):
            self.assertIsNone(post_holiday_load_reference(observed(), history_end=ORIGIN,
                origin=ORIGIN, interval=GRID, periods=96))

    def test_missing_or_expired_complete_days_still_blocks(self):
        self.assertIsNone(self.reference(values=observed().loc[ORIGIN - timedelta(days=7):]))
        old_slots = pd.date_range(START - timedelta(days=2), periods=96, freq=GRID)
        self.assertIsNone(self.reference(values=pd.Series(100.0, index=old_slots)))
        self.assertIsNone(self.reference(origin=ORIGIN + timedelta(days=7)))

    def test_future_rest_day_requires_matching_pre_holiday_rest_day(self):
        values = observed()
        # Remove Sep 27, 20, 13, the three pre-holiday Sundays in the window.
        values = values[values.index.weekday != 6]
        self.assertIsNone(self.reference(values=values, periods=4 * 96))

    def test_custom_and_conventional_paths_publish_degraded_reference(self):
        self.assert_paths_publish_reference(observed())

    def test_no_continuous_tail_still_uses_real_pre_holiday_reference(self):
        values = observed()
        values.loc[values.index >= datetime.fromisoformat("2026-10-01T00:00:00+08:00")] = float("nan")
        self.assert_paths_publish_reference(values)

    def assert_paths_publish_reference(self, values):
        custom_points, regular_points = [], []
        for stamp, value in values.items():
            valid = pd.notna(value)
            common = dict(unique_id=LOAD, ds=stamp.to_pydatetime(), y=float(value) if valid else None,
                          quality="valid" if valid else "invalid", source_revision=1)
            regular_points.append(ObservationPoint(**common))
            custom_points.append(CustomObservationPoint(**common,
                source_state="valid" if valid else "negative"))
        config = CustomForecastConfig(history_start=START, history_end=ORIGIN, history_days=28,
            forecast_start=ORIGIN, forecast_end=ORIGIN + timedelta(days=1), forecast_days=1,
            interval_seconds=900, points_per_day=96, expected_points_per_series=96,
            model_policy="full_selection")
        with use_schedule(calendar()):
            custom = build_custom_training_dataset(custom_points, LOAD, config)
            champion = select_custom_champion(custom, config)
            custom_result = forecast_custom_series(custom, champion, config)
            regular = build_training_dataset(regular_points, LOAD, history_days=28)
            regular_result = forecast_one(regular, None, ORIGIN)
        self.assertEqual(len(custom.usable_weeks), 0)
        self.assertIsNone(champion.cv_mape_percent)
        self.assertEqual(champion.candidate_scores, ())
        for result in (custom_result, regular_result):
            self.assertEqual(result.model_name, POST_HOLIDAY_LOAD_MODEL)
            self.assertEqual(result.status, "degraded")
            self.assertEqual(result.fallback_reason, POST_HOLIDAY_LOAD_REASON)
            self.assertEqual(len(result.points), 96)
            self.assertEqual(result.points[40].forecast_value, 300.0)

    def test_valid_holiday_week_does_not_get_copied_as_a_workday(self):
        values = observed(holiday_invalid=False)
        points = [CustomObservationPoint(unique_id=LOAD, ds=stamp.to_pydatetime(), y=float(value),
            quality="valid", source_state="valid", source_revision=1) for stamp, value in values.items()]
        config = CustomForecastConfig(history_start=START, history_end=ORIGIN, history_days=28,
            forecast_start=ORIGIN, forecast_end=ORIGIN + timedelta(days=1), forecast_days=1,
            interval_seconds=900, points_per_day=96, expected_points_per_series=96,
            model_policy="full_selection")
        with use_schedule(calendar()):
            dataset = build_custom_training_dataset(points, LOAD, config)
            result = forecast_custom_series(dataset, select_custom_champion(dataset, config), config)
        self.assertGreater(len(dataset.usable_weeks), 0)
        self.assertEqual(result.model_name, POST_HOLIDAY_LOAD_MODEL)
        self.assertEqual(result.points[40].forecast_value, 300.0)
