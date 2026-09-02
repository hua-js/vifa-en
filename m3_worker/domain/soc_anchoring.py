"""Shared level anchoring for scheduled and custom SOC forecasts."""

import math
from statistics import median

import numpy as np
import pandas as pd

from m3_worker.errors import M3Error


RECENT_RESIDUAL_POINTS = 8


def recent_soc_residual_offset(
    fitted: pd.DataFrame,
    model_name: str,
    imputed_keys: frozenset[tuple[str, object]],
) -> float:
    """Return the median of the latest real SOC fitted residuals."""

    required = {"unique_id", "ds", "y", model_name}
    if not isinstance(fitted, pd.DataFrame) or not required.issubset(fitted.columns):
        raise M3Error("soc_anchor_unavailable", "SOC fitted values are unavailable")

    residuals: list[tuple[pd.Timestamp, float]] = []
    for row in fitted.itertuples(index=False):
        if row.unique_id != "storage_soc":
            continue
        timestamp = pd.Timestamp(row.ds)
        timestamp_value = timestamp.to_pydatetime()
        if (row.unique_id, timestamp_value) in imputed_keys:
            continue
        try:
            actual = float(row.y)
            prediction = float(getattr(row, model_name))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(actual) or not math.isfinite(prediction):
            continue
        residuals.append((timestamp, actual - prediction))

    if not residuals:
        raise M3Error("soc_anchor_unavailable", "SOC fitted residuals are unavailable")
    recent = [
        value
        for _, value in sorted(residuals, key=lambda item: item[0])[
            -RECENT_RESIDUAL_POINTS:
        ]
    ]
    return float(median(recent))


def anchored_soc_cv_predictions(
    cv: pd.DataFrame,
    fitted: pd.DataFrame,
    model_name: str,
    imputed_keys: frozenset[tuple[str, object]],
) -> pd.Series:
    """Apply each CV window's past-only SOC residual anchor and clipping."""

    required_cv = {"unique_id", "ds", "cutoff", model_name}
    required_fitted = {"unique_id", "ds", "cutoff", "y", model_name}
    if (
        not isinstance(cv, pd.DataFrame)
        or not required_cv.issubset(cv.columns)
        or not isinstance(fitted, pd.DataFrame)
        or not required_fitted.issubset(fitted.columns)
    ):
        raise M3Error("soc_anchor_unavailable", "SOC CV fitted values are unavailable")

    anchored = pd.to_numeric(cv[model_name], errors="coerce").copy()
    for cutoff, indices in cv.groupby("cutoff", sort=False).groups.items():
        cutoff_time = pd.Timestamp(cutoff)
        fitted_times = pd.to_datetime(fitted["ds"], errors="coerce")
        window = fitted[
            (fitted["cutoff"] == cutoff)
            & fitted_times.notna()
            & (fitted_times <= cutoff_time)
        ]
        offset = recent_soc_residual_offset(
            window, model_name, imputed_keys
        )
        anchored.loc[indices] = np.clip(anchored.loc[indices] + offset, 0.0, 100.0)
    return anchored
