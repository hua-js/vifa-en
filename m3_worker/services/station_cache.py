"""Thread-safe, revision-aware in-memory history for one station."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from threading import Lock, RLock
from typing import Iterator

from pydantic import ValidationError

from m3_worker.contracts import SERIES_IDS, ObservationPoint, SeriesId
from m3_worker.errors import M3Error


SERIES_SET = frozenset(SERIES_IDS)
GRID = timedelta(minutes=15)
RETENTION = timedelta(days=90)


@dataclass
class StationState:
    state: str = "initializing"
    champions: dict[str, object | None] = field(
        default_factory=lambda: {unique_id: None for unique_id in SERIES_IDS}
    )
    last_source_at: datetime | None = None
    last_published_at: datetime | None = None
    last_error_code: str | None = None


def _validated_points(points: list[ObservationPoint]) -> list[ObservationPoint]:
    if not isinstance(points, list) or not points:
        raise M3Error(
            "source_contract_invalid", "Source batch must contain both series"
        )

    validated: list[ObservationPoint] = []
    seen: set[tuple[str, datetime]] = set()
    last_by_series: dict[str, datetime] = {}
    for supplied in points:
        if not isinstance(supplied, ObservationPoint):
            raise M3Error(
                "source_contract_invalid", "Source batch contains an invalid point"
            )
        if type(supplied.source_revision) is not int or supplied.source_revision < 0:
            raise M3Error(
                "source_contract_invalid", "Source revision must be a non-negative integer"
            )
        try:
            point = ObservationPoint.model_validate(supplied.model_dump())
        except (ValidationError, TypeError, ValueError) as error:
            raise M3Error(
                "source_contract_invalid", "Source batch contains an invalid point"
            ) from error
        key = (point.unique_id, point.ds)
        if key in seen:
            raise M3Error(
                "source_contract_invalid", "Source batch contains duplicate keys"
            )
        prior_ds = last_by_series.get(point.unique_id)
        if prior_ds is not None and point.ds <= prior_ds:
            raise M3Error(
                "source_contract_invalid",
                "Source series timestamps must be strictly increasing",
            )
        seen.add(key)
        last_by_series[point.unique_id] = point.ds
        validated.append(point)

    if frozenset(last_by_series) != SERIES_SET:
        raise M3Error(
            "source_contract_invalid", "Source batch must contain exactly two series"
        )
    return validated


def _trim(points: dict[tuple[str, datetime], ObservationPoint]):
    if not points:
        return points
    latest = max(point.ds for point in points.values())
    earliest_retained = latest - RETENTION + GRID
    return {
        key: point
        for key, point in points.items()
        if point.ds >= earliest_retained
    }


class StationCache:
    """Owns one station's cache, state, and multi-step operation boundary."""

    def __init__(self, station_id: str):
        if not isinstance(station_id, str) or not station_id:
            raise ValueError("station_id must be non-empty")
        self.station_id = station_id
        self.state = StationState()
        self._points: dict[tuple[str, datetime], ObservationPoint] = {}
        self._lock = RLock()
        self._selection_lock = Lock()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize bootstrap, selection, and publication for this station."""

        with self._lock:
            yield

    @contextmanager
    def selection_exclusive(self) -> Iterator[None]:
        """Allow one long-running model selection without blocking online reads."""

        with self._selection_lock:
            yield

    def replace(self, points: list[ObservationPoint]) -> None:
        validated = _validated_points(points)
        candidate = {(point.unique_id, point.ds): point for point in validated}
        if candidate:
            timestamps = [point.ds for point in candidate.values()]
            if max(timestamps) - min(timestamps) > RETENTION - GRID:
                raise M3Error(
                    "source_contract_invalid", "Full source pull exceeds 90 days"
                )
        with self._lock:
            self._points = candidate

    def merge(self, points: list[ObservationPoint]) -> None:
        validated = _validated_points(points)
        with self._lock:
            candidate = dict(self._points)
            for point in validated:
                key = (point.unique_id, point.ds)
                current = candidate.get(key)
                if current is None:
                    candidate[key] = point
                    continue
                if point.source_revision < current.source_revision:
                    continue
                if point.source_revision == current.source_revision:
                    if point != current:
                        raise M3Error(
                            "source_contract_invalid",
                            "Same source revision contains conflicting values",
                        )
                    continue
                candidate[key] = point
            self._points = _trim(candidate)

    def window(self, unique_id: SeriesId) -> list[ObservationPoint]:
        if unique_id not in SERIES_SET:
            raise M3Error("source_contract_invalid", "Unknown source series")
        with self._lock:
            return sorted(
                (
                    point
                    for (series, _), point in self._points.items()
                    if series == unique_id
                ),
                key=lambda point: point.ds,
            )

    def state_snapshot(self) -> StationState:
        with self._lock:
            return replace(self.state, champions=dict(self.state.champions))
