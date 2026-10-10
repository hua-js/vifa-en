"""Server-local rolling cache of verbatim raw upstream energy rows.

Forecast tasks historically re-pulled the full training window from NocoBase
on every run.  This module stores the raw rows per station in SQLite and
fetches only the missing tail instead:

- first request for a window fetches it fully, in seven-day chunks;
- a later window sharing the cached interval only fetches the uncovered
  head and the tail beyond the coverage end; the tail fetch overlaps the
  cached coverage by ``REFRESH_OVERLAP`` so late or revised samples replace
  their stale copies (upsert by timestamp);
- rows older than ``RETENTION`` behind the coverage end are pruned, keeping
  a rolling ninety-day local mirror;
- a request window disjoint from the cached interval bypasses the cache
  entirely, preserving the single-interval invariant.

Bucketing still happens in ``RawEnergySourceClient.bucket_custom_rows`` from
the cached verbatim rows, so quality states and values are identical to the
direct path.  Any upstream fetch error propagates and fails the task exactly
as before; a corrupted cache file is reset and rebuilt from upstream, never
used as a silent substitute for live data.
"""

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import threading

from m3.worker.clients.raw_energy_api import RawEnergySourceClient
from m3.worker.custom_forecast_contracts import CustomObservationPoint
from m3.worker.errors import M3Error

logger = logging.getLogger(__name__)

FETCH_CHUNK = timedelta(days=7)
REFRESH_OVERLAP = timedelta(hours=48)
RETENTION = timedelta(days=90)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cached timestamps must be timezone-aware")
    return value.isoformat()


class RawHistoryCache:
    """Single-contiguous-interval-per-station store of verbatim raw rows."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError("cache path must be a Path")
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30)

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS coverage ("
                " station TEXT PRIMARY KEY,"
                " range_start TEXT NOT NULL,"
                " range_end TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS raw_rows ("
                " station TEXT NOT NULL,"
                " ts TEXT NOT NULL,"
                " payload TEXT NOT NULL,"
                " PRIMARY KEY (station, ts));"
            )

    def reset(self) -> None:
        """Drop all cached content; the next request refetches upstream."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        self._ensure_schema()

    def coverage(self, station_id: str) -> tuple[datetime, datetime] | None:
        """Return the contiguous cached interval for one station, if any."""
        with self._connect() as db:
            row = db.execute(
                "SELECT range_start, range_end FROM coverage WHERE station = ?",
                (station_id,),
            ).fetchone()
        if row is None:
            return None
        return (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]))

    def store(
        self,
        station_id: str,
        fetch_start: datetime,
        fetch_end: datetime,
        rows: list[tuple[datetime, dict[str, object]]],
    ) -> None:
        """Upsert freshly fetched rows and extend the contiguous coverage.

        The fetched interval must touch or overlap the existing coverage so
        each station keeps exactly one contiguous covered interval.
        """
        start_text = _iso(fetch_start)
        end_text = _iso(fetch_end)
        previous: str | None = None
        payloads: list[tuple[str, str, str]] = []
        for timestamp, row in rows:
            ts_text = _iso(timestamp)
            if ts_text < start_text or ts_text >= end_text:
                raise ValueError("cache store row falls outside fetched window")
            if previous is not None and ts_text <= previous:
                raise ValueError("cache store rows are unordered or duplicated")
            if not isinstance(row, dict):
                raise ValueError("cache store row payload must be a dict")
            payloads.append((station_id, ts_text, json.dumps(row)))
            previous = ts_text
        with self._connect() as db:
            existing = db.execute(
                "SELECT range_start, range_end FROM coverage WHERE station = ?",
                (station_id,),
            ).fetchone()
            if existing is not None:
                if start_text > existing[1] or end_text < existing[0]:
                    raise ValueError(
                        "cache store interval must touch existing coverage"
                    )
                range_start = min(start_text, existing[0])
                range_end = max(end_text, existing[1])
            else:
                range_start, range_end = start_text, end_text
            db.executemany(
                "INSERT OR REPLACE INTO raw_rows (station, ts, payload)"
                " VALUES (?, ?, ?)",
                payloads,
            )
            db.execute(
                "INSERT OR REPLACE INTO coverage (station, range_start, range_end)"
                " VALUES (?, ?, ?)",
                (station_id, range_start, range_end),
            )

    def read(
        self, station_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, dict[str, object]]]:
        """Return cached verbatim rows for a window inside the coverage."""
        start_text = _iso(start)
        end_text = _iso(end)
        with self._connect() as db:
            existing = db.execute(
                "SELECT range_start, range_end FROM coverage WHERE station = ?",
                (station_id,),
            ).fetchone()
            if (
                existing is None
                or existing[0] > start_text
                or existing[1] < end_text
            ):
                raise ValueError("cache read window is not fully covered")
            records = db.execute(
                "SELECT ts, payload FROM raw_rows"
                " WHERE station = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (station_id, start_text, end_text),
            ).fetchall()
        return [
            (datetime.fromisoformat(ts_text), json.loads(payload))
            for ts_text, payload in records
        ]

    def prune(self, station_id: str, cutoff: datetime) -> None:
        """Drop rows older than cutoff and advance the coverage start."""
        cutoff_text = _iso(cutoff)
        with self._connect() as db:
            db.execute(
                "DELETE FROM raw_rows WHERE station = ? AND ts < ?",
                (station_id, cutoff_text),
            )
            existing = db.execute(
                "SELECT range_start, range_end FROM coverage WHERE station = ?",
                (station_id,),
            ).fetchone()
            if existing is None or existing[0] >= cutoff_text:
                return
            if cutoff_text >= existing[1]:
                db.execute(
                    "DELETE FROM coverage WHERE station = ?", (station_id,)
                )
            else:
                db.execute(
                    "UPDATE coverage SET range_start = ? WHERE station = ?",
                    (cutoff_text, station_id),
                )


class CachedRawEnergySource:
    """RawEnergySourceClient drop-in serving custom observations from cache."""

    def __init__(
        self, inner: RawEnergySourceClient, cache: RawHistoryCache
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._lock = threading.Lock()

    def list_observations(self, station_id: str, start: datetime, end: datetime):
        return self._inner.list_observations(station_id, start, end)

    def latest_timestamp(self, station_id: str):
        return self._inner.latest_timestamp(station_id)

    def storage_capacity(self, station_id: str):
        return self._inner.storage_capacity(station_id)

    def list_custom_observations(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        *,
        interval_seconds: int,
    ) -> list[CustomObservationPoint]:
        self._inner.require_station(station_id)
        RawEnergySourceClient._validate_custom_window(start, end, interval_seconds)
        with self._lock:
            try:
                return self._cached_points(station_id, start, end, interval_seconds)
            except sqlite3.DatabaseError:
                logger.warning(
                    "m3_raw_history_cache_corrupt station_id=%s; rebuilding",
                    station_id,
                )
                self._cache.reset()
                return self._cached_points(station_id, start, end, interval_seconds)

    def _cached_points(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        interval_seconds: int,
    ) -> list[CustomObservationPoint]:
        coverage = self._cache.coverage(station_id)
        if coverage is not None and (end < coverage[0] or start > coverage[1]):
            # Strictly disjoint from the cached interval: bypass without
            # storing so the single-interval invariant is preserved.  Windows
            # merely touching the coverage boundary extend it instead.
            return self._inner.list_custom_observations(
                station_id, start, end, interval_seconds=interval_seconds
            )
        self._ensure_coverage(station_id, start, end, coverage)
        coverage = self._cache.coverage(station_id)
        if coverage is None:
            raise M3Error("source_contract_invalid", "Cache coverage missing after fetch")
        cutoff = min(start, coverage[1] - RETENTION)
        if coverage[0] < cutoff:
            self._cache.prune(station_id, cutoff)
        rows = self._cache.read(station_id, start, end)
        return RawEnergySourceClient.bucket_custom_rows(
            rows, start, end, interval_seconds
        )

    def _ensure_coverage(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        coverage: tuple[datetime, datetime] | None,
    ) -> None:
        if coverage is None:
            self._fetch_and_store(station_id, start, end)
            return
        cov_start, cov_end = coverage
        if start < cov_start:
            self._fetch_and_store(station_id, start, cov_start)
        if end > cov_end:
            # Overlap the cached tail so late or revised samples are refetched;
            # contiguous chunk extensions (start == cov_end) skip the overlap.
            fetch_from = max(start, cov_end - REFRESH_OVERLAP)
            self._fetch_and_store(station_id, fetch_from, end)

    def _fetch_and_store(self, station_id: str, start: datetime, end: datetime) -> None:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + FETCH_CHUNK, end)
            rows = self._inner.fetch_rows(station_id, cursor, chunk_end)
            self._cache.store(station_id, cursor, chunk_end, rows)
            cursor = chunk_end
