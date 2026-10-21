"""Runtime allowlist for custom forecast generation; saved results stay readable."""

import os

from m3.worker.custom_forecast_contracts import ALLOWED_INTERVAL_SECONDS


def enabled_forecast_intervals() -> tuple[int, ...]:
    raw = os.environ.get("M3_CUSTOM_FORECAST_INTERVALS")
    if raw is None:
        return ALLOWED_INTERVAL_SECONDS
    try:
        values = tuple(int(item.strip()) for item in raw.split(","))
    except ValueError:
        raise ValueError("M3_CUSTOM_FORECAST_INTERVALS must contain interval seconds") from None
    if (
        not values
        or len(values) != len(set(values))
        or any(value not in ALLOWED_INTERVAL_SECONDS for value in values)
    ):
        raise ValueError("M3_CUSTOM_FORECAST_INTERVALS contains unsupported or duplicate intervals")
    return tuple(value for value in ALLOWED_INTERVAL_SECONDS if value in values)
