"""Build interval-independent M3 training frames without changing v1 data."""

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from m3_worker.contracts import SeriesId
from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomObservationPoint,
)


SHORT_GAP_BUCKETS = 2


@dataclass(frozen=True)
class CustomTrainingDataset:
    frame: pd.DataFrame
    imputed_keys: frozenset[tuple[str, datetime]]
    start: datetime
    end: datetime
    mode: str
    interval_seconds: int
    points_per_day: int


def _history_mode(real_point_count: int, points_per_day: int) -> str:
    days = real_point_count / points_per_day
    if days < 7:
        return "insufficient"
    if days < 28:
        return "warming_up"
    return "full"


def build_custom_training_dataset(
    points: list[CustomObservationPoint],
    unique_id: SeriesId,
    config: CustomForecastConfig,
) -> CustomTrainingDataset:
    config = CustomForecastConfig.model_validate(config)
    selected = sorted(
        (
            CustomObservationPoint.model_validate(point)
            for point in points
            if point.unique_id == unique_id
            and config.history_start <= point.ds < config.history_end
        ),
        key=lambda point: point.ds,
    )
    if not selected:
        raise ValueError(f"no observations for {unique_id}")
    if len({point.ds for point in selected}) != len(selected):
        raise ValueError("duplicate observation times")
    if any(
        (point.ds - config.history_start).total_seconds()
        % config.interval_seconds
        for point in selected
    ):
        raise ValueError("observation time is not aligned to the configured interval")

    rows = [
        {
            "unique_id": unique_id,
            "ds": point.ds,
            "y": point.y if point.quality != "invalid" else None,
        }
        for point in selected
    ]
    full_index = pd.date_range(
        start=config.history_start,
        end=config.history_end - config.interval,
        freq=config.pandas_frequency,
    )
    frame = pd.DataFrame(rows).set_index("ds").reindex(full_index)
    frame.index.name = "ds"
    frame["y"] = pd.to_numeric(frame["y"], errors="coerce")

    missing = frame["y"].isna()
    missing_groups = (missing != missing.shift()).cumsum()
    interpolated = frame["y"].interpolate(
        method="time", limit=SHORT_GAP_BUCKETS, limit_area="inside"
    )
    imputed_times: set[datetime] = set()
    for _, group in frame[missing].groupby(missing_groups[missing]):
        if len(group) <= SHORT_GAP_BUCKETS:
            frame.loc[group.index, "y"] = interpolated.loc[group.index]
    imputed_times.update(
        value.to_pydatetime()
        for value in frame.index[missing & frame["y"].notna()]
    )

    seasonal_donors = frame["y"].copy()
    seasonal_lags = (config.points_per_day, config.points_per_day * 7)
    for value in frame.index[frame["y"].isna()]:
        for lag in seasonal_lags:
            donor = value - lag * config.interval
            if donor in seasonal_donors.index and pd.notna(seasonal_donors.loc[donor]):
                frame.loc[value, "y"] = seasonal_donors.loc[donor]
                imputed_times.add(value.to_pydatetime())
                break

    unresolved = frame["y"].isna()
    if unresolved.any():
        unresolved_groups = (unresolved != unresolved.shift()).cumsum()
        last_unresolved = max(
            group.index[-1]
            for _, group in frame[unresolved].groupby(
                unresolved_groups[unresolved]
            )
        )
        frame = frame.loc[last_unresolved + config.interval :]

    frame = frame.loc[frame["y"].notna()].reset_index()
    if frame.empty:
        raise ValueError(f"no continuous observations for {unique_id}")
    frame["unique_id"] = unique_id
    retained_imputed = frame.loc[frame["ds"].isin(imputed_times), "ds"]
    real_point_count = len(frame) - len(retained_imputed)
    return CustomTrainingDataset(
        frame=frame[["unique_id", "ds", "y"]],
        imputed_keys=frozenset(
            (unique_id, value.to_pydatetime()) for value in retained_imputed
        ),
        start=frame["ds"].iloc[0].to_pydatetime(),
        end=frame["ds"].iloc[-1].to_pydatetime(),
        mode=_history_mode(real_point_count, config.points_per_day),
        interval_seconds=config.interval_seconds,
        points_per_day=config.points_per_day,
    )
