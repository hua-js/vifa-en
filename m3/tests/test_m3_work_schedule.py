"""Work/rest boundaries, training isolation and forecast-entry integration."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import SeasonalNaive

from m3.worker.domain.custom_forecasting import forecast_custom_series, weekly_naive_champion
from m3.worker.domain.custom_load_profiles import weekly_profile_values
from m3.worker.domain.custom_training_data import build_custom_training_dataset
from m3.worker.domain.forecasting import forecast_one, seasonal_naive_champion
from m3.worker.domain.training_data import TrainingDataset
from m3.worker.domain.work_schedule import (
    LoadScheduleProfile,
    schedule_cross_validation,
    schedule_forecast,
    schedule_interpolate,
    schedule_slot,
)
from m3.worker.errors import M3Error
from m3.tests.test_m3_custom_forecasting import load_dataset_with_weeks, make_selection_config
from m3.tests.test_m3_custom_training_data import make_config, make_points


MONDAY = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def schedule_history(origin=MONDAY, days=28):
    times = pd.date_range(origin - timedelta(days=days), periods=days * 96, freq="15min")
    return pd.DataFrame({
        "unique_id": "station_total_load", "ds": times,
        "y": [600.0 if timestamp.weekday() < 6 else 30.0 for timestamp in times],
    })


class WorkScheduleTests(unittest.TestCase):
    def test_saturday_is_work_sunday_rest_monday_work_in_shanghai(self):
        for value, expected in (
            ("2026-08-29T23:45:00+08:00", True),
            ("2026-08-30T00:00:00+08:00", False),
            ("2026-08-30T23:45:00+08:00", False),
            ("2026-08-31T00:00:00+08:00", True),
            ("2026-08-29T16:00:00+00:00", False),
        ):
            with self.subTest(value=value):
                self.assertEqual(schedule_slot(value)[0], expected)

    def test_future_actuals_are_never_used_in_schedule_profile(self):
        history = schedule_history()
        future = pd.DataFrame({"unique_id": ["station_total_load"], "ds": [MONDAY], "y": [object()]})
        profile = LoadScheduleProfile.fit(pd.concat([history, future]), origin=MONDAY)
        np.testing.assert_array_equal(profile.offsets([MONDAY, MONDAY - timedelta(days=1)]), [600, 30])

    def test_missing_rest_history_does_not_use_working_days(self):
        history = schedule_history()
        history = history.loc[history["ds"].dt.weekday < 6]
        profile = LoadScheduleProfile.fit(history, origin=MONDAY)
        with self.assertRaises(M3Error):
            profile.offsets([MONDAY + timedelta(days=6)])

    def test_short_gap_does_not_interpolate_across_work_rest_boundary(self):
        times = pd.date_range("2026-08-29T23:30:00+08:00", periods=4, freq="15min")
        values = pd.Series([600.0, np.nan, np.nan, 30.0], index=times)
        result = schedule_interpolate(values, limit=2)
        self.assertTrue(result.iloc[1:3].isna().all())

    def test_daily_model_restores_sunday_and_monday_separately(self):
        for origin in (MONDAY, MONDAY - timedelta(days=1)):
            with self.subTest(origin=origin):
                engine = StatsForecast(models=[SeasonalNaive(96)], freq="15min", n_jobs=1)
                predicted = schedule_forecast(
                    engine, schedule_history(origin), origin=origin,
                    periods=7 * 96, model_name="SeasonalNaive",
                )
                expected = [600 if t.weekday() < 6 else 30 for t in predicted["ds"]]
                np.testing.assert_allclose(predicted["SeasonalNaive"], expected)

    def test_cv_refits_only_before_each_holdout(self):
        history = schedule_history()
        # A new load level in the last day must remain unseen by that fold.
        history.loc[history.index[-96:], "y"] = 9000
        frames = []
        class ZeroResidualEngine:
            def forecast(self, *, df, h):
                frames.append(df.copy())
                start = df["ds"].iloc[-1] + timedelta(minutes=15)
                return pd.DataFrame({"ds": pd.date_range(start, periods=h, freq="15min"), "Zero": [0.] * h})
        cv = schedule_cross_validation(ZeroResidualEngine(), history, model_name="Zero")
        self.assertEqual(len(cv), 7 * 96)
        self.assertEqual(len(frames), 7)
        self.assertTrue((cv.iloc[-96:]["Zero"] == 30).all())
        for frame, (_, fold) in zip(frames, cv.groupby("cutoff", sort=True), strict=True):
            self.assertLess(frame["ds"].max(), fold["ds"].min())

    def test_custom_automatic_failure_keeps_day_types_in_weekly_fallback(self):
        config = make_selection_config(28, interval_seconds=900, forecast_days=7)
        dataset = replace(load_dataset_with_weeks(4, interval_seconds=900), frame=schedule_history())
        champion = replace(weekly_naive_champion(dataset), model_name="AutoARIMA")
        with patch("m3.worker.domain.custom_forecasting.StatsForecast") as engine:
            engine.return_value.forecast.side_effect = RuntimeError("model failed")
            series = forecast_custom_series(dataset, champion, config)
        self.assertEqual(series.model_name, "WeeklyNaive")
        self.assertEqual(series.status, "degraded")
        self.assertEqual([p.forecast_value for p in series.points], [600.] * (6 * 96) + [30.] * 96)

    def test_custom_automatic_forecast_restores_each_target_day_type(self):
        config = make_selection_config(28, interval_seconds=900, forecast_days=7)
        dataset = replace(load_dataset_with_weeks(4, interval_seconds=900), frame=schedule_history())
        champion = replace(weekly_naive_champion(dataset), model_name="AutoARIMA")
        with patch("m3.worker.domain.custom_forecasting.StatsForecast") as engine:
            engine.return_value.forecast.return_value = pd.DataFrame({
                "ds": pd.date_range(MONDAY, periods=7 * 96, freq="15min"),
                "AutoARIMA": [0.] * (7 * 96),
            })
            series = forecast_custom_series(dataset, champion, config)
            self.assertTrue(np.allclose(engine.return_value.forecast.call_args.kwargs["df"]["y"], 0.))
        self.assertEqual(series.model_name, "AutoARIMA")
        self.assertEqual([p.forecast_value for p in series.points], [600.] * (6 * 96) + [30.] * 96)

    def test_regular_forecast_and_fallback_use_schedule(self):
        history = schedule_history()
        dataset = TrainingDataset(history, frozenset(), history["ds"].iloc[0].to_pydatetime(),
                                  history["ds"].iloc[-1].to_pydatetime(), "full")
        champion = seasonal_naive_champion(dataset)
        series = forecast_one(dataset, champion, MONDAY)
        self.assertTrue(all(point.forecast_value == 600 for point in series.points))
        failed = Mock()
        failed.forecast.side_effect = RuntimeError("model failed")
        fallback = StatsForecast(models=[SeasonalNaive(96)], freq="15min", n_jobs=1)
        with patch("m3.worker.domain.forecasting.StatsForecast", side_effect=[failed, fallback]):
            series = forecast_one(dataset, replace(champion, model_name="AutoETS"), MONDAY)
        self.assertEqual(series.status, "degraded")
        self.assertTrue(all(point.forecast_value == 600 for point in series.points))

    def test_sunday_does_not_reduce_monday_regime_adjustment(self):
        dataset = load_dataset_with_weeks(3)
        times = dataset.frame["ds"]
        dataset.frame["y"] = [
            1.0 if t.weekday() == 6 else (500.0 if 8 <= t.hour < 18 else 20.0)
            for t in times
        ]
        values = weekly_profile_values(dataset, "WeeklyRegimeAdjusted", origin=MONDAY, periods=24)
        self.assertEqual(values[0], 20.0)
        self.assertEqual(values[8], 500.0)

    def test_sunday_gap_uses_previous_sunday_not_saturday(self):
        start = MONDAY - timedelta(days=21)
        gap_start = MONDAY - timedelta(days=1) + timedelta(hours=12)
        gaps = {gap_start + timedelta(minutes=15 * i) for i in range(3)}
        points = make_points(start, MONDAY, imputed_times=gaps)
        points = [point if point.y is None else point.model_copy(update={
            "y": 600.0 if point.ds.weekday() < 6 else 30.0,
        }) for point in points]
        dataset = build_custom_training_dataset(points, "station_total_load", make_config(start))
        filled = dataset.frame.loc[dataset.frame["ds"].isin(gaps), "y"]
        self.assertEqual(filled.tolist(), [30.0] * 3)


if __name__ == "__main__":
    unittest.main()
