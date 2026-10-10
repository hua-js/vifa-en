"""Tests for the server-local rolling raw-row history cache."""

from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest
from typing import cast
from zoneinfo import ZoneInfo

from m3.worker.clients.raw_energy_api import RawEnergySourceClient
from m3.worker.errors import M3Error
from m3.worker.services.raw_history_cache import (
    FETCH_CHUNK,
    REFRESH_OVERLAP,
    RETENTION,
    CachedRawEnergySource,
    RawHistoryCache,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
STATION = "ES01"
INTERVAL = 900
BASE = datetime(2026, 9, 1, 0, 0, tzinfo=SHANGHAI)


def make_rows(start: datetime, end: datetime, *, load: float = 12.5) -> list:
    """One raw row per minute, matching the upstream verbatim shape."""
    rows = []
    cursor = start
    while cursor < end:
        rows.append((cursor, {
            "timestamp": cursor.isoformat(),
            "es_sn": STATION,
            "load_power": f"{load:.2f}",
            "emus_soc": "55.10",
            "emus_power": "3.00",
        }))
        cursor += timedelta(seconds=60)
    return rows


class FakeInnerSource:
    """Duck-typed RawEnergySourceClient stand-in over an in-memory dataset."""

    def __init__(self, rows: list) -> None:
        self.rows = list(rows)
        self.fetch_calls: list[tuple[datetime, datetime]] = []
        self.direct_calls: list[tuple[datetime, datetime]] = []

    def require_station(self, station_id: str) -> None:
        if station_id != STATION:
            raise M3Error("source_contract_invalid", "unknown station")

    def fetch_rows(self, station_id: str, start: datetime, end: datetime) -> list:
        self.require_station(station_id)
        self.fetch_calls.append((start, end))
        return [
            (timestamp, row)
            for timestamp, row in self.rows
            if start <= timestamp < end
        ]

    def list_custom_observations(self, station_id, start, end, *, interval_seconds):
        self.require_station(station_id)
        RawEnergySourceClient._validate_custom_window(start, end, interval_seconds)
        self.direct_calls.append((start, end))
        rows = [
            (timestamp, row)
            for timestamp, row in self.rows
            if start <= timestamp < end
        ]
        return RawEnergySourceClient.bucket_custom_rows(rows, start, end, interval_seconds)

    def list_observations(self, station_id, start, end):  # pragma: no cover
        raise NotImplementedError

    def latest_timestamp(self, station_id):  # pragma: no cover
        raise NotImplementedError

    def storage_capacity(self, station_id):  # pragma: no cover
        raise NotImplementedError


class CachedRawEnergySourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "raw_history.db"

    def _source(self, rows: list) -> tuple[CachedRawEnergySource, FakeInnerSource]:
        inner = FakeInnerSource(rows)
        wrapped = CachedRawEnergySource(
            cast(RawEnergySourceClient, inner), RawHistoryCache(self.cache_path)
        )
        return wrapped, inner

    def test_first_request_fetches_full_window_once(self):
        start, end = BASE, BASE + timedelta(days=7)
        source, inner = self._source(make_rows(start, end))
        points = source.list_custom_observations(
            STATION, start, end, interval_seconds=INTERVAL
        )
        self.assertEqual(inner.fetch_calls, [(start, end)])
        self.assertEqual(len(points), 2 * 7 * 24 * 4)
        self.assertTrue(all(point.quality == "valid" for point in points))

    def test_results_match_direct_upstream_path(self):
        start, end = BASE, BASE + timedelta(days=2)
        rows = make_rows(start, end)
        source, _ = self._source(rows)
        cached = source.list_custom_observations(
            STATION, start, end, interval_seconds=INTERVAL
        )
        direct = RawEnergySourceClient.bucket_custom_rows(rows, start, end, INTERVAL)
        self.assertEqual(cached, direct)

    def test_second_identical_request_does_not_refetch(self):
        start, end = BASE, BASE + timedelta(days=3)
        source, inner = self._source(make_rows(start, end))
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        self.assertEqual(inner.fetch_calls, [(start, end)])

    def test_contiguous_extension_skips_overlap(self):
        start, mid = BASE, BASE + timedelta(days=7)
        end = mid + timedelta(days=7)
        source, inner = self._source(make_rows(start, end))
        source.list_custom_observations(STATION, start, mid, interval_seconds=INTERVAL)
        source.list_custom_observations(STATION, mid, end, interval_seconds=INTERVAL)
        self.assertEqual(inner.fetch_calls, [(start, mid), (mid, end)])

    def test_new_window_refetches_overlapping_tail(self):
        start, end = BASE, BASE + timedelta(days=7)
        source, inner = self._source(make_rows(start, end + timedelta(days=1)))
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        new_start, new_end = start + timedelta(days=1), end + timedelta(days=1)
        source.list_custom_observations(
            STATION, new_start, new_end, interval_seconds=INTERVAL
        )
        self.assertEqual(
            inner.fetch_calls,
            [(start, end), (end - REFRESH_OVERLAP, new_end)],
        )

    def test_revision_inside_overlap_is_picked_up(self):
        start, end = BASE, BASE + timedelta(days=7)
        rows = make_rows(start, end + timedelta(days=1), load=12.5)
        source, inner = self._source(rows)
        before = source.list_custom_observations(
            STATION, start, end, interval_seconds=INTERVAL
        )
        revised_bucket = (end - timedelta(hours=1)).replace(minute=0)
        inner.rows = [
            (timestamp, {**row, "load_power": "99.00"}
             if revised_bucket <= timestamp < revised_bucket + timedelta(minutes=15)
             else row)
            for timestamp, row in inner.rows
        ]
        after = source.list_custom_observations(
            STATION, start + timedelta(days=1), end + timedelta(days=1),
            interval_seconds=INTERVAL,
        )
        before_point = next(
            point for point in before
            if point.unique_id == "station_total_load" and point.ds == revised_bucket
        )
        after_point = next(
            point for point in after
            if point.unique_id == "station_total_load" and point.ds == revised_bucket
        )
        self.assertEqual(before_point.y, 12.5)
        self.assertEqual(after_point.y, 99.0)

    def test_backfill_earlier_start(self):
        start, end = BASE, BASE + timedelta(days=7)
        earlier = start - timedelta(days=7)
        source, inner = self._source(make_rows(earlier, end))
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        source.list_custom_observations(STATION, earlier, start, interval_seconds=INTERVAL)
        self.assertEqual(inner.fetch_calls, [(start, end), (earlier, start)])

    def test_disjoint_window_bypasses_cache(self):
        start, end = BASE, BASE + timedelta(days=7)
        old_start, old_end = BASE - timedelta(days=30), BASE - timedelta(days=23)
        source, inner = self._source(make_rows(old_start, end))
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        points = source.list_custom_observations(
            STATION, old_start, old_end, interval_seconds=INTERVAL
        )
        self.assertEqual(inner.direct_calls, [(old_start, old_end)])
        self.assertEqual(len(inner.fetch_calls), 1)
        self.assertTrue(all(point.quality == "valid" for point in points))

    def test_prune_keeps_rolling_retention(self):
        start = BASE
        days = int(RETENTION.days) + 14
        end = start + timedelta(days=days)
        source, _ = self._source(make_rows(start, end))
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=7), end)
            source.list_custom_observations(
                STATION, cursor, chunk_end, interval_seconds=INTERVAL
            )
            cursor = chunk_end
        cache = RawHistoryCache(self.cache_path)
        coverage = cache.coverage(STATION)
        self.assertIsNotNone(coverage)
        assert coverage is not None
        self.assertGreaterEqual(coverage[0], end - RETENTION)
        retained = cache.read(STATION, coverage[0], coverage[1])
        self.assertTrue(all(timestamp >= coverage[0] for timestamp, _ in retained))
        self.assertLess((end - coverage[0]).days, days)

    def test_corrupt_cache_file_is_reset_and_rebuilt(self):
        start, end = BASE, BASE + timedelta(days=2)
        rows = make_rows(start, end)
        source, inner = self._source(rows)
        source.list_custom_observations(STATION, start, end, interval_seconds=INTERVAL)
        self.cache_path.write_bytes(b"not a sqlite database")
        points = source.list_custom_observations(
            STATION, start, end, interval_seconds=INTERVAL
        )
        self.assertEqual(len(inner.fetch_calls), 2)
        self.assertTrue(all(point.quality == "valid" for point in points))

    def test_fetch_chunking_uses_seven_day_steps(self):
        # _fetch_and_store must split spans beyond FETCH_CHUNK into 7-day steps.
        start, end = BASE, BASE + timedelta(days=15)
        source, inner = self._source(make_rows(start, end))
        source._fetch_and_store(STATION, start, end)
        self.assertEqual(
            inner.fetch_calls,
            [
                (start, start + FETCH_CHUNK),
                (start + FETCH_CHUNK, start + 2 * FETCH_CHUNK),
                (start + 2 * FETCH_CHUNK, end),
            ],
        )


class RawHistoryCacheStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = RawHistoryCache(Path(self._tmp.name) / "cache.db")

    def test_store_rejects_unordered_rows(self):
        start, end = BASE, BASE + timedelta(days=1)
        rows = make_rows(start, end)
        rows[5], rows[6] = rows[6], rows[5]
        with self.assertRaises(ValueError):
            self.cache.store(STATION, start, end, rows)

    def test_store_rejects_rows_outside_window(self):
        start, end = BASE, BASE + timedelta(days=1)
        rows = make_rows(start, end)
        outside = end + timedelta(minutes=5)
        rows.append((outside, {"timestamp": outside.isoformat(), "es_sn": STATION}))
        with self.assertRaises(ValueError):
            self.cache.store(STATION, start, end, rows)

    def test_store_rejects_gap_interval(self):
        start, end = BASE, BASE + timedelta(days=1)
        self.cache.store(STATION, start, end, make_rows(start, end))
        gap_start = end + timedelta(days=10)
        with self.assertRaises(ValueError):
            self.cache.store(
                STATION, gap_start, gap_start + timedelta(days=1),
                make_rows(gap_start, gap_start + timedelta(days=1)),
            )

    def test_read_rejects_uncovered_window(self):
        start, end = BASE, BASE + timedelta(days=1)
        self.cache.store(STATION, start, end, make_rows(start, end))
        with self.assertRaises(ValueError):
            self.cache.read(STATION, start - timedelta(days=1), end)

    def test_rows_roundtrip_verbatim(self):
        start, end = BASE, BASE + timedelta(hours=3)
        rows = make_rows(start, end)
        rows[0] = (rows[0][0], {**rows[0][1], "load_power": "N/A"})
        self.cache.store(STATION, start, end, rows)
        loaded = self.cache.read(STATION, start, end)
        self.assertEqual(loaded, rows)
        self.assertEqual(loaded[0][1]["load_power"], "N/A")


if __name__ == "__main__":
    unittest.main()
