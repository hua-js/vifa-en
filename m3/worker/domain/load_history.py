"""Bounded real-observation recovery shared by both load forecast paths."""
from collections import Counter
from datetime import datetime, timedelta

import pandas as pd

from m3.worker.domain.work_schedule import schedule_slot

MAX_LOAD_FALLBACK_DAYS = 28
MAX_WEEK_IMPUTATION_RATIO = 0.05


def fill_load_schedule_gaps(
    frame: pd.DataFrame,
    observed_load: pd.Series,
    imputed_times: set[datetime],
    *,
    history_end: datetime,
    interval: timedelta,
    points_per_day: int,
    history_days: int,
) -> list[tuple[datetime, datetime]]:
    """Fill a training copy from earlier real samples; preserve imputation evidence."""
    def week_key(timestamp):
        return (history_end - timestamp - interval) // timedelta(days=7)

    weekly_imputed = Counter(week_key(t) for t in imputed_times)
    budget = int(points_per_day * 7 * MAX_WEEK_IMPUTATION_RATIO)
    donors = []
    for value in frame.index[frame["y"].isna()]:
        week = week_key(value)
        if weekly_imputed[week] >= budget:
            continue
        working = schedule_slot(value)[0]
        for days in range(1, min(MAX_LOAD_FALLBACK_DAYS, history_days) + 1):
            donor = value - timedelta(days=days)
            if donor not in observed_load.index:
                continue
            observed = observed_load.loc[donor]
            if pd.isna(observed) or observed < 0:
                continue
            if schedule_slot(donor)[0] != working:
                continue
            frame.loc[value, "y"] = observed
            imputed_times.add(value.to_pydatetime())
            weekly_imputed[week] += 1
            donors.append((value.to_pydatetime(), donor.to_pydatetime()))
            break
    return donors
