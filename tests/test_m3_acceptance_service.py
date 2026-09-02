"""Seven-day acceptance orchestration and persistence-boundary tests."""

from collections import UserDict
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
import math
from threading import Event, Thread
from types import SimpleNamespace
import unittest

import httpx

from m3_worker.clients.http import RetryPolicy
from m3_worker.clients.nocobase_api import NocoBaseApiClient
from m3_worker.contracts import (
    AcceptanceContext,
    ForecastPoint,
    ForecastSeries,
    LatestSnapshot,
    ObservationPoint,
)
from m3_worker.domain.evaluation import MetricResult
from m3_worker.errors import M3Error
from m3_worker.services.acceptance_service import AcceptanceService
from m3_worker.sinks.forecast_sink import ForecastSink, acceptance_content_hash


START = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
END = START + timedelta(days=7)
AS_OF = datetime.fromisoformat("2026-08-25T01:02:00+08:00")
SERIES = (
    ("station_total_load", "kW", 800.0),
    ("storage_soc", "%", 55.0),
)
SERIES_IDS = tuple(item[0] for item in SERIES)


def active_context(**updates) -> AcceptanceContext:
    values = {
        "active": True,
        "acceptance_run_id": "run-20260825",
        "window_start": START,
        "window_end": END,
    }
    values.update(updates)
    return AcceptanceContext(**values)


def snapshot_at(as_of: datetime = AS_OF, *, status: str = "ok") -> LatestSnapshot:
    baseline_start = as_of.replace(minute=0, second=0, microsecond=0)
    series = []
    for unique_id, unit, value in SERIES:
        points = [
            ForecastPoint(
                data_time=baseline_start + timedelta(minutes=15 * index),
                target_time=baseline_start + timedelta(minutes=15 * (index + 1)),
                horizon_step=index + 1,
                raw_forecast=value,
                forecast_value=value,
                is_clipped=False,
            )
            for index in range(96)
        ]
        series.append(
            ForecastSeries(
                unique_id=unique_id,
                unit=unit,
                model_name="SeasonalNaive",
                status=status,
                points=points,
                fallback_reason="champion_failed" if status == "degraded" else None,
            )
        )
    return LatestSnapshot(
        station_id="station-1",
        as_of=as_of,
        generated_at=as_of + timedelta(seconds=5),
        source_data_end=baseline_start,
        status=status,
        series=series,
        model_manifest={"statsforecast_version": "2.1.1"},
        content_hash="latest-hash",
    )


def stored_point(
    unique_id: str,
    data_time: datetime,
    *,
    record_id: int = 1,
    batch_id: int = 41,
    forecast_value: float = 90.0,
    revision: int | None = None,
) -> dict:
    return {
        "id": record_id,
        "batch_id": batch_id,
        "unique_id": unique_id,
        "data_time": data_time.isoformat(),
        "forecast_value": forecast_value,
        "actual_source_revision": revision,
    }


def evaluation_rows(unique_id: str, *, value: float = 100.0) -> list[dict]:
    return [
        {
            "batch_id": 41 + index // 96,
            "unique_id": unique_id,
            "data_time": (START + timedelta(minutes=15 * index)).isoformat(),
            "actual_value": value,
            "forecast_value": 90.0,
            "actual_quality": "valid",
        }
        for index in range(672)
    ]


def complete_batch_row(
    *,
    record_id: int = 41,
    station_id: str = "station-1",
    acceptance_run_id: str = "run-20260825",
    issued_at: datetime | None = None,
    forecast_start: datetime = START,
    forecast_end: datetime = START + timedelta(days=1),
    write_state: str = "complete",
) -> dict:
    return {
        "id": record_id,
        "station_id": station_id,
        "acceptance_run_id": acceptance_run_id,
        "issued_at": (
            forecast_start + timedelta(minutes=2)
            if issued_at is None
            else issued_at
        ).isoformat(),
        "forecast_start_time": forecast_start.isoformat(),
        "forecast_end_time": forecast_end.isoformat(),
        "write_state": write_state,
    }


class FakeSource:
    def __init__(self, *, context=None, observations=None) -> None:
        self.context = context if context is not None else active_context()
        self.observations = list(observations or [])
        self.context_calls: list[str] = []
        self.observation_calls: list[SimpleNamespace] = []
        self.progress_calls: list[SimpleNamespace] = []
        self.result_calls: list[SimpleNamespace] = []

    def active_run(self, station_id):
        self.context_calls.append(station_id)
        if not self.context.active:
            return None
        return SimpleNamespace(
            acceptance_run_id=self.context.acceptance_run_id,
            window_start=self.context.window_start,
            window_end=self.context.window_end,
        )

    def list_observations(self, station_id, start, end):
        self.observation_calls.append(
            SimpleNamespace(station_id=station_id, start=start, end=end)
        )
        return [point for point in self.observations if start <= point.ds < end]

    def sync_progress(self, station_id, acceptance_run_id):
        self.progress_calls.append(
            SimpleNamespace(
                station_id=station_id, acceptance_run_id=acceptance_run_id
            )
        )

    def sync_result(self, station_id, acceptance_run_id, outcome, calculated_at):
        self.result_calls.append(
            SimpleNamespace(
                station_id=station_id,
                acceptance_run_id=acceptance_run_id,
                outcome=outcome,
                calculated_at=calculated_at,
            )
        )


class FakeApi:
    def __init__(
        self,
        *,
        complete_batches=None,
        backfill_rows=None,
        rows_by_series=None,
        point_list_error=None,
        ignore_point_batch_filter: bool = False,
        ignore_point_time_filter: bool = False,
    ) -> None:
        self.complete_batches = list(
            [
                complete_batch_row(
                    record_id=41 + day,
                    forecast_start=START + timedelta(days=day),
                    forecast_end=START + timedelta(days=day + 1),
                )
                for day in range(7)
            ]
            if complete_batches is None
            else complete_batches
        )
        self.backfill_rows = list(backfill_rows or [])
        self.rows_by_series = dict(rows_by_series or {})
        self.point_list_error = point_list_error
        self.ignore_point_batch_filter = ignore_point_batch_filter
        self.ignore_point_time_filter = ignore_point_time_filter
        self.list_calls: list[SimpleNamespace] = []
        self.update_calls: list[SimpleNamespace] = []
        self.upsert_calls: list[SimpleNamespace] = []
        self.writing_batches: list[dict] = []
        self.wrong_update_response: dict | None = None
        self.wrong_upsert_response: dict | None = None

    def list_records(self, collection, *, filter, fields, sort=None):
        self.list_calls.append(
            SimpleNamespace(
                collection=collection, filter=filter, fields=fields, sort=sort
            )
        )
        if collection == "energy_forecast_batches":
            rows = (
                self.writing_batches
                if "model_manifest" in fields
                else self.complete_batches
            )
            return [dict(row) for row in rows]
        if collection != "energy_forecast_points":
            raise AssertionError(collection)
        if self.point_list_error is not None:
            raise self.point_list_error
        unique_id = filter.get("unique_id")
        rows = (
            self.backfill_rows
            if "id" in fields
            else self.rows_by_series.get(unique_id, [])
        )
        if unique_id is not None:
            rows = [row for row in rows if row.get("unique_id") == unique_id]
        if "batch_id" in filter and not self.ignore_point_batch_filter:
            rows = [row for row in rows if row.get("batch_id") == filter["batch_id"]]
        if "data_time" in filter and not self.ignore_point_time_filter:
            lower = datetime.fromisoformat(filter["data_time"]["$gte"])
            upper = datetime.fromisoformat(filter["data_time"]["$lt"])
            filtered = []
            for row in rows:
                try:
                    data_time = datetime.fromisoformat(row["data_time"])
                    if data_time.tzinfo is not None and lower <= data_time < upper:
                        filtered.append(row)
                    elif data_time.tzinfo is None:
                        filtered.append(row)
                except (TypeError, ValueError):
                    filtered.append(row)
            rows = filtered
        return [{field: row[field] for field in fields} for row in rows]

    def update_record(self, collection, record_id, values):
        call = SimpleNamespace(
            collection=collection, record_id=record_id, values=dict(values)
        )
        self.update_calls.append(call)
        if self.wrong_update_response is not None:
            return dict(self.wrong_update_response)
        row = next(row for row in self.backfill_rows if row["id"] == record_id)
        return {**row, **values}

    def update_or_create(self, collection, filter, values):
        call = SimpleNamespace(
            collection=collection, filter=dict(filter), values=dict(values)
        )
        self.upsert_calls.append(call)
        if self.wrong_upsert_response is not None:
            return dict(self.wrong_upsert_response)
        return {"id": len(self.upsert_calls), **values}


class FakeSink:
    def __init__(self) -> None:
        self.publish_calls: list[SimpleNamespace] = []
        self.reconcile_calls: list[SimpleNamespace] = []

    def publish_acceptance(self, batch, points):
        self.publish_calls.append(
            SimpleNamespace(batch=dict(batch), points=[dict(point) for point in points])
        )
        return {"id": 10, "write_state": "complete", **batch}

    def reconcile_acceptance(self, batch, points=None):
        self.reconcile_calls.append(
            SimpleNamespace(batch=dict(batch), points=points)
        )
        return {**batch, "write_state": "complete"}


class FakeForecastService:
    def __init__(self, snapshot=None) -> None:
        self.snapshot = snapshot if snapshot is not None else snapshot_at()
        self.calls: list[SimpleNamespace] = []

    def run_forecast(self, station_id, as_of):
        self.calls.append(SimpleNamespace(station_id=station_id, as_of=as_of))
        return self.snapshot


def make_service(
    *,
    source=None,
    api=None,
    sink=None,
    forecast_service=None,
    now=None,
    evaluator=None,
) -> tuple[AcceptanceService, FakeSource, FakeApi, FakeSink, FakeForecastService]:
    source = source or FakeSource()
    api = api or FakeApi()
    sink = sink or FakeSink()
    forecast_service = forecast_service or FakeForecastService()
    service = AcceptanceService(
        run_service=source,
        observation_source=source,
        api=api,
        sink=sink,
        forecast_service=forecast_service,
        now=now or (lambda: datetime.fromisoformat("2026-08-25T01:02:05+08:00")),
        evaluator=evaluator,
    )
    return service, source, api, sink, forecast_service


class BaselineTests(unittest.TestCase):
    def test_baseline_publishes_before_an_unavailable_backfill_point_query(self):
        """Historical point-list outages must not suppress an exact-slot baseline."""
        api = FakeApi(
            point_list_error=M3Error("sink_http_failed", "point list unavailable")
        )
        service, _, api, sink, _ = make_service(api=api)

        service.run_baseline("station-1", AS_OF)

        self.assertEqual(len(sink.publish_calls), 1)
        self.assertEqual(len(sink.publish_calls[0].points), 192)
        self.assertFalse(
            any(
                call.collection == "energy_forecast_points"
                for call in api.list_calls
            )
        )

    def test_exact_0102_publishes_sorted_immutable_0100_to_0100_templates(self):
        """Shifting the baseline one bucket or persisting mutable actual fields corrupts evidence."""
        service, _, _, sink, _ = make_service()

        result = service.run_baseline("station-1", AS_OF)

        self.assertEqual(result["write_state"], "complete")
        self.assertEqual(len(sink.publish_calls), 1)
        published = sink.publish_calls[0]
        self.assertEqual(len(published.points), 192)
        self.assertEqual(
            [(point["unique_id"], point["data_time"]) for point in published.points],
            sorted(
                (point["unique_id"], point["data_time"])
                for point in published.points
            ),
        )
        first = next(
            point
            for point in published.points
            if point["unique_id"] == "station_total_load"
        )
        last = [
            point
            for point in published.points
            if point["unique_id"] == "station_total_load"
        ][-1]
        self.assertEqual(first["data_time"], "2026-08-25T01:00:00+08:00")
        self.assertEqual(first["target_time"], "2026-08-25T01:15:00+08:00")
        self.assertEqual(last["data_time"], "2026-08-26T00:45:00+08:00")
        self.assertEqual(last["target_time"], "2026-08-26T01:00:00+08:00")
        immutable = {
            "unique_id",
            "data_time",
            "target_time",
            "horizon_step",
            "model_name",
            "raw_forecast",
            "forecast_value",
            "is_clipped",
        }
        self.assertTrue(all(set(point) == immutable for point in published.points))
        self.assertNotIn("batch_id", published.points[0])
        self.assertIsNotNone(published.batch["point_templates"])
        self.assertEqual(
            published.batch["content_hash"],
            acceptance_content_hash(published.batch, published.points),
        )

    def test_any_time_other_than_exact_0102_cannot_create_a_posthoc_baseline(self):
        """Accepting a late/manual rolling run would allow a missing daily baseline to be replaced."""
        invalid = (
            datetime.fromisoformat("2026-08-25T01:01:00+08:00"),
            datetime.fromisoformat("2026-08-25T01:02:01+08:00"),
            datetime.fromisoformat("2026-08-25T02:02:00+08:00"),
            datetime.fromisoformat("2026-08-25T01:02:00+00:00"),
        )
        for as_of in invalid:
            service, source, _, sink, forecast = make_service()
            with self.subTest(as_of=as_of), self.assertRaises(M3Error) as raised:
                service.run_baseline("station-1", as_of)
            self.assertEqual(raised.exception.code, "acceptance_slot_invalid")
            self.assertEqual(source.context_calls, [])
            self.assertEqual(forecast.calls, [])
            self.assertEqual(sink.publish_calls, [])

    def test_exact_historical_or_expired_0102_is_rejected_before_external_work(self):
        """An exact-looking old as_of must not regenerate a baseline after its scheduled slot."""
        current_times = (
            datetime.fromisoformat("2026-08-26T01:02:00+08:00"),
            datetime.fromisoformat("2026-08-25T01:17:00+08:00"),
        )
        for current in current_times:
            clock_calls = 0

            def now(current=current):
                nonlocal clock_calls
                clock_calls += 1
                return current

            service, source, api, sink, forecast = make_service(now=now)

            with self.subTest(current=current), self.assertRaises(M3Error) as raised:
                service.run_baseline("station-1", AS_OF)

            self.assertEqual(raised.exception.code, "acceptance_slot_invalid")
            self.assertEqual(clock_calls, 1)
            self.assertEqual(source.context_calls, [])
            self.assertEqual(api.list_calls, [])
            self.assertEqual(forecast.calls, [])
            self.assertEqual(sink.publish_calls, [])

    def test_baseline_accepts_current_0102_slot_and_reads_gate_clock_once(self):
        """Normal computation and retries during [01:02, 01:17) must remain allowed."""
        clock_calls = 0

        def now():
            nonlocal clock_calls
            clock_calls += 1
            return datetime.fromisoformat("2026-08-25T01:02:47+08:00")

        service, _, _, sink, _ = make_service(now=now)

        result = service.run_baseline("station-1", AS_OF)

        self.assertEqual(result["write_state"], "complete")
        self.assertEqual(clock_calls, 1)
        self.assertEqual(len(sink.publish_calls), 1)

    def test_context_must_be_active_nonempty_exactly_seven_days_and_contain_batch(self):
        """A stale, anonymous, short, or wrong-window run must never receive formal evidence."""
        inactive_service, _, _, inactive_sink, inactive_forecast = make_service(
            source=FakeSource(context=AcceptanceContext(active=False))
        )
        self.assertIsNone(inactive_service.run_baseline("station-1", AS_OF))
        self.assertEqual(inactive_forecast.calls, [])
        self.assertEqual(inactive_sink.publish_calls, [])
        contexts = (
            active_context(acceptance_run_id=""),
            active_context(window_end=END - timedelta(minutes=15)),
            active_context(window_start=START + timedelta(days=1), window_end=END + timedelta(days=1)),
        )
        expected_codes = (
            "acceptance_context_invalid",
            "acceptance_window_invalid",
            "acceptance_window_invalid",
        )
        for context, expected_code in zip(contexts, expected_codes, strict=True):
            service, _, _, sink, forecast = make_service(
                source=FakeSource(context=context)
            )
            with self.subTest(code=expected_code), self.assertRaises(M3Error) as raised:
                service.run_baseline("station-1", AS_OF)
            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(forecast.calls, [])
            self.assertEqual(sink.publish_calls, [])

    def test_snapshot_identity_status_and_exact_topology_are_checked_before_acceptance_write(self):
        """A cross-station, wrong-run-time, warming, or shifted horizon cannot enter the evidence table."""
        wrong_station = snapshot_at().model_copy(update={"station_id": "station-2"})
        wrong_as_of = snapshot_at().model_copy(
            update={"as_of": AS_OF + timedelta(minutes=15)}
        )
        warming = snapshot_at(status="ok").model_copy(update={"status": "warming_up"})
        shifted = snapshot_at()
        shifted_series = list(shifted.series)
        shifted_points = list(shifted_series[0].points)
        shifted_points[0] = shifted_points[0].model_copy(
            update={
                "data_time": START + timedelta(minutes=15),
                "target_time": START + timedelta(minutes=30),
            }
        )
        shifted_series[0] = shifted_series[0].model_copy(update={"points": shifted_points})
        shifted = shifted.model_copy(update={"series": shifted_series})
        cases = (wrong_station, wrong_as_of, warming, shifted)
        for snapshot in cases:
            service, _, _, sink, _ = make_service(
                forecast_service=FakeForecastService(snapshot)
            )
            with self.subTest(snapshot=snapshot), self.assertRaises(M3Error) as raised:
                service.run_baseline("station-1", AS_OF)
            self.assertEqual(raised.exception.code, "acceptance_write_incomplete")
            self.assertEqual(sink.publish_calls, [])

    def test_invalid_snapshot_prevents_prior_actual_mutation_and_valid_retry_publishes(self):
        """Baseline retries publish without coupling to historical actual backfill."""
        day_two_as_of = AS_OF + timedelta(days=1)
        prior = ObservationPoint(
            unique_id="station_total_load",
            ds=START,
            y=100.0,
            quality="valid",
            source_revision=2,
        )
        source = FakeSource(observations=[prior])
        api = FakeApi(
            backfill_rows=[
                stored_point(
                    "station_total_load", START, record_id=1, revision=1
                )
            ]
        )
        invalid = snapshot_at(day_two_as_of).model_copy(
            update={"station_id": "station-2"}
        )
        forecast = FakeForecastService(invalid)
        service, _, api, sink, _ = make_service(
            source=source,
            api=api,
            sink=FakeSink(),
            forecast_service=forecast,
            now=lambda: day_two_as_of + timedelta(seconds=5),
        )

        with self.assertRaises(M3Error) as raised:
            service.run_baseline("station-1", day_two_as_of)

        self.assertEqual(raised.exception.code, "acceptance_write_incomplete")
        self.assertEqual(api.update_calls, [])
        self.assertEqual(sink.publish_calls, [])

        forecast.snapshot = snapshot_at(day_two_as_of)
        result = service.run_baseline("station-1", day_two_as_of)

        self.assertEqual(result["write_state"], "complete")
        self.assertEqual(api.update_calls, [])
        self.assertEqual(len(sink.publish_calls), 1)

    def test_boolean_horizon_in_raw_snapshot_fails_before_any_acceptance_mutation(self):
        """Python bool must not masquerade as horizon step one at the typed boundary."""
        raw = snapshot_at().model_dump(mode="python")
        raw["series"][0]["points"][0]["horizon_step"] = True
        service, _, api, sink, _ = make_service(
            forecast_service=FakeForecastService(raw)
        )

        with self.assertRaises(M3Error) as raised:
            service.run_baseline("station-1", AS_OF)

        self.assertEqual(raised.exception.code, "acceptance_write_incomplete")
        self.assertEqual(api.update_calls, [])
        self.assertEqual(sink.publish_calls, [])

    def test_non_exact_json_payload_is_rejected_before_backfill_or_acceptance_http(self):
        """Every outbound value must be provably exact JSON before prior evidence mutates."""
        class ManifestEnum(Enum):
            VALUE = "value"

        deeply_nested: dict = {"value": "leaf"}
        for _ in range(40):
            deeply_nested = {"nested": deeply_nested}
        manifests = {
            "top-level-custom-mapping": UserDict({"series": "coerced"}),
            "tuple": {"series": ("not", "exact", "json")},
            "decimal": {"value": Decimal("1.5")},
            "enum": {"value": ManifestEnum.VALUE},
            "custom-mapping": {"value": UserDict({"nested": "value"})},
            "non-string-key": {"value": {1: "value"}},
            "excessive-depth": deeply_nested,
        }
        day_two_as_of = AS_OF + timedelta(days=1)
        for case, manifest in manifests.items():
            requests: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(200, json={"data": {"id": 1}})

            nocobase = NocoBaseApiClient(
                "http://nocobase.internal",
                "sink-secret",
                httpx.Client(transport=httpx.MockTransport(handler)),
                RetryPolicy(max_attempts=1, base_delay_seconds=0),
            )
            prior = ObservationPoint(
                unique_id="station_total_load",
                ds=START,
                y=100.0,
                quality="valid",
                source_revision=2,
            )
            source = FakeSource(observations=[prior])
            api = FakeApi(
                backfill_rows=[
                    stored_point(
                        "station_total_load", START, record_id=1, revision=1
                    )
                ]
            )
            snapshot = snapshot_at(day_two_as_of).model_copy(
                update={"model_manifest": manifest}
            )
            service, _, api, _, _ = make_service(
                source=source,
                api=api,
                sink=ForecastSink(nocobase),
                forecast_service=FakeForecastService(snapshot),
                now=lambda: day_two_as_of + timedelta(seconds=5),
            )

            with self.subTest(case=case), self.assertRaises(M3Error) as raised:
                service.run_baseline("station-1", day_two_as_of)

            self.assertEqual(raised.exception.code, "sink_contract_invalid")
            self.assertEqual(api.update_calls, [])
            self.assertEqual(requests, [])

    def test_raw_snapshot_coercible_types_fail_before_backfill_or_acceptance_http(self):
        """Raw tuple/Mapping/Enum origins must be inspected before Pydantic can erase them."""
        class ModelName(str, Enum):
            SEASONAL_NAIVE = "SeasonalNaive"

        day_two_as_of = AS_OF + timedelta(days=1)
        valid = snapshot_at(day_two_as_of).model_dump(mode="python")
        tuple_series = {**valid, "series": tuple(valid["series"])}
        custom_manifest = {
            **valid,
            "model_manifest": UserDict({"series": "coerced"}),
        }
        enum_model = snapshot_at(day_two_as_of).model_dump(mode="python")
        enum_model["series"][0]["model_name"] = ModelName.SEASONAL_NAIVE
        cases = {
            "tuple-series": tuple_series,
            "custom-manifest": custom_manifest,
            "enum-model-name": enum_model,
        }

        for case, raw_snapshot in cases.items():
            requests: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(200, json={"data": {"id": 1}})

            nocobase = NocoBaseApiClient(
                "http://nocobase.internal",
                "sink-secret",
                httpx.Client(transport=httpx.MockTransport(handler)),
                RetryPolicy(max_attempts=1, base_delay_seconds=0),
            )
            prior = ObservationPoint(
                unique_id="station_total_load",
                ds=START,
                y=100.0,
                quality="valid",
                source_revision=2,
            )
            api = FakeApi(
                backfill_rows=[
                    stored_point(
                        "station_total_load", START, record_id=1, revision=1
                    )
                ]
            )
            service, _, api, _, _ = make_service(
                source=FakeSource(observations=[prior]),
                api=api,
                sink=ForecastSink(nocobase),
                forecast_service=FakeForecastService(raw_snapshot),
                now=lambda: day_two_as_of + timedelta(seconds=5),
            )

            with self.subTest(case=case), self.assertRaises(M3Error):
                service.run_baseline("station-1", day_two_as_of)

            self.assertEqual(api.update_calls, [])
            self.assertEqual(requests, [])

    def test_complete_nested_origin_graph_fails_before_any_baseline_side_effect(self):
        """Raw and model_copy-bypassed nested values must fail before backfill or publish."""
        class Text(str, Enum):
            STATION = "station-1"
            SERIES_STATUS = "ok"
            DATA_TIME = "2026-08-26T01:00:00+08:00"

        day_two_as_of = AS_OF + timedelta(days=1)

        def raw_case(field: str, value: object) -> dict:
            raw = snapshot_at(day_two_as_of).model_dump(mode="python")
            if field == "points":
                raw["series"][0]["points"] = value
            else:
                raw["series"][0]["points"][0][field] = value
            return raw

        valid_raw = snapshot_at(day_two_as_of).model_dump(mode="python")
        raw_points_tuple = raw_case(
            "points", tuple(valid_raw["series"][0]["points"])
        )
        raw_decimal = raw_case("raw_forecast", Decimal("800"))
        raw_clipped_integer = raw_case("is_clipped", 0)
        raw_boolean_forecast = raw_case("raw_forecast", True)
        raw_boolean_forecast["series"][0]["points"][0]["forecast_value"] = 1.0

        typed = snapshot_at(day_two_as_of)
        typed_series = list(typed.series)
        typed_series[0] = typed_series[0].model_copy(
            update={"points": tuple(typed_series[0].points)}
        )
        typed_points_tuple = typed.model_copy(update={"series": typed_series})

        def typed_point_case(**updates) -> LatestSnapshot:
            snapshot = snapshot_at(day_two_as_of)
            series = list(snapshot.series)
            points = list(series[0].points)
            points[0] = points[0].model_copy(update=updates)
            series[0] = series[0].model_copy(update={"points": points})
            return snapshot.model_copy(update={"series": series})

        cases = {
            "raw-points-tuple": raw_points_tuple,
            "raw-decimal": raw_decimal,
            "raw-is-clipped-integer": raw_clipped_integer,
            "raw-boolean-forecast": raw_boolean_forecast,
            "typed-points-tuple": typed_points_tuple,
            "typed-decimal": typed_point_case(raw_forecast=Decimal("800")),
            "typed-is-clipped-integer": typed_point_case(is_clipped=0),
            "typed-boolean-forecast": typed_point_case(
                raw_forecast=True, forecast_value=1.0
            ),
            "nested-copy-timestamp-subclass": typed_point_case(
                data_time=Text.DATA_TIME
            ),
            "series-copy-status-enum": snapshot_at(day_two_as_of).model_copy(
                update={
                    "series": [
                        snapshot_at(day_two_as_of).series[0].model_copy(
                            update={"status": Text.SERIES_STATUS}
                        ),
                        *snapshot_at(day_two_as_of).series[1:],
                    ]
                }
            ),
            "snapshot-copy-station-enum": snapshot_at(day_two_as_of).model_copy(
                update={"station_id": Text.STATION}
            ),
        }

        for case, raw_snapshot in cases.items():
            requests: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(200, json={"data": {"id": 1}})

            nocobase = NocoBaseApiClient(
                "http://nocobase.internal",
                "sink-secret",
                httpx.Client(transport=httpx.MockTransport(handler)),
                RetryPolicy(max_attempts=1, base_delay_seconds=0),
            )

            class CountingSink:
                def __init__(self) -> None:
                    self.publish_calls = 0
                    self.real = ForecastSink(nocobase)

                def publish_acceptance(self, batch, points):
                    self.publish_calls += 1
                    return self.real.publish_acceptance(batch, points)

            sink = CountingSink()
            prior = ObservationPoint(
                unique_id="station_total_load",
                ds=START,
                y=100.0,
                quality="valid",
                source_revision=2,
            )
            api = FakeApi(
                backfill_rows=[
                    stored_point(
                        "station_total_load", START, record_id=1, revision=1
                    )
                ]
            )
            service, _, api, _, _ = make_service(
                source=FakeSource(observations=[prior]),
                api=api,
                sink=sink,
                forecast_service=FakeForecastService(raw_snapshot),
                now=lambda: day_two_as_of + timedelta(seconds=5),
            )

            with self.subTest(case=case), self.assertRaises(M3Error):
                service.run_baseline("station-1", day_two_as_of)

            self.assertEqual(api.update_calls, [])
            self.assertEqual(sink.publish_calls, 0)
            self.assertEqual(requests, [])

    def test_station_mutex_serializes_two_baselines_for_the_same_station(self):
        """Concurrent baseline construction must not race context, backfill, and publication."""
        entered = Event()
        release = Event()
        second_finished = Event()

        class BlockingForecast(FakeForecastService):
            def run_forecast(self, station_id, as_of):
                if not entered.is_set():
                    entered.set()
                    release.wait(timeout=3)
                return super().run_forecast(station_id, as_of)

        forecast = BlockingForecast()
        service, _, _, sink, _ = make_service(forecast_service=forecast)
        errors: list[Exception] = []

        def run(mark_second=False):
            try:
                service.run_baseline("station-1", AS_OF)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)
            finally:
                if mark_second:
                    second_finished.set()

        first = Thread(target=run)
        second = Thread(target=lambda: run(True))
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        self.assertFalse(second_finished.wait(timeout=0.15))
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(len(sink.publish_calls), 2)


class BackfillTests(unittest.TestCase):
    def test_backfill_lists_complete_batches_then_points_by_direct_batch_id(self):
        """NocoBase must receive only direct batch and point filters."""
        api = FakeApi(
            complete_batches=[complete_batch_row(record_id=41)],
            backfill_rows=[stored_point("station_total_load", START, batch_id=41)],
        )
        service, _, api, _, _ = make_service(api=api)

        service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
        )

        batch_call = next(
            call
            for call in api.list_calls
            if call.collection == "energy_forecast_batches"
        )
        self.assertEqual(
            batch_call.filter,
            {
                "station_id": "station-1",
                "acceptance_run_id": "run-20260825",
                "write_state": "complete",
            },
        )
        point_calls = [
            call
            for call in api.list_calls
            if call.collection == "energy_forecast_points"
        ]
        self.assertTrue(point_calls)
        self.assertTrue(all(call.filter["batch_id"] == 41 for call in point_calls))
        self.assertTrue(
            all(not any("." in key for key in call.filter) for call in point_calls)
        )

    def test_rejects_invalid_complete_batch_identity_or_window(self):
        """A malformed complete batch must not authorize its returned points."""
        cases = (
            [complete_batch_row(), complete_batch_row()],
            [complete_batch_row(station_id="station-2")],
            [complete_batch_row(acceptance_run_id="run-foreign")],
            [complete_batch_row(forecast_end=START + timedelta(hours=23))],
            [
                complete_batch_row(),
                complete_batch_row(
                    record_id=42,
                    forecast_start=START + timedelta(hours=23),
                    forecast_end=START + timedelta(hours=47),
                ),
            ],
        )
        for complete_batches in cases:
            with self.subTest(complete_batches=complete_batches):
                service, _, _, _, _ = make_service(
                    api=FakeApi(complete_batches=complete_batches)
                )

                with self.assertRaises(M3Error) as raised:
                    service.backfill_actuals(
                        "station-1",
                        datetime.fromisoformat("2026-08-25T01:17:00+08:00"),
                    )

                self.assertEqual(raised.exception.code, "acceptance_points_incomplete")

    def test_rejects_complete_batch_offset_from_formal_daily_slot(self):
        """A 24-hour batch must start on an integer day from the run window."""
        forecast_start = START + timedelta(minutes=15)
        service, _, _, _, _ = make_service(
            api=FakeApi(
                complete_batches=[
                    complete_batch_row(
                        forecast_start=forecast_start,
                        forecast_end=forecast_start + timedelta(days=1),
                        issued_at=forecast_start + timedelta(minutes=2),
                    )
                ]
            )
        )

        with self.assertRaises(M3Error) as raised:
            service.backfill_actuals(
                "station-1",
                datetime.fromisoformat("2026-08-25T01:17:00+08:00"),
            )

        self.assertEqual(raised.exception.code, "acceptance_points_incomplete")

    def test_rejects_complete_batch_with_noncanonical_issued_at(self):
        """A formal batch must be issued exactly two minutes after its start."""
        service, _, _, _, _ = make_service(
            api=FakeApi(
                complete_batches=[
                    complete_batch_row(issued_at=START + timedelta(minutes=3))
                ]
            )
        )

        with self.assertRaises(M3Error) as raised:
            service.backfill_actuals(
                "station-1",
                datetime.fromisoformat("2026-08-25T01:17:00+08:00"),
            )

        self.assertEqual(raised.exception.code, "acceptance_points_incomplete")

    def test_rejects_a_point_returned_under_the_wrong_batch_id(self):
        """A point response must prove that it belongs to the requested complete batch."""
        service, _, _, _, _ = make_service(
            api=FakeApi(
                complete_batches=[complete_batch_row(record_id=41)],
                backfill_rows=[
                    stored_point("station_total_load", START, batch_id=42)
                ],
                ignore_point_batch_filter=True,
            )
        )

        with self.assertRaises(M3Error) as raised:
            service.backfill_actuals(
                "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
            )

        self.assertEqual(raised.exception.code, "acceptance_points_incomplete")

    def test_queries_each_series_below_page_cap_and_pulls_only_completed_half_open_window(self):
        """One all-series query exceeds 1,000 rows and using as_of includes an unfinished bucket."""
        rows = [
            stored_point(unique_id, START, record_id=index + 1)
            for index, unique_id in enumerate(SERIES_IDS)
        ]
        source = FakeSource(
            observations=[
                ObservationPoint(
                    unique_id=unique_id,
                    ds=START,
                    y=100.0 if unique_id == "station_total_load" else 50.0,
                    quality="valid",
                    source_revision=1,
                )
                for unique_id in SERIES_IDS
            ]
        )
        service, source, api, _, _ = make_service(
            source=source, api=FakeApi(backfill_rows=rows)
        )

        updated = service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T01:17:42+08:00")
        )

        self.assertEqual(updated, 2)
        point_queries = [call for call in api.list_calls if call.collection == "energy_forecast_points"]
        self.assertEqual([call.filter["unique_id"] for call in point_queries], list(SERIES_IDS))
        self.assertTrue(all(call.filter["batch_id"] == 41 for call in point_queries))
        self.assertTrue(
            all(not any("." in key for key in call.filter) for call in point_queries)
        )
        self.assertTrue(all(call.filter["data_time"]["$lt"] == "2026-08-25T01:15:00+08:00" for call in point_queries))
        self.assertEqual(len(source.observation_calls), 1)
        pull = source.observation_calls[0]
        self.assertEqual(pull.start, START)
        self.assertEqual(pull.end, datetime.fromisoformat("2026-08-25T01:15:00+08:00"))
        self.assertLessEqual(pull.end - pull.start, timedelta(days=7))

    def test_current_incomplete_bucket_is_never_updated(self):
        """A point whose interval has not ended must not receive a premature actual."""
        current = datetime.fromisoformat("2026-08-25T01:15:00+08:00")
        source = FakeSource(
            observations=[
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=current,
                    y=100.0,
                    quality="valid",
                    source_revision=1,
                )
            ]
        )
        api = FakeApi(
            backfill_rows=[stored_point("station_total_load", current)]
        )
        service, _, api, _, _ = make_service(source=source, api=api)

        updated = service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
        )

        self.assertEqual(updated, 0)
        self.assertEqual(api.update_calls, [])

    def test_absent_or_higher_revision_updates_but_same_or_lower_is_ignored(self):
        """Revision order is the only permitted way to replace an accepted actual."""
        times = [START + timedelta(minutes=15 * index) for index in range(4)]
        rows = [
            stored_point("station_total_load", times[0], record_id=1, revision=None),
            stored_point("station_total_load", times[1], record_id=2, revision=1),
            stored_point("station_total_load", times[2], record_id=3, revision=2),
            stored_point("station_total_load", times[3], record_id=4, revision=3),
        ]
        revisions = (1, 2, 2, 2)
        source = FakeSource(
            observations=[
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=at,
                    y=100.0,
                    quality="valid",
                    source_revision=revision,
                )
                for at, revision in zip(times, revisions, strict=True)
            ]
        )
        service, _, api, _, _ = make_service(
            source=source, api=FakeApi(backfill_rows=rows)
        )

        updated = service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T02:02:00+08:00")
        )

        self.assertEqual(updated, 2)
        self.assertEqual([call.record_id for call in api.update_calls], [1, 2])

    def test_zero_actual_is_valid_uses_one_timestamp_and_updates_only_allowed_columns(self):
        """Zero is evidence, not missing data, but its APE denominator must remain undefined."""
        source = FakeSource(
            observations=[
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=START,
                    y=0.0,
                    quality="valid",
                    source_revision=1,
                )
            ]
        )
        api = FakeApi(
            backfill_rows=[stored_point("station_total_load", START)]
        )
        now_calls = 0

        def now():
            nonlocal now_calls
            now_calls += 1
            return datetime.fromisoformat("2026-08-25T01:17:05+08:00")

        service, _, api, _, _ = make_service(source=source, api=api, now=now)

        service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
        )

        values = api.update_calls[0].values
        self.assertEqual(values["actual_value"], 0.0)
        self.assertEqual(values["actual_quality"], "valid")
        self.assertIsNone(values["absolute_percentage_error"])
        self.assertEqual(values["actual_recorded_at"], values["evaluated_at"])
        self.assertEqual(now_calls, 1)
        self.assertEqual(
            set(values),
            {
                "actual_value",
                "actual_quality",
                "actual_source_revision",
                "actual_recorded_at",
                "evaluated_at",
                "absolute_percentage_error",
            },
        )

    def test_invalid_actual_keeps_null_value_and_null_ape(self):
        """Invalid source quality must never be converted into a scored or zero actual."""
        source = FakeSource(
            observations=[
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=START,
                    y=None,
                    quality="invalid",
                    source_revision=1,
                )
            ]
        )
        api = FakeApi(backfill_rows=[stored_point("station_total_load", START)])
        service, _, api, _, _ = make_service(source=source, api=api)

        service.backfill_actuals(
            "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
        )

        self.assertIsNone(api.update_calls[0].values["actual_value"])
        self.assertIsNone(api.update_calls[0].values["absolute_percentage_error"])

    def test_malformed_duplicate_actuals_or_mismatched_update_response_fail_closed(self):
        """Ambiguous source keys and a response for another revision cannot count as a write."""
        point = ObservationPoint(
            unique_id="station_total_load",
            ds=START,
            y=100.0,
            quality="valid",
            source_revision=1,
        )
        duplicate_source = FakeSource(observations=[point, point])
        service, _, api, _, _ = make_service(
            source=duplicate_source,
            api=FakeApi(backfill_rows=[stored_point("station_total_load", START)]),
        )
        with self.assertRaises(M3Error) as duplicate_error:
            service.backfill_actuals(
                "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
            )
        self.assertEqual(duplicate_error.exception.code, "source_contract_invalid")
        self.assertEqual(api.update_calls, [])

        wrong_api = FakeApi(backfill_rows=[stored_point("station_total_load", START)])
        wrong_api.wrong_update_response = {"id": 1, "actual_source_revision": 0}
        service, _, _, _, _ = make_service(
            source=FakeSource(observations=[point]), api=wrong_api
        )
        with self.assertRaises(M3Error) as response_error:
            service.backfill_actuals(
                "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
            )
        self.assertEqual(response_error.exception.code, "sink_contract_invalid")

        revision_only_api = FakeApi(
            backfill_rows=[stored_point("station_total_load", START)]
        )
        revision_only_api.wrong_update_response = {
            "actual_source_revision": 1
        }
        service, _, _, _, _ = make_service(
            source=FakeSource(observations=[point]), api=revision_only_api
        )
        with self.assertRaises(M3Error) as shape_error:
            service.backfill_actuals(
                "station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00")
            )
        self.assertEqual(shape_error.exception.code, "sink_contract_invalid")


class RecalculationTests(unittest.TestCase):
    def test_evaluation_rejects_a_correct_batch_id_outside_that_batch_window(self):
        """A faulty point API must not let one batch supply another batch's day."""
        complete_batches = [
            complete_batch_row(
                record_id=41 + day,
                forecast_start=START + timedelta(days=day),
                forecast_end=START + timedelta(days=day + 1),
            )
            for day in range(7)
        ]
        rows = []
        for day, batch in enumerate(complete_batches):
            for interval in range(96):
                if day == 1 and interval == 0:
                    continue
                rows.append(
                    {
                        "batch_id": batch["id"],
                        "unique_id": "station_total_load",
                        "data_time": (
                            START + timedelta(days=day, minutes=15 * interval)
                        ).isoformat(),
                        "actual_value": 100.0,
                        "forecast_value": 90.0,
                        "actual_quality": "valid",
                    }
                )
        rows.append(
            {
                "batch_id": 41,
                "unique_id": "station_total_load",
                "data_time": (START + timedelta(days=1)).isoformat(),
                "actual_value": 100.0,
                "forecast_value": 90.0,
                "actual_quality": "valid",
            }
        )
        service, _, _, _, _ = make_service(
            api=FakeApi(
                complete_batches=complete_batches,
                rows_by_series={"station_total_load": rows},
                ignore_point_time_filter=True,
            )
        )

        with self.assertRaises(M3Error) as raised:
            service._series_evaluation(
                "station-1", "run-20260825", "station_total_load", active_context()
            )

        self.assertEqual(raised.exception.code, "acceptance_points_incomplete")

    def test_persists_all_six_series_metrics_and_keeps_overall_metrics_null(self):
        rows = {unique_id: evaluation_rows(unique_id) for unique_id in SERIES_IDS}
        metrics = iter(
            (
                MetricResult(
                    672,
                    672,
                    0,
                    2.5,
                    1.25,
                    2.6,
                    "passed",
                    2.4,
                    1.9,
                    5.2,
                ),
                MetricResult(
                    672,
                    672,
                    0,
                    3.5,
                    1.5,
                    3.6,
                    "passed",
                    3.4,
                    2.9,
                    6.2,
                ),
            )
        )
        api = FakeApi(rows_by_series=rows)
        service, _, api, _, _ = make_service(
            api=api, evaluator=lambda *args: next(metrics)
        )

        service.recalculate("station-1", "run-20260825")

        expected = (
            (2.5, 1.25, 2.6, 2.4, 1.9, 5.2),
            (3.5, 1.5, 3.6, 3.4, 2.9, 6.2),
        )
        fields = (
            "mape_percent",
            "mae",
            "smape_percent",
            "wape_percent",
            "median_ape_percent",
            "p90_ape_percent",
        )
        for call, metric_values in zip(api.upsert_calls[:2], expected, strict=True):
            self.assertEqual(tuple(call.values[field] for field in fields), metric_values)
        self.assertTrue(
            all(api.upsert_calls[2].values[field] is None for field in fields)
        )

    def test_queries_each_exact_series_and_upserts_two_results_plus_overall(self):
        """Combining all 1,344 rows would exceed the fixed one-page Resource API contract."""
        rows = {unique_id: evaluation_rows(unique_id) for unique_id in SERIES_IDS}
        service, _, api, _, _ = make_service(api=FakeApi(rows_by_series=rows))

        results = service.recalculate("station-1", "run-20260825")

        point_queries = [call for call in api.list_calls if call.collection == "energy_forecast_points"]
        self.assertEqual(
            [call.filter["unique_id"] for call in point_queries],
            [unique_id for unique_id in SERIES_IDS for _ in range(7)],
        )
        self.assertEqual(
            [call.filter["batch_id"] for call in point_queries],
            [batch_id for _ in SERIES_IDS for batch_id in range(41, 48)],
        )
        self.assertTrue(
            all(not any("." in key for key in call.filter) for call in point_queries)
        )
        self.assertTrue(all(len(rows[call.filter["unique_id"]]) == 672 for call in point_queries))
        self.assertEqual([call.values["evaluation_key"] for call in api.upsert_calls], [*SERIES_IDS, "overall"])
        self.assertTrue(all(results[key]["expected_count"] == 672 for key in SERIES_IDS))
        self.assertEqual(results["overall"]["expected_count"], 1344)
        self.assertEqual(results["overall"]["outcome"], "passed")
        self.assertIsNone(results["overall"]["mape_percent"])
        self.assertEqual(results["overall"]["window_start"], START.isoformat())
        self.assertEqual(results["overall"]["window_end"], END.isoformat())

    def test_overall_uses_insufficient_then_failed_precedence_without_cross_series_average(self):
        """One weak series must dominate even when the other series pass."""
        rows = {unique_id: evaluation_rows(unique_id) for unique_id in SERIES_IDS}
        metrics = iter(
            (
                MetricResult(672, 605, 0, 40.0, 1.0, 35.0, "failed"),
                MetricResult(672, 604, 0, 10.0, 1.0, 9.0, "insufficient_data"),
            )
        )
        service, _, _, _, _ = make_service(
            api=FakeApi(rows_by_series=rows), evaluator=lambda *args: next(metrics)
        )

        results = service.recalculate("station-1", "run-20260825")

        self.assertEqual(results["overall"]["outcome"], "insufficient_data")

    def test_duplicate_foreign_malformed_offgrid_nonfinite_or_incomplete_rows_are_rejected_before_upsert(self):
        """Bad topology must not fabricate the 605-point qualification threshold."""
        mutations = (
            "duplicate",
            "foreign",
            "naive",
            "offgrid",
            "outside",
            "nonfinite-forecast",
            "nonfinite-actual",
            "negative-load-actual",
            "soc-over-actual",
            "incomplete",
        )
        for mutation in mutations:
            rows = {unique_id: evaluation_rows(unique_id) for unique_id in SERIES_IDS}
            target = rows["station_total_load"]
            if mutation == "duplicate":
                target[-1] = dict(target[0])
            elif mutation == "foreign":
                target[0]["unique_id"] = "storage_soc"
            elif mutation == "naive":
                target[0]["data_time"] = "2026-08-25T01:00:00"
            elif mutation == "offgrid":
                target[0]["data_time"] = "2026-08-25T01:01:00+08:00"
            elif mutation == "outside":
                target[0]["data_time"] = END.isoformat()
            elif mutation == "nonfinite-forecast":
                target[0]["forecast_value"] = math.inf
            elif mutation == "nonfinite-actual":
                target[0]["actual_value"] = math.nan
            elif mutation == "negative-load-actual":
                target[0]["actual_value"] = -1.0
            elif mutation == "soc-over-actual":
                target = rows["storage_soc"]
                target[0]["actual_value"] = 101.0
            else:
                target.pop()
            api = FakeApi(rows_by_series=rows)
            service, _, api, _, _ = make_service(api=api)

            with self.subTest(mutation=mutation), self.assertRaises(M3Error) as raised:
                service.recalculate("station-1", "run-20260825")

            self.assertEqual(raised.exception.code, "acceptance_points_incomplete")
            self.assertEqual(api.upsert_calls, [])

    def test_upsert_response_business_identity_is_rechecked(self):
        """A successful HTTP response for another run must not be accepted as evaluation persistence."""
        rows = {unique_id: evaluation_rows(unique_id) for unique_id in SERIES_IDS}
        api = FakeApi(rows_by_series=rows)
        api.wrong_upsert_response = {
            "id": 1,
            "station_id": "station-1",
            "acceptance_run_id": "another-run",
            "evaluation_key": "station_total_load",
        }
        service, _, _, _, _ = make_service(api=api)

        with self.assertRaises(M3Error) as raised:
            service.recalculate("station-1", "run-20260825")

        self.assertEqual(raised.exception.code, "sink_contract_invalid")

        minimal_api = FakeApi(rows_by_series=rows)
        minimal_api.wrong_upsert_response = {
            "id": 1,
            "station_id": "station-1",
            "acceptance_run_id": "run-20260825",
            "evaluation_key": "station_total_load",
        }
        service, _, _, _, _ = make_service(api=minimal_api)

        with self.assertRaises(M3Error) as shape_error:
            service.recalculate("station-1", "run-20260825")

        self.assertEqual(shape_error.exception.code, "sink_contract_invalid")
        self.assertEqual(len(minimal_api.upsert_calls), 1)


class ReconciliationTests(unittest.TestCase):
    @staticmethod
    def writing_batch() -> dict:
        snapshot = snapshot_at()
        points = []
        for series in snapshot.series:
            for point in series.points:
                points.append(
                    {
                        "unique_id": series.unique_id,
                        "data_time": point.data_time.isoformat(),
                        "target_time": point.target_time.isoformat(),
                        "horizon_step": point.horizon_step,
                        "model_name": series.model_name,
                        "raw_forecast": point.raw_forecast,
                        "forecast_value": point.forecast_value,
                        "is_clipped": point.is_clipped,
                    }
                )
        batch = {
            "id": 10,
            "station_id": "station-1",
            "acceptance_run_id": "run-20260825",
            "issued_at": AS_OF.isoformat(),
            "forecast_start_time": START.isoformat(),
            "forecast_end_time": (START + timedelta(days=1)).isoformat(),
            "status": "ok",
            "write_state": "writing",
            "model_manifest": snapshot.model_manifest,
            "point_templates": points,
        }
        batch["content_hash"] = acceptance_content_hash(batch, points)
        return batch

    def test_reconcile_uses_only_persisted_templates_and_never_runs_model(self):
        """Restart recovery must resume the original hash, never create replacement predictions."""
        api = FakeApi()
        api.writing_batches = [self.writing_batch()]
        forecast = FakeForecastService()
        service, _, _, sink, forecast = make_service(
            api=api, forecast_service=forecast
        )

        recovered = service.reconcile_writing_batches("station-1")

        self.assertEqual(recovered, 1)
        self.assertEqual(forecast.calls, [])
        self.assertEqual(len(sink.reconcile_calls), 1)
        self.assertIsNone(sink.reconcile_calls[0].points)
        self.assertEqual(
            sink.reconcile_calls[0].batch["content_hash"],
            acceptance_content_hash(
                sink.reconcile_calls[0].batch,
                sink.reconcile_calls[0].batch["point_templates"],
            ),
        )

    def test_reconcile_rejects_boolean_response_id_for_batch_one(self):
        """A boolean acknowledgement cannot recover persisted batch primary key one."""
        class BooleanIdSink(FakeSink):
            def reconcile_acceptance(self, batch, points=None):
                returned = super().reconcile_acceptance(batch, points)
                returned["id"] = True
                return returned

        batch = self.writing_batch()
        batch["id"] = 1
        api = FakeApi()
        api.writing_batches = [batch]
        service, _, _, _, _ = make_service(api=api, sink=BooleanIdSink())

        with self.assertRaises(M3Error) as raised:
            service.reconcile_writing_batches("station-1")

        self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_reconcile_rejects_missing_or_hash_mismatched_templates(self):
        """Recovery without exact evidence must stop instead of rerunning or completing the batch."""
        for mutation in (
            "missing",
            "wrong-hash",
            "extra-field",
            "duplicate",
            "wrong-issued-second",
            "non-0100-window",
        ):
            batch = self.writing_batch()
            if mutation == "missing":
                batch["point_templates"] = []
            elif mutation == "wrong-hash":
                batch["content_hash"] = "wrong"
            elif mutation == "extra-field":
                batch["point_templates"][0]["actual_value"] = 99.0
                batch["content_hash"] = acceptance_content_hash(
                    batch, batch["point_templates"]
                )
            else:
                if mutation == "duplicate":
                    batch["point_templates"][-1] = dict(batch["point_templates"][0])
                elif mutation == "wrong-issued-second":
                    batch["issued_at"] = (AS_OF + timedelta(seconds=1)).isoformat()
                else:
                    shift = timedelta(hours=12)
                    batch["issued_at"] = (AS_OF + shift).isoformat()
                    batch["forecast_start_time"] = (START + shift).isoformat()
                    batch["forecast_end_time"] = (
                        START + shift + timedelta(days=1)
                    ).isoformat()
                    for point in batch["point_templates"]:
                        point["data_time"] = (
                            datetime.fromisoformat(point["data_time"]) + shift
                        ).isoformat()
                        point["target_time"] = (
                            datetime.fromisoformat(point["target_time"]) + shift
                        ).isoformat()
                batch["content_hash"] = acceptance_content_hash(
                    batch, batch["point_templates"]
                )
            api = FakeApi()
            api.writing_batches = [batch]
            service, _, _, sink, forecast = make_service(api=api)

            with self.subTest(mutation=mutation), self.assertRaises(M3Error) as raised:
                service.reconcile_writing_batches("station-1")

            self.assertIn(
                raised.exception.code,
                {"acceptance_write_incomplete", "idempotency_conflict"},
            )
            self.assertEqual(sink.reconcile_calls, [])
            self.assertEqual(forecast.calls, [])


if __name__ == "__main__":
    unittest.main()
