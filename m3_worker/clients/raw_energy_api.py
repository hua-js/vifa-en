"""Bounded reader for raw NocoBase energy rows."""

from datetime import datetime, timedelta
import json
import math
import re
from zoneinfo import ZoneInfo

import httpx

from m3_worker.contracts import ObservationPoint
from m3_worker.custom_forecast_contracts import (
    ALLOWED_INTERVAL_SECONDS,
    CustomObservationPoint,
)
from m3_worker.errors import M3Error


PAGE_SIZE = 1_000
MAX_PAGES = 300
MAX_RESPONSE_BYTES = 1_048_576
SHANGHAI = ZoneInfo("Asia/Shanghai")
NUMBER_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
MAX_SOURCE_CADENCE_SECONDS = 120
MINIMUM_BUCKET_COVERAGE = 0.8


class RawEnergySourceClient:
    """Fetch only the two configured station identities from one fixed target."""

    def __init__(
        self, api_url: str, token: str, http: httpx.Client, *,
        allowed_station_ids: tuple[str, str],
    ) -> None:
        """Bind one fixed target and the exact two configured station identities."""
        self._api_url = self._validate_url(api_url)
        self._headers = {"Authorization": self._authorization(token)}
        self._http = http
        if type(allowed_station_ids) is not tuple or len(allowed_station_ids) != 2:
            raise ValueError("raw source requires exactly two configured station IDs")
        if any(type(value) is not str or not value or value != value.strip() for value in allowed_station_ids) or len(set(allowed_station_ids)) != 2:
            raise ValueError("raw source station IDs must be distinct exact strings")
        self._allowed_station_ids = allowed_station_ids

    @staticmethod
    def _validate_url(value: str) -> httpx.URL:
        if type(value) is not str:
            raise ValueError("raw source URL must be an exact string")
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("raw source URL is invalid") from error
        if url.scheme not in {"http", "https"} or not url.host or url.username or url.password or url.query or url.fragment:
            raise ValueError("raw source URL must be fixed HTTP(S) without credentials, query, or fragment")
        return url

    @staticmethod
    def _authorization(token: str) -> str:
        if type(token) is not str:
            raise ValueError("raw source token must be an exact string")
        raw = token[7:] if token.startswith("Bearer ") else token
        if not raw or raw != raw.strip() or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw):
            raise ValueError("raw source token is invalid")
        return f"Bearer {raw}"

    def _require_station(self, station_id: str) -> None:
        if type(station_id) is not str or station_id not in self._allowed_station_ids:
            raise M3Error("source_contract_invalid", "Raw source station_id is not configured")

    @staticmethod
    def _validate_window(start: datetime, end: datetime) -> None:
        if (start.tzinfo is None or end.tzinfo is None or start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8) or end <= start or start.minute % 15 or end.minute % 15 or start.second or end.second or start.microsecond or end.microsecond):
            raise M3Error("source_contract_invalid", "Raw source window must use Asia/Shanghai 15-minute boundaries")

    @staticmethod
    def _validate_custom_window(
        start: datetime, end: datetime, interval_seconds: int
    ) -> None:
        if (
            type(interval_seconds) is not int
            or interval_seconds not in ALLOWED_INTERVAL_SECONDS
        ):
            raise M3Error(
                "source_contract_invalid", "Raw source interval is not supported"
            )
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() != timedelta(hours=8)
            or end.utcoffset() != timedelta(hours=8)
            or end <= start
            or end - start > timedelta(days=7)
            or start.microsecond
            or end.microsecond
        ):
            raise M3Error(
                "source_contract_invalid",
                "Raw source custom window must be in (0, 7 days] and use Asia/Shanghai",
            )
        start_seconds = start.hour * 3600 + start.minute * 60 + start.second
        end_seconds = end.hour * 3600 + end.minute * 60 + end.second
        if start_seconds % interval_seconds or end_seconds % interval_seconds:
            raise M3Error(
                "source_contract_invalid",
                "Raw source custom window must align to interval boundaries",
            )

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if type(value) is not str or value != value.strip():
            raise M3Error("source_contract_invalid", "Raw source timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error("source_contract_invalid", "Raw source timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise M3Error("source_contract_invalid", "Raw source timestamp is invalid")
        return parsed.astimezone(SHANGHAI)

    @staticmethod
    def _number(value: object) -> float | None:
        if type(value) is str:
            if value != value.strip() or NUMBER_TEXT.fullmatch(value) is None:
                return None
            number = float(value)
        elif type(value) in {int, float}:
            number = float(value)
        else:
            return None
        if not math.isfinite(number):
            return None
        return number

    @staticmethod
    def _covered(
        samples: list[tuple[datetime, float]], bucket_start: datetime, bucket_end: datetime
    ) -> bool:
        bucket_seconds = (bucket_end - bucket_start).total_seconds()
        expected_samples = max(
            1, math.ceil(bucket_seconds / MAX_SOURCE_CADENCE_SECONDS)
        )
        required_samples = max(
            1, math.ceil(expected_samples * MINIMUM_BUCKET_COVERAGE)
        )
        boundary_tolerance = timedelta(seconds=MAX_SOURCE_CADENCE_SECONDS)
        internal_gap_tolerance = timedelta(
            seconds=MAX_SOURCE_CADENCE_SECONDS * 2
        )
        return (
            len(samples) >= required_samples
            and samples[0][0] <= bucket_start + boundary_tolerance
            and samples[-1][0] >= bucket_end - boundary_tolerance
            and all(
                right[0] - left[0] <= internal_gap_tolerance
                for left, right in zip(samples, samples[1:])
            )
        )

    @staticmethod
    def _limited_body(response: httpx.Response) -> bytes:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > MAX_RESPONSE_BYTES:
                    raise M3Error("source_contract_invalid", "Raw source response is too large")
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(chunk) > MAX_RESPONSE_BYTES - len(body):
                raise M3Error("source_contract_invalid", "Raw source response is too large")
            body.extend(chunk)
        return bytes(body)

    def _page(self, filter_value: dict[str, object], page: int) -> tuple[list[dict[str, object]], bool]:
        params = {"filter": json.dumps(filter_value, separators=(",", ":")), "page": str(page), "pageSize": str(PAGE_SIZE), "sort": "timestamp"}
        try:
            request = self._http.build_request("GET", self._api_url, params=params, headers=self._headers)
            response = self._http.send(request, stream=True, follow_redirects=False)
        except httpx.RequestError as error:
            raise M3Error("source_http_failed", "Raw source request failed") from error
        try:
            if response.is_redirect or response.history:
                raise M3Error("source_contract_invalid", "Raw source redirects are not allowed")
            if response.status_code in {401, 403}:
                raise M3Error("source_unauthorized", "Raw source authorization failed")
            if response.status_code >= 400:
                raise M3Error("source_http_failed", "Raw source request failed", {"status_code": response.status_code})
            body = self._limited_body(response)
        finally:
            response.close()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise M3Error("source_contract_invalid", "Raw source response is invalid") from error
        if type(payload) is not dict or set(payload) != {"data", "meta"} or type(payload["data"]) is not list or type(payload["meta"]) is not dict:
            raise M3Error("source_contract_invalid", "Raw source response envelope is invalid")
        meta = payload["meta"]
        if set(meta) != {"hasNext", "page", "pageSize"} or type(meta["hasNext"]) is not bool or type(meta["page"]) is not int or type(meta["pageSize"]) is not int or meta["page"] != page or meta["pageSize"] != PAGE_SIZE:
            raise M3Error("source_contract_invalid", "Raw source response metadata is invalid")
        if any(type(row) is not dict for row in payload["data"]):
            raise M3Error("source_contract_invalid", "Raw source row is invalid")
        return payload["data"], meta["hasNext"]

    def _rows(self, station_id: str, start: datetime | None, end: datetime | None) -> list[tuple[datetime, dict[str, object]]]:
        filter_value: dict[str, object] = {"es_sn": {"$eq": station_id}}
        if start is not None:
            filter_value["timestamp"] = {"$gte": start.isoformat(), "$lt": end.isoformat()}
        rows: list[tuple[datetime, dict[str, object]]] = []
        previous: datetime | None = None
        for page in range(1, MAX_PAGES + 1):
            page_rows, has_next = self._page(filter_value, page)
            for row in page_rows:
                timestamp = self._timestamp(row.get("timestamp"))
                if row.get("es_sn") != station_id:
                    raise M3Error("source_contract_invalid", "Raw source station_id mismatch")
                if start is not None and (timestamp < start or timestamp >= end):
                    raise M3Error("source_contract_invalid", "Raw source row falls outside requested window")
                if previous is not None and timestamp <= previous:
                    raise M3Error("source_contract_invalid", "Raw source rows are unordered or duplicated")
                rows.append((timestamp, row))
                previous = timestamp
            if not has_next:
                return rows
        raise M3Error("source_contract_invalid", "Raw source pagination exceeded limit")

    def latest_timestamp(self, station_id: str) -> datetime:
        """Return the newest timezone-aware timestamp for one configured full es_sn."""
        self._require_station(station_id)
        rows = self._rows(station_id, None, None)
        if not rows:
            raise M3Error("source_contract_invalid", "Raw source history is empty")
        return rows[-1][0]

    def list_observations(self, station_id: str, start: datetime, end: datetime) -> list[ObservationPoint]:
        """Return ordered two-series 15-minute points for one station."""
        self._require_station(station_id)
        self._validate_window(start, end)
        rows = self._rows(station_id, start, end)
        points: list[ObservationPoint] = []
        bucket_start = start
        while bucket_start < end:
            bucket_end = bucket_start + timedelta(minutes=15)
            bucket = [(timestamp, row) for timestamp, row in rows if bucket_start <= timestamp < bucket_end]
            load_values = [
                (timestamp, value)
                for timestamp, row in bucket
                if (value := self._number(row.get("load_power"))) is not None
            ]
            soc_values = [
                (timestamp, value)
                for timestamp, row in bucket
                if (value := self._number(row.get("emus_soc"))) is not None
            ]
            valid_load = [(timestamp, value) for timestamp, value in load_values if value >= 0]
            valid_soc = [(timestamp, value) for timestamp, value in soc_values if 0 <= value <= 100]
            load_valid = self._covered(valid_load, bucket_start, bucket_end)
            soc_valid = (
                self._covered(valid_soc, bucket_start, bucket_end)
                and bool(soc_values)
                and 0 <= soc_values[-1][1] <= 100
            )
            points.extend((
                ObservationPoint(unique_id="station_total_load", ds=bucket_start, y=sum(value for _, value in valid_load) / len(valid_load) if load_valid else None, quality="valid" if load_valid else "invalid", source_revision=0),
                ObservationPoint(unique_id="storage_soc", ds=bucket_start, y=valid_soc[-1][1] if soc_valid else None, quality="valid" if soc_valid else "invalid", source_revision=0),
            ))
            bucket_start = bucket_end
        return points

    def list_custom_observations(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        *,
        interval_seconds: int,
    ) -> list[CustomObservationPoint]:
        """Return ordered two-series points for one approved custom interval."""

        self._require_station(station_id)
        self._validate_custom_window(start, end, interval_seconds)
        rows = self._rows(station_id, start, end)
        bucket_count = int((end - start).total_seconds() // interval_seconds)
        buckets: list[list[tuple[datetime, dict[str, object]]]] = [
            [] for _ in range(bucket_count)
        ]
        for timestamp, row in rows:
            index = int((timestamp - start).total_seconds() // interval_seconds)
            if not 0 <= index < bucket_count:
                raise M3Error(
                    "source_contract_invalid",
                    "Raw source row falls outside the custom bucket range",
                )
            buckets[index].append((timestamp, row))

        points: list[CustomObservationPoint] = []
        interval = timedelta(seconds=interval_seconds)
        for index, bucket in enumerate(buckets):
            bucket_start = start + index * interval
            bucket_end = bucket_start + interval
            load_values = [
                (timestamp, value)
                for timestamp, row in bucket
                if (value := self._number(row.get("load_power"))) is not None
            ]
            soc_values = [
                (timestamp, value)
                for timestamp, row in bucket
                if (value := self._number(row.get("emus_soc"))) is not None
            ]
            valid_load = [
                (timestamp, value)
                for timestamp, value in load_values
                if value >= 0
            ]
            valid_soc = [
                (timestamp, value)
                for timestamp, value in soc_values
                if 0 <= value <= 100
            ]
            load_valid = self._covered(valid_load, bucket_start, bucket_end)
            soc_valid = self._covered(valid_soc, bucket_start, bucket_end)
            if load_valid:
                load_state = "valid"
            elif not bucket:
                load_state = "no_rows"
            elif not load_values:
                load_state = "no_numeric"
            elif any(value < 0 for _, value in load_values):
                load_state = "negative"
            else:
                load_state = "coverage"
            if soc_valid:
                soc_state = "valid"
            elif not bucket:
                soc_state = "no_rows"
            elif not soc_values:
                soc_state = "no_numeric"
            elif not valid_soc:
                soc_state = "out_of_range"
            else:
                soc_state = "coverage"
            points.extend(
                (
                    CustomObservationPoint(
                        unique_id="station_total_load",
                        ds=bucket_start,
                        y=(
                            sum(value for _, value in valid_load) / len(valid_load)
                            if load_valid
                            else None
                        ),
                        quality="valid" if load_valid else "invalid",
                        source_state=load_state,
                        source_revision=0,
                    ),
                    CustomObservationPoint(
                        unique_id="storage_soc",
                        ds=bucket_start,
                        y=valid_soc[-1][1] if soc_valid else None,
                        quality="valid" if soc_valid else "invalid",
                        source_state=soc_state,
                        source_revision=0,
                    ),
                )
            )
        return points
