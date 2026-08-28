"""Independent per-station forecasting, publication, and dashboard caching."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
from threading import Condition, Event, Lock, Thread, current_thread, get_ident
from typing import Callable, Protocol

from pydantic import ValidationError

from m3_worker.clients.raw_energy_api import RawEnergySourceClient
from m3_worker.config import StationBinding
from m3_worker.contracts import (
    SERIES_IDS,
    ForecastSeries,
    ObservationPoint,
    SeriesId,
    is_load_series,
    validate_shanghai_timestamp,
)
from m3_worker.dashboard_contracts import (
    DashboardAcceptance,
    DashboardAcceptanceResult,
    DashboardActualPoint,
    DashboardAggregateSystem,
    DashboardData,
    DashboardEnvelope,
    DashboardForecastPoint,
    DashboardRange,
    DashboardReadiness,
    DashboardSeries,
    DashboardStation,
    DashboardStationSystem,
)
from m3_worker.domain.evaluation import evaluate_series, overall_outcome
from m3_worker.domain.forecasting import (
    Champion,
    forecast_one_safe,
    seasonal_naive_champion,
    select_champion,
)
from m3_worker.domain.training_data import (
    OPERATIONAL_HISTORY_DAYS,
    TrainingDataset,
    build_training_dataset,
)
from m3_worker.errors import M3Error


LOGGER = logging.getLogger("m3_worker.live_dashboard")
STALE_AFTER = timedelta(minutes=30)
SAFE_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _validate_public_bindings(
    bindings: tuple[StationBinding, StationBinding],
) -> None:
    if [item.station_key for item in bindings] != ["station_1", "station_2"]:
        raise ValueError("dashboard requires ordered station bindings")
    internal_ids = tuple(item.station_id for item in bindings)
    if any(
        station_id in binding.station_name
        for binding in bindings
        for station_id in internal_ids
    ):
        raise ValueError("public station names must not contain internal identities")


def floor_quarter_hour(value: datetime) -> datetime:
    validate_shanghai_timestamp(value, "latest source timestamp", quarter_hour=False)
    return value.replace(
        minute=(value.minute // 15) * 15,
        second=0,
        microsecond=0,
    )


def forecast_station(
    actual: list[ObservationPoint],
    as_of: datetime,
    *,
    champions: dict[SeriesId, Champion] | None = None,
) -> list[ForecastSeries]:
    forecasts: list[ForecastSeries] = []
    for unique_id in SERIES_IDS:
        try:
            dataset = build_training_dataset(
                actual,
                unique_id,
                history_days=OPERATIONAL_HISTORY_DAYS,
            )
        except ValueError:
            has_valid_history = any(
                point.unique_id == unique_id and point.quality == "valid"
                for point in actual
            )
            forecasts.append(ForecastSeries(
                unique_id=unique_id,
                unit="kW" if unique_id == "station_total_load" else "%",
                model_name="none",
                status="error" if has_valid_history else "insufficient_history",
                points=[],
                fallback_reason=(
                    "training_data_invalid"
                    if has_valid_history
                    else "history_under_7_days"
                ),
            ))
            continue
        except M3Error as error:
            insufficient = error.code == "insufficient_history"
            forecasts.append(ForecastSeries(
                unique_id=unique_id,
                unit="kW" if unique_id == "station_total_load" else "%",
                model_name="none",
                status="insufficient_history" if insufficient else "error",
                points=[],
                fallback_reason=(
                    "history_under_7_days" if insufficient else "training_data_failed"
                ),
            ))
            continue
        if champions is not None:
            champion = champions.get(unique_id)
        else:
            try:
                champion = select_champion(dataset)
            except M3Error as error:
                if error.code != "insufficient_history":
                    champion = seasonal_naive_champion(
                        dataset, "model_selection_failed"
                    )
                else:
                    champion = None
        forecasts.append(forecast_one_safe(dataset, champion, as_of))
    return forecasts


BACKTEST_DAYS = 7
BACKTEST_POINTS = BACKTEST_DAYS * 96
BACKTEST_MINIMUM_VALID = 605


def build_historical_backtest(
    actual: list[ObservationPoint],
    as_of: datetime,
    *,
    forecast_at: Callable[
        [list[ObservationPoint], datetime], list[ForecastSeries]
    ] | None = None,
    champion_selector: Callable[[TrainingDataset], Champion] = select_champion,
) -> DashboardAcceptance:
    """Evaluate seven completed rolling days without future-data leakage."""
    validate_shanghai_timestamp(as_of, "backtest as_of", quarter_hour=True)
    actual_by_key: dict[tuple[SeriesId, datetime], ObservationPoint] = {}
    for point in actual:
        key = (point.unique_id, point.ds)
        if key in actual_by_key:
            raise ValueError("backtest observations contain duplicate series timestamps")
        actual_by_key[key] = point

    aligned = {
        unique_id: {"actual": [], "forecast": [], "quality": []}
        for unique_id in SERIES_IDS
    }
    if forecast_at is None:
        selection_cutoff = as_of - timedelta(days=BACKTEST_DAYS)
        selection_training = [
            point for point in actual if point.ds < selection_cutoff
        ]
        champions: dict[SeriesId, Champion] = {}
        for unique_id in SERIES_IDS:
            try:
                dataset = build_training_dataset(
                    selection_training,
                    unique_id,
                    history_days=OPERATIONAL_HISTORY_DAYS,
                )
            except (M3Error, ValueError):
                continue
            try:
                champions[unique_id] = champion_selector(dataset)
            except M3Error as error:
                if error.code != "insufficient_history":
                    champions[unique_id] = seasonal_naive_champion(
                        dataset, "model_selection_failed"
                    )

        def fixed_champion_forecast(
            training: list[ObservationPoint], cutoff: datetime
        ) -> list[ForecastSeries]:
            return forecast_station(training, cutoff, champions=champions)

        forecast_at = fixed_champion_forecast

    for days_before in range(BACKTEST_DAYS, 0, -1):
        cutoff = as_of - timedelta(days=days_before)
        training = [point for point in actual if point.ds < cutoff]
        try:
            forecast_series = forecast_at(training, cutoff)
        except Exception:
            forecast_series = []
        forecast_by_id = {
            series.unique_id: series
            for series in forecast_series
            if series.status in {"ok", "warming_up", "degraded"}
            and len(series.points) == 96
        }
        for unique_id in SERIES_IDS:
            series = forecast_by_id.get(unique_id)
            forecast_by_time = (
                {point.data_time: point.forecast_value for point in series.points}
                if series is not None
                else {}
            )
            for step in range(96):
                data_time = cutoff + timedelta(minutes=15 * step)
                observation = actual_by_key.get((unique_id, data_time))
                aligned[unique_id]["actual"].append(
                    observation.y if observation is not None else None
                )
                aligned[unique_id]["quality"].append(
                    observation.quality if observation is not None else "invalid"
                )
                aligned[unique_id]["forecast"].append(
                    forecast_by_time.get(data_time)
                )

    metrics = []
    results = []
    for unique_id in SERIES_IDS:
        values = aligned[unique_id]
        metric = evaluate_series(
            values["actual"],
            values["forecast"],
            values["quality"],
            minimum_valid=BACKTEST_MINIMUM_VALID,
        )
        if metric.expected_count != BACKTEST_POINTS:
            raise ValueError("backtest must evaluate exactly 672 points per series")
        metrics.append(metric)
        results.append(DashboardAcceptanceResult(
            unique_id=unique_id,
            expected_count=BACKTEST_POINTS,
            valid_count=metric.valid_count,
            zero_actual_count=metric.zero_actual_count,
            mape_percent=metric.mape_percent,
            mae=metric.mae,
            smape_percent=metric.smape_percent,
            wape_percent=metric.wape_percent,
            median_ape_percent=metric.median_ape_percent,
            p90_ape_percent=metric.p90_ape_percent,
            outcome=metric.outcome,
        ))

    return DashboardAcceptance(
        acceptance_run_id=f"historical-backtest-{as_of:%Y%m%d-%H%M}",
        status=overall_outcome(metrics),
        completed_days=BACKTEST_DAYS,
        results=results,
    )


@dataclass(frozen=True)
class DashboardStationResult:
    binding: StationBinding
    as_of: datetime
    generated_at: datetime
    actual: list[ObservationPoint]
    forecasts: list[ForecastSeries]
    acceptance: DashboardAcceptance | None = None
    readiness: DashboardReadiness | None = None
    degraded: bool = False


@dataclass(frozen=True)
class _PublicActualProjection:
    points: list[DashboardActualPoint]
    valid: bool


class LiveDashboardProvider:
    def __init__(
        self,
        source: RawEnergySourceClient,
        *,
        clock: Callable[[], datetime],
        history_days: int = 90,
        backtest_builder: Callable[
            [list[ObservationPoint], datetime], DashboardAcceptance
        ] = build_historical_backtest,
    ) -> None:
        if type(history_days) is not int or not 7 <= history_days <= 90:
            raise ValueError("history_days must be an integer in 7..90")
        self._source = source
        self._clock = clock
        self._history_days = history_days
        self._backtest_builder = backtest_builder
        self._backtest_cache: dict[
            tuple[str, datetime], DashboardAcceptance
        ] = {}

    def build_station(self, binding: StationBinding) -> DashboardStationResult:
        latest = self._source.latest_timestamp(binding.station_id)
        as_of = floor_quarter_hour(latest)
        actual = self._source.list_observations(
            binding.station_id,
            as_of - timedelta(days=self._history_days),
            as_of,
        )
        forecasts = forecast_station(actual, as_of)
        backtest_as_of = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        backtest_key = (binding.station_id, backtest_as_of)
        acceptance = self._backtest_cache.get(backtest_key)
        if acceptance is None:
            acceptance = self._backtest_builder(actual, backtest_as_of)
            self._backtest_cache[backtest_key] = acceptance
        generated_at = self._clock().replace(microsecond=0)
        validate_shanghai_timestamp(
            generated_at, "dashboard generation time", quarter_hour=False
        )
        return DashboardStationResult(
            binding, as_of, generated_at, actual, forecasts, acceptance
        )


def _public_series(
    actual: _PublicActualProjection, forecast: ForecastSeries
) -> DashboardSeries:
    if not actual.valid:
        return DashboardSeries(
            unique_id=forecast.unique_id,
            unit=forecast.unit,
            model_name=None,
            status="error",
            fallback_reason="actual_data_invalid",
            actual=[],
            forecast=[],
        )
    model_name = None if forecast.model_name == "none" else forecast.model_name
    return DashboardSeries(
        unique_id=forecast.unique_id,
        unit=forecast.unit,
        model_name=model_name,
        status=forecast.status,
        fallback_reason=forecast.fallback_reason,
        actual=actual.points,
        forecast=[
            DashboardForecastPoint(
                data_time=point.data_time,
                target_time=point.target_time,
                value=point.forecast_value,
                raw_value=point.raw_forecast,
                is_clipped=point.is_clipped,
            )
            for point in forecast.points
        ],
    )


def _validated_public_actual(
    actual: list[ObservationPoint],
    unique_id: SeriesId,
    history_start: datetime,
    history_end: datetime,
) -> _PublicActualProjection:
    selected: list[tuple[ObservationPoint, datetime]] = []
    for point in actual:
        if point.unique_id != unique_id:
            continue
        try:
            data_time = point.ds
        except AttributeError:
            return _PublicActualProjection([], False)
        if type(data_time) is not datetime:
            return _PublicActualProjection([], False)
        try:
            validate_shanghai_timestamp(
                data_time, "actual data_time", quarter_hour=True
            )
            in_public_window = history_start <= data_time < history_end
        except (AttributeError, TypeError, ValueError):
            return _PublicActualProjection([], False)
        if in_public_window:
            selected.append((point, data_time))
    try:
        public = [
            DashboardActualPoint(
                data_time=data_time,
                value=point.y,
                quality=point.quality,
                source_revision=point.source_revision,
            )
            for point, data_time in selected
        ]
    except ValidationError:
        return _PublicActualProjection([], False)
    if len(public) > 96:
        return _PublicActualProjection([], False)
    if any(
        current.data_time - previous.data_time != timedelta(minutes=15)
        for previous, current in zip(public, public[1:])
    ):
        return _PublicActualProjection([], False)
    for point in public:
        if point.value is None:
            continue
        if is_load_series(unique_id) and point.value < 0:
            return _PublicActualProjection([], False)
        if not is_load_series(unique_id) and not 0 <= point.value <= 100:
            return _PublicActualProjection([], False)
    return _PublicActualProjection(public, True)


def _station_from_result(result: DashboardStationResult) -> DashboardStation:
    validate_shanghai_timestamp(result.as_of, "station as_of", quarter_hour=True)
    validate_shanghai_timestamp(
        result.generated_at, "station generated_at", quarter_hour=False
    )
    if result.binding.station_key not in {"station_1", "station_2"}:
        raise ValueError("dashboard result has an unknown public station key")
    if result.binding.station_id in result.binding.station_name:
        raise ValueError("public station name must not contain the internal station ID")
    history_start = result.as_of - timedelta(hours=24)
    forecast_by_id = {item.unique_id: item for item in result.forecasts}
    if (
        len(result.forecasts) != len(SERIES_IDS)
        or [item.unique_id for item in result.forecasts] != list(SERIES_IDS)
        or set(forecast_by_id) != set(SERIES_IDS)
    ):
        raise ValueError("dashboard requires the two ordered forecast series")
    public_actual_by_id = {
        unique_id: _validated_public_actual(
            result.actual,
            unique_id,
            history_start,
            result.as_of,
        )
        for unique_id in SERIES_IDS
    }
    actual_times = [
        point.data_time
        for projection in public_actual_by_id.values()
        for point in projection.points
    ]
    public_series = tuple(
        _public_series(public_actual_by_id[unique_id], forecast_by_id[unique_id])
        for unique_id in SERIES_IDS
    )
    statuses = {item.status for item in public_series}
    explicitly_degraded = bool(getattr(result, "degraded", False))
    normal = statuses.issubset({"ok", "warming_up"}) and not explicitly_degraded
    if statuses == {"ok"} and not explicitly_degraded:
        state = "ready"
    elif normal:
        state = "initializing"
    else:
        state = "degraded"
    return DashboardStation(
        station_key=result.binding.station_key,
        station_name=result.binding.station_name,
        range=DashboardRange(
            history_start=history_start,
            history_end=result.as_of,
            actual_latest=max(actual_times) if actual_times else None,
            forecast_start=result.as_of,
            forecast_end=result.as_of + timedelta(hours=24),
            now_separator=result.generated_at,
        ),
        system=DashboardStationSystem(
            state=state,
            mode="normal" if normal else "degraded",
            generated_at=result.generated_at,
            stale=False,
        ),
        series=public_series,
        acceptance=getattr(result, "acceptance", None),
        readiness=getattr(result, "readiness", None),
    )


def _reject_internal_identities(
    value: object,
    bindings: tuple[StationBinding, StationBinding],
) -> None:
    internal_ids = tuple(item.station_id for item in bindings)

    def contains_identity(item: object) -> bool:
        if isinstance(item, str):
            return any(station_id in item for station_id in internal_ids)
        if hasattr(item, "model_dump"):
            return contains_identity(item.model_dump(mode="python"))
        if isinstance(item, dict):
            return any(contains_identity(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(contains_identity(child) for child in item)
        return False

    if contains_identity(value):
        raise ValueError("public dashboard payload contains an internal identity")


def _empty_station(
    binding: StationBinding,
    generated_at: datetime,
    *,
    failed: bool,
) -> DashboardStation:
    state = "error" if failed else "initializing"
    fallback_reason = "station_unavailable" if failed else None
    return DashboardStation(
        station_key=binding.station_key,
        station_name=binding.station_name,
        range=DashboardRange(
            history_start=None,
            history_end=None,
            actual_latest=None,
            forecast_start=None,
            forecast_end=None,
            now_separator=generated_at,
        ),
        system=DashboardStationSystem(
            state=state,
            mode="error" if failed else "initializing",
            generated_at=None,
            stale=False,
        ),
        series=tuple(
            DashboardSeries(
                unique_id=unique_id,
                unit="kW" if unique_id == "station_total_load" else "%",
                model_name=None,
                status=state,
                fallback_reason=fallback_reason,
                actual=[],
                forecast=[],
            )
            for unique_id in SERIES_IDS
        ),
        acceptance=None,
        readiness=None,
    )


def _aggregate_envelope(
    stations: tuple[DashboardStation, DashboardStation],
    generated_at: datetime,
) -> DashboardEnvelope:
    states = [item.system.state for item in stations]
    if any(state in {"error", "degraded"} for state in states):
        aggregate_state = "degraded"
    elif "stale" in states:
        aggregate_state = "stale"
    elif "initializing" in states:
        aggregate_state = "initializing"
    else:
        aggregate_state = "ready"
    healthy = sum(
        item.system.state in {"ready", "initializing"} and not item.system.stale
        for item in stations
    )
    return DashboardEnvelope(
        data=DashboardData(
            system=DashboardAggregateSystem(
                state=aggregate_state,
                generated_at=generated_at,
                healthy_station_count=healthy,
            ),
            stations=stations,
        )
    )


def build_dashboard_payload(
    results: list[DashboardStationResult], generated_at: datetime
) -> DashboardEnvelope:
    """Translate internal station bindings into the only public dashboard identity."""
    validate_shanghai_timestamp(
        generated_at, "dashboard generated_at", quarter_hour=False
    )
    if (
        len(results) != 2
        or [item.binding.station_key for item in results]
        != ["station_1", "station_2"]
    ):
        raise ValueError("dashboard requires two ordered station results")
    _validate_public_bindings((results[0].binding, results[1].binding))
    stations = tuple(_station_from_result(item) for item in results)
    envelope = _aggregate_envelope(stations, generated_at)
    _reject_internal_identities(
        envelope, (results[0].binding, results[1].binding)
    )
    return envelope


class DashboardCache:
    """Thread-safe, independently replaceable last-good station snapshots."""

    def __init__(
        self,
        bindings: tuple[StationBinding, StationBinding],
        *,
        stale_after: timedelta = STALE_AFTER,
    ) -> None:
        _validate_public_bindings(bindings)
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self._bindings = bindings
        self._stale_after = stale_after
        self._lock = Lock()
        self._results: dict[str, DashboardStationResult] = {}
        self._failed: set[str] = set()

    @property
    def station_keys(self) -> tuple[str, str]:
        return tuple(item.station_key for item in self._bindings)

    def publish_station(self, result: DashboardStationResult) -> None:
        """Atomically replace one public station while preserving its peer."""
        configured = next(
            (item for item in self._bindings if item.station_key == result.binding.station_key),
            None,
        )
        if configured is None or configured != result.binding:
            raise ValueError("dashboard result does not match a configured station binding")
        public_station = _station_from_result(result)
        _reject_internal_identities(public_station, self._bindings)
        with self._lock:
            self._results[result.binding.station_key] = deepcopy(result)
            self._failed.discard(result.binding.station_key)

    def mark_station_failure(self, binding: StationBinding) -> None:
        if binding not in self._bindings:
            raise ValueError("dashboard failure does not match a configured station")
        with self._lock:
            self._failed.add(binding.station_key)

    def has_any_success(self) -> bool:
        with self._lock:
            return bool(self._results)

    def snapshot(self, now: datetime) -> DashboardEnvelope:
        """Recompute per-station stale flags and aggregate state without modeling."""
        validate_shanghai_timestamp(now, "dashboard snapshot time", quarter_hour=False)
        with self._lock:
            results = deepcopy(self._results)
            failed = set(self._failed)
        stations: list[DashboardStation] = []
        for binding in self._bindings:
            result = results.get(binding.station_key)
            if result is None:
                stations.append(
                    _empty_station(binding, now, failed=binding.station_key in failed)
                )
                continue
            station = _station_from_result(result)
            age = now - result.generated_at
            if age > self._stale_after:
                station_values = station.model_dump(mode="python")
                station_values["system"] = {
                    **station_values["system"],
                    "state": "stale",
                    "stale": True,
                }
                station = DashboardStation.model_validate(station_values)
            elif binding.station_key in failed:
                station_values = station.model_dump(mode="python")
                station_values["system"] = {
                    **station_values["system"],
                    "state": "degraded",
                    "mode": "degraded",
                    "stale": False,
                }
                station = DashboardStation.model_validate(station_values)
            stations.append(station)
        envelope = _aggregate_envelope((stations[0], stations[1]), now)
        _reject_internal_identities(envelope, self._bindings)
        return envelope


class StationDashboardProvider(Protocol):
    def build_station(self, binding: StationBinding) -> DashboardStationResult: ...


class PeriodicDashboardRefresher:
    """Refresh each configured station separately at startup and on a timer."""

    def __init__(
        self,
        provider: StationDashboardProvider,
        cache: DashboardCache,
        bindings: tuple[StationBinding, StationBinding],
        *,
        interval_seconds: float = 900,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or interval_seconds <= 0
        ):
            raise ValueError("refresh interval must be positive")
        if [item.station_key for item in bindings] != ["station_1", "station_2"]:
            raise ValueError("refresher requires ordered station bindings")
        self._provider = provider
        self._cache = cache
        self._bindings = bindings
        self._interval_seconds = float(interval_seconds)
        self._stop = Event()
        self._condition = Condition(Lock())
        self._state = "stopped"
        self._start_owner: int | None = None
        self._stop_requested = False
        self._thread: Thread | None = None

    def refresh_all(self) -> int:
        successful = 0
        for binding in self._bindings:
            try:
                result = self._provider.build_station(binding)
                self._cache.publish_station(result)
            except Exception as error:
                self._cache.mark_station_failure(binding)
                candidate_code = (
                    error.code if isinstance(error, M3Error) else "unexpected_error"
                )
                safe_code = (
                    candidate_code
                    if isinstance(candidate_code, str)
                    and SAFE_ERROR_CODE.fullmatch(candidate_code)
                    else "unexpected_error"
                )
                LOGGER.error(
                    "m3_live_dashboard_refresh_failed station_key=%s error_type=%s error_code=%s",
                    binding.station_key,
                    "m3_error" if isinstance(error, M3Error) else "unexpected_error",
                    safe_code,
                )
            else:
                successful += 1
        return successful

    def _run(self) -> None:
        worker = current_thread()
        try:
            while not self._stop.wait(self._interval_seconds):
                self.refresh_all()
        finally:
            with self._condition:
                if self._thread is worker:
                    self._thread = None
                    self._state = "stopped"
                    self._start_owner = None
                    self._stop_requested = False
                    self._stop.set()
                self._condition.notify_all()

    def start(self) -> None:
        owner = get_ident()
        with self._condition:
            if self._state == "stopping":
                raise RuntimeError("dashboard refresher is stopping")
            if self._state == "running":
                return
            if self._state == "starting":
                if self._start_owner == owner:
                    return
                while self._state == "starting":
                    self._condition.wait()
                if self._state == "running":
                    return
                if self._state == "stopping":
                    raise RuntimeError("dashboard refresher is stopping")
            self._state = "starting"
            self._start_owner = owner
            self._stop_requested = False
            self._stop.clear()

        thread: Thread | None = None
        try:
            self.refresh_all()
            if not self._cache.has_any_success():
                raise RuntimeError("initial dashboard refresh failed")
            with self._condition:
                if self._stop_requested:
                    self._state = "stopped"
                    self._start_owner = None
                    self._stop_requested = False
                    self._stop.set()
                    self._condition.notify_all()
                    return
            thread = Thread(
                target=self._run,
                name="m3-live-dashboard-refresh",
                daemon=True,
            )
            with self._condition:
                if self._stop_requested:
                    self._state = "stopped"
                    self._start_owner = None
                    self._stop_requested = False
                    self._stop.set()
                    self._condition.notify_all()
                    return
                self._thread = thread
            thread.start()
            with self._condition:
                self._start_owner = None
                if self._thread is thread:
                    if self._stop_requested:
                        self._state = "stopping"
                        self._stop.set()
                    else:
                        self._state = "running"
                self._condition.notify_all()
        except Exception:
            with self._condition:
                if self._thread is thread:
                    self._thread = None
                if self._state == "starting":
                    self._state = "stopped"
                self._start_owner = None
                self._stop_requested = False
                self._stop.set()
                self._condition.notify_all()
            raise

    def stop(self) -> None:
        caller = current_thread()
        with self._condition:
            if self._state == "stopped":
                self._stop.set()
                return
            if self._state == "starting":
                self._stop_requested = True
                self._stop.set()
                if self._start_owner == get_ident():
                    return
                while self._state == "starting":
                    self._condition.wait()
                if self._state == "stopped":
                    return
            if self._state == "running":
                self._state = "stopping"
            self._stop.set()
            thread = self._thread
            if thread is None:
                self._state = "stopped"
                self._stop_requested = False
                self._condition.notify_all()
                return
            if thread is caller:
                return
        if thread is not None:
            thread.join()
        with self._condition:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._state = "stopped"
                self._start_owner = None
                self._stop_requested = False
                self._stop.set()
                self._condition.notify_all()
            while self._state != "stopped":
                self._condition.wait()
