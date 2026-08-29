"""Bounded execution and recovery for persistent custom M3 forecasts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import BoundedSemaphore, Lock
from typing import Callable

from m3_worker.contracts import SERIES_IDS
from m3_worker.custom_forecast_contracts import CustomForecastRequest
from m3_worker.domain.custom_forecasting import (
    CustomChampion,
    forecast_custom_series,
    seasonal_naive_champion,
    select_custom_champion,
)
from m3_worker.domain.custom_training_data import build_custom_training_dataset
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, M3Error):
        code = error.code
        if (
            type(code) is str
            and 1 <= len(code) <= 64
            and code[0].islower()
            and all(
                character.islower()
                or character.isdigit()
                or character == "_"
                for character in code
            )
        ):
            return code
    return "internal_error"


class CustomForecastService:
    """Use a dedicated executor so large manual runs cannot block scheduled M3."""

    def __init__(
        self,
        repository: CustomForecastRepository,
        source: object,
        now: Callable[[], datetime],
        *,
        station_ids: tuple[str, ...],
        max_workers: int = 1,
        max_pending: int = 16,
    ) -> None:
        if (
            type(station_ids) is not tuple
            or not station_ids
            or any(type(item) is not str or not item for item in station_ids)
            or len(station_ids) != len(set(station_ids))
        ):
            raise ValueError("station_ids must be unique non-empty strings")
        if max_workers < 1 or max_pending < max_workers:
            raise ValueError("custom forecast executor bounds are invalid")
        self._repository = repository
        self._source = source
        self._now = now
        self._station_ids = frozenset(station_ids)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="m3-custom-forecast"
        )
        self._capacity = BoundedSemaphore(max_pending)
        self._active: set[str] = set()
        self._lock = Lock()
        self._closing = False

    def _require_station(self, station_id: str) -> None:
        if station_id not in self._station_ids:
            raise M3Error("station_not_configured", "Station is not configured")

    def submit(
        self,
        station_id: str,
        request: CustomForecastRequest,
        *,
        requested_by: str | None,
    ) -> StoredCustomRun:
        self._require_station(station_id)
        run = self._repository.create_or_get(
            station_id, request, requested_by=requested_by
        )
        if run.status in {"queued", "running"}:
            self._schedule(run)
        return run

    def _schedule(self, run: StoredCustomRun) -> None:
        with self._lock:
            if self._closing:
                raise M3Error(
                    "job_service_closed", "Custom forecast service is closed"
                )
            if run.run_id in self._active:
                return
            if not self._capacity.acquire(blocking=False):
                raise M3Error(
                    "job_capacity_exceeded",
                    "Custom forecast capacity is exhausted",
                )
            self._active.add(run.run_id)
            try:
                self._pool.submit(self._execute, run.run_id)
            except Exception as error:
                self._active.remove(run.run_id)
                self._capacity.release()
                raise M3Error(
                    "job_service_closed", "Custom forecast service is unavailable"
                ) from error

    def recover(self) -> int:
        runs = self._repository.list_recoverable()
        for run in runs:
            self._schedule(run)
        return len(runs)

    def get(self, run_id: str) -> StoredCustomRun | None:
        return self._repository.get_by_run_id(run_id)

    def result(
        self, run_id: str
    ) -> tuple[StoredCustomRun, list[dict[str, object]]] | None:
        run = self.get(run_id)
        if run is None:
            return None
        points = (
            self._repository.list_points(run)
            if run.status in {"succeeded", "evaluated"}
            else []
        )
        return run, points

    def _history(self, run: StoredCustomRun) -> list:
        points = []
        cursor = run.config.history_start
        while cursor < run.config.history_end:
            end = min(cursor + timedelta(days=7), run.config.history_end)
            chunk = self._source.list_custom_observations(
                run.station_id,
                cursor,
                end,
                interval_seconds=run.config.interval_seconds,
            )
            if type(chunk) is not list:
                raise M3Error(
                    "source_contract_invalid", "Custom source batch is invalid"
                )
            points.extend(chunk)
            cursor = end
        return points

    @staticmethod
    def _champion_manifest(champion: CustomChampion) -> dict[str, object]:
        return {
            "model_name": champion.model_name,
            "cv_mape_percent": champion.cv_mape_percent,
            "selected_at": champion.selected_at.isoformat(),
            "training_start": champion.training_start.isoformat(),
            "training_end": champion.training_end.isoformat(),
            "statsforecast_version": champion.statsforecast_version,
            "selection_reason": champion.selection_reason,
        }

    def _execute(self, run_id: str) -> None:
        run: StoredCustomRun | None = None
        points_persisted = False
        try:
            run = self._repository.get_by_run_id(run_id)
            if run is None or run.status not in {"queued", "running"}:
                return
            run = self._repository.transition(run, "running", at=self._now())
            observations = self._history(run)
            datasets = {
                unique_id: build_custom_training_dataset(
                    observations, unique_id, run.config
                )
                for unique_id in SERIES_IDS
            }
            champions = {
                unique_id: select_custom_champion(
                    datasets[unique_id], run.config
                )
                for unique_id in SERIES_IDS
            }
            series = [
                forecast_custom_series(
                    datasets[unique_id], champions[unique_id], run.config
                )
                for unique_id in SERIES_IDS
            ]
            series_by_id = {item.unique_id: item for item in series}
            baseline_series = [
                (
                    series_by_id[unique_id]
                    if champions[unique_id].model_name == "SeasonalNaive"
                    else forecast_custom_series(
                        datasets[unique_id],
                        seasonal_naive_champion(datasets[unique_id]),
                        run.config,
                    )
                )
                for unique_id in SERIES_IDS
            ]
            source_manifest = {
                "history_start": run.config.history_start.isoformat(),
                "history_end": run.config.history_end.isoformat(),
                "interval_seconds": run.config.interval_seconds,
                "observation_count": len(observations),
                "series": {
                    unique_id: {
                        "retained_points": len(datasets[unique_id].frame),
                        "imputed_points": len(datasets[unique_id].imputed_keys),
                        "mode": datasets[unique_id].mode,
                    }
                    for unique_id in SERIES_IDS
                },
            }
            model_manifest = {
                "model_policy": run.config.model_policy,
                "interval_seconds": run.config.interval_seconds,
                "daily_season_length": run.config.daily_season_length,
                "weekly_season_length": run.config.weekly_season_length,
                "series": {
                    unique_id: self._champion_manifest(champions[unique_id])
                    for unique_id in SERIES_IDS
                },
            }
            digest = self._repository.store_points(run, series, baseline_series)
            points_persisted = True
            self._repository.transition(
                run,
                "succeeded",
                at=self._now(),
                model_manifest=model_manifest,
                source_manifest=source_manifest,
                content_hash=digest,
            )
        except Exception as error:
            if (
                not points_persisted
                and run is not None
                and run.status in {"queued", "running"}
            ):
                try:
                    self._repository.transition(
                        run,
                        "failed",
                        at=self._now(),
                        error_code=_safe_error_code(error),
                    )
                except Exception:
                    pass
        finally:
            with self._lock:
                self._active.discard(run_id)
                self._capacity.release()

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        self._pool.shutdown(wait=True, cancel_futures=False)
