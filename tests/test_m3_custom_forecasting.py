"""Behavior tests for configurable M3 forecast post-processing."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from m3_worker.custom_forecast_contracts import CustomForecastConfig
from m3_worker.domain.custom_forecasting import (
    forecast_custom_series,
    seasonal_naive_champion,
    select_custom_champion,
    weekly_naive_champion,
)
from m3_worker.errors import M3Error
from m3_worker.domain.custom_training_data import (
    CustomTrainingDataset,
    CustomWeekSummary,
)


FORECAST_START = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_config() -> CustomForecastConfig:
    return CustomForecastConfig(
        history_start=FORECAST_START - timedelta(days=7),
        history_end=FORECAST_START,
        history_days=7,
        forecast_start=FORECAST_START,
        forecast_end=FORECAST_START + timedelta(days=1),
        forecast_days=1,
        interval_seconds=3600,
        points_per_day=24,
        expected_points_per_series=24,
        model_policy="seasonal_naive_only",
    )


def make_dataset(
    unique_id: str, *, last_point_imputed: bool = False
) -> CustomTrainingDataset:
    times = pd.date_range(
        FORECAST_START - timedelta(days=7), periods=7 * 24, freq="1h"
    )
    values = [50.0] * len(times)
    values[-2] = 80.0
    values[-1] = 20.0 if last_point_imputed else 80.0
    imputed_keys = (
        frozenset({(unique_id, times[-1].to_pydatetime())})
        if last_point_imputed
        else frozenset()
    )
    return CustomTrainingDataset(
        frame=pd.DataFrame({"unique_id": unique_id, "ds": times, "y": values}),
        imputed_keys=imputed_keys,
        start=times[0].to_pydatetime(),
        end=times[-1].to_pydatetime(),
        mode="warming_up",
        interval_seconds=3600,
        points_per_day=24,
    )


def make_selection_config(
    history_days: int = 14,
    *,
    interval_seconds: int = 3600,
    forecast_days: int = 1,
) -> CustomForecastConfig:
    points_per_day = 86_400 // interval_seconds
    return CustomForecastConfig(
        history_start=FORECAST_START - timedelta(days=history_days),
        history_end=FORECAST_START,
        history_days=history_days,
        forecast_start=FORECAST_START,
        forecast_end=FORECAST_START + timedelta(days=forecast_days),
        forecast_days=forecast_days,
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
        expected_points_per_series=points_per_day * forecast_days,
        model_policy=(
            "seasonal_naive_only" if history_days < 28 else "full_selection"
        ),
    )


def load_dataset_with_weeks(
    weeks: int,
    *,
    interval_seconds: int = 3600,
    week_values: list[float] | None = None,
) -> CustomTrainingDataset:
    points_per_day = 86_400 // interval_seconds
    frequency = f"{interval_seconds}s"
    if weeks < 1:
        times = pd.date_range(
            FORECAST_START - timedelta(days=6), periods=points_per_day, freq=frequency
        )
        usable_weeks = ()
    else:
        times = pd.date_range(
            FORECAST_START - timedelta(days=7 * weeks),
            periods=weeks * 7 * points_per_day,
            freq=frequency,
        )
        usable_weeks = tuple(
            CustomWeekSummary(
                start=FORECAST_START - timedelta(days=7 * (weeks - index)),
                end=FORECAST_START - timedelta(days=7 * (weeks - index - 1)),
                point_count=7 * points_per_day,
                real_point_count=7 * points_per_day,
                imputed_point_count=0,
                imputation_ratio=0.0,
            )
            for index in range(weeks)
        )
    if week_values is None:
        values = [80.0] * len(times)
    else:
        if len(week_values) != weeks:
            raise ValueError("week_values must provide one literal per usable week")
        values = [
            value
            for value in week_values
            for _ in range(7 * points_per_day)
        ]
    return CustomTrainingDataset(
        frame=pd.DataFrame(
            {"unique_id": "station_total_load", "ds": times, "y": values}
        ),
        imputed_keys=frozenset(),
        start=times[0].to_pydatetime(),
        end=times[-1].to_pydatetime(),
        mode="full",
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
        usable_weeks=usable_weeks,
    )


def load_dataset_with_literal_week_values() -> CustomTrainingDataset:
    return load_dataset_with_weeks(3, week_values=[50.0, 80.0, 70.0])


def reconstructed_load_week_above_quality_threshold() -> CustomTrainingDataset:
    dataset = load_dataset_with_weeks(1)
    imputed_times = frozenset(
        (
            "station_total_load",
            pd.Timestamp(dataset.frame.iloc[index]["ds"]).to_pydatetime(),
        )
        for index in (24, 37, 50, 63, 76, 89, 102, 115, 128)
    )
    return replace(
        dataset,
        imputed_keys=imputed_times,
        mode="insufficient",
        usable_weeks=(),
    )


def soc_dataset_with_days(
    days: int = 28, *, interval_seconds: int = 3600
) -> CustomTrainingDataset:
    points_per_day = 86_400 // interval_seconds
    times = pd.date_range(
        FORECAST_START - timedelta(days=days),
        periods=days * points_per_day,
        freq=f"{interval_seconds}s",
    )
    return CustomTrainingDataset(
        frame=pd.DataFrame(
            {"unique_id": "storage_soc", "ds": times, "y": [50.0] * len(times)}
        ),
        imputed_keys=frozenset(),
        start=times[0].to_pydatetime(),
        end=times[-1].to_pydatetime(),
        mode="full",
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
    )


def soc_dataset_with_weekly_pattern() -> CustomTrainingDataset:
    """Four Shanghai weeks with production-day cycling and a flat Sunday."""

    times = pd.date_range(
        FORECAST_START - timedelta(days=28), periods=28 * 24, freq="1h"
    )
    values: list[float] = []
    soc = 50.0
    for timestamp in times:
        if timestamp.weekday() != 6:
            if 1 <= timestamp.hour <= 6:
                soc += 2.0
            elif 7 <= timestamp.hour <= 12:
                soc -= 2.0
        values.append(soc)
    return CustomTrainingDataset(
        frame=pd.DataFrame(
            {"unique_id": "storage_soc", "ds": times, "y": values}
        ),
        imputed_keys=frozenset(),
        start=times[0].to_pydatetime(),
        end=times[-1].to_pydatetime(),
        mode="full",
        interval_seconds=3600,
        points_per_day=24,
    )


def model_frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "unique_id": ["storage_soc"] * 24,
            "ds": pd.date_range(FORECAST_START, periods=24, freq="1h"),
            "SeasonalNaive": values,
        }
    )


class CustomForecastAnchoringTests(unittest.TestCase):
    @patch("m3_worker.domain.custom_forecasting._forecast_frame")
    def test_soc_starts_at_last_real_history_value_and_preserves_shape(
        self, forecast_frame
    ):
        dataset = make_dataset("storage_soc", last_point_imputed=True)
        forecast_frame.return_value = (
            model_frame([10.0, 15.0, 25.0] + [25.0] * 21),
            "SeasonalNaive",
            None,
        )

        series = forecast_custom_series(
            dataset, seasonal_naive_champion(dataset), make_config()
        )

        self.assertEqual(
            [point.forecast_value for point in series.points[:3]],
            [80.0, 85.0, 95.0],
        )

    @patch("m3_worker.domain.custom_forecasting._forecast_frame")
    def test_soc_anchor_is_clipped_to_physical_bounds(self, forecast_frame):
        dataset = make_dataset("storage_soc")
        forecast_frame.return_value = (
            model_frame([10.0, 40.0] + [40.0] * 22),
            "SeasonalNaive",
            None,
        )

        series = forecast_custom_series(
            dataset, seasonal_naive_champion(dataset), make_config()
        )

        self.assertEqual(series.points[1].raw_forecast, 110.0)
        self.assertEqual(series.points[1].forecast_value, 100.0)
        self.assertTrue(series.points[1].is_clipped)

    @patch("m3_worker.domain.custom_forecasting._forecast_frame")
    def test_load_forecast_is_not_anchored(self, forecast_frame):
        dataset = make_dataset("station_total_load")
        frame = model_frame([10.0, 15.0] + [15.0] * 22)
        frame["unique_id"] = "station_total_load"
        forecast_frame.return_value = (frame, "SeasonalNaive", None)

        series = forecast_custom_series(
            dataset, weekly_naive_champion(dataset), make_config()
        )

        self.assertEqual(
            [point.forecast_value for point in series.points[:2]], [10.0, 15.0]
        )


class LoadDispatchSelectionTests(unittest.TestCase):
    def test_reconstructed_latest_week_above_quality_threshold_degrades_to_weekly_naive(self):
        dataset = reconstructed_load_week_above_quality_threshold()

        champion = select_custom_champion(dataset, make_selection_config(7))
        series = forecast_custom_series(dataset, champion, make_selection_config(7))

        self.assertEqual(champion.model_name, "WeeklyNaive")
        self.assertEqual(champion.selection_reason, "latest_week_high_imputation")
        self.assertEqual(champion.selection_status, "degraded")
        self.assertEqual(series.model_name, "WeeklyNaive")
        self.assertEqual(series.status, "degraded")
        self.assertEqual(len(series.points), 24)

    def test_one_or_two_usable_weeks_select_weekly_naive_without_cv(self):
        champion = select_custom_champion(
            load_dataset_with_weeks(2), make_selection_config()
        )

        self.assertEqual(champion.model_name, "WeeklyNaive")
        self.assertIsNone(champion.cv_mape_percent)
        self.assertEqual(champion.selection_reason, "fewer_than_three_usable_weeks")
        self.assertEqual(champion.selection_status, "warming_up")

    def test_latest_week_unusable_raises_insufficient_history(self):
        with self.assertRaisesRegex(M3Error, "insufficient_history"):
            select_custom_champion(load_dataset_with_weeks(0), make_selection_config())

    def test_three_weeks_compare_weekly_naive_and_weighted_two_on_latest_holdout(self):
        champion = select_custom_champion(
            load_dataset_with_literal_week_values(), make_selection_config(21)
        )

        self.assertEqual(champion.model_name, "WeeklyWeighted2")
        self.assertEqual(champion.selection_metric, "wape_percent")

    def test_zero_load_holdout_persists_mae_selection_without_skipping_scores(self):
        """A zero WAPE denominator still yields a scored, deterministic MAE winner."""
        champion = select_custom_champion(
            load_dataset_with_weeks(3, week_values=[10.0, 20.0, 0.0]),
            make_selection_config(21),
        )

        self.assertEqual(champion.model_name, "WeeklyWeighted2")
        self.assertEqual(champion.selection_metric, "mae")
        self.assertEqual(champion.selection_reason, "wape_unavailable")
        self.assertTrue(champion.candidate_scores)
        self.assertTrue(
            all(score.wape_percent is None for score in champion.candidate_scores)
        )
        self.assertTrue(
            all(score.mae is not None for score in champion.candidate_scores)
        )
        self.assertTrue(
            all(score.skip_reason is None for score in champion.candidate_scores)
        )

    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_soc_weekly_delta_wins_mae_and_keeps_sunday_flat(
        self, statsforecast_type
    ):
        engines = [Mock() for _ in range(4)]
        for engine, model_name in zip(
            engines, ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"), strict=True
        ):
            engine.forecast.return_value = pd.DataFrame(
                {model_name: [50.0] * (7 * 24)}
            )
        statsforecast_type.side_effect = engines

        dataset = soc_dataset_with_weekly_pattern()
        config = make_selection_config(28, forecast_days=7)
        champion = select_custom_champion(
            dataset, config
        )
        series = forecast_custom_series(dataset, champion, config)

        self.assertEqual(champion.model_name, "SOCWeeklyDelta")
        self.assertEqual(champion.selection_metric, "mae")
        self.assertEqual(champion.cv_mape_percent, 0.0)
        self.assertEqual(
            [score.model_name for score in champion.candidate_scores],
            ["SOCWeeklyDelta", "SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"],
        )
        self.assertEqual(statsforecast_type.call_count, 4)
        for call in [engine.forecast.call_args for engine in engines]:
            self.assertEqual(call.kwargs["h"], 7 * 24)
        self.assertEqual(series.points[0].forecast_value, 50.0)
        monday = [point.forecast_value for point in series.points[:24]]
        sunday = [point.forecast_value for point in series.points[6 * 24 :]]
        self.assertGreater(max(monday), min(monday))
        self.assertEqual(len(set(sunday)), 1)

    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_full_policy_uses_soc_weekly_selection_even_with_warming_mode(
        self, statsforecast_type
    ):
        statsforecast_type.side_effect = RuntimeError("candidate unavailable")
        dataset = replace(soc_dataset_with_weekly_pattern(), mode="warming_up")

        champion = select_custom_champion(dataset, make_selection_config(28))

        self.assertEqual(champion.model_name, "SOCWeeklyDelta")

    @patch("m3_worker.domain.custom_forecasting.MSTL")
    @patch("m3_worker.domain.custom_forecasting.AutoARIMA")
    @patch("m3_worker.domain.custom_forecasting.AutoETS")
    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_thirty_second_soc_selection_skips_automatic_candidates(
        self,
        statsforecast_type,
        autoets_type,
        autoarima_type,
        mstl_type,
    ):
        statsforecast_type.side_effect = RuntimeError("candidate unavailable")
        dataset = soc_dataset_with_days(interval_seconds=30)

        champion = select_custom_champion(
            dataset, make_selection_config(28, interval_seconds=30)
        )

        self.assertEqual(champion.model_name, "SOCWeeklyDelta")
        self.assertEqual(
            [score.model_name for score in champion.candidate_scores],
            ["SOCWeeklyDelta", "SeasonalNaive"],
        )
        autoets_type.assert_not_called()
        autoarima_type.assert_not_called()
        mstl_type.assert_not_called()

    @patch("m3_worker.domain.custom_forecasting.MSTL")
    @patch("m3_worker.domain.custom_forecasting.AutoARIMA")
    def test_thirty_second_load_selection_never_constructs_automatic_models(
        self, autoarima_type, mstl_type
    ):
        champion = select_custom_champion(
            load_dataset_with_weeks(4, interval_seconds=30),
            make_selection_config(28, interval_seconds=30),
        )

        self.assertIn(
            champion.model_name,
            {"WeeklyNaive", "WeeklyWeighted2", "WeeklyMedian3"},
        )
        self.assertNotIn(
            "AutoARIMA", [score.model_name for score in champion.candidate_scores]
        )
        self.assertNotIn(
            "MSTL", [score.model_name for score in champion.candidate_scores]
        )
        autoarima_type.assert_not_called()
        mstl_type.assert_not_called()

    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_five_minute_automatic_models_participate_on_latest_week_holdout(
        self, statsforecast_type
    ):
        dataset = load_dataset_with_weeks(
            4,
            interval_seconds=300,
            week_values=[10.0, 20.0, 30.0, 100.0],
        )
        latest_week = dataset.usable_weeks[-1]
        holdout_length = 7 * dataset.points_per_day
        autoarima_engine = Mock()
        autoarima_engine.forecast.return_value = pd.DataFrame(
            {"AutoARIMA": [100.0] * holdout_length}
        )
        mstl_engine = Mock()
        mstl_engine.forecast.return_value = pd.DataFrame(
            {"MSTL": [90.0] * holdout_length}
        )
        statsforecast_type.side_effect = [autoarima_engine, mstl_engine]

        config = make_selection_config(28, interval_seconds=300)
        champion = select_custom_champion(dataset, config)

        self.assertEqual(champion.model_name, "AutoARIMA")
        self.assertEqual(
            [score.model_name for score in champion.candidate_scores],
            ["WeeklyNaive", "WeeklyWeighted2", "WeeklyMedian3", "AutoARIMA", "MSTL"],
        )
        self.assertEqual(statsforecast_type.call_count, 2)
        self.assertEqual(
            statsforecast_type.call_args_list[0].kwargs["models"][0].season_length,
            config.weekly_season_length,
        )
        self.assertEqual(
            statsforecast_type.call_args_list[1].kwargs["models"][0].season_length,
            [config.daily_season_length, config.weekly_season_length],
        )
        for engine in (autoarima_engine, mstl_engine):
            training = engine.forecast.call_args.kwargs["df"]
            self.assertTrue((training["ds"] < latest_week.start).all())
            self.assertEqual(engine.forecast.call_args.kwargs["h"], holdout_length)

    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_failed_automatic_candidate_does_not_block_other_candidate_winning(
        self, statsforecast_type
    ):
        dataset = load_dataset_with_weeks(
            4,
            interval_seconds=300,
            week_values=[10.0, 20.0, 30.0, 100.0],
        )
        holdout_length = 7 * dataset.points_per_day
        failed_autoarima = Mock()
        failed_autoarima.forecast.side_effect = RuntimeError("private model failure")
        winning_mstl = Mock()
        winning_mstl.forecast.return_value = pd.DataFrame(
            {"MSTL": [100.0] * holdout_length}
        )
        statsforecast_type.side_effect = [failed_autoarima, winning_mstl]

        champion = select_custom_champion(
            dataset, make_selection_config(28, interval_seconds=300)
        )

        self.assertEqual(champion.model_name, "MSTL")
        scores = {score.model_name: score for score in champion.candidate_scores}
        self.assertEqual(scores["AutoARIMA"].skip_reason, "RuntimeError")
        self.assertEqual(scores["MSTL"].wape_percent, 0.0)


class LoadForecastingTests(unittest.TestCase):
    def test_usable_load_week_overrides_legacy_insufficient_mode(self):
        dataset = replace(load_dataset_with_weeks(1), mode="insufficient")
        champion = select_custom_champion(dataset, make_selection_config(7))

        series = forecast_custom_series(
            dataset, champion, make_selection_config(7)
        )

        self.assertEqual(series.model_name, "WeeklyNaive")
        self.assertEqual(series.status, "warming_up")
        self.assertEqual(len(series.points), 24)

    def test_one_or_two_week_load_champion_emits_warming_up_without_final_fallback(self):
        dataset = load_dataset_with_weeks(2)
        champion = select_custom_champion(dataset, make_selection_config())

        series = forecast_custom_series(dataset, champion, make_selection_config())

        self.assertEqual(series.model_name, "WeeklyNaive")
        self.assertEqual(series.status, "warming_up")
        self.assertIsNone(series.fallback_reason)

    @patch("m3_worker.domain.custom_forecasting.weekly_profile_values")
    def test_failed_load_winner_falls_back_to_weekly_naive_without_error_detail(
        self, profile_values
    ):
        profile_values.side_effect = [
            RuntimeError("database password"),
            [42.0] * 24,
        ]
        dataset = load_dataset_with_weeks(3)
        champion = replace(weekly_naive_champion(dataset), model_name="WeeklyWeighted2")

        series = forecast_custom_series(dataset, champion, make_selection_config())

        self.assertEqual(series.model_name, "WeeklyNaive")
        self.assertEqual(series.status, "degraded")
        self.assertEqual(series.fallback_reason, "RuntimeError")
        self.assertNotIn("password", series.fallback_reason)
        self.assertEqual(
            [call.args[1] for call in profile_values.call_args_list],
            ["WeeklyWeighted2", "WeeklyNaive"],
        )

    @patch("m3_worker.domain.custom_forecasting.weekly_profile_values")
    @patch("m3_worker.domain.custom_forecasting.StatsForecast")
    def test_malformed_automatic_load_winner_retries_weekly_naive(
        self, statsforecast_type, profile_values
    ):
        dataset = load_dataset_with_weeks(4)
        config = make_selection_config(28)
        champion = replace(weekly_naive_champion(dataset), model_name="AutoARIMA")
        profile_values.return_value = [42.0] * config.expected_points_per_series
        valid_times = pd.date_range(
            config.forecast_start,
            periods=config.expected_points_per_series,
            freq=config.pandas_frequency,
        )
        cases = {
            "incomplete": pd.DataFrame(
                {
                    "ds": valid_times[:-1],
                    "AutoARIMA": [42.0] * (config.expected_points_per_series - 1),
                }
            ),
            "missing_model_column": pd.DataFrame({"ds": valid_times}),
            "nonfinite": pd.DataFrame(
                {
                    "ds": valid_times,
                    "AutoARIMA": [float("inf")]
                    + [42.0] * (config.expected_points_per_series - 1),
                }
            ),
            "noncoercible": pd.DataFrame(
                {
                    "ds": valid_times,
                    "AutoARIMA": [object()]
                    + [42.0] * (config.expected_points_per_series - 1),
                }
            ),
            "misaligned": pd.DataFrame(
                {
                    "ds": valid_times + config.interval,
                    "AutoARIMA": [42.0] * config.expected_points_per_series,
                }
            ),
        }

        for problem, frame in cases.items():
            with self.subTest(problem=problem):
                statsforecast_type.return_value.forecast.return_value = frame
                profile_values.reset_mock()

                series = forecast_custom_series(dataset, champion, config)

                self.assertEqual(series.model_name, "WeeklyNaive")
                self.assertEqual(series.status, "degraded")
                self.assertEqual(series.fallback_reason, "M3Error")
                self.assertEqual(profile_values.call_args.args[1], "WeeklyNaive")

    @patch("m3_worker.domain.custom_forecasting._forecast_load_model")
    def test_malformed_weekly_naive_result_raises_safe_m3_error(
        self, forecast_load_model
    ):
        dataset = load_dataset_with_weeks(1)
        config = make_selection_config(7)
        forecast_load_model.return_value = (
            pd.DataFrame(
                {
                    "ds": pd.date_range(
                        config.forecast_start,
                        periods=config.expected_points_per_series - 1,
                        freq=config.pandas_frequency,
                    ),
                    "WeeklyNaive": [42.0]
                    * (config.expected_points_per_series - 1),
                }
            ),
            "WeeklyNaive",
            None,
        )

        with self.assertRaises(M3Error) as raised:
            forecast_custom_series(dataset, weekly_naive_champion(dataset), config)

        self.assertEqual(raised.exception.code, "weekly_naive_failed")
        self.assertNotIn("forecast horizon", raised.exception.message)

    @patch("m3_worker.domain.custom_forecasting.weekly_profile_values")
    def test_failed_weekly_naive_raises_safe_m3_error(self, profile_values):
        profile_values.side_effect = RuntimeError("database password")
        dataset = load_dataset_with_weeks(1)

        with self.assertRaises(M3Error) as raised:
            forecast_custom_series(
                dataset, weekly_naive_champion(dataset), make_selection_config(7)
            )

        self.assertEqual(raised.exception.code, "weekly_naive_failed")
        self.assertNotIn("password", raised.exception.message)

    def test_weekly_naive_load_forecast_has_exact_one_and_seven_day_topology(self):
        dataset = load_dataset_with_weeks(1)
        for forecast_days, expected_count in ((1, 24), (7, 168)):
            with self.subTest(forecast_days=forecast_days):
                config = make_selection_config(7, forecast_days=forecast_days)

                series = forecast_custom_series(
                    dataset, weekly_naive_champion(dataset), config
                )

                self.assertEqual(series.model_name, "WeeklyNaive")
                self.assertEqual(len(series.points), expected_count)
                self.assertEqual(series.points[0].target_time, config.forecast_start)
                self.assertEqual(series.points[-1].target_time, config.forecast_end - config.interval)
                self.assertTrue(
                    all(
                        config.forecast_start <= point.target_time < config.forecast_end
                        for point in series.points
                    )
                )
                self.assertEqual(
                    [point.target_time for point in series.points],
                    [
                        config.forecast_start + index * config.interval
                        for index in range(expected_count)
                    ],
                )


if __name__ == "__main__":
    unittest.main()
