"""Daily rolling of completed custom forecast configurations."""

from datetime import datetime, time, timedelta
from hashlib import sha256
from threading import Lock

from m3_worker.contracts import validate_shanghai_timestamp
from m3_worker.custom_forecast_contracts import (
    ALLOWED_INTERVAL_SECONDS,
    CustomForecastConfig,
    CustomForecastRequest,
)
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import (
    DAILY_REQUESTED_BY,
    CustomForecastRepository,
    StoredCustomRun,
)
from m3_worker.services.custom_forecast_service import CustomForecastService


DAILY_START = time(hour=0, minute=17)
MAX_DAILY_ATTEMPTS = 3
_COMPLETED_DAILY_STATUSES = frozenset({"succeeded", "evaluated"})


def _roll_request(
    config: CustomForecastConfig,
    day: datetime,
    key: str,
) -> CustomForecastRequest:
    return CustomForecastRequest(
        history_start=day - timedelta(days=config.history_days),
        history_end=day,
        forecast_days=config.forecast_days,
        interval_seconds=config.interval_seconds,
        idempotency_key=key,
    )


def _persisted_request(run: StoredCustomRun) -> CustomForecastRequest:
    return CustomForecastRequest(
        history_start=run.config.history_start,
        history_end=run.config.history_end,
        forecast_days=run.config.forecast_days,
        interval_seconds=run.config.interval_seconds,
        idempotency_key=run.record.idempotency_key,
    )


def _attempt_key(
    station_id: str,
    day: datetime,
    config: CustomForecastConfig,
    attempt: int,
) -> str:
    identity = (
        f"{station_id}|{day.date().isoformat()}|{config.interval_seconds}|"
        f"{config.history_days}|{config.forecast_days}"
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"m3-daily:{day:%Y%m%d}:{config.interval_seconds}:{digest}:{attempt}"


def _safe_error(error: Exception) -> M3Error:
    if isinstance(error, M3Error):
        return error
    return M3Error(
        "daily_custom_forecast_failed",
        "Daily custom forecast coordination failed",
    )


class DailyCustomForecastService:
    """Discover completed templates and submit one rolled run per interval."""

    def __init__(
        self,
        repository: CustomForecastRepository,
        forecast_service: CustomForecastService,
    ) -> None:
        self._repository = repository
        self._forecast_service = forecast_service
        self._completed_dates: set[tuple[str, datetime]] = set()
        self._station_date_locks: dict[tuple[str, datetime], Lock] = {}
        self._cache_lock = Lock()

    def _station_date_lock(self, key: tuple[str, datetime]) -> Lock:
        with self._cache_lock:
            return self._station_date_locks.setdefault(key, Lock())

    def _is_completed(self, key: tuple[str, datetime]) -> bool:
        with self._cache_lock:
            return key in self._completed_dates

    def _remember_completed(self, key: tuple[str, datetime]) -> None:
        with self._cache_lock:
            self._completed_dates.add(key)

    def run_station(self, station_id: str, now: datetime) -> int:
        validate_shanghai_timestamp(
            now, "daily custom forecast time", quarter_hour=False
        )
        if now.time() < DAILY_START:
            return 0
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cache_key = (station_id, day)
        if self._is_completed(cache_key):
            return 0

        station_date_lock = self._station_date_lock(cache_key)
        with station_date_lock:
            if self._is_completed(cache_key):
                return 0
            return self._run_uncached(station_id, day, cache_key)

    def _run_uncached(
        self,
        station_id: str,
        day: datetime,
        cache_key: tuple[str, datetime],
    ) -> int:
        errors: list[M3Error] = []
        templates: dict[int, StoredCustomRun] = {}
        for interval_seconds in ALLOWED_INTERVAL_SECONDS:
            try:
                template = self._repository.latest_completed_template(
                    station_id, interval_seconds=interval_seconds
                )
            except Exception as error:
                errors.append(_safe_error(error))
                continue
            if template is not None:
                templates[interval_seconds] = template

        if not templates:
            if errors:
                raise errors[0]
            return 0

        uncovered = {
            interval_seconds: template
            for interval_seconds, template in templates.items()
            if template.config.forecast_start < day
        }
        attempted = 0
        complete_intervals = set(templates) - set(uncovered)

        for interval_seconds, template in uncovered.items():
            try:
                matching = self._repository.list_daily_runs(
                    station_id,
                    forecast_start=day,
                    interval_seconds=interval_seconds,
                )
            except Exception as error:
                errors.append(_safe_error(error))
                continue
            terminal = [
                run for run in matching if run.status in _COMPLETED_DAILY_STATUSES
            ]
            if terminal:
                complete_intervals.add(interval_seconds)
                continue
            if any(run.status == "running" for run in matching):
                continue

            queued = [run for run in matching if run.status == "queued"]
            if queued:
                request = _persisted_request(queued[-1])
            else:
                failed = [run for run in matching if run.status == "failed"]
                if len(failed) >= MAX_DAILY_ATTEMPTS:
                    errors.append(
                        M3Error(
                            "daily_custom_forecast_failed",
                            "Daily custom forecast attempts are exhausted",
                        )
                    )
                    continue
                source_config = failed[0].config if failed else template.config
                attempt = len(failed) + 1
                request = _roll_request(
                    source_config,
                    day,
                    _attempt_key(station_id, day, source_config, attempt),
                )

            attempted += 1
            try:
                submitted = self._forecast_service.submit(
                    station_id,
                    request,
                    requested_by=DAILY_REQUESTED_BY,
                )
            except Exception as error:
                errors.append(_safe_error(error))
                continue
            if submitted.status in _COMPLETED_DAILY_STATUSES:
                complete_intervals.add(interval_seconds)

        if (
            not errors
            and set(templates) == set(ALLOWED_INTERVAL_SECONDS)
            and complete_intervals == set(templates)
        ):
            self._remember_completed(cache_key)
        if errors:
            raise errors[0]
        return attempted
