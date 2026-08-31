"""Behavior tests for configurable M3 forecast post-processing."""

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import pandas as pd

from m3_worker.custom_forecast_contracts import CustomForecastConfig
from m3_worker.domain.custom_forecasting import (
    forecast_custom_series,
    seasonal_naive_champion,
)
from m3_worker.domain.custom_training_data import CustomTrainingDataset


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
            dataset, seasonal_naive_champion(dataset), make_config()
        )

        self.assertEqual(
            [point.forecast_value for point in series.points[:2]], [10.0, 15.0]
        )


if __name__ == "__main__":
    unittest.main()
