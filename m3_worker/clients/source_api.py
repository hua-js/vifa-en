"""Typed read-only client for Node-RED's fixed internal Source API."""

from datetime import datetime, timedelta
import re
from typing import Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from m3_worker.clients.http import RetryPolicy, send_with_retry
from m3_worker.contracts import (
    ASIA_SHANGHAI_OFFSET,
    AcceptanceContext,
    ObservationPoint,
    SourcePage,
)
from m3_worker.errors import M3Error


MAX_SOURCE_PAGES = 100
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
STATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
Payload = TypeVar("Payload", bound=BaseModel)


class SourceEnvelope(BaseModel, Generic[Payload]):
    """Exact successful Source API response envelope."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    data: Payload


class SourceApiClient:
    """Fetch normalized observations without exposing or accepting arbitrary URLs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.Client,
        retry: RetryPolicy,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client
        self._retry = retry
        self._max_response_bytes = max_response_bytes

    def _get_typed(
        self, path: str, params: dict[str, str], payload_type: type[Payload]
    ) -> Payload:
        def request_factory() -> httpx.Request:
            return self._client.build_request(
                "GET", f"{self._base_url}{path}", params=params, headers=self._headers
            )

        def consume(response: httpx.Response) -> Payload:
            if response.status_code in (401, 403):
                raise M3Error("source_unauthorized", "Source API authorization failed")
            if response.status_code >= 400:
                raise M3Error(
                    "source_http_failed",
                    "Source API request failed",
                    {"status_code": response.status_code},
                )
            body = self._read_limited_body(response)
            try:
                return SourceEnvelope[payload_type].model_validate_json(body).data
            except ValidationError as error:
                raise M3Error(
                    "source_contract_invalid", "Source API response is invalid"
                ) from error

        try:
            return send_with_retry(
                self._client,
                request_factory,
                self._retry,
                stream=True,
                response_consumer=consume,
            )
        except M3Error as error:
            if error.code == "http_retry_exhausted":
                raise M3Error("source_http_failed", "Source API retry exhausted") from error
            raise

    def _read_limited_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise M3Error(
                        "source_contract_invalid", "Source API response is too large"
                    )
            except ValueError:
                pass

        body = bytearray()
        for chunk in response.iter_bytes():
            if len(chunk) > self._max_response_bytes - len(body):
                raise M3Error("source_contract_invalid", "Source API response is too large")
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _station_path_segment(station_id: str) -> str:
        if (
            station_id in {".", ".."}
            or STATION_ID_PATTERN.fullmatch(station_id) is None
        ):
            raise M3Error("source_contract_invalid", "Source API station_id is invalid")
        return station_id

    @staticmethod
    def _validate_window(start: datetime, end: datetime) -> None:
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() != ASIA_SHANGHAI_OFFSET
            or end.utcoffset() != ASIA_SHANGHAI_OFFSET
            or end <= start
            or end - start > timedelta(days=7)
        ):
            raise M3Error(
                "source_contract_invalid",
                "Source API request window must be in (0, 7 days] and use Asia/Shanghai UTC+08:00",
            )

    def list_observations(
        self, station_id: str, start: datetime, end: datetime
    ) -> list[ObservationPoint]:
        self._validate_window(start, end)
        station_segment = self._station_path_segment(station_id)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_point_keys: set[tuple[datetime, str]] = set()
        last_point_key: tuple[datetime, str] | None = None
        points: list[ObservationPoint] = []

        for _ in range(MAX_SOURCE_PAGES):
            params = {"start": start.isoformat(), "end": end.isoformat()}
            if cursor is not None:
                params["cursor"] = cursor
            page = self._get_typed(
                f"/internal/energy-forecast/v1/stations/{station_segment}/observations",
                params,
                SourcePage,
            )
            if page.station_id != station_id:
                raise M3Error("source_contract_invalid", "Source API station_id mismatch")
            for point in page.points:
                key = (point.ds, point.unique_id)
                if key in seen_point_keys:
                    raise M3Error(
                        "source_contract_invalid", "Source API observations are duplicated"
                    )
                if last_point_key is not None and key <= last_point_key:
                    raise M3Error(
                        "source_contract_invalid",
                        "Source API observations are not in stable order",
                    )
                if point.ds < start or point.ds >= end:
                    raise M3Error(
                        "source_contract_invalid",
                        "Source API observations fall outside the requested window",
                    )
                seen_point_keys.add(key)
                last_point_key = key
            points.extend(page.points)
            if page.next_cursor is None:
                return points
            if page.next_cursor in seen_cursors:
                raise M3Error(
                    "source_contract_invalid", "Source API pagination did not terminate"
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

        raise M3Error("source_contract_invalid", "Source API pagination exceeded limit")

    def get_acceptance_context(self, station_id: str) -> AcceptanceContext:
        station_segment = self._station_path_segment(station_id)
        return self._get_typed(
            f"/internal/energy-forecast/v1/stations/{station_segment}/acceptance-context",
            {},
            AcceptanceContext,
        )
