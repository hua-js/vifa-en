"""Boundary tests for the Worker-owned, read-only Source API client."""

from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

import httpx

from m3_worker.clients import source_api
from m3_worker.clients.http import RetryPolicy
from m3_worker.clients.source_api import SourceApiClient
from m3_worker.errors import M3Error


START = datetime.fromisoformat("2026-08-18T00:00:00+08:00")
END = datetime.fromisoformat("2026-08-25T00:00:00+08:00")


def source_page(*, next_cursor: str | None = None, station_id: str = "station-1"):
    return {
        "station_id": station_id,
        "timezone": "Asia/Shanghai",
        "interval_seconds": 900,
        "points": [
            {
                "unique_id": "station_total_load",
                "ds": "2026-08-24T23:45:00+08:00",
                "y": 800.0,
                "quality": "valid",
                "source_revision": 1,
            }
        ],
        "next_cursor": next_cursor,
    }


def source_response(page: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "data": page or source_page()})


def make_client(
    handler,
    retry: RetryPolicy | None = None,
    max_response_bytes: int | None = None,
) -> SourceApiClient:
    options = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
    return SourceApiClient(
        "http://source.internal",
        "source-secret",
        httpx.Client(transport=httpx.MockTransport(handler)),
        retry or RetryPolicy(max_attempts=3, base_delay_seconds=0),
        **options,
    )


class TrackingStream(httpx.SyncByteStream):
    """A real HTTPX response stream that records whether a client drains it."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_yielded = 0
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class ReadErrorStream(httpx.SyncByteStream):
    """A source response body that fails only after a successful HTTP response."""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        raise httpx.ReadError("simulated source body read failure")
        yield b""  # pragma: no cover - makes this a generator for HTTPX's protocol.

    def close(self) -> None:
        self.closed = True


class SourceClientTests(unittest.TestCase):
    def test_paginates_with_stable_bearer_auth_without_token_in_url(self):
        """Adding a cursor page must keep credentials in headers, never query text."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            first_page = request.url.params.get("cursor") is None
            page = source_page(next_cursor="page-2" if first_page else None)
            page["points"][0]["ds"] = (
                "2026-08-24T23:30:00+08:00"
                if first_page
                else "2026-08-24T23:45:00+08:00"
            )
            return source_response(page)

        points = make_client(handler).list_observations("station-1", START, END)

        self.assertEqual(len(points), 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            [request.headers["Authorization"] for request in requests],
            ["Bearer source-secret", "Bearer source-secret"],
        )
        self.assertTrue(all("source-secret" not in str(request.url) for request in requests))
        self.assertEqual(requests[0].url.params["start"], START.isoformat())
        self.assertEqual(requests[0].url.params["end"], END.isoformat())
        self.assertEqual(requests[1].url.params["cursor"], "page-2")

    def test_retries_429_with_a_fresh_request_and_retry_after_delay(self):
        """A throttled GET must retry once with a new request after the server delay."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return source_response()

        with (
            patch("m3_worker.clients.http.random.uniform", return_value=0.0),
            patch("m3_worker.clients.http.time.sleep") as sleep,
        ):
            points = make_client(handler, RetryPolicy(max_attempts=3, base_delay_seconds=0.25)).list_observations(
                "station-1", START, END
            )

        self.assertEqual(len(points), 1)
        self.assertEqual(len(requests), 2)
        self.assertIsNot(requests[0], requests[1])
        sleep.assert_called_once_with(2.0)

    def test_honors_retry_after_on_retryable_503(self):
        """Ignoring a valid 503 Retry-After would retry before the source permits it."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return httpx.Response(503, headers={"Retry-After": "3"})
            return source_response()

        with (
            patch("m3_worker.clients.http.random.uniform", return_value=0.0),
            patch("m3_worker.clients.http.time.sleep") as sleep,
        ):
            points = make_client(handler, RetryPolicy(base_delay_seconds=0.25)).list_observations(
                "station-1", START, END
            )

        self.assertEqual(len(points), 1)
        self.assertEqual(request_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_ignores_malformed_or_nonfinite_retry_after_values_safely(self):
        """Non-finite Retry-After values must fall back to finite local backoff."""
        for retry_after in ("not-a-delay", "NaN", "+inf", "-inf"):
            with self.subTest(retry_after=retry_after):
                request_count = 0
                sleeps: list[float] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal request_count
                    request_count += 1
                    if request_count == 1:
                        return httpx.Response(429, headers={"Retry-After": retry_after})
                    return source_response()

                def record_finite_sleep(delay: float) -> None:
                    if delay != 0.25:
                        raise AssertionError(f"non-finite server delay leaked into sleep: {delay}")
                    sleeps.append(delay)

                with patch("m3_worker.clients.http.random.uniform", return_value=0.0), patch(
                    "m3_worker.clients.http.time.sleep", side_effect=record_finite_sleep
                ):
                    points = make_client(
                        handler, RetryPolicy(base_delay_seconds=0.25)
                    ).list_observations("station-1", START, END)

                self.assertEqual(len(points), 1)
                self.assertEqual(request_count, 2)
                self.assertEqual(sleeps, [0.25])

    def test_retries_retryable_server_failure_only_up_to_policy_bound(self):
        """Changing the retry bound must cap repeated 5xx traffic at that exact count."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(503, json={"status": "error"})

        with self.assertRaises(M3Error) as raised:
            make_client(handler).list_observations("station-1", START, END)

        self.assertEqual(request_count, 3)
        self.assertEqual(raised.exception.code, "source_http_failed")
        self.assertNotIn("source-secret", raised.exception.message)

    def test_does_not_retry_forbidden_response(self):
        """Treating a 403 as transient would hide an authorization configuration error."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(403, json={"status": "error"})

        with self.assertRaises(M3Error) as raised:
            make_client(handler).list_observations("station-1", START, END)

        self.assertEqual(request_count, 1)
        self.assertEqual(raised.exception.code, "source_unauthorized")

    def test_rejects_invalid_request_time_window_before_network_call(self):
        """A reversed, oversized, or non-local request window must never reach Source API."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return source_response()

        client = make_client(handler)
        invalid_windows = (
            (END, START),
            (START, END + timedelta(seconds=1)),
            (START.replace(tzinfo=None), END.replace(tzinfo=None)),
            (
                datetime.fromisoformat("2026-08-18T00:00:00+00:00"),
                datetime.fromisoformat("2026-08-19T00:00:00+00:00"),
            ),
        )
        for start, end in invalid_windows:
            with self.subTest(start=start, end=end), self.assertRaises(M3Error) as raised:
                client.list_observations("station-1", start, end)
            self.assertEqual(raised.exception.code, "source_contract_invalid")

        self.assertEqual(request_count, 0)

    def test_rejects_page_for_a_different_station(self):
        """Accepting a page labelled for another station would corrupt the station cache."""
        with self.assertRaises(M3Error) as raised:
            make_client(lambda request: source_response(source_page(station_id="station-2"))).list_observations(
                "station-1", START, END
            )

        self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_observations_outside_requested_half_open_window(self):
        """A point at end time is outside [start, end) and must not enter the cache."""
        page = source_page()
        page["points"][0]["ds"] = END.isoformat()

        with self.assertRaises(M3Error) as raised:
            make_client(lambda request: source_response(page)).list_observations(
                "station-1", START, END
            )

        self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_page_with_observations_out_of_stable_source_order(self):
        """A source page that breaks documented ds/series ordering must not be cached."""
        page = source_page()
        page["points"] = [
            {
                "unique_id": "storage_1_soc",
                "ds": "2026-08-24T23:45:00+08:00",
                "y": 50.0,
                "quality": "valid",
                "source_revision": 1,
            },
            {
                "unique_id": "station_total_load",
                "ds": "2026-08-24T23:30:00+08:00",
                "y": 800.0,
                "quality": "valid",
                "source_revision": 1,
            },
        ]

        with self.assertRaises(M3Error) as raised:
            make_client(lambda request: source_response(page)).list_observations(
                "station-1", START, END
            )

        self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_reverse_page_boundary_and_duplicate_observation_keys(self):
        """Page-local order is insufficient: cache keys must increase globally and be unique."""
        page_one = source_page(next_cursor="page-2")
        page_two = source_page()
        page_two["points"][0]["ds"] = "2026-08-24T23:30:00+08:00"
        duplicate_page = source_page()
        duplicate_page["points"].append(duplicate_page["points"][0].copy())

        for pages in ((page_one, page_two), (duplicate_page,), (page_one, source_page())):
            with self.subTest(pages=len(pages)):
                request_count = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal request_count
                    response_page = pages[request_count]
                    request_count += 1
                    return source_response(response_page)

                with self.assertRaises(M3Error) as raised:
                    make_client(handler).list_observations("station-1", START, END)
                self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_dot_segment_station_ids_before_building_a_request(self):
        """Dot path segments must never let HTTPX normalize a request out of /stations/{id}."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return source_response()

        client = make_client(handler)
        for station_id in (".", ".."):
            with self.subTest(station_id=station_id), self.assertRaises(M3Error) as raised:
                client.list_observations(station_id, START, END)
            self.assertEqual(raised.exception.code, "source_contract_invalid")

        self.assertEqual(request_count, 0)

    def test_rejects_cyclic_pagination_without_unbounded_fetching(self):
        """A repeated cursor must stop the client instead of looping indefinitely."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return source_response(source_page(next_cursor="loop"))

        with self.assertRaises(M3Error) as raised:
            make_client(handler).list_observations("station-1", START, END)

        self.assertEqual(request_count, 2)
        self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_malformed_page_and_acceptance_context_as_contract_errors(self):
        """Missing typed response fields must become safe Source contract errors."""
        malformed_page = {"station_id": "station-1"}
        client = make_client(lambda request: source_response(malformed_page))

        with self.assertRaises(M3Error) as page_error:
            client.list_observations("station-1", START, END)
        self.assertEqual(page_error.exception.code, "source_contract_invalid")

        malformed_context = {"active": True, "acceptance_run_id": "run-1"}
        context_client = make_client(
            lambda request: httpx.Response(200, json={"status": "ok", "data": malformed_context})
        )
        with self.assertRaises(M3Error) as context_error:
            context_client.get_acceptance_context("station-1")
        self.assertEqual(context_error.exception.code, "source_contract_invalid")

    def test_rejects_non_exact_source_envelopes(self):
        """Missing, wrong, or extra envelope fields must not be silently ignored."""
        envelopes = (
            {},
            {"status": "error", "data": source_page()},
            {"status": "ok", "data": "not-a-page"},
            {"status": "ok", "data": source_page(), "debug": True},
        )
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                client = make_client(lambda request: httpx.Response(200, json=envelope))
                with self.assertRaises(M3Error) as raised:
                    client.list_observations("station-1", START, END)
                self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_rejects_oversize_response_before_draining_it_and_closes_stream(self):
        """A declared oversized response must be closed without buffering its payload."""
        stream = TrackingStream([b"x" * 128])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": "128"}, stream=stream
            )

        with self.assertRaises(M3Error) as raised:
            make_client(handler, max_response_bytes=64).list_observations(
                "station-1", START, END
            )

        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(stream.chunks_yielded, 0)
        self.assertTrue(stream.closed)

    def test_rejects_oversize_chunked_response_without_draining_remaining_chunks(self):
        """Without Content-Length, the limit must stop streaming at the first excess chunk."""
        stream = TrackingStream([b"x" * 40, b"x" * 40, b"x" * 40])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        with self.assertRaises(M3Error) as raised:
            make_client(handler, max_response_bytes=64).list_observations(
                "station-1", START, END
            )

        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(stream.chunks_yielded, 2)
        self.assertTrue(stream.closed)

    def test_single_huge_chunk_never_expands_the_response_accumulator_past_limit(self):
        """Checking only after extend would briefly allocate an attacker-sized body chunk."""
        stream = TrackingStream([b"x" * 128])

        class ObservedBytearray(bytearray):
            maximum_length = 0

            def extend(self, chunk) -> None:
                super().extend(chunk)
                type(self).maximum_length = max(type(self).maximum_length, len(self))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        with patch.object(source_api, "bytearray", ObservedBytearray, create=True):
            with self.assertRaises(M3Error) as raised:
                make_client(handler, max_response_bytes=64).list_observations(
                    "station-1", START, END
                )

        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertLessEqual(ObservedBytearray.maximum_length, 64)
        self.assertTrue(stream.closed)

    def test_retries_a_body_read_error_then_returns_the_next_complete_page(self):
        """A successful status with a failed body read must retry from a fresh request."""
        request_count = 0
        failed_stream = ReadErrorStream()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return httpx.Response(200, stream=failed_stream)
            return source_response()

        try:
            points = make_client(
                handler, RetryPolicy(max_attempts=3, base_delay_seconds=0)
            ).list_observations("station-1", START, END)
        except httpx.ReadError as error:
            self.fail(f"body read error leaked instead of retrying: {error}")

        self.assertEqual(len(points), 1)
        self.assertEqual(request_count, 2)
        self.assertTrue(failed_stream.closed)

    def test_exhausted_body_read_errors_map_to_safe_source_error_after_policy_bound(self):
        """Repeated stream failures must close every response and expose no raw HTTPX error."""
        request_count = 0
        failed_streams: list[ReadErrorStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            stream = ReadErrorStream()
            failed_streams.append(stream)
            return httpx.Response(200, stream=stream)

        with self.assertRaises(M3Error) as raised:
            try:
                make_client(handler, RetryPolicy(max_attempts=3, base_delay_seconds=0)).list_observations(
                    "station-1", START, END
                )
            except httpx.ReadError as error:
                raise AssertionError(f"body read error leaked instead of mapping safely: {error}") from error

        self.assertEqual(raised.exception.code, "source_http_failed")
        self.assertEqual(request_count, 3)
        self.assertTrue(all(stream.closed for stream in failed_streams))

    def test_returns_typed_inactive_acceptance_context(self):
        """The explicit inactive response must remain distinguishable from a malformed run."""
        client = make_client(
            lambda request: httpx.Response(200, json={"status": "ok", "data": {"active": False}})
        )

        context = client.get_acceptance_context("station-1")

        self.assertFalse(context.active)
        self.assertIsNone(context.acceptance_run_id)


if __name__ == "__main__":
    unittest.main()
