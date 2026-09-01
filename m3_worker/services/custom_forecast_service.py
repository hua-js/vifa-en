"""Bounded execution and recovery for persistent custom M3 forecasts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import re
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Callable

from m3_worker.contracts import SERIES_IDS
from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastRequest,
    CustomForecastSeries,
)
from m3_worker.domain.custom_forecasting import (
    CustomChampion,
    forecast_custom_series,
    interpolate_soc_forecast,
    seasonal_naive_champion,
    select_custom_champion,
    weekly_naive_champion,
)
from m3_worker.domain.custom_load_profiles import (
    LOAD_SELECTION_POLICY,
    LoadCandidateScore,
)
from m3_worker.domain.custom_training_data import (
    CustomWeekSummary,
    build_custom_training_dataset,
)
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)


LOGGER = logging.getLogger("m3_worker.custom_forecast")
SAFE_DIAGNOSTIC_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
DIAGNOSTIC_TEXT_FIELDS = ("collection", "action", "failure_type")
DIAGNOSTIC_INTEGER_FIELDS = (
    "batch_number",
    "batch_offset",
    "batch_size",
    "status_code",
    "elapsed_ms",
)
SAFE_LOAD_SKIP_REASONS = frozenset(
    {"no_scorable_points", "non_finite_prediction", "wape_unavailable"}
)
SAFE_REALIZED_FALLBACK_REASONS = SAFE_LOAD_SKIP_REASONS | {
    "latest_week_high_imputation"
}


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


def _diagnostic_details(error: BaseException) -> dict[str, str | int]:
    source = error.details if isinstance(error, M3Error) else {}
    details: dict[str, str | int] = {}
    for field in DIAGNOSTIC_TEXT_FIELDS:
        value = source.get(field)
        details[field] = (
            value
            if isinstance(value, str)
            and SAFE_DIAGNOSTIC_TEXT.fullmatch(value) is not None
            else "unknown"
        )
    for field in DIAGNOSTIC_INTEGER_FIELDS:
        value = source.get(field)
        details[field] = (
            value
            if type(value) is int and 0 <= value <= 86_400_000
            else -1
        )
    return details


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

    def latest(
        self,
        station_id: str,
        *,
        interval_seconds: int,
        forecast_days: int,
    ) -> StoredCustomRun | None:
        self._require_station(station_id)
        return self._repository.latest_usable(
            station_id,
            interval_seconds=interval_seconds,
            forecast_days=forecast_days,
            selection_policy=LOAD_SELECTION_POLICY,
        )

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

    def _history(
        self, run: StoredCustomRun, config: CustomForecastConfig | None = None
    ) -> list:
        selected_config = config or run.config
        points = []
        cursor = selected_config.history_start
        while cursor < selected_config.history_end:
            end = min(cursor + timedelta(days=7), selected_config.history_end)
            chunk = self._source.list_custom_observations(
                run.station_id,
                cursor,
                end,
                interval_seconds=selected_config.interval_seconds,
            )
            if type(chunk) is not list:
                raise M3Error(
                    "source_contract_invalid", "Custom source batch is invalid"
                )
            points.extend(chunk)
            cursor = end
        return points

    @staticmethod
    def _soc_model_config(config: CustomForecastConfig) -> CustomForecastConfig:
        if config.interval_seconds != 60:
            return config
        values = config.model_dump(mode="python")
        values.update(
            interval_seconds=300,
            points_per_day=288,
            expected_points_per_series=288 * config.forecast_days,
        )
        return CustomForecastConfig.model_validate(values)

    @staticmethod
    def _candidate_score_manifest(score: LoadCandidateScore) -> dict[str, object]:
        def number_or_none(value: object) -> int | float | None:
            if type(value) not in {int, float} or not math.isfinite(value):
                return None
            return value

        skip_reason = score.skip_reason
        if skip_reason is not None:
            if type(skip_reason) is str and skip_reason in SAFE_LOAD_SKIP_REASONS:
                pass
            elif (
                type(skip_reason) is str
                and skip_reason
                and skip_reason[0].isupper()
                and SAFE_DIAGNOSTIC_TEXT.fullmatch(skip_reason) is not None
            ):
                pass
            else:
                skip_reason = "unknown"
        return {
            "model_name": score.model_name,
            "wape_percent": number_or_none(score.wape_percent),
            "mae": number_or_none(score.mae),
            "mape_percent": number_or_none(score.mape_percent),
            "scorable_point_count": (
                score.scorable_point_count
                if type(score.scorable_point_count) is int
                and score.scorable_point_count >= 0
                else 0
            ),
            "skip_reason": skip_reason,
        }

    @staticmethod
    def _week_manifest(week: CustomWeekSummary) -> dict[str, object]:
        return {
            "start": week.start.isoformat(),
            "end": week.end.isoformat(),
            "point_count": week.point_count,
            "real_point_count": week.real_point_count,
            "imputed_point_count": week.imputed_point_count,
            "imputation_ratio": week.imputation_ratio,
        }

    @staticmethod
    def _safe_realized_fallback_reason(value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            return "unknown"
        if value in SAFE_REALIZED_FALLBACK_REASONS:
            return value
        if (
            value
            and value[0].isupper()
            and SAFE_DIAGNOSTIC_TEXT.fullmatch(value) is not None
        ):
            return value
        return "unknown"

    @classmethod
    def _load_champion_manifest(
        cls,
        champion: CustomChampion,
        realized: CustomForecastSeries,
    ) -> dict[str, object]:
        return {
            "model_name": champion.model_name,
            "selection_metric": champion.selection_metric,
            "selection_reason": champion.selection_reason,
            "selection_status": champion.selection_status,
            "candidate_scores": [
                cls._candidate_score_manifest(score)
                for score in champion.candidate_scores
            ],
            "realized_model_name": realized.model_name,
            "realized_status": realized.status,
            "realized_fallback_reason": cls._safe_realized_fallback_reason(
                realized.fallback_reason
            ),
            "training_start": champion.training_start.isoformat(),
            "training_end": champion.training_end.isoformat(),
            "statsforecast_version": champion.statsforecast_version,
        }

    @classmethod
    def _soc_champion_manifest(
        cls,
        champion: CustomChampion,
        realized: CustomForecastSeries,
        model_config: CustomForecastConfig,
        output_config: CustomForecastConfig,
    ) -> dict[str, object]:
        return {
            "model_name": champion.model_name,
            "realized_model_name": realized.model_name,
            "model_interval_seconds": model_config.interval_seconds,
            "output_interval_seconds": output_config.interval_seconds,
            "output_interpolation": (
                "linear"
                if model_config.interval_seconds != output_config.interval_seconds
                else None
            ),
            "cv_mape_percent": champion.cv_mape_percent,
            "selected_at": champion.selected_at.isoformat(),
            "selection_metric": champion.selection_metric,
            "candidate_scores": [
                cls._candidate_score_manifest(score)
                for score in champion.candidate_scores
            ],
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
            model_configs = {
                "station_total_load": run.config,
                "storage_soc": self._soc_model_config(run.config),
            }
            soc_requires_interpolation = (
                model_configs["storage_soc"].interval_seconds
                != run.config.interval_seconds
            )
            observations_by_series = {
                "station_total_load": observations,
                "storage_soc": (
                    self._history(run, model_configs["storage_soc"])
                    if soc_requires_interpolation
                    else observations
                ),
            }
            datasets = {
                unique_id: build_custom_training_dataset(
                    observations_by_series[unique_id],
                    unique_id,
                    model_configs[unique_id],
                )
                for unique_id in SERIES_IDS
            }
            champions: dict[str, CustomChampion] = {}
            for unique_id in SERIES_IDS:
                selection_started = monotonic()
                LOGGER.info(
                    "m3_custom_forecast_selection_started "
                    "run_id=%s station_id=%s series=%s",
                    run.run_id,
                    run.station_id,
                    unique_id,
                )
                try:
                    champions[unique_id] = select_custom_champion(
                        datasets[unique_id], model_configs[unique_id]
                    )
                except Exception as error:
                    LOGGER.warning(
                        "m3_custom_forecast_selection_finished "
                        "run_id=%s station_id=%s series=%s status=failed "
                        "error_type=%s elapsed_ms=%d",
                        run.run_id,
                        run.station_id,
                        unique_id,
                        type(error).__name__,
                        int((monotonic() - selection_started) * 1000),
                    )
                    raise
                LOGGER.info(
                    "m3_custom_forecast_selection_finished "
                    "run_id=%s station_id=%s series=%s status=ok "
                    "model=%s elapsed_ms=%d",
                    run.run_id,
                    run.station_id,
                    unique_id,
                    champions[unique_id].model_name,
                    int((monotonic() - selection_started) * 1000),
                )
            series = []
            for unique_id in SERIES_IDS:
                model_series = forecast_custom_series(
                    datasets[unique_id],
                    champions[unique_id],
                    model_configs[unique_id],
                )
                series.append(
                    interpolate_soc_forecast(
                        model_series, model_configs[unique_id], run.config
                    )
                    if unique_id == "storage_soc" and soc_requires_interpolation
                    else model_series
                )
            series_by_id = {item.unique_id: item for item in series}
            selected_load_champion = champions["station_total_load"]
            soc_baseline = forecast_custom_series(
                datasets["storage_soc"],
                seasonal_naive_champion(datasets["storage_soc"]),
                model_configs["storage_soc"],
            )
            if soc_requires_interpolation:
                soc_baseline = interpolate_soc_forecast(
                    soc_baseline, model_configs["storage_soc"], run.config
                )
            baseline_series = [
                forecast_custom_series(
                    datasets["station_total_load"],
                    (
                        selected_load_champion
                        if selected_load_champion.model_name == "WeeklyNaive"
                        else weekly_naive_champion(datasets["station_total_load"])
                    ),
                    run.config,
                ),
                soc_baseline,
            ]
            source_manifest = {
                "history_start": run.config.history_start.isoformat(),
                "history_end": run.config.history_end.isoformat(),
                "interval_seconds": run.config.interval_seconds,
                "observation_count": len(observations),
                "series": {
                    unique_id: {
                        "interval_seconds": datasets[unique_id].interval_seconds,
                        "source_available_start": datasets[
                            unique_id
                        ].source_available_start.isoformat(),
                        "training_start": datasets[unique_id].start.isoformat(),
                        "source_available_points": datasets[
                            unique_id
                        ].source_available_points,
                        "leading_no_data_points": datasets[
                            unique_id
                        ].leading_no_data_points,
                        "invalid_points": datasets[unique_id].invalid_points,
                        "negative_invalid_points": datasets[
                            unique_id
                        ].negative_invalid_points,
                        "retained_points": len(datasets[unique_id].frame),
                        "imputed_points": len(datasets[unique_id].imputed_keys),
                        "mode": datasets[unique_id].mode,
                        "usable_week_count": len(datasets[unique_id].usable_weeks),
                        "weeks": [
                            self._week_manifest(week)
                            for week in datasets[unique_id].usable_weeks
                        ],
                    }
                    for unique_id in SERIES_IDS
                },
            }
            model_manifest = {
                "selection_policy": LOAD_SELECTION_POLICY,
                "model_policy": run.config.model_policy,
                "interval_seconds": run.config.interval_seconds,
                "daily_season_length": run.config.daily_season_length,
                "weekly_season_length": run.config.weekly_season_length,
                "series": {
                    "station_total_load": self._load_champion_manifest(
                        champions["station_total_load"],
                        series_by_id["station_total_load"],
                    ),
                    "storage_soc": self._soc_champion_manifest(
                        champions["storage_soc"],
                        series_by_id["storage_soc"],
                        model_configs["storage_soc"],
                        run.config,
                    ),
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
            diagnostic = _diagnostic_details(error)
            LOGGER.error(
                "m3_custom_forecast_failed run_id=%s station_id=%s "
                "error_code=%s collection=%s action=%s "
                "batch_number=%s batch_offset=%s batch_size=%s "
                "status_code=%s failure_type=%s elapsed_ms=%s",
                run_id,
                run.station_id if run is not None else "unknown",
                _safe_error_code(error),
                diagnostic["collection"],
                diagnostic["action"],
                diagnostic["batch_number"],
                diagnostic["batch_offset"],
                diagnostic["batch_size"],
                diagnostic["status_code"],
                diagnostic["failure_type"],
                diagnostic["elapsed_ms"],
            )
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
