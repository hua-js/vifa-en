"""Shared formal acceptance-batch window invariants."""

from datetime import datetime, timedelta


FORMAL_BATCH_DURATION = timedelta(days=1)
FORMAL_ISSUE_DELAY = timedelta(minutes=2)


def is_valid_formal_batch_window(
    *,
    issued_at: datetime,
    forecast_start: datetime,
    forecast_end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Return whether one batch occupies an exact formal day in the run window."""

    offset = forecast_start - window_start
    return (
        offset >= timedelta(0)
        and offset % FORMAL_BATCH_DURATION == timedelta(0)
        and forecast_end == forecast_start + FORMAL_BATCH_DURATION
        and issued_at == forecast_start + FORMAL_ISSUE_DELAY
        and forecast_end <= window_end
    )
