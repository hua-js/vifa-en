"""Station-scoped active pull, model selection, forecast, and publication flow."""

from dataclasses import replace
from datetime import datetime, timedelta
from importlib.metadata import version
import math
from pathlib import Path
import re
import tomllib

from pydantic import ValidationError

from m3_worker.contracts import (
    SERIES_IDS,
    ForecastSeries,
    LatestSnapshot,
    SeriesId,
    validate_shanghai_timestamp,
)
from m3_worker.domain.forecasting import (
    Champion,
    MODEL_NAMES,
    seasonal_naive_champion,
    select_champion,
)
from m3_worker.domain.training_data import (
    OPERATIONAL_HISTORY_DAYS,
    READY_HISTORY_DAYS,
    READY_REQUIRED_POINTS,
    TrainingDataset,
    build_training_dataset,
)
from m3_worker.errors import M3Error
from m3_worker.services.station_cache import StationCache, StationState
from m3_worker.sinks.forecast_sink import canonical_hash


SERIES_SET = frozenset(SERIES_IDS)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXACT_STATSFORECAST = re.compile(
    r"statsforecast==([0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?)\Z"
)
LOCKED_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?\Z"
)
PERSISTED_CHAMPION_FIELDS = {
    "model_name",
    "cv_mape_percent",
    "selected_at",
    "training_start",
    "training_end",
    "selection_reason",
}
PERSISTED_MANIFEST_FIELDS = {
    frozenset({"statsforecast_version", "series"}),
    frozenset({"statsforecast_version", "series", "readiness"}),
}


def project_statsforecast_version(project_root: Path = PROJECT_ROOT) -> str:
    """Return one exact StatsForecast version shared by project and uv lock."""

    try:
        with (project_root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
        dependencies = project["project"]["dependencies"]
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise ValueError("invalid project dependencies")
        statsforecast_dependencies = [
            item for item in dependencies if item.lower().startswith("statsforecast")
        ]
        pins = [
            match.group(1)
            for item in statsforecast_dependencies
            if (match := EXACT_STATSFORECAST.fullmatch(item)) is not None
        ]
        with (project_root / "uv.lock").open("rb") as stream:
            lock = tomllib.load(stream)
        packages = lock.get("package")
        if not isinstance(packages, list):
            raise ValueError("invalid lock packages")
        locked = [
            item.get("version")
            for item in packages
            if isinstance(item, dict) and item.get("name") == "statsforecast"
        ]
        if (
            len(statsforecast_dependencies) != 1
            or len(pins) != 1
            or len(locked) != 1
            or not isinstance(locked[0], str)
            or LOCKED_VERSION.fullmatch(locked[0]) is None
            or pins[0] != locked[0]
        ):
            raise ValueError("StatsForecast pin and lock do not match")
        return pins[0]
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise M3Error(
            "model_version_invalid",
            "StatsForecast project pin and lock must match exactly",
        ) from error


def verify_statsforecast_runtime(project_root: Path = PROJECT_ROOT) -> str:
    expected = project_statsforecast_version(project_root)
    try:
        runtime = version("statsforecast")
    except Exception as error:
        raise M3Error(
            "model_version_invalid", "StatsForecast runtime version is unavailable"
        ) from error
    if runtime != expected:
        raise M3Error(
            "model_version_invalid",
            "StatsForecast runtime does not match the project lock",
        )
    return expected


def _validate_champion_versions(
    champions: dict[str, Champion | None],
    required_version: str,
    *,
    allow_none: bool,
) -> None:
    if set(champions) != SERIES_SET or any(
        champion is None and not allow_none
        or champion is not None
        and (
            not isinstance(champion, Champion)
            or champion.statsforecast_version != required_version
        )
        for champion in champions.values()
    ):
        raise M3Error(
            "model_version_invalid",
            "Every champion must match the locked StatsForecast version",
        )


def _safe_time(value: datetime, name: str, *, quarter_hour: bool) -> None:
    try:
        validate_shanghai_timestamp(value, name, quarter_hour=quarter_hour)
    except (TypeError, ValueError) as error:
        raise M3Error(
            "time_contract_invalid", f"{name} must be an explicit Asia/Shanghai time"
        ) from error


def _safe_error(error: Exception, code: str, message: str) -> M3Error:
    if isinstance(error, M3Error):
        return error
    return M3Error(code, message)


def _persisted_time(value: object, field_name: str, *, quarter_hour: bool) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        validate_shanghai_timestamp(
            parsed, field_name, quarter_hour=quarter_hour
        )
    except ValueError as error:
        raise ValueError(f"{field_name} is invalid") from error
    return parsed


def _persisted_champions(
    manifest: object, required_version: str
) -> dict[str, Champion | None] | None:
    try:
        if (
            type(manifest) is not dict
            or frozenset(manifest) not in PERSISTED_MANIFEST_FIELDS
            or manifest["statsforecast_version"] != required_version
        ):
            return None
        series = manifest["series"]
        if type(series) is not dict or set(series) != SERIES_SET:
            return None
        champions: dict[str, Champion | None] = {}
        for unique_id in SERIES_IDS:
            item = series[unique_id]
            if type(item) is not dict:
                return None
            if item.get("model_name") is None:
                if set(item) != {"model_name", "reason"} or item.get(
                    "reason"
                ) != "insufficient_history":
                    return None
                champions[unique_id] = None
                continue
            if set(item) != PERSISTED_CHAMPION_FIELDS:
                return None
            model_name = item["model_name"]
            metric = item["cv_mape_percent"]
            reason = item["selection_reason"]
            if (
                type(model_name) is not str
                or model_name not in MODEL_NAMES
                or metric is not None
                and (
                    type(metric) not in {int, float}
                    or not math.isfinite(metric)
                    or metric < 0
                )
                or reason is not None
                and (
                    type(reason) is not str
                    or not reason
                    or reason != reason.strip()
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in reason
                    )
                )
            ):
                return None
            selected_at = _persisted_time(
                item["selected_at"], "selected_at", quarter_hour=False
            )
            training_start = _persisted_time(
                item["training_start"], "training_start", quarter_hour=True
            )
            training_end = _persisted_time(
                item["training_end"], "training_end", quarter_hour=True
            )
            if training_end < training_start:
                return None
            champions[unique_id] = Champion(
                model_name=model_name,
                cv_mape_percent=float(metric) if metric is not None else None,
                selected_at=selected_at,
                training_start=training_start,
                training_end=training_end,
                statsforecast_version=required_version,
                selection_reason=reason,
            )
        return champions
    except (KeyError, TypeError, ValueError):
        return None


def _champion_manifest(champion: Champion | None) -> dict[str, object]:
    if champion is None:
        return {"model_name": None, "reason": "insufficient_history"}
    return {
        "model_name": champion.model_name,
        "cv_mape_percent": champion.cv_mape_percent,
        "selected_at": champion.selected_at.isoformat(),
        "training_start": champion.training_start.isoformat(),
        "training_end": champion.training_end.isoformat(),
        "selection_reason": champion.selection_reason,
    }


def _readiness_manifest(
    datasets: dict[SeriesId, TrainingDataset],
) -> dict[str, object]:
    if set(datasets) != SERIES_SET:
        raise M3Error(
            "forecast_incomplete", "Readiness requires both training datasets"
        )
    return {
        "required_days": READY_HISTORY_DAYS,
        "required_points": READY_REQUIRED_POINTS,
        "series": {
            unique_id: {
                "real_points": min(
                    len(datasets[unique_id].frame)
                    - len(datasets[unique_id].imputed_keys),
                    READY_REQUIRED_POINTS,
                )
            }
            for unique_id in SERIES_IDS
        },
    }


def build_latest_snapshot(
    station_id: str,
    as_of: datetime,
    generated_at: datetime,
    source_data_end: datetime,
    series: list[ForecastSeries],
    champions: dict[str, Champion | None],
    *,
    readiness: dict[str, object] | None = None,
) -> LatestSnapshot:
    """Build one canonical typed terminal snapshot and verify its digest."""

    _safe_time(as_of, "as_of", quarter_hour=False)
    _safe_time(generated_at, "generated_at", quarter_hour=False)
    _safe_time(source_data_end, "source_data_end", quarter_hour=True)
    required_version = verify_statsforecast_runtime()
    _validate_champion_versions(champions, required_version, allow_none=True)
    if not isinstance(series, list):
        raise M3Error("forecast_incomplete", "Forecast series payload is invalid")
    try:
        typed_series = [ForecastSeries.model_validate(item) for item in series]
    except (ValidationError, TypeError, ValueError) as error:
        raise M3Error(
            "forecast_incomplete", "Forecast series payload is invalid"
        ) from error
    by_id = {item.unique_id: item for item in typed_series}
    if len(typed_series) != len(SERIES_IDS) or set(by_id) != SERIES_SET:
        raise M3Error(
            "forecast_incomplete",
            "Latest snapshot requires load and SOC exactly once",
        )

    statuses = {item.status for item in typed_series}
    if statuses & {"degraded", "insufficient_history", "error"}:
        status = "degraded"
    elif "warming_up" in statuses:
        status = "warming_up"
    elif statuses == {"ok"}:
        status = "ok"
    else:
        raise M3Error("forecast_status_invalid", "Forecast status set is invalid")

    manifest = {
        "statsforecast_version": required_version,
        "series": {
            unique_id: _champion_manifest(champions[unique_id])
            for unique_id in SERIES_IDS
        },
    }
    if readiness is not None:
        manifest["readiness"] = readiness
    body = {
        "station_id": station_id,
        "as_of": as_of,
        "generated_at": generated_at,
        "source_data_end": source_data_end,
        "status": status,
        "series": [by_id[unique_id] for unique_id in SERIES_IDS],
        "model_manifest": manifest,
    }
    digest = canonical_hash(body)
    try:
        snapshot = LatestSnapshot(**body, content_hash=digest)
    except (ValidationError, TypeError, ValueError) as error:
        raise M3Error(
            "forecast_incomplete", "Latest forecast snapshot is invalid"
        ) from error
    if canonical_hash(snapshot.model_dump(exclude={"content_hash"})) != snapshot.content_hash:
        raise M3Error("forecast_hash_invalid", "Latest forecast hash verification failed")
    return snapshot


class ForecastService:
    def __init__(self, source, sink, caches, forecast_one, now):
        if not isinstance(caches, dict) or any(
            not isinstance(key, str)
            or not isinstance(cache, StationCache)
            or cache.station_id != key
            for key, cache in caches.items()
        ):
            raise ValueError("caches must map station IDs to matching StationCache objects")
        self._source = source
        self._sink = sink
        self._caches: dict[str, StationCache] = dict(caches)
        self._forecast_one = forecast_one
        self._now = now
        self._statsforecast_version = verify_statsforecast_runtime()
        self._pending: dict[str, LatestSnapshot] = {}
        self._completed: dict[str, LatestSnapshot] = {}

    def _require_runtime(self, cache: StationCache | None = None) -> str:
        try:
            required = verify_statsforecast_runtime()
            if required != self._statsforecast_version:
                raise M3Error(
                    "model_version_invalid",
                    "StatsForecast project version changed after service startup",
                )
            return required
        except M3Error as error:
            if cache is not None:
                with cache.exclusive():
                    cache.state.state = "initializing"
                    self._record_failure(cache, error)
            raise

    def _cache(self, station_id: str) -> StationCache:
        cache = self._caches.get(station_id)
        if cache is None:
            raise M3Error("station_not_configured", "Station is not configured")
        return cache

    def state(self, station_id: str) -> StationState:
        return self._cache(station_id).state_snapshot()

    @staticmethod
    def _record_failure(cache: StationCache, error: M3Error) -> None:
        cache.state.last_error_code = error.code
        if error.code == "model_version_invalid":
            cache.state.state = "initializing"

    def bootstrap(self, station_id: str, as_of: datetime) -> None:
        cache = self._cache(station_id)
        _safe_time(as_of, "as_of", quarter_hour=True)
        self._require_runtime(cache)
        with cache.exclusive():
            points = []
            cursor = as_of - timedelta(days=90)
            try:
                while cursor < as_of:
                    end = min(cursor + timedelta(days=7), as_of)
                    chunk = self._source.list_observations(station_id, cursor, end)
                    if not isinstance(chunk, list):
                        raise M3Error(
                            "source_contract_invalid", "Source API returned an invalid batch"
                        )
                    points.extend(chunk)
                    cursor = end
                cache.replace(points)
            except Exception as cause:
                error = _safe_error(
                    cause, "source_pull_failed", "Station history pull failed"
                )
                self._record_failure(cache, error)
                raise error from (cause if cause is not error else None)
            cache.state.last_source_at = as_of
            cache.state.last_error_code = None

    def _datasets(self, cache: StationCache) -> dict[SeriesId, TrainingDataset]:
        datasets: dict[SeriesId, TrainingDataset] = {}
        for unique_id in SERIES_IDS:
            try:
                datasets[unique_id] = build_training_dataset(
                    cache.window(unique_id),
                    unique_id,
                    history_days=OPERATIONAL_HISTORY_DAYS,
                )
            except Exception as cause:
                raise _safe_error(
                    cause,
                    "training_data_invalid",
                    "Station training history is unavailable",
                ) from cause
        return datasets

    def restore_models(self, station_id: str) -> bool:
        cache = self._cache(station_id)
        required_version = self._require_runtime(cache)
        loader = getattr(self._sink, "load_latest_model_manifest", None)
        if not callable(loader):
            return False
        try:
            manifest = loader(station_id)
        except M3Error as error:
            if error.code == "sink_contract_invalid":
                return False
            raise
        champions = _persisted_champions(manifest, required_version)
        if champions is None:
            return False
        with cache.exclusive():
            if cache.state.last_source_at is None or any(
                champion is not None
                and champion.training_end > cache.state.last_source_at
                for champion in champions.values()
            ):
                return False
            cache.state.champions = champions
            if any(champion is None for champion in champions.values()):
                cache.state.state = "degraded"
                cache.state.last_error_code = "insufficient_history"
            elif any(
                champion.selection_reason is not None
                for champion in champions.values()
                if champion is not None
            ):
                cache.state.state = "degraded"
                cache.state.last_error_code = "model_selection_failed"
            else:
                cache.state.state = "ready"
                cache.state.last_error_code = None
        return True

    def select_models(self, station_id: str) -> None:
        cache = self._cache(station_id)
        with cache.selection_exclusive():
            required_version = self._require_runtime(cache)
            with cache.exclusive():
                previous = dict(cache.state.champions)
                has_previous = any(
                    isinstance(champion, Champion)
                    for champion in previous.values()
                )
                try:
                    datasets = self._datasets(cache)
                except Exception as cause:
                    error = _safe_error(
                        cause,
                        "model_selection_failed",
                        "Station model selection failed",
                    )
                    cache.state.state = (
                        "degraded" if has_previous else "initializing"
                    )
                    self._record_failure(cache, error)
                    raise error from (cause if cause is not error else None)
            try:
                champions: dict[str, Champion | None] = {}
                degraded = False
                unavailable = False
                for unique_id in SERIES_IDS:
                    dataset = datasets[unique_id]
                    try:
                        champions[unique_id] = select_champion(dataset)
                    except M3Error as cause:
                        if cause.code == "insufficient_history":
                            champions[unique_id] = None
                            unavailable = True
                            continue
                        if cause.code != "model_selection_failed":
                            raise
                        prior = previous.get(unique_id)
                        if prior is not None:
                            champions[unique_id] = replace(
                                prior, selection_reason="model_selection_failed"
                            )
                        else:
                            champions[unique_id] = seasonal_naive_champion(
                                dataset, "model_selection_failed"
                            )
                        degraded = True
                required_version = self._require_runtime(cache)
                _validate_champion_versions(
                    champions, required_version, allow_none=True
                )
            except Exception as cause:
                error = _safe_error(
                    cause, "model_selection_failed", "Station model selection failed"
                )
                with cache.exclusive():
                    cache.state.champions = previous
                    cache.state.state = (
                        "degraded" if has_previous else "initializing"
                    )
                    self._record_failure(cache, error)
                raise error from (cause if cause is not error else None)
            with cache.exclusive():
                cache.state.champions = champions
                if unavailable:
                    cache.state.state = "degraded"
                    cache.state.last_error_code = "insufficient_history"
                elif degraded:
                    cache.state.state = "degraded"
                    cache.state.last_error_code = "model_selection_failed"
                else:
                    cache.state.state = "ready"
                    cache.state.last_error_code = None

    def run_forecast(self, station_id: str, as_of: datetime) -> LatestSnapshot:
        cache = self._cache(station_id)
        _safe_time(as_of, "as_of", quarter_hour=False)
        required_version = self._require_runtime(cache)
        with cache.exclusive():
            if cache.state.state not in {"ready", "degraded"}:
                error = M3Error(
                    "station_not_ready", "Station forecast state is unavailable"
                )
                self._record_failure(cache, error)
                raise error
            if set(cache.state.champions) != SERIES_SET or any(
                champion is not None and not isinstance(champion, Champion)
                for champion in cache.state.champions.values()
            ):
                error = M3Error(
                    "station_not_ready", "Station champions are unavailable"
                )
                self._record_failure(cache, error)
                raise error
            try:
                _validate_champion_versions(
                    cache.state.champions,
                    required_version,
                    allow_none=True,
                )
            except M3Error as error:
                self._record_failure(cache, error)
                raise error

            pending = self._pending.get(station_id)
            if pending is not None and pending.as_of != as_of:
                error = M3Error(
                    "publication_pending",
                    "Another forecast publication is pending for this station",
                )
                self._record_failure(cache, error)
                raise error
            completed = self._completed.get(station_id)
            if completed is not None:
                if completed.as_of == as_of:
                    return completed
                if as_of < completed.as_of:
                    error = M3Error(
                        "forecast_superseded",
                        "Forecast as_of is older than the retained station result",
                    )
                    self._record_failure(cache, error)
                    raise error
            snapshot = pending
            if snapshot is None:
                completed_end = as_of.replace(
                    minute=(as_of.minute // 15) * 15,
                    second=0,
                    microsecond=0,
                )
                overlap_start = completed_end - timedelta(minutes=45)
                try:
                    pulled = self._source.list_observations(
                        station_id, overlap_start, completed_end
                    )
                    if not isinstance(pulled, list):
                        raise M3Error(
                            "source_contract_invalid", "Source API returned an invalid batch"
                        )
                    cache.merge(pulled)
                except Exception as cause:
                    error = _safe_error(
                        cause, "source_pull_failed", "Station overlap pull failed"
                    )
                    self._record_failure(cache, error)
                    raise error from (cause if cause is not error else None)
                cache.state.last_source_at = completed_end

                try:
                    datasets = self._datasets(cache)
                    if any(
                        datasets[unique_id].mode != "insufficient"
                        and cache.state.champions[unique_id] is None
                        for unique_id in SERIES_IDS
                    ):
                        raise M3Error(
                            "forecast_unavailable",
                            "Station champion is unavailable for usable history",
                        )
                    forecast_series = [
                        self._forecast_one(
                            datasets[unique_id],
                            cache.state.champions[unique_id],
                            as_of,
                        )
                        for unique_id in SERIES_IDS
                    ]
                    generated_at = self._now()
                    _safe_time(generated_at, "generated_at", quarter_hour=False)
                    snapshot = build_latest_snapshot(
                        station_id,
                        as_of,
                        generated_at,
                        completed_end,
                        forecast_series,
                        cache.state.champions,
                        readiness=_readiness_manifest(datasets),
                    )
                except Exception as cause:
                    error = _safe_error(
                        cause, "forecast_failed", "Station forecast generation failed"
                    )
                    self._record_failure(cache, error)
                    raise error from (cause if cause is not error else None)
                self._pending[station_id] = snapshot

            try:
                self._sink.publish_latest(snapshot)
            except Exception as cause:
                error = _safe_error(
                    cause, "sink_http_failed", "Forecast publication failed"
                )
                self._record_failure(cache, error)
                raise error from (cause if cause is not error else None)

            cache.state.last_published_at = snapshot.generated_at
            if snapshot.status == "degraded":
                cache.state.state = "degraded"
                if cache.state.last_error_code == "sink_http_failed":
                    cache.state.last_error_code = "forecast_degraded"
            else:
                cache.state.state = "ready"
                cache.state.last_error_code = None
            self._completed[station_id] = snapshot
            self._pending.pop(station_id, None)
            return snapshot
