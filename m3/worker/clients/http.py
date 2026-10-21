"""Shared, bounded HTTP retry behavior for outbound Worker clients."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
import time
from threading import Event
from typing import TypeVar, overload

import httpx

from m3.worker.errors import M3Error


RequestFactory = Callable[[], httpx.Request]
ResponseValue = TypeVar("ResponseValue")
ResponseConsumer = Callable[[httpx.Response], ResponseValue]
RETRYABLE_STATUS_CODES = frozenset({408, 429, *range(500, 600)})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if not math.isfinite(self.base_delay_seconds) or self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be finite and non-negative")
        if self.max_delay_seconds is not None and (
            not math.isfinite(self.max_delay_seconds) or self.max_delay_seconds < 0
        ):
            raise ValueError("max_delay_seconds must be finite and non-negative")


def _request_factory(client: httpx.Client, request: httpx.Request) -> RequestFactory:
    """Rebuild a request so retries never send a consumed HTTPX Request instance."""

    headers = dict(request.headers)
    content = request.content
    return lambda: client.build_request(
        request.method,
        request.url,
        headers=headers,
        content=content,
    )


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


@overload
def send_with_retry(
    client: httpx.Client,
    request: httpx.Request | RequestFactory,
    policy: RetryPolicy,
    *,
    stream: bool = False,
    follow_redirects: bool | None = None,
    stop_event: Event | None = None,
    response_consumer: None = None,
) -> httpx.Response: ...


@overload
def send_with_retry(
    client: httpx.Client,
    request: httpx.Request | RequestFactory,
    policy: RetryPolicy,
    *,
    stream: bool,
    follow_redirects: bool | None = None,
    stop_event: Event | None = None,
    response_consumer: ResponseConsumer[ResponseValue],
) -> ResponseValue: ...


def send_with_retry(
    client: httpx.Client,
    request: httpx.Request | RequestFactory,
    policy: RetryPolicy,
    *,
    stream: bool = False,
    follow_redirects: bool | None = None,
    stop_event: Event | None = None,
    response_consumer: ResponseConsumer[ResponseValue] | None = None,
) -> httpx.Response | ResponseValue:
    """Send a request with finite retryable-status and transport-error retries."""

    factory = request if callable(request) else _request_factory(client, request)
    for attempt in range(1, policy.max_attempts + 1):
        if stop_event is not None and stop_event.is_set():
            raise M3Error("http_request_cancelled", "HTTP request stopped during shutdown")
        response: httpx.Response | None = None
        try:
            options = {"stream": stream}
            if follow_redirects is not None:
                options["follow_redirects"] = follow_redirects
            response = client.send(factory(), **options)
            if response.status_code not in RETRYABLE_STATUS_CODES:
                if response_consumer is None:
                    return response
                try:
                    return response_consumer(response)
                finally:
                    response.close()
            if attempt == policy.max_attempts:
                if response_consumer is not None:
                    try:
                        return response_consumer(response)
                    finally:
                        response.close()
                return response
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            response.close()
        except httpx.RequestError as error:
            if response is not None:
                response.close()
            if attempt == policy.max_attempts:
                raise M3Error("http_retry_exhausted", "HTTP request retry exhausted") from error
            retry_after = 0.0

        delay = max(retry_after, policy.base_delay_seconds * (2 ** (attempt - 1)))
        if delay:
            delay += random.uniform(0, delay / 4)
            if policy.max_delay_seconds is not None:
                delay = min(delay, policy.max_delay_seconds)
            if stop_event is None:
                time.sleep(delay)
            elif stop_event.wait(delay):
                raise M3Error("http_request_cancelled", "HTTP request stopped during shutdown")

    raise AssertionError("retry loop exhausted without returning")
