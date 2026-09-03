"""Boundary tests for NocoBase publication and recoverable acceptance writes."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import httpx

from m3_worker.clients.http import RetryPolicy
from m3_worker.clients.nocobase_api import NocoBaseApiClient
from m3_worker.contracts import ForecastPoint, ForecastSeries, LatestSnapshot
from m3_worker.errors import M3Error
from m3_worker.sinks.forecast_sink import (
    BATCH_HASH_FIELDS,
    POINT_HASH_FIELDS,
    ForecastSink,
    acceptance_content_hash,
    canonical_hash,
)


START = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
SERIES = (
    ("station_total_load", "kW", 800.0),
    ("storage_soc", "%", 55.0),
)


def make_latest_snapshot(
    *, station_id: str = "station-1", content_hash: str = "snapshot-hash"
) -> LatestSnapshot:
    series = []
    for unique_id, unit, base in SERIES:
        points = [
            ForecastPoint(
                data_time=START + timedelta(minutes=15 * index),
                target_time=START + timedelta(minutes=15 * (index + 1)),
                horizon_step=index + 1,
                raw_forecast=base,
                forecast_value=base,
                is_clipped=False,
            )
            for index in range(96)
        ]
        series.append(
            ForecastSeries(
                unique_id=unique_id,
                unit=unit,
                model_name="SeasonalNaive",
                status="ok",
                points=points,
            )
        )
    return LatestSnapshot(
        station_id=station_id,
        as_of=datetime.fromisoformat("2026-08-25T01:02:00+08:00"),
        generated_at=datetime.fromisoformat("2026-08-25T01:02:05+08:00"),
        source_data_end=START,
        status="ok",
        series=series,
        model_manifest={"statsforecast_version": "2.1.1"},
        content_hash=content_hash,
    )


def make_acceptance_records() -> tuple[dict, list[dict]]:
    snapshot = make_latest_snapshot()
    points = []
    for item in snapshot.series:
        for point in item.points:
            points.append(
                {
                    "unique_id": item.unique_id,
                    "data_time": point.data_time.isoformat(),
                    "target_time": point.target_time.isoformat(),
                    "horizon_step": point.horizon_step,
                    "model_name": item.model_name,
                    "raw_forecast": point.raw_forecast,
                    "forecast_value": point.forecast_value,
                    "is_clipped": point.is_clipped,
                }
            )
    batch = {
        "station_id": "station-1",
        "acceptance_run_id": "run-20260825",
        "issued_at": "2026-08-25T01:02:00+08:00",
        "forecast_start_time": START.isoformat(),
        "forecast_end_time": (START + timedelta(days=1)).isoformat(),
        "status": "ok",
        "model_manifest": snapshot.model_manifest,
    }
    batch["content_hash"] = acceptance_content_hash(batch, points)
    batch["point_templates"] = points
    return batch, points


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    def __iter__(self):
        yield self.body

    def close(self) -> None:
        self.closed = True


class FakeNocoBase:
    """In-memory Resource API boundary; deliberately returns persisted rows."""

    def __init__(self, fail_after_point: int | None = None) -> None:
        self.fail_after_point = fail_after_point
        self.actions: list[tuple[str, dict]] = []
        self.batch: dict | None = None
        self.points: dict[tuple, dict] = {}
        self.latest: dict | None = None
        self.next_id = 1

    def update_or_create(self, collection, filter, values):
        self.actions.append((f"{collection}:updateOrCreate", {"filter": filter, "values": values}))
        if collection == "energy_forecast_latest":
            self.latest = {"id": self.latest["id"] if self.latest else 1, **values}
            return dict(self.latest)
        raise AssertionError(collection)

    def first_or_create(self, collection, filter, values):
        self.actions.append((f"{collection}:firstOrCreate", {"filter": filter, "values": values}))
        if collection == "energy_forecast_batches":
            if self.batch is None:
                self.batch = {"id": 10, **values}
            return dict(self.batch)
        if collection != "energy_forecast_points":
            raise AssertionError(collection)
        if self.fail_after_point is not None and len(self.points) >= self.fail_after_point:
            raise RuntimeError("injected write failure")
        key = (values["batch_id"], values["unique_id"], values["data_time"])
        if key not in self.points:
            self.next_id += 1
            self.points[key] = {"id": self.next_id, **values}
        return dict(self.points[key])

    def list_records(self, collection, *, filter, fields, sort=None):
        self.actions.append((f"{collection}:list", {"filter": filter, "fields": fields, "sort": sort}))
        if collection == "energy_forecast_latest":
            rows = [] if self.latest is None else [self.latest]
        elif collection == "energy_forecast_points":
            rows = [value for _, value in sorted(self.points.items())]
        else:
            raise AssertionError(collection)
        return [{field: value[field] for field in fields} for value in rows]

    def update_record(self, collection, record_id, values):
        self.actions.append((f"{collection}:update", {"record_id": record_id, "values": values}))
        if collection != "energy_forecast_batches" or self.batch is None:
            raise AssertionError(collection)
        self.batch.update(values)
        return dict(self.batch)


def make_api(handler, retry: RetryPolicy | None = None) -> NocoBaseApiClient:
    return NocoBaseApiClient(
        "http://nocobase.internal",
        "sink-secret",
        httpx.Client(transport=httpx.MockTransport(handler)),
        retry or RetryPolicy(max_attempts=1, base_delay_seconds=0),
    )


class NocoBaseClientTests(unittest.TestCase):
    def test_bulk_create_uses_extended_read_timeout(self):
        """Large NocoBase creates need longer than the shared 15-second read window."""
        timeout: dict[str, float] | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal timeout
            timeout = request.extensions.get("timeout")
            return httpx.Response(200, json={"data": [{"id": 1}]})

        result = make_api(handler).create_records(
            "energy_forecast_manual_points", [{"run_pk": 14}]
        )

        self.assertEqual(result, [{"id": 1}])
        self.assertIsNotNone(timeout)
        self.assertEqual(timeout["connect"], 2.0)
        self.assertEqual(timeout["read"], 60.0)
        self.assertEqual(timeout["write"], 15.0)
        self.assertEqual(timeout["pool"], 2.0)

    def test_uses_fixed_resource_action_body_and_bearer_header_only(self):
        """Moving the token into URL/body or changing the fixed action breaks the API/security contract."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 1,
                        "station_id": "station-1",
                        "status": "ok",
                    }
                },
            )

        result = make_api(handler).update_or_create(
            "energy_forecast_latest",
            {"station_id": "station-1"},
            {"station_id": "station-1", "status": "ok"},
        )

        self.assertEqual(result["id"], 1)
        self.assertEqual(requests[0].url.path, "/api/energy_forecast_latest:updateOrCreate")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer sink-secret")
        self.assertEqual(
            json.loads(requests[0].content),
            {"station_id": "station-1", "status": "ok"},
        )
        self.assertEqual(
            list(requests[0].url.params.multi_items()),
            [("filterKeys[]", "station_id")],
        )
        self.assertNotIn("sink-secret", str(requests[0].url))
        self.assertNotIn(b"sink-secret", requests[0].content)

    def test_first_or_create_uses_filter_keys_from_matching_values(self):
        """NocoBase derives firstOrCreate's lookup filter from values plus filterKeys."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"id": 9}})

        make_api(handler).first_or_create(
            "energy_forecast_batches",
            {"station_id": "station-1", "acceptance_run_id": "run-1"},
            {
                "station_id": "station-1",
                "acceptance_run_id": "run-1",
                "status": "ok",
            },
        )

        self.assertEqual(
            json.loads(requests[0].content),
            {
                "station_id": "station-1",
                "acceptance_run_id": "run-1",
                "status": "ok",
            },
        )
        self.assertEqual(
            list(requests[0].url.params.multi_items()),
            [
                ("filterKeys[]", "station_id"),
                ("filterKeys[]", "acceptance_run_id"),
            ],
        )

    def test_rejects_filter_keys_missing_or_mismatched_in_values(self):
        """The adapter must not silently upsert a different business identity."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, json={"data": {"id": 1}})

        client = make_api(handler)
        calls = (
            lambda: client.update_or_create(
                "energy_forecast_latest",
                {"station_id": "station-1"},
                {"status": "ok"},
            ),
            lambda: client.first_or_create(
                "energy_forecast_batches",
                {"station_id": "station-1"},
                {"station_id": "station-2", "status": "ok"},
            ),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(M3Error) as raised:
                call()
            self.assertEqual(raised.exception.code, "sink_contract_invalid")
        self.assertEqual(request_count, 0)

    def test_rejects_collection_path_injection_before_network_io(self):
        """A caller-controlled path/action fragment must never escape the fixed Resource API route."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, json={"data": []})

        client = make_api(handler)
        for collection in (
            "../users",
            "energy_forecast_latest:delete",
            "energy/forecast",
            ".",
            "energy_forecast_latest?token=x",
        ):
            with self.subTest(collection=collection), self.assertRaises(M3Error) as raised:
                client.list_records(collection, filter={}, fields=["id"])
            self.assertEqual(raised.exception.code, "sink_contract_invalid")
        self.assertEqual(request_count, 0)

    def test_list_encodes_structured_query_and_requests_enough_rows(self):
        """Dropping filter/sort or accepting the default page size could verify the wrong/truncated batch."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"data": [{"id": 1, "unique_id": "station_total_load"}], "meta": {"count": 1, "page": 1, "pageSize": 1000, "totalPage": 1}},
            )

        rows = make_api(handler).list_records(
            "energy_forecast_points",
            filter={"batch_id": 10},
            fields=["id", "unique_id"],
            sort=["unique_id", "data_time"],
        )

        self.assertEqual(rows, [{"id": 1, "unique_id": "station_total_load"}])
        params = requests[0].url.params
        self.assertEqual(json.loads(params["filter"]), {"batch_id": 10})
        self.assertEqual(params["fields"], "id,unique_id")
        self.assertEqual(params["sort"], "unique_id,data_time")
        self.assertEqual(params["pageSize"], "1000")

    def test_list_first_record_reads_one_sorted_row_from_more_than_1000_records(self):
        """Template discovery must stay bounded when retained history spans many pages."""
        requests: list[httpx.Request] = []
        first = {
            "id": 1001,
            "completed_at": "2026-09-02T12:01:00+08:00",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [first],
                    "meta": {
                        "count": 1001,
                        "page": 1,
                        "pageSize": 1,
                        "totalPage": 1001,
                    },
                },
            )

        row = make_api(handler).list_first_record(
            "energy_forecast_manual_runs",
            filter={"station_id": "ES01"},
            fields=["id", "completed_at"],
            sort=["-completed_at", "-createdAt"],
        )

        self.assertEqual(row, first)
        self.assertEqual(len(requests), 1)
        params = requests[0].url.params
        self.assertEqual(params["page"], "1")
        self.assertEqual(params["pageSize"], "1")
        self.assertEqual(params["sort"], "-completed_at,-createdAt")

    def test_list_first_record_strictly_rejects_malformed_page_boundaries(self):
        """One-row reads must not trust contradictory pagination or row shapes."""
        valid_row = {"id": 1, "completed_at": "2026-09-02T12:01:00+08:00"}
        responses = (
            {
                "data": [valid_row],
                "meta": {
                    "count": 2,
                    "page": 1,
                    "pageSize": 1,
                    "totalPage": 1,
                },
            },
            {
                "data": [],
                "meta": {
                    "count": 1,
                    "page": 1,
                    "pageSize": 1,
                    "totalPage": 1,
                },
            },
            {
                "data": [valid_row, valid_row],
                "meta": {
                    "count": 2,
                    "page": 1,
                    "pageSize": 1,
                    "totalPage": 2,
                },
            },
            {
                "data": [{"id": 1}],
                "meta": {
                    "count": 1,
                    "page": 1,
                    "pageSize": 1,
                    "totalPage": 1,
                },
            },
            {
                "data": [],
                "meta": {
                    "count": 0,
                    "page": 1,
                    "pageSize": 1,
                    "totalPage": 1,
                },
            },
        )
        for payload in responses:
            with self.subTest(payload=payload), self.assertRaises(M3Error) as raised:
                make_api(
                    lambda request, payload=payload: httpx.Response(200, json=payload)
                ).list_first_record(
                    "energy_forecast_manual_runs",
                    filter={"station_id": "ES01"},
                    fields=["id", "completed_at"],
                    sort=["-completed_at"],
                )

            self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_list_first_record_returns_none_for_an_exact_empty_page(self):
        """No matching sorted row is a valid bounded empty result."""
        payload = {
            "data": [],
            "meta": {"count": 0, "page": 1, "pageSize": 1, "totalPage": 0},
        }

        row = make_api(
            lambda request: httpx.Response(200, json=payload)
        ).list_first_record(
            "energy_forecast_manual_runs",
            filter={"station_id": "ES01"},
            fields=["id", "completed_at"],
            sort=["-completed_at"],
        )

        self.assertIsNone(row)

    def test_retries_with_fresh_requests_and_closes_every_response(self):
        """Replaying one consumed request or leaking retry responses breaks reliable publication."""
        requests: list[httpx.Request] = []
        streams: list[TrackingStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                stream = TrackingStream(b'{"data":{}}')
                streams.append(stream)
                return httpx.Response(503, stream=stream)
            stream = TrackingStream(b'{"data":{"id":1,"value":2}}')
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        with patch("m3_worker.clients.http.time.sleep"):
            result = make_api(
                handler, RetryPolicy(max_attempts=2, base_delay_seconds=0)
            ).create_record("energy_forecast_points", {"value": 2})

        self.assertEqual(result, {"id": 1, "value": 2})
        self.assertEqual(len(requests), 2)
        self.assertIsNot(requests[0], requests[1])
        self.assertEqual(requests[0].content, requests[1].content)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_maps_auth_http_and_malformed_responses_without_secret_leakage(self):
        """Raw HTTP/errors and permissive response parsing must not cross the sink boundary."""
        cases = (
            (httpx.Response(403), "sink_unauthorized"),
            (httpx.Response(400, json={"error": "sink-secret"}), "sink_http_failed"),
            (httpx.Response(200, json={"data": "wrong"}), "sink_contract_invalid"),
            (httpx.Response(200, json={"data": {"id": 1}, "debug": True}), "sink_contract_invalid"),
            (httpx.Response(200, content=b"not-json"), "sink_contract_invalid"),
        )
        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(M3Error) as raised:
                make_api(lambda request, response=response: response).create_record(
                    "energy_forecast_points", {"value": 2}
                )
            self.assertEqual(raised.exception.code, expected_code)
            self.assertNotIn("sink-secret", raised.exception.message)

    def test_rejects_unsafe_write_arguments_and_non_json_values_before_network_io(self):
        """Broad/list-shaped writes, NaN, and opaque objects must never reach NocoBase."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, json={"data": {"id": 1}})

        client = make_api(handler)
        calls = (
            lambda: client.create_record("energy_forecast_points", []),
            lambda: client.create_record("energy_forecast_points", {}),
            lambda: client.update_record("energy_forecast_points", 1, []),
            lambda: client.update_record("energy_forecast_points", 1, {"value": float("nan")}),
            lambda: client.update_or_create("energy_forecast_latest", [], {"status": "ok"}),
            lambda: client.update_or_create("energy_forecast_latest", {"station_id": "station-1"}, []),
            lambda: client.first_or_create("energy_forecast_batches", {}, {"status": "ok"}),
            lambda: client.first_or_create("energy_forecast_batches", {"id": 1}, {"value": object()}),
            lambda: client.list_records("energy_forecast_points", filter={"raw": object()}, fields=["id"]),
            lambda: client.list_records("energy_forecast_points", filter={"value": float("nan")}, fields=["id"]),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(M3Error) as raised:
                call()
            self.assertEqual(raised.exception.code, "sink_contract_invalid")
        self.assertEqual(request_count, 0)

    def test_rejects_coercible_but_non_exact_json_and_excessive_depth_locally(self):
        """Integer keys, tuples, and deep structures must not be silently coerced or recurse in json."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, json={"data": {"id": 1}})

        deeply_nested: dict = {"value": "leaf"}
        for _ in range(40):
            deeply_nested = {"nested": deeply_nested}
        client = make_api(handler)
        calls = (
            ("integer-key", lambda: client.create_record("energy_forecast_points", {1: "x"})),
            (
                "tuple-array",
                lambda: client.create_record(
                    "energy_forecast_points", {"values": (1, 2)}
                ),
            ),
            (
                "excessive-depth",
                lambda: client.list_records(
                    "energy_forecast_points", filter=deeply_nested, fields=["id"]
                ),
            ),
        )

        for case, call in calls:
            with self.subTest(case=case), self.assertRaises(M3Error) as raised:
                call()
            self.assertEqual(raised.exception.code, "sink_contract_invalid")
        self.assertEqual(request_count, 0)

    def test_accepts_nested_nocobase_operator_filter_without_coercion(self):
        """Exact JSON validation must preserve legitimate nested list/operator filters."""
        requests: list[httpx.Request] = []
        filter_value = {
            "$and": [
                {"station_id": {"$eq": "station-1"}},
                {"write_state": {"$in": ["writing", "complete"]}},
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "meta": {
                        "count": 0,
                        "page": 1,
                        "pageSize": 1000,
                        "totalPage": 0,
                    },
                },
            )

        rows = make_api(handler).list_records(
            "energy_forecast_batches", filter=filter_value, fields=["id"]
        )

        self.assertEqual(rows, [])
        self.assertEqual(json.loads(requests[0].url.params["filter"]), filter_value)

    def test_rejects_impossible_one_page_list_metadata(self):
        """Contradictory page metadata must not let a truncated or malformed listing verify a batch."""
        responses = (
            {
                "data": [{"id": 1}],
                "meta": {"count": 1, "page": 1, "pageSize": 1000, "totalPage": 0},
            },
            {
                "data": [],
                "meta": {"count": 0, "page": 1, "pageSize": 1000, "totalPage": 1},
            },
            {
                "data": [{"id": 1}, {"id": 2}],
                "meta": {"count": 2, "page": 1, "pageSize": 1, "totalPage": 1},
            },
        )
        for payload in responses:
            with self.subTest(meta=payload["meta"]), self.assertRaises(M3Error) as raised:
                make_api(lambda request, payload=payload: httpx.Response(200, json=payload)).list_records(
                    "energy_forecast_points", filter={}, fields=["id"]
                )
            self.assertEqual(raised.exception.code, "sink_contract_invalid")


class ForecastSinkTests(unittest.TestCase):
    def test_latest_model_manifest_read_is_station_bound_and_minimal(self):
        manifest = {
            "statsforecast_version": "2.1.1",
            "series": {
                "station_total_load": {"model_name": "AutoETS"},
                "storage_soc": {"model_name": "AutoARIMA"},
            },
        }
        fake = FakeNocoBase()
        fake.latest = {
            "id": 1,
            "station_id": "plant-alpha-ES01",
            "model_manifest": manifest,
        }

        sink = ForecastSink(fake)
        load = getattr(sink, "load_latest_model_manifest", None)
        self.assertIsNotNone(load, "forecast sink must load persisted models")
        restored = load("plant-alpha-ES01")

        self.assertEqual(restored, manifest)
        action, arguments = fake.actions[-1]
        self.assertEqual(action, "energy_forecast_latest:list")
        self.assertEqual(arguments, {
            "filter": {"station_id": "plant-alpha-ES01"},
            "fields": ["station_id", "model_manifest"],
            "sort": None,
        })

    def test_latest_model_manifest_read_returns_none_when_station_has_no_snapshot(self):
        fake = FakeNocoBase()

        sink = ForecastSink(fake)
        load = getattr(sink, "load_latest_model_manifest", None)
        self.assertIsNotNone(load, "forecast sink must load persisted models")
        restored = load("station-1")

        self.assertIsNone(restored)

    def test_latest_snapshot_uses_station_upsert_and_rechecks_persisted_identity(self):
        """Publishing by any key except station_id or trusting a wrong returned row can cross stations."""
        fake = FakeNocoBase()
        result = ForecastSink(fake).publish_latest(make_latest_snapshot())

        action, arguments = fake.actions[-1]
        self.assertEqual(action, "energy_forecast_latest:updateOrCreate")
        self.assertEqual(arguments["filter"], {"station_id": "station-1"})
        self.assertNotIn("series", arguments["values"])
        self.assertEqual(len(arguments["values"]["series_payload"]), 2)
        self.assertEqual(result["station_id"], "station-1")

    def test_latest_upsert_key_is_full_station_id(self):
        """Using a suffix or public key would let one station overwrite its peer."""
        fake = FakeNocoBase()
        sink = ForecastSink(fake)

        sink.publish_latest(
            make_latest_snapshot(station_id="plant-alpha-ES01")
        )
        sink.publish_latest(
            make_latest_snapshot(station_id="plant-beta-ES02")
        )

        self.assertEqual(
            [
                arguments["filter"]
                for action, arguments in fake.actions
                if action == "energy_forecast_latest:updateOrCreate"
            ],
            [
                {"station_id": "plant-alpha-ES01"},
                {"station_id": "plant-beta-ES02"},
            ],
        )

    def test_same_latest_as_of_with_different_hash_conflicts_before_upsert(self):
        """Overwriting the same scheduled snapshot with different content destroys idempotency evidence."""
        fake = FakeNocoBase()
        sink = ForecastSink(fake)
        snapshot = make_latest_snapshot()
        sink.publish_latest(snapshot)
        upserts_before = sum(action.endswith(":updateOrCreate") for action, _ in fake.actions)

        with self.assertRaises(M3Error) as raised:
            sink.publish_latest(make_latest_snapshot(content_hash="different"))

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertIn("latest snapshot hash mismatch", raised.exception.message)
        self.assertEqual(
            sum(action.endswith(":updateOrCreate") for action, _ in fake.actions),
            upserts_before,
        )

    def test_older_latest_snapshot_never_overwrites_newer_persisted_snapshot(self):
        """A delayed run must not move the station's latest snapshot backwards in time."""
        fake = FakeNocoBase()
        sink = ForecastSink(fake)
        older = make_latest_snapshot()
        newer = older.model_copy(
            update={
                "as_of": datetime.fromisoformat("2026-08-25T01:17:00+08:00"),
                "generated_at": datetime.fromisoformat("2026-08-25T01:17:05+08:00"),
                "content_hash": "newer-hash",
            }
        )
        sink.publish_latest(newer)
        upserts_before = sum(action.endswith(":updateOrCreate") for action, _ in fake.actions)

        with self.assertRaises(M3Error) as raised:
            sink.publish_latest(older)

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertIn("older", raised.exception.message)
        self.assertEqual(
            sum(action.endswith(":updateOrCreate") for action, _ in fake.actions),
            upserts_before,
        )
        self.assertEqual(fake.latest["content_hash"], "newer-hash")

    def test_latest_lookup_rejects_malformed_or_naive_as_of_before_upsert(self):
        """An unparseable persisted ordering key must not be treated as merely a different instant."""
        for as_of in ("not-a-time", "2026-08-25T01:02:00"):
            fake = FakeNocoBase()
            fake.latest = {"id": 1, "as_of": as_of, "content_hash": "snapshot-hash"}
            with self.subTest(as_of=as_of), self.assertRaises(M3Error) as raised:
                ForecastSink(fake).publish_latest(make_latest_snapshot())
            self.assertEqual(raised.exception.code, "sink_contract_invalid")
            self.assertFalse(any(action.endswith(":updateOrCreate") for action, _ in fake.actions))

    def test_latest_lookup_compares_equivalent_utc_and_local_instants(self):
        """UTC serialization of the same aware as_of remains an idempotent retry."""
        fake = FakeNocoBase()
        fake.latest = {
            "id": 1,
            "as_of": "2026-08-24T17:02:00Z",
            "content_hash": "snapshot-hash",
        }

        result = ForecastSink(fake).publish_latest(make_latest_snapshot())

        self.assertEqual(result["content_hash"], "snapshot-hash")

    def test_latest_upsert_accepts_nocobase_millisecond_timestamp_precision(self):
        """NocoBase's millisecond datetime storage must not reject the written snapshot."""
        class MillisecondLatest(FakeNocoBase):
            def update_or_create(self, collection, filter, values):
                row = super().update_or_create(collection, filter, values)
                persisted = datetime.fromisoformat(row["as_of"]).astimezone(
                    timezone.utc
                )
                persisted = persisted.replace(
                    microsecond=(persisted.microsecond // 1000) * 1000
                )
                row["as_of"] = persisted.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                )
                self.latest = dict(row)
                return row

        snapshot = make_latest_snapshot().model_copy(
            update={
                "as_of": datetime.fromisoformat(
                    "2026-08-25T01:02:00.123456+08:00"
                )
            }
        )

        result = ForecastSink(MillisecondLatest()).publish_latest(snapshot)

        self.assertEqual(result["content_hash"], "snapshot-hash")

    def test_latest_rejects_mismatched_upsert_response(self):
        """A successful HTTP status returning another station/hash must not count as publication."""
        class WrongLatest(FakeNocoBase):
            def update_or_create(self, collection, filter, values):
                return {"id": 1, **values, "station_id": "station-2"}

        with self.assertRaises(M3Error) as raised:
            ForecastSink(WrongLatest()).publish_latest(make_latest_snapshot())
        self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_rejects_wrong_acceptance_hash_before_any_write(self):
        """Persisting templates under an unverified supplied digest makes later reconciliation ambiguous."""
        fake = FakeNocoBase()
        batch, points = make_acceptance_records()
        batch["content_hash"] = "not-the-canonical-hash"

        with self.assertRaises(M3Error) as raised:
            ForecastSink(fake).publish_acceptance(batch, points)

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(fake.actions, [])

    def test_rejects_noncanonical_or_duplicate_point_templates_before_writing(self):
        """Extra mutable fields or duplicate business keys can masquerade as a 192-point batch."""
        for mutate in ("duplicate", "extra-field", "stored-extra-field", "wrong-horizon"):
            fake = FakeNocoBase()
            batch, points = make_acceptance_records()
            points = [dict(point) for point in points]
            if mutate == "duplicate":
                points[-1] = dict(points[0])
            elif mutate == "extra-field":
                points[0]["actual_value"] = 999.0
            elif mutate == "stored-extra-field":
                batch["point_templates"] = [dict(point) for point in points]
                batch["point_templates"][0]["actual_value"] = 999.0
            else:
                points[0]["horizon_step"] = 2
            if mutate != "stored-extra-field":
                batch["point_templates"] = points
            batch["content_hash"] = acceptance_content_hash(batch, points)

            with self.subTest(mutate=mutate), self.assertRaises(M3Error) as raised:
                ForecastSink(fake).publish_acceptance(batch, points)
            self.assertEqual(raised.exception.code, "acceptance_write_incomplete")
            self.assertEqual(fake.actions, [])

    def test_acceptance_stays_writing_until_reconcile_restores_all_192_points(self):
        """A process failure after some point writes must remain invisible and resume original templates."""
        fake = FakeNocoBase(fail_after_point=100)
        sink = ForecastSink(fake)
        batch, points = make_acceptance_records()

        with self.assertRaises(RuntimeError):
            sink.publish_acceptance(batch, points)

        self.assertEqual(fake.batch["write_state"], "writing")
        self.assertEqual(len(fake.points), 100)
        self.assertEqual(fake.batch["point_templates"], points)

        fake.fail_after_point = None
        result = sink.reconcile_acceptance(dict(fake.batch))

        self.assertEqual(result["write_state"], "complete")
        self.assertEqual(len(fake.points), 192)
        self.assertEqual(fake.batch["point_templates"], points)

    def test_existing_point_with_different_immutable_value_conflicts_without_update(self):
        """A unique-key collision must compare prediction content, never mutate the existing forecast."""
        fake = FakeNocoBase(fail_after_point=1)
        sink = ForecastSink(fake)
        batch, points = make_acceptance_records()
        with self.assertRaises(RuntimeError):
            sink.publish_acceptance(batch, points)
        original_key = next(iter(fake.points))
        fake.points[original_key]["forecast_value"] = 777.0
        fake.fail_after_point = None

        with self.assertRaises(M3Error) as raised:
            sink.reconcile_acceptance(dict(fake.batch))

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(fake.points[original_key]["forecast_value"], 777.0)
        self.assertFalse(any(action == "energy_forecast_points:update" for action, _ in fake.actions))

    def test_restart_reconciles_batch_and_templates_serialized_as_utc(self):
        """Restart recovery must accept the database's timestamptz representation without rerunning a model."""
        def utc(value: str) -> str:
            return datetime.fromisoformat(value).astimezone(
                datetime.fromisoformat("2000-01-01T00:00:00+00:00").tzinfo
            ).isoformat().replace("+00:00", "Z")

        fake = FakeNocoBase(fail_after_point=1)
        sink = ForecastSink(fake)
        batch, points = make_acceptance_records()
        with self.assertRaises(RuntimeError):
            sink.publish_acceptance(batch, points)
        for field in ("issued_at", "forecast_start_time", "forecast_end_time"):
            fake.batch[field] = utc(fake.batch[field])
        for point in fake.batch["point_templates"]:
            point["data_time"] = utc(point["data_time"])
            point["target_time"] = utc(point["target_time"])
        serialized_points = {}
        for row in fake.points.values():
            row["data_time"] = utc(row["data_time"])
            row["target_time"] = utc(row["target_time"])
            serialized_points[(row["batch_id"], row["unique_id"], row["data_time"])] = row
        fake.points = serialized_points
        fake.fail_after_point = None

        result = sink.reconcile_acceptance(dict(fake.batch))

        self.assertEqual(result["write_state"], "complete")
        self.assertEqual(len(fake.points), 192)

    def test_batch_business_key_or_hash_conflict_is_deterministic(self):
        """A firstOrCreate collision with another immutable batch must stop before point writes."""
        for field, wrong_value in (
            ("acceptance_run_id", "other-run"),
            ("content_hash", "other-hash"),
        ):
            fake = FakeNocoBase()
            batch, points = make_acceptance_records()
            fake.batch = {
                "id": 10,
                **{key: batch[key] for key in BATCH_HASH_FIELDS},
                "content_hash": batch["content_hash"],
                "point_templates": points,
                "write_state": "writing",
                field: wrong_value,
            }

            with self.subTest(field=field), self.assertRaises(M3Error) as raised:
                ForecastSink(fake).publish_acceptance(batch, points)
            self.assertEqual(raised.exception.code, "idempotency_conflict")
            self.assertEqual(fake.points, {})

    def test_persisted_batch_templates_are_fully_revalidated_before_point_writes(self):
        """A matching digest cannot hide mutable fields or invalid topology in returned templates."""
        mutations = ("extra-field", "duplicate", "out-of-bounds", "naive-time")
        for mutation in mutations:
            class InvalidPersistedBatch(FakeNocoBase):
                def first_or_create(self, collection, filter, values):
                    returned = super().first_or_create(collection, filter, values)
                    if collection != "energy_forecast_batches":
                        return returned
                    templates = [dict(point) for point in returned["point_templates"]]
                    if mutation == "extra-field":
                        templates[0]["actual_value"] = 99.0
                    elif mutation == "duplicate":
                        templates[-1] = dict(templates[0])
                    elif mutation == "out-of-bounds":
                        templates[96]["forecast_value"] = 101.0
                    else:
                        templates[0]["data_time"] = "2026-08-25T01:00:00"
                    returned["point_templates"] = templates
                    return returned

            fake = InvalidPersistedBatch()
            batch, points = make_acceptance_records()

            with self.subTest(mutation=mutation), self.assertRaises(M3Error) as raised:
                ForecastSink(fake).publish_acceptance(batch, points)

            self.assertEqual(raised.exception.code, "idempotency_conflict")
            self.assertEqual(fake.points, {})

    def test_completion_response_must_return_the_requested_batch_primary_key(self):
        """A successful update response for another primary key must not complete this batch."""
        class WrongCompletionId(FakeNocoBase):
            def update_record(self, collection, record_id, values):
                returned = super().update_record(collection, record_id, values)
                returned["id"] = record_id + 1
                return returned

        fake = WrongCompletionId()
        batch, points = make_acceptance_records()

        with self.assertRaises(M3Error) as raised:
            ForecastSink(fake).publish_acceptance(batch, points)

        self.assertEqual(raised.exception.code, "sink_contract_invalid")

    def test_recovery_batch_id_mismatch_stops_before_any_point_or_completion_write(self):
        """Reconciliation must never redirect persisted templates to a different returned batch."""
        class WrongRecoveryBatchId(FakeNocoBase):
            def first_or_create(self, collection, filter, values):
                returned = super().first_or_create(collection, filter, values)
                if collection == "energy_forecast_batches":
                    returned["id"] = 11
                return returned

        fake = WrongRecoveryBatchId()
        batch, points = make_acceptance_records()
        batch["id"] = 10

        with self.assertRaises(M3Error) as raised:
            ForecastSink(fake).reconcile_acceptance(batch)

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(fake.points, {})
        self.assertFalse(
            any(action == "energy_forecast_batches:update" for action, _ in fake.actions)
        )

    def test_recovery_requires_an_exact_positive_persisted_batch_id(self):
        """Boolean, zero, or text recovery IDs cannot identify the original persisted batch."""
        for invalid_id in (True, 0, "10"):
            fake = FakeNocoBase()
            batch, _ = make_acceptance_records()
            batch["id"] = invalid_id

            with self.subTest(invalid_id=invalid_id), self.assertRaises(M3Error) as raised:
                ForecastSink(fake).reconcile_acceptance(batch)

            self.assertEqual(raised.exception.code, "sink_contract_invalid")
            self.assertEqual(fake.actions, [])

    def test_point_ack_rejects_boolean_batch_id_before_remaining_writes(self):
        """A boolean point batch acknowledgement cannot authorize batch completion."""
        class BooleanPointBatchId(FakeNocoBase):
            def first_or_create(self, collection, filter, values):
                returned = super().first_or_create(collection, filter, values)
                if collection == "energy_forecast_batches":
                    self.batch["id"] = 1
                    returned["id"] = 1
                elif collection == "energy_forecast_points":
                    returned["batch_id"] = True
                return returned

        fake = BooleanPointBatchId()
        batch, points = make_acceptance_records()

        with self.assertRaises(M3Error) as raised:
            ForecastSink(fake).publish_acceptance(batch, points)

        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(len(fake.points), 1)
        self.assertEqual(fake.batch["write_state"], "writing")
        self.assertFalse(
            any(action == "energy_forecast_batches:update" for action, _ in fake.actions)
        )

    def test_completion_requires_exact_stored_count_and_canonical_hash(self):
        """A duplicate/mutated server listing must keep the batch writing despite 192 write calls."""
        class CorruptListing(FakeNocoBase):
            def list_records(self, collection, *, filter, fields, sort=None):
                rows = super().list_records(collection, filter=filter, fields=fields, sort=sort)
                if collection == "energy_forecast_points" and rows:
                    rows[0]["raw_forecast"] = rows[0]["raw_forecast"] + 1
                return rows

        fake = CorruptListing()
        batch, points = make_acceptance_records()

        with self.assertRaises(M3Error) as raised:
            ForecastSink(fake).publish_acceptance(batch, points)

        self.assertEqual(raised.exception.code, "acceptance_write_incomplete")
        self.assertEqual(fake.batch["write_state"], "writing")

    def test_replaying_complete_identical_batch_is_idempotent(self):
        """An identical retry after completion must verify and return the same batch deterministically."""
        fake = FakeNocoBase()
        sink = ForecastSink(fake)
        batch, points = make_acceptance_records()
        first = sink.publish_acceptance(batch, points)
        point_ids = {key: row["id"] for key, row in fake.points.items()}

        second = sink.publish_acceptance(batch, points)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(point_ids, {key: row["id"] for key, row in fake.points.items()})
        self.assertEqual(second["write_state"], "complete")

    def test_equivalent_database_timestamp_serialization_remains_idempotent(self):
        """A timestamptz returned in UTC must not conflict with the original +08 instant."""
        class UtcSerializingFake(FakeNocoBase):
            @staticmethod
            def _utc(value):
                if not isinstance(value, str) or "+08:00" not in value:
                    return value
                return datetime.fromisoformat(value).astimezone().astimezone(
                    datetime.fromisoformat("2000-01-01T00:00:00+00:00").tzinfo
                ).isoformat().replace("+00:00", "Z")

            def first_or_create(self, collection, filter, values):
                returned = super().first_or_create(collection, filter, values)
                for field in ("issued_at", "forecast_start_time", "forecast_end_time", "data_time", "target_time"):
                    if field in returned:
                        returned[field] = self._utc(returned[field])
                return returned

            def list_records(self, collection, *, filter, fields, sort=None):
                rows = super().list_records(collection, filter=filter, fields=fields, sort=sort)
                for row in rows:
                    for field in ("data_time", "target_time"):
                        if field in row:
                            row[field] = self._utc(row[field])
                return rows

        fake = UtcSerializingFake()
        batch, points = make_acceptance_records()

        result = ForecastSink(fake).publish_acceptance(batch, points)

        self.assertEqual(result["write_state"], "complete")

    def test_hashes_are_order_stable_but_content_sensitive(self):
        """Dictionary order must not change identity while immutable content changes must do so."""
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))
        self.assertNotEqual(canonical_hash({"value": 1}), canonical_hash({"value": 2}))


if __name__ == "__main__":
    unittest.main()
