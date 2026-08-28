"""Revision-aware cache and active forecast orchestration behavior."""

from datetime import datetime, timedelta
import gc
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import weakref

from m3_worker.contracts import (
    SERIES_IDS,
    ForecastPoint,
    ForecastSeries,
    ObservationPoint,
)
from m3_worker.domain.forecasting import Champion
from m3_worker.domain.training_data import build_training_dataset
from m3_worker.errors import M3Error
from m3_worker.services import forecast_service
from m3_worker.services.forecast_service import (
    ForecastService,
    build_latest_snapshot,
)
from m3_worker.services.station_cache import StationCache
from m3_worker.sinks.forecast_sink import canonical_hash
from tests.m3_test_support import RecordingForecastSink, StationKeyedObservationSource


BOOTSTRAP_AT = datetime.fromisoformat("2026-08-25T00:30:00+08:00")
FORECAST_AT = datetime.fromisoformat("2026-08-25T01:17:00+08:00")
GENERATED_AT = datetime.fromisoformat("2026-08-25T01:17:05+08:00")
ES01 = "plant-alpha-ES01"
ES02 = "plant-beta-ES02"


def make_points(
    start: datetime,
    periods: int,
    *,
    revision: int = 1,
) -> list[ObservationPoint]:
    points: list[ObservationPoint] = []
    for unique_id in SERIES_IDS:
        for index in range(periods):
            value = 800.0 + index % 96 if unique_id == "station_total_load" else 50.0
            points.append(
                ObservationPoint(
                    unique_id=unique_id,
                    ds=start + timedelta(minutes=15 * index),
                    y=value,
                    quality="valid",
                    source_revision=revision,
                )
            )
    return points


def points_at(
    at: datetime,
    *,
    revision: int = 1,
    value_delta: float = 0.0,
) -> list[ObservationPoint]:
    return [
        point.model_copy(update={"y": point.y + value_delta})
        for point in make_points(at, 1, revision=revision)
    ]


class FakeSource:
    def __init__(self, points: list[ObservationPoint]) -> None:
        self.points = points
        self.calls: list[SimpleNamespace] = []
        self.fail_call: int | None = None

    def list_observations(
        self, station_id: str, start: datetime, end: datetime
    ) -> list[ObservationPoint]:
        self.calls.append(
            SimpleNamespace(station_id=station_id, start=start, end=end)
        )
        if self.fail_call == len(self.calls):
            raise M3Error("source_http_failed", "Source API request failed")
        return [point for point in self.points if start <= point.ds < end]


class FakeSink:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.attempts = []
        self.latest = []

    def publish_latest(self, snapshot):
        self.attempts.append(snapshot)
        if len(self.attempts) <= self.fail_times:
            raise M3Error("sink_http_failed", "Forecast publication failed")
        self.latest.append(snapshot)
        return {"id": 1}


class StatelessSink:
    def __init__(self) -> None:
        self.publish_count = 0

    def publish_latest(self, snapshot):
        self.publish_count += 1
        return {"id": 1}


def fake_champion(dataset, *, name: str = "SeasonalNaive") -> Champion:
    return Champion(
        model_name=name,
        cv_mape_percent=10.0,
        selected_at=dataset.end,
        training_start=dataset.start,
        training_end=dataset.end,
        statsforecast_version="2.1.1",
    )


def fake_forecast_one(dataset, champion, as_of):
    unique_id = dataset.frame["unique_id"].iloc[0]
    first_target = as_of.replace(
        minute=(as_of.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    points = [
        ForecastPoint(
            data_time=first_target + timedelta(minutes=15 * (index - 1)),
            target_time=first_target + timedelta(minutes=15 * index),
            horizon_step=index + 1,
            raw_forecast=50.0,
            forecast_value=50.0,
            is_clipped=False,
        )
        for index in range(96)
    ]
    return ForecastSeries(
        unique_id=unique_id,
        unit="kW" if unique_id == "station_total_load" else "%",
        model_name=champion.model_name,
        status="degraded" if champion.selection_reason else "ok",
        points=points,
        fallback_reason=champion.selection_reason,
    )


def source_with_history(days: int = 28) -> FakeSource:
    start = BOOTSTRAP_AT - timedelta(days=days)
    history = make_points(start, days * 96)
    incremental = make_points(BOOTSTRAP_AT, 3)
    return FakeSource(history + incremental)


def make_service(
    source: FakeSource,
    sink: FakeSink,
    *,
    forecast=fake_forecast_one,
    generated_times: list[datetime] | None = None,
    cache: StationCache | None = None,
) -> ForecastService:
    times = iter(generated_times or [GENERATED_AT] * 20)
    return ForecastService(
        source,
        sink,
        {"station-1": cache or StationCache("station-1")},
        forecast,
        now=lambda: next(times),
    )


def make_two_station_service(*, forecast=fake_forecast_one):
    start = BOOTSTRAP_AT - timedelta(days=28)
    source = StationKeyedObservationSource(
        {
            ES01: make_points(start, 28 * 96 + 3),
            ES02: [
                point.model_copy(
                    update={"y": point.y + 100.0}
                )
                if point.unique_id == "station_total_load"
                else point
                for point in make_points(start, 28 * 96 + 3)
            ],
        }
    )
    sink = RecordingForecastSink()
    caches = {station_id: StationCache(station_id) for station_id in (ES01, ES02)}
    service = ForecastService(
        source,
        sink,
        caches,
        forecast,
        now=lambda: GENERATED_AT,
    )
    return service, source, sink, caches


class StationCacheTests(unittest.TestCase):
    def test_replace_rejects_incomplete_or_duplicate_two_series_atomically(self):
        """Removing one series or duplicating a key must not replace usable history."""
        cache = StationCache("station-1")
        original = points_at(BOOTSTRAP_AT)
        cache.replace(original)
        out_of_order = make_points(BOOTSTRAP_AT, 2)
        out_of_order[0], out_of_order[1] = out_of_order[1], out_of_order[0]

        invalid_batches = (
            original[:-1],
            original + [original[0]],
            out_of_order,
        )
        for batch in invalid_batches:
            with self.subTest(size=len(batch)), self.assertRaises(M3Error) as raised:
                cache.replace(batch)
            self.assertEqual(raised.exception.code, "source_contract_invalid")
            self.assertEqual(cache.window("station_total_load"), [original[0]])

    def test_merge_is_revision_aware_and_same_revision_conflict_is_atomic(self):
        """A conflicting revision must roll back earlier changes from the same merge."""
        cache = StationCache("station-1")
        original = points_at(BOOTSTRAP_AT, revision=2)
        cache.replace(original)

        stale = points_at(BOOTSTRAP_AT, revision=1, value_delta=10.0)
        cache.merge(stale)
        self.assertEqual(cache.window("station_total_load")[0].y, original[0].y)

        newer = points_at(BOOTSTRAP_AT, revision=3, value_delta=10.0)
        cache.merge(newer)
        self.assertEqual(cache.window("station_total_load")[0].source_revision, 3)

        conflict = points_at(BOOTSTRAP_AT, revision=3, value_delta=20.0)
        conflict[0] = conflict[0].model_copy(update={"source_revision": 4})
        with self.assertRaises(M3Error) as raised:
            cache.merge(conflict)
        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(cache.window("station_total_load")[0].source_revision, 3)

    def test_merge_rejects_non_exact_revision_and_wrong_series_batch(self):
        """Boolean revisions and partial source batches must not enter the cache."""
        cache = StationCache("station-1")
        original = points_at(BOOTSTRAP_AT)
        cache.replace(original)
        bad_revision = points_at(BOOTSTRAP_AT + timedelta(minutes=15))
        bad_revision[0] = bad_revision[0].model_copy(update={"source_revision": True})

        for batch in (bad_revision, points_at(BOOTSTRAP_AT + timedelta(minutes=15))[:-1]):
            with self.subTest(size=len(batch)), self.assertRaises(M3Error) as raised:
                cache.merge(batch)
            self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(len(cache.window("station_total_load")), 1)

    def test_merge_keeps_exactly_the_latest_ninety_days_on_the_grid(self):
        """Retaining the inclusive 90-day boundary would leave 8,641 points per series."""
        cache = StationCache("station-1")
        start = datetime.fromisoformat("2026-05-27T00:00:00+08:00")
        cache.replace(make_points(start, 90 * 96))
        next_at = start + timedelta(days=90)

        cache.merge(points_at(next_at))

        load = cache.window("station_total_load")
        self.assertEqual(len(load), 90 * 96)
        self.assertEqual(load[0].ds, start + timedelta(minutes=15))
        self.assertEqual(load[-1].ds, next_at)


class ForecastServiceTests(unittest.TestCase):
    def test_es01_refresh_cannot_change_es02_cache_or_forecast(self):
        """A full station key must bound cache mutation and latest publication."""
        service, source, sink, caches = make_two_station_service()
        for station_id in (ES01, ES02):
            service.bootstrap(station_id, BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            for station_id in (ES01, ES02):
                service.select_models(station_id)
        before = [
            point.model_copy(deep=True)
            for point in caches[ES02].window("station_total_load")
        ]

        source.replace_station_load(ES01, 4321.0)
        service.run_forecast(ES01, FORECAST_AT)

        self.assertEqual(caches[ES02].window("station_total_load"), before)
        self.assertEqual(
            [snapshot.station_id for snapshot in sink.latest_calls],
            [ES01],
        )

    def test_single_series_error_publishes_degraded_without_changing_peer_station(self):
        """One failed series must not discard its healthy pair or mutate a peer station."""

        def per_series_safe_forecast(dataset, champion, as_of):
            unique_id = dataset.frame["unique_id"].iloc[0]
            if unique_id == "station_total_load":
                return fake_forecast_one(dataset, champion, as_of)
            return ForecastSeries(
                unique_id="storage_soc",
                unit="%",
                model_name=champion.model_name,
                status="error",
                points=[],
                fallback_reason="forecast_failed",
            )

        service, _source, sink, caches = make_two_station_service(
            forecast=per_series_safe_forecast
        )
        for station_id in (ES01, ES02):
            service.bootstrap(station_id, BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            for station_id in (ES01, ES02):
                service.select_models(station_id)

        peer_snapshot = service.run_forecast(ES02, FORECAST_AT)
        peer_publication = peer_snapshot.model_copy(deep=True)
        peer_cache = [
            point.model_copy(deep=True)
            for unique_id in SERIES_IDS
            for point in caches[ES02].window(unique_id)
        ]
        snapshot = service.run_forecast(ES01, FORECAST_AT)

        self.assertEqual(snapshot.status, "degraded")
        self.assertEqual(
            [series.unique_id for series in snapshot.series],
            ["station_total_load", "storage_soc"],
        )
        self.assertEqual(len(snapshot.series[0].points), 96)
        self.assertEqual(snapshot.series[0].status, "ok")
        self.assertEqual(snapshot.series[1].points, [])
        self.assertEqual(snapshot.series[1].status, "error")
        self.assertEqual(
            [
                point
                for unique_id in SERIES_IDS
                for point in caches[ES02].window(unique_id)
            ],
            peer_cache,
        )
        self.assertEqual(sink.latest_calls[0], peer_publication)
        self.assertEqual(
            [call.station_id for call in sink.latest_calls],
            [ES02, ES01],
        )

    def test_bootstrap_uses_contiguous_bounded_windows_and_commits_after_all_succeed(self):
        """A 90-day pull must never become one oversized request or a partial commit."""
        source = source_with_history()
        service = make_service(source, FakeSink())

        service.bootstrap("station-1", BOOTSTRAP_AT)

        self.assertEqual(len(source.calls), 13)
        self.assertEqual(source.calls[0].start, BOOTSTRAP_AT - timedelta(days=90))
        self.assertEqual(source.calls[-1].end, BOOTSTRAP_AT)
        self.assertTrue(
            all(
                call.end - call.start <= timedelta(days=7)
                for call in source.calls
            )
        )
        self.assertTrue(
            all(
                left.end == right.start
                for left, right in zip(source.calls, source.calls[1:])
            )
        )
        state = service.state("station-1")
        self.assertEqual(state.state, "initializing")
        self.assertEqual(state.last_source_at, BOOTSTRAP_AT)
        self.assertEqual(state.last_error_code, None)

    def test_model_selection_uses_latest_twenty_eight_days_from_ninety_day_cache(self):
        """Selection expands to the Ready window without consuming all 90 days."""
        service = make_service(source_with_history(), FakeSink())
        service.bootstrap("station-1", BOOTSTRAP_AT)

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")

        champions = service.state("station-1").champions
        for unique_id in SERIES_IDS:
            self.assertEqual(
                champions[unique_id].training_start,
                BOOTSTRAP_AT - timedelta(days=28),
            )
            self.assertEqual(
                champions[unique_id].training_end,
                BOOTSTRAP_AT - timedelta(minutes=15),
            )

    def test_failed_bootstrap_preserves_prior_cache_and_first_failure_stays_initializing(self):
        """Committing each chunk would erase or partially overwrite the last usable cache."""
        first_source = source_with_history()
        cache = StationCache("station-1")
        service = make_service(first_source, FakeSink(), cache=cache)
        first_source.fail_call = 2

        with self.assertRaises(M3Error) as first_error:
            service.bootstrap("station-1", BOOTSTRAP_AT)
        self.assertEqual(first_error.exception.code, "source_http_failed")
        self.assertEqual(service.state("station-1").state, "initializing")
        self.assertEqual(service.state("station-1").last_source_at, None)

        first_source.fail_call = None
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        before = cache.window("station_total_load")
        prior_source_at = service.state("station-1").last_source_at
        first_source.fail_call = len(first_source.calls) + 3

        with self.assertRaises(M3Error):
            service.bootstrap("station-1", BOOTSTRAP_AT + timedelta(days=1))

        self.assertEqual(cache.window("station_total_load"), before)
        self.assertEqual(service.state("station-1").state, "ready")
        self.assertEqual(service.state("station-1").last_source_at, prior_source_at)
        self.assertEqual(service.state("station-1").last_error_code, "source_http_failed")

    def test_model_selection_swaps_two_champions_atomically_and_uses_specified_fallbacks(self):
        """A later candidate failure must not expose a partly replaced champion mapping."""
        cache = StationCache("station-1")
        service = make_service(source_with_history(), FakeSink(), cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        previous = {
            unique_id: fake_champion(
                SimpleNamespace(
                    start=BOOTSTRAP_AT - timedelta(days=28),
                    end=BOOTSTRAP_AT - timedelta(minutes=15),
                ),
                name="AutoETS",
            )
            for unique_id in SERIES_IDS
        }
        cache.state.champions = previous
        cache.state.state = "ready"

        calls = 0

        def fail_second(dataset):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise M3Error("model_selection_failed", "all candidates failed")
            return fake_champion(dataset, name="AutoARIMA")

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fail_second,
        ):
            service.select_models("station-1")

        selected = service.state("station-1")
        self.assertEqual(set(selected.champions), set(SERIES_IDS))
        self.assertEqual(selected.champions["station_total_load"].model_name, "AutoARIMA")
        self.assertEqual(selected.champions["storage_soc"].model_name, "AutoETS")
        self.assertEqual(
            selected.champions["storage_soc"].selection_reason,
            "model_selection_failed",
        )
        self.assertEqual(selected.state, "degraded")
        self.assertEqual(selected.last_error_code, "model_selection_failed")

    def test_online_forecast_uses_previous_champions_while_selection_cv_is_blocked(self):
        """Holding the cache operation lock across CV would stall the online forecast."""
        source = source_with_history()
        cache = StationCache("station-1")
        used_models: list[str] = []

        def recording_forecast(dataset, champion, as_of):
            used_models.append(champion.model_name)
            return fake_forecast_one(dataset, champion, as_of)

        service = make_service(source, FakeSink(), forecast=recording_forecast, cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        old = {
            unique_id: fake_champion(
                SimpleNamespace(
                    start=BOOTSTRAP_AT - timedelta(days=28),
                    end=BOOTSTRAP_AT - timedelta(minutes=15),
                ),
                name="AutoETS",
            )
            for unique_id in SERIES_IDS
        }
        cache.state.champions = old
        cache.state.state = "ready"
        cv_entered = Event()
        release_cv = Event()
        forecast_done = Event()
        errors: list[Exception] = []

        def blocked_selector(dataset):
            cv_entered.set()
            release_cv.wait(timeout=5)
            return fake_champion(dataset, name="AutoARIMA")

        def select():
            try:
                service.select_models("station-1")
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def run():
            try:
                service.run_forecast("station-1", FORECAST_AT)
                forecast_done.set()
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=blocked_selector,
        ):
            selection_thread = Thread(target=select)
            forecast_thread = Thread(target=run)
            selection_thread.start()
            self.assertTrue(cv_entered.wait(timeout=2))
            forecast_thread.start()
            completed_while_cv_blocked = forecast_done.wait(timeout=0.5)
            release_cv.set()
            selection_thread.join(timeout=5)
            forecast_thread.join(timeout=5)

        self.assertTrue(completed_while_cv_blocked)
        self.assertEqual(errors, [])
        self.assertEqual(used_models, ["AutoETS", "AutoETS"])
        self.assertEqual(
            [service.state("station-1").champions[item].model_name for item in SERIES_IDS],
            ["AutoARIMA", "AutoARIMA"],
        )

    def test_second_concurrent_model_selection_waits_for_the_station_selection_guard(self):
        """A second daily selection must not enter candidate CV beside the first one."""
        service = make_service(source_with_history(), FakeSink())
        service.bootstrap("station-1", BOOTSTRAP_AT)
        first_entered = Event()
        release_first = Event()
        calls: list[str] = []
        errors: list[Exception] = []

        def selector(dataset):
            calls.append(dataset.frame["unique_id"].iloc[0])
            if len(calls) == 1:
                first_entered.set()
                release_first.wait(timeout=5)
            return fake_champion(dataset)

        def select():
            try:
                service.select_models("station-1")
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=selector,
        ):
            first = Thread(target=select)
            second = Thread(target=select)
            first.start()
            self.assertTrue(first_entered.wait(timeout=2))
            second.start()
            second.join(timeout=0.25)
            calls_before_release = list(calls)
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertEqual(calls_before_release, ["station_total_load"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(errors, [])

    def test_unexpected_selection_failure_preserves_all_previous_champions(self):
        """An unhandled second-series error must not leak the first new champion."""
        service = make_service(source_with_history(), FakeSink())
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        before = service.state("station-1").champions
        calls = 0

        def fail_second(dataset):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("secret backend details")
            return fake_champion(dataset, name="AutoETS")

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fail_second,
        ), self.assertRaises(M3Error) as raised:
            service.select_models("station-1")

        self.assertEqual(raised.exception.code, "model_selection_failed")
        self.assertNotIn("secret", raised.exception.message)
        self.assertEqual(service.state("station-1").champions, before)
        self.assertEqual(service.state("station-1").state, "degraded")

    def test_selection_failure_without_prior_champion_installs_seasonal_naive(self):
        """A failed first full-history selection still needs the sole allowed baseline."""
        service = make_service(source_with_history(), FakeSink())
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=M3Error("model_selection_failed", "all candidates failed"),
        ):
            service.select_models("station-1")

        state = service.state("station-1")
        self.assertEqual(
            [state.champions[item].model_name for item in SERIES_IDS],
            ["SeasonalNaive", "SeasonalNaive"],
        )
        self.assertEqual(state.state, "degraded")

    def test_forecast_refuses_unconfigured_or_initializing_station_without_source_io(self):
        """An unavailable station must not perform a pull or publish an empty snapshot."""
        source = source_with_history()
        sink = FakeSink()
        cache = StationCache("station-1")
        service = make_service(source, sink, cache=cache)

        for station_id in ("missing", "station-1"):
            with self.subTest(station_id=station_id), self.assertRaises(M3Error) as raised:
                service.run_forecast(station_id, FORECAST_AT)
            self.assertIn(raised.exception.code, {"station_not_configured", "station_not_ready"})
        self.assertEqual(source.calls, [])
        self.assertEqual(sink.latest, [])

    def test_forecast_refuses_unavailable_champion_for_full_history(self):
        """A missing champion is valid only when its own dataset is insufficient."""
        source = source_with_history()
        cache = StationCache("station-1")
        service = make_service(source, FakeSink(), cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        cache.state.champions["storage_soc"] = None
        cache.state.state = "ready"
        source_calls = len(source.calls)

        with self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", FORECAST_AT)

        self.assertEqual(raised.exception.code, "forecast_unavailable")
        self.assertEqual(len(source.calls), source_calls + 1)
        self.assertEqual(
            service.state("station-1").last_error_code,
            "forecast_unavailable",
        )

    def test_run_forecast_pulls_completed_bucket_and_two_overlap_buckets_then_publishes(self):
        """Starting only two buckets back would omit one required late-revision bucket."""
        source = source_with_history()
        sink = FakeSink()
        cache = StationCache("station-1")
        service = make_service(source, sink, cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")

        snapshot = service.run_forecast("station-1", FORECAST_AT)

        pull = source.calls[-1]
        self.assertEqual(pull.start, datetime.fromisoformat("2026-08-25T00:30:00+08:00"))
        self.assertEqual(pull.end, datetime.fromisoformat("2026-08-25T01:15:00+08:00"))
        self.assertEqual(len(snapshot.series), 2)
        self.assertEqual(sum(len(item.points) for item in snapshot.series), 192)
        self.assertEqual(snapshot.generated_at, GENERATED_AT)
        self.assertEqual(service.state("station-1").last_source_at, pull.end)
        self.assertEqual(service.state("station-1").last_published_at, GENERATED_AT)
        self.assertEqual(sink.latest, [snapshot])
        self.assertIn("readiness", snapshot.model_manifest)
        self.assertEqual(
            snapshot.model_manifest["readiness"],
            {
                "required_days": 28,
                "required_points": 2688,
                "series": {
                    "station_total_load": {"real_points": 2688},
                    "storage_soc": {"real_points": 2688},
                },
            },
        )

        body = snapshot.model_dump(exclude={"content_hash"})
        self.assertEqual(snapshot.content_hash, canonical_hash(body))

    def test_sink_failure_keeps_publish_time_and_reuses_identical_pending_snapshot(self):
        """Recomputing after an uncertain write would change generated_at/hash for one as_of."""
        source = source_with_history()
        sink = FakeSink(fail_times=1)
        service = make_service(
            source,
            sink,
            generated_times=[
                GENERATED_AT,
                GENERATED_AT + timedelta(seconds=10),
            ],
        )
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")

        with self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", FORECAST_AT)
        self.assertEqual(raised.exception.code, "sink_http_failed")
        self.assertEqual(service.state("station-1").last_published_at, None)

        snapshot = service.run_forecast("station-1", FORECAST_AT)
        self.assertIs(snapshot, sink.attempts[0])
        self.assertIs(snapshot, sink.attempts[1])
        self.assertEqual(snapshot.generated_at, GENERATED_AT)
        self.assertEqual(service.state("station-1").last_published_at, GENERATED_AT)

    def test_duplicate_success_returns_one_logical_result_without_second_publication(self):
        """A repeated successful call for one as_of must not create another hash or write."""
        source = source_with_history()
        sink = FakeSink()
        service = make_service(
            source,
            sink,
            generated_times=[
                GENERATED_AT,
                GENERATED_AT + timedelta(seconds=10),
            ],
        )
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")

        first = service.run_forecast("station-1", FORECAST_AT)
        second = service.run_forecast("station-1", FORECAST_AT)

        self.assertIs(first, second)
        self.assertEqual(len(sink.latest), 1)

    def test_many_newer_results_release_superseded_snapshots_and_reject_old_as_of(self):
        """A per-as_of result dictionary would retain every prior 15-minute snapshot."""
        start = BOOTSTRAP_AT - timedelta(days=28)
        source = FakeSource(make_points(start, 28 * 96 + 40))
        sink = StatelessSink()
        service = make_service(source, sink)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        references = []
        first_as_of = FORECAST_AT
        for index in range(12):
            snapshot = service.run_forecast(
                "station-1", first_as_of + timedelta(minutes=15 * index)
            )
            references.append(weakref.ref(snapshot))
        del snapshot
        gc.collect()

        self.assertEqual(sum(reference() is not None for reference in references), 1)
        source_calls = len(source.calls)
        with self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", first_as_of)
        self.assertEqual(raised.exception.code, "forecast_superseded")
        self.assertEqual(len(source.calls), source_calls)
        self.assertEqual(sink.publish_count, 12)

    def test_newer_as_of_cannot_bypass_a_different_pending_publication(self):
        """A failed pending write must not be lost or multiplied by a newer computation."""
        start = BOOTSTRAP_AT - timedelta(days=28)
        source = FakeSource(make_points(start, 28 * 96 + 12))
        sink = FakeSink(fail_times=1)
        service = make_service(source, sink)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        with self.assertRaises(M3Error):
            service.run_forecast("station-1", FORECAST_AT)
        calls_after_pending = len(source.calls)

        with self.assertRaises(M3Error) as raised:
            service.run_forecast(
                "station-1", FORECAST_AT + timedelta(minutes=15)
            )

        self.assertEqual(raised.exception.code, "publication_pending")
        self.assertEqual(len(source.calls), calls_after_pending)
        retried = service.run_forecast("station-1", FORECAST_AT)
        self.assertIs(retried, sink.attempts[0])
        self.assertIs(retried, sink.attempts[1])


    @staticmethod
    def write_metadata(
        root: Path,
        dependency: str | list[str] | None,
        lock_version: str | None,
    ):
        dependencies = (
            []
            if dependency is None
            else dependency
            if isinstance(dependency, list)
            else [dependency]
        )
        (root / "pyproject.toml").write_text(
            "[project]\nname='fixture'\nversion='0.1.0'\ndependencies="
            + repr(dependencies)
            + "\n",
            encoding="utf-8",
        )
        packages = (
            ""
            if lock_version is None
            else f'[[package]]\nname = "statsforecast"\nversion = "{lock_version}"\n'
        )
        (root / "uv.lock").write_text(packages, encoding="utf-8")

    def test_project_version_requires_matching_exact_pin_and_lock_entry(self):
        """A range, missing lock row, or disagreeing lock cannot define readiness."""
        cases = (
            ("statsforecast>=2.1.1", "2.1.1"),
            ("statsforecast==2.1.1", None),
            ("statsforecast==2.1.1", "2.1.0"),
            (["statsforecast==2.1.1", "statsforecast>=2.0"], "2.1.1"),
            (None, "2.1.1"),
        )
        for dependency, lock_version in cases:
            with self.subTest(dependency=dependency, lock=lock_version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.write_metadata(root, dependency, lock_version)
                with self.assertRaises(M3Error) as raised:
                    forecast_service.project_statsforecast_version(root)
                self.assertEqual(raised.exception.code, "model_version_invalid")

    def test_runtime_mismatch_refuses_service_construction_before_source_io(self):
        """A different installed StatsForecast build must fail closed before bootstrap."""
        source = source_with_history()
        with patch(
            "m3_worker.services.forecast_service.version", return_value="9.9.9"
        ), self.assertRaises(M3Error) as raised:
            make_service(source, FakeSink())

        self.assertEqual(raised.exception.code, "model_version_invalid")
        self.assertEqual(source.calls, [])

    def test_runtime_drift_removes_ready_state_and_records_error_before_source_io(self):
        """A service that becomes version-invalid must not keep advertising ready."""
        source = source_with_history()
        service = make_service(source, FakeSink())
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        source_calls = len(source.calls)

        with patch(
            "m3_worker.services.forecast_service.version", return_value="9.9.9"
        ), self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", FORECAST_AT)

        self.assertEqual(raised.exception.code, "model_version_invalid")
        self.assertEqual(len(source.calls), source_calls)
        self.assertEqual(service.state("station-1").state, "initializing")
        self.assertEqual(
            service.state("station-1").last_error_code, "model_version_invalid"
        )

    def test_post_cv_version_drift_cannot_be_overwritten_as_degraded(self):
        """The generic selection failure branch must not undo fail-closed state."""
        source = source_with_history()
        cache = StationCache("station-1")
        service = make_service(source, FakeSink(), cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        prior = {
            unique_id: fake_champion(
                SimpleNamespace(
                    start=BOOTSTRAP_AT - timedelta(days=28),
                    end=BOOTSTRAP_AT - timedelta(minutes=15),
                ),
                name="AutoETS",
            )
            for unique_id in SERIES_IDS
        }
        cache.state.champions = prior
        cache.state.state = "ready"

        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ), patch(
            "m3_worker.services.forecast_service.version",
            side_effect=["2.1.1", "9.9.9"],
        ), self.assertRaises(M3Error) as raised:
            service.select_models("station-1")

        self.assertEqual(raised.exception.code, "model_version_invalid")
        state = service.state("station-1")
        self.assertEqual(state.state, "initializing")
        self.assertEqual(state.last_error_code, "model_version_invalid")
        self.assertEqual(state.champions, prior)

    def test_snapshot_time_version_drift_cannot_leave_station_ready_or_publish(self):
        """A post-compute version failure must outrank the generic forecast branch."""
        source = source_with_history()
        sink = FakeSink()
        cache = StationCache("station-1")
        service = make_service(source, sink, cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        champion = fake_champion(
            SimpleNamespace(
                start=BOOTSTRAP_AT - timedelta(days=28),
                end=BOOTSTRAP_AT - timedelta(minutes=15),
            )
        )
        cache.state.champions = {unique_id: champion for unique_id in SERIES_IDS}
        cache.state.state = "ready"

        with patch(
            "m3_worker.services.forecast_service.version",
            side_effect=["2.1.1", "9.9.9"],
        ), self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", FORECAST_AT)

        self.assertEqual(raised.exception.code, "model_version_invalid")
        state = service.state("station-1")
        self.assertEqual(state.state, "initializing")
        self.assertEqual(state.last_error_code, "model_version_invalid")
        self.assertEqual(state.last_published_at, None)
        self.assertEqual(sink.latest, [])

    def test_snapshot_rejects_champion_version_that_differs_from_project_pin(self):
        """Internally consistent champions are still invalid when they differ from the lock."""
        cache = StationCache("station-1")
        service = make_service(source_with_history(), FakeSink(), cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        champions = {
            key: value.__class__(
                **{**value.__dict__, "statsforecast_version": "9.9.9"}
            )
            for key, value in service.state("station-1").champions.items()
        }
        series = [
            fake_forecast_one(
                build_training_dataset(cache.window(unique_id), unique_id),
                service.state("station-1").champions[unique_id],
                FORECAST_AT,
            )
            for unique_id in SERIES_IDS
        ]

        with self.assertRaises(M3Error) as raised:
            build_latest_snapshot(
                "station-1",
                FORECAST_AT,
                GENERATED_AT,
                datetime.fromisoformat("2026-08-25T01:15:00+08:00"),
                series,
                champions,
            )

        self.assertEqual(raised.exception.code, "model_version_invalid")

    def test_source_merge_failure_does_not_advance_source_or_publish_state(self):
        """A conflicting overlap revision must not partially mutate source/publication state."""
        source = source_with_history()
        sink = FakeSink()
        cache = StationCache("station-1")
        service = make_service(source, sink, cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        before = cache.window("station_total_load")
        source.points = points_at(
            BOOTSTRAP_AT - timedelta(minutes=15), value_delta=999.0
        )

        with self.assertRaises(M3Error) as raised:
            service.run_forecast("station-1", FORECAST_AT)

        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(cache.window("station_total_load"), before)
        self.assertEqual(service.state("station-1").last_source_at, BOOTSTRAP_AT)
        self.assertEqual(service.state("station-1").last_published_at, None)
        self.assertEqual(sink.latest, [])

    def test_snapshot_builder_rejects_missing_champion_keys_and_duplicate_series(self):
        """A superficially two-item payload must not hide a duplicate terminal series."""
        source = source_with_history()
        cache = StationCache("station-1")
        service = make_service(source, FakeSink(), cache=cache)
        service.bootstrap("station-1", BOOTSTRAP_AT)
        with patch(
            "m3_worker.services.forecast_service.select_champion",
            side_effect=fake_champion,
        ):
            service.select_models("station-1")
        champions = service.state("station-1").champions
        datasets = {
            unique_id: build_training_dataset(cache.window(unique_id), unique_id)
            for unique_id in SERIES_IDS
        }
        series = [
            fake_forecast_one(datasets[unique_id], champions[unique_id], FORECAST_AT)
            for unique_id in SERIES_IDS
        ]

        with self.assertRaises(M3Error):
            build_latest_snapshot(
                "station-1",
                FORECAST_AT,
                GENERATED_AT,
                datetime.fromisoformat("2026-08-25T01:15:00+08:00"),
                [series[0], series[0]],
                champions,
            )
        with self.assertRaises(M3Error):
            build_latest_snapshot(
                "station-1",
                FORECAST_AT,
                GENERATED_AT,
                datetime.fromisoformat("2026-08-25T01:15:00+08:00"),
                series,
                {key: champions[key] for key in SERIES_IDS[:-1]},
            )

    def test_station_operation_lock_prevents_bootstrap_and_forecast_overlap(self):
        """Without a station boundary, forecast could read cache during full replacement."""
        entered = Event()
        release = Event()

        class BlockingSource(FakeSource):
            def list_observations(self, station_id, start, end):
                if start == BOOTSTRAP_AT - timedelta(days=90):
                    entered.set()
                    release.wait(timeout=5)
                return super().list_observations(station_id, start, end)

        source = BlockingSource(source_with_history().points)
        cache = StationCache("station-1")
        service = make_service(source, FakeSink(), cache=cache)
        cache.state.champions = {
                unique_id: fake_champion(
                    SimpleNamespace(
                        start=BOOTSTRAP_AT - timedelta(days=28),
                        end=BOOTSTRAP_AT - timedelta(minutes=15),
                    )
                )
                for unique_id in SERIES_IDS
            }
        cache.state.state = "ready"
        errors: list[Exception] = []

        def bootstrap():
            try:
                service.bootstrap("station-1", BOOTSTRAP_AT)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def forecast():
            try:
                service.run_forecast("station-1", FORECAST_AT)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        bootstrap_thread = Thread(target=bootstrap)
        forecast_thread = Thread(target=forecast)
        bootstrap_thread.start()
        self.assertTrue(entered.wait(timeout=2))
        forecast_thread.start()
        self.assertEqual(len(source.calls), 0)
        release.set()
        bootstrap_thread.join(timeout=5)
        forecast_thread.join(timeout=5)

        self.assertFalse(bootstrap_thread.is_alive())
        self.assertFalse(forecast_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(source.calls), 14)


if __name__ == "__main__":
    unittest.main()
