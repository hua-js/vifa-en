"""Deterministic selection fixtures shared by the baseline capture and the
equivalence test.  Any change to selection semantics must regenerate the
baseline JSON consciously (see test_m3_selection_equivalence.py)."""

from datetime import datetime, timedelta

import pandas as pd

from m3.worker.custom_forecast_contracts import CustomForecastConfig
from m3.worker.domain.custom_forecasting import (
    forecast_custom_series,
    select_custom_champion,
)
from m3.worker.domain.custom_training_data import (
    CustomTrainingDataset,
    CustomWeekSummary,
)

FORECAST_START = datetime.fromisoformat("2026-08-31T00:00:00+08:00")  # Monday


def make_config(history_days: int, *, interval_seconds: int, forecast_days: int = 1):
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


def _week_summaries(weeks: int, points_per_day: int):
    return tuple(
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


def load_dataset(weeks: int, *, interval_seconds: int, imputed_holdout: bool = False):
    """Load frame with weekly structure: workday ramp + lower Sunday."""
    points_per_day = 86_400 // interval_seconds
    times = pd.date_range(
        FORECAST_START - timedelta(days=7 * weeks),
        periods=weeks * 7 * points_per_day,
        freq=f"{interval_seconds}s",
    )
    values = []
    for index, timestamp in enumerate(times):
        hour_fraction = (timestamp.hour * 3600 + timestamp.minute * 60
                         + timestamp.second) / 86_400
        base = 120.0 if timestamp.weekday() < 6 else 40.0
        wave = 30.0 * (1 if timestamp.weekday() < 6 else 0) * (
            1 if 0.3 <= hour_fraction <= 0.8 else 0.2
        )
        jitter = ((index * 37) % 11) - 5  # deterministic wobble
        values.append(base + wave + jitter + 0.5 * (index % 3))
    imputed_keys = frozenset()
    if imputed_holdout:
        holdout_start = times[-7 * points_per_day]
        imputed_keys = frozenset(
            ("station_total_load", (holdout_start + step * timedelta(hours=3))
             .to_pydatetime())
            for step in range(0, 7 * points_per_day // (10_800 // interval_seconds))
        )
    return CustomTrainingDataset(
        frame=pd.DataFrame(
            {"unique_id": "station_total_load", "ds": times, "y": values}
        ),
        imputed_keys=imputed_keys,
        source_available_start=pd.Timestamp(times[0]).to_pydatetime(),
        source_available_points=len(times),
        leading_no_data_points=0,
        invalid_points=0,
        negative_invalid_points=0,
        start=pd.Timestamp(times[0]).to_pydatetime(),
        end=pd.Timestamp(times[-1]).to_pydatetime(),
        mode="full",
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
        usable_weeks=_week_summaries(weeks, points_per_day),
    )


def soc_dataset(days: int, *, interval_seconds: int, with_power: bool):
    """SOC frame with workday charge/discharge cycling and a flat Sunday."""
    points_per_day = 86_400 // interval_seconds
    times = pd.date_range(
        FORECAST_START - timedelta(days=days),
        periods=days * points_per_day,
        freq=f"{interval_seconds}s",
    )
    step = 24 * 3600 / (points_per_day * 10)  # visible deltas at any interval
    values: list[float] = []
    powers: list[float] = []
    soc = 55.0
    for timestamp in times:
        if timestamp.weekday() != 6:
            if 1 <= timestamp.hour <= 6:
                soc += step
                powers.append(-50.0)
            elif 8 <= timestamp.hour <= 13:
                soc -= step
                powers.append(60.0)
            else:
                powers.append(0.0)
        else:
            powers.append(0.0)
        soc = min(95.0, max(10.0, soc))
        values.append(soc)
    frame = pd.DataFrame({"unique_id": "storage_soc", "ds": times, "y": values})
    if with_power:
        frame["storage_power_kw"] = powers
    return CustomTrainingDataset(
        frame=frame,
        imputed_keys=frozenset(),
        source_available_start=pd.Timestamp(times[0]).to_pydatetime(),
        source_available_points=len(times),
        leading_no_data_points=0,
        invalid_points=0,
        negative_invalid_points=0,
        start=pd.Timestamp(times[0]).to_pydatetime(),
        end=pd.Timestamp(times[-1]).to_pydatetime(),
        mode="full",
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
        energy_capacity_kwh=200.0 if with_power else None,
    )


def all_cases():
    """(name, dataset, config) tuples; order is stable for the baseline JSON."""
    return [
        ("load_weekly_4wk_900",
         load_dataset(4, interval_seconds=900),
         make_config(28, interval_seconds=900)),
        ("load_weekly_4wk_300",
         load_dataset(4, interval_seconds=300),
         make_config(28, interval_seconds=300)),
        ("load_weekly_imputed_900",
         load_dataset(4, interval_seconds=900, imputed_holdout=True),
         make_config(28, interval_seconds=900)),
        ("soc_35d_900_with_power",
         soc_dataset(35, interval_seconds=900, with_power=True),
         make_config(35, interval_seconds=900)),
        ("soc_35d_900_no_capacity",
         soc_dataset(35, interval_seconds=900, with_power=False),
         make_config(35, interval_seconds=900)),
        ("soc_35d_300_fine",
         soc_dataset(35, interval_seconds=300, with_power=True),
         make_config(35, interval_seconds=300)),
    ]


def run_case(dataset, config) -> dict:
    """Run champion selection and the final forecast; return comparable data."""
    champion = select_custom_champion(dataset, config)
    series = forecast_custom_series(dataset, champion, config)
    return {
        "champion": {
            "model_name": champion.model_name,
            "cv_mape_percent": champion.cv_mape_percent,
            "selection_reason": champion.selection_reason,
            "selection_metric": champion.selection_metric,
            "selection_status": champion.selection_status,
            "candidate_scores": [
                {
                    "model_name": score.model_name,
                    "wape_percent": score.wape_percent,
                    "mae": score.mae,
                    "mape_percent": score.mape_percent,
                    "scorable_point_count": score.scorable_point_count,
                    "skip_reason": score.skip_reason,
                }
                for score in champion.candidate_scores
            ],
        },
        "series": {
            "model_name": series.model_name,
            "status": series.status,
            "values": [point.forecast_value for point in series.points],
        },
    }
