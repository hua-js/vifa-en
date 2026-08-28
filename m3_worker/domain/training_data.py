from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from m3_worker.contracts import ObservationPoint, SeriesId


GRID = timedelta(minutes=15)
POINTS_PER_DAY = 96
READY_HISTORY_DAYS = 28
READY_REQUIRED_POINTS = READY_HISTORY_DAYS * POINTS_PER_DAY
OPERATIONAL_HISTORY_DAYS = READY_HISTORY_DAYS
SHORT_GAP_BUCKETS = 2
SEASONAL_LAG_BUCKETS = (96, 672)


@dataclass(frozen=True)
class TrainingDataset:
    frame: pd.DataFrame
    imputed_keys: frozenset[tuple[str, datetime]]
    start: datetime
    end: datetime
    mode: str


def _history_mode(point_count: int) -> str:
    days = point_count / POINTS_PER_DAY
    if days < 7:
        return "insufficient"
    if days < READY_HISTORY_DAYS:
        return "warming_up"
    return "full"


def build_training_dataset(
    points: list[ObservationPoint],
    unique_id: SeriesId,
    *,
    history_days: int | None = None,
) -> TrainingDataset:
    if history_days is not None and (
        type(history_days) is not int or not 7 <= history_days <= 90
    ):
        raise ValueError("history_days must be an integer in 7..90")
    selected = sorted(
        (point for point in points if point.unique_id == unique_id),
        key=lambda point: point.ds,
    )
    if not selected:
        raise ValueError(f"no observations for {unique_id}")
    if len({point.ds for point in selected}) != len(selected):
        raise ValueError("duplicate observation times")
    if history_days is not None:
        earliest = selected[-1].ds - timedelta(days=history_days) + GRID
        selected = [point for point in selected if point.ds >= earliest]

    rows = [
        {
            "unique_id": unique_id,
            "ds": point.ds,
            "y": point.y if point.quality != "invalid" else None,
        }
        for point in selected
    ]
    frame = pd.DataFrame(rows).set_index("ds").asfreq("15min")
    frame["y"] = pd.to_numeric(frame["y"], errors="coerce")
    missing = frame["y"].isna()
    groups = (missing != missing.shift()).cumsum()
    missing_groups = [
        group.index
        for _, group in frame[missing].groupby(groups[missing])
    ]
    imputed_times: set[datetime] = set()
    if history_days is None:
        long_gap_ends = [
            group[-1]
            for group in missing_groups
            if len(group) > SHORT_GAP_BUCKETS
        ]
        if long_gap_ends:
            frame = frame.loc[max(long_gap_ends) + GRID :]
        missing_before_interpolation = frame["y"].isna()
        frame["y"] = frame["y"].interpolate(
            method="time", limit=SHORT_GAP_BUCKETS, limit_area="inside"
        )
        imputed_times.update(
            value.to_pydatetime()
            for value in frame.index[
                missing_before_interpolation & frame["y"].notna()
            ]
        )
    else:
        interpolated = frame["y"].interpolate(
            method="time", limit=SHORT_GAP_BUCKETS, limit_area="inside"
        )
        for group in missing_groups:
            if len(group) <= SHORT_GAP_BUCKETS:
                frame.loc[group, "y"] = interpolated.loc[group]
        imputed_times.update(
            value.to_pydatetime()
            for value in frame.index[missing & frame["y"].notna()]
        )

        seasonal_donors = frame["y"].copy()
        for value in frame.index[frame["y"].isna()]:
            for lag in SEASONAL_LAG_BUCKETS:
                donor = value - lag * GRID
                if donor in seasonal_donors.index and pd.notna(
                    seasonal_donors.loc[donor]
                ):
                    frame.loc[value, "y"] = seasonal_donors.loc[donor]
                    imputed_times.add(value.to_pydatetime())
                    break

        unresolved = frame["y"].isna()
        if unresolved.any():
            unresolved_groups = (
                unresolved != unresolved.shift()
            ).cumsum()
            last_unresolved = max(
                group.index[-1]
                for _, group in frame[unresolved].groupby(
                    unresolved_groups[unresolved]
                )
            )
            frame = frame.loc[last_unresolved + GRID :]

    frame = frame.loc[frame["y"].notna()].reset_index()
    if frame.empty:
        raise ValueError(f"no continuous observations for {unique_id}")

    frame["unique_id"] = unique_id
    retained_imputed_times = frame.loc[
        frame["ds"].isin(imputed_times), "ds"
    ]
    real_point_count = len(frame) - len(retained_imputed_times)
    return TrainingDataset(
        frame=frame[["unique_id", "ds", "y"]],
        imputed_keys=frozenset(
            (unique_id, value.to_pydatetime())
            for value in retained_imputed_times
        ),
        start=frame["ds"].iloc[0].to_pydatetime(),
        end=frame["ds"].iloc[-1].to_pydatetime(),
        mode=_history_mode(
            real_point_count if history_days is not None else len(frame)
        ),
    )
