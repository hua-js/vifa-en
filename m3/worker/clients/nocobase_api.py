"""Typed, fixed-action adapter for NocoBase's Resource API."""

from collections.abc import Callable
import json
import re
from typing import Any

import httpx

from m3.worker.clients.http import RetryPolicy, send_with_retry
from m3.worker.errors import M3Error
from m3.worker.json_contract import (
    ExactJsonError,
    validate_exact_json_mapping as _validate_exact_json_mapping,
    validate_exact_json_value as _validate_exact_json_value,
)


DEFAULT_MAX_RESPONSE_BYTES = 2_097_152
LIST_PAGE_SIZE = 1000
MAX_LIST_PAGES = 64
MAX_LIST_RECORDS = 50_000
MAX_CREATE_RECORDS = 500
BULK_CREATE_TIMEOUT = httpx.Timeout(
    connect=2.0,
    read=60.0,
    write=15.0,
    pool=2.0,
)
IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
NOCOBASE_SYSTEM_FIELDS = frozenset(
    {"createdAt", "createdBy", "updatedAt", "updatedBy"}
)


def validate_exact_json_value(
    value: object, field_name: str, *, depth: int = 0
) -> Any:
    """Adapt the shared exact JSON value contract to the sink taxonomy."""

    try:
        return _validate_exact_json_value(value, field_name, depth=depth)
    except ExactJsonError as error:
        raise M3Error("sink_contract_invalid", f"NocoBase {error}") from error


def validate_exact_json_mapping(
    value: object, field_name: str, *, allow_empty: bool
) -> dict[str, Any]:
    """Adapt the shared exact JSON mapping contract to the sink taxonomy."""

    try:
        return _validate_exact_json_mapping(
            value, field_name, allow_empty=allow_empty
        )
    except ExactJsonError as error:
        raise M3Error("sink_contract_invalid", f"NocoBase {error}") from error


class NocoBaseApiClient:
    """Expose only the fixed Resource API actions used by the forecast sink."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.Client,
        retry: RetryPolicy,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        parsed_url = httpx.URL(base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.host
            or parsed_url.userinfo
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) origin or path without credentials/query"
            )
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client
        self._retry = retry
        self._max_response_bytes = max_response_bytes

    @staticmethod
    def _identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str) or (
            IDENTIFIER_PATTERN.fullmatch(value) is None
            and value not in NOCOBASE_SYSTEM_FIELDS
        ):
            raise M3Error(
                "sink_contract_invalid", f"NocoBase {field_name} is invalid"
            )
        return value

    @classmethod
    def _sort_identifier(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise M3Error("sink_contract_invalid", "NocoBase sort field is invalid")
        field_name = value[1:] if value.startswith("-") else value
        cls._identifier(field_name, "sort field")
        return value

    @staticmethod
    def _exact_json_value(
        value: object, field_name: str, *, depth: int = 0
    ) -> Any:
        return validate_exact_json_value(value, field_name, depth=depth)

    @classmethod
    def _json_mapping(
        cls, value: object, field_name: str, *, allow_empty: bool
    ) -> dict[str, Any]:
        return validate_exact_json_mapping(
            value, field_name, allow_empty=allow_empty
        )

    @classmethod
    def _filter_keys_for_values(
        cls, filter: dict[str, Any], values: dict[str, Any]
    ) -> list[str]:
        """Translate the local equality-filter contract to NocoBase filterKeys."""

        filter_keys: list[str] = []
        for key, expected in filter.items():
            checked_key = cls._identifier(key, "filter field")
            if (
                checked_key not in values
                or type(values[checked_key]) is not type(expected)
                or values[checked_key] != expected
            ):
                raise M3Error(
                    "sink_contract_invalid",
                    "NocoBase filter identity must match values",
                )
            filter_keys.append(checked_key)
        return filter_keys

    def _read_limited_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise M3Error(
                        "sink_contract_invalid", "NocoBase response is too large"
                    )
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(chunk) > self._max_response_bytes - len(body):
                raise M3Error("sink_contract_invalid", "NocoBase response is too large")
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _parse_record_envelope(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"data"}:
            raise M3Error("sink_contract_invalid", "NocoBase response envelope is invalid")
        data = value["data"]
        if not isinstance(data, dict):
            raise M3Error("sink_contract_invalid", "NocoBase record response is invalid")
        return data

    @staticmethod
    def _parse_records_envelope(
        value: object, expected_count: int
    ) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or set(value) != {"data"}:
            raise M3Error("sink_contract_invalid", "NocoBase response envelope is invalid")
        data = value["data"]
        if (
            not isinstance(data, list)
            or len(data) != expected_count
            or any(not isinstance(item, dict) for item in data)
        ):
            raise M3Error(
                "sink_contract_invalid", "NocoBase bulk create response is invalid"
            )
        return data

    @staticmethod
    def _parse_upsert_record_envelope(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"data"}:
            raise M3Error("sink_contract_invalid", "NocoBase response envelope is invalid")
        data = value["data"]
        if isinstance(data, dict):
            return data
        if (
            isinstance(data, list)
            and len(data) == 1
            and isinstance(data[0], dict)
        ):
            return data[0]
        raise M3Error("sink_contract_invalid", "NocoBase upsert response is invalid")

    @staticmethod
    def _parse_list_envelope(
        value: object, required_fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or set(value) != {"data", "meta"}:
            raise M3Error("sink_contract_invalid", "NocoBase list envelope is invalid")
        data = value["data"]
        meta = value["meta"]
        required_meta = {"count", "page", "pageSize", "totalPage"}
        if (
            not isinstance(data, list)
            or not isinstance(meta, dict)
            or set(meta) != required_meta
            or any(
                not isinstance(meta[key], int) or isinstance(meta[key], bool)
                for key in required_meta
            )
            or meta["count"] < 0
            or meta["page"] != 1
            or meta["pageSize"] < 1
            or meta["count"] != len(data)
            or meta["count"] > meta["pageSize"]
            or meta["totalPage"] != (0 if meta["count"] == 0 else 1)
        ):
            raise M3Error("sink_contract_invalid", "NocoBase list response is invalid")
        rows: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or any(
                field not in item for field in required_fields
            ):
                raise M3Error("sink_contract_invalid", "NocoBase list row is invalid")
            rows.append(item)
        return rows

    @staticmethod
    def _parse_list_page(
        value: object,
        required_fields: tuple[str, ...],
        *,
        expected_page: int,
        expected_page_size: int,
    ) -> tuple[list[dict[str, Any]], int, int]:
        if not isinstance(value, dict) or set(value) != {"data", "meta"}:
            raise M3Error("sink_contract_invalid", "NocoBase list envelope is invalid")
        data = value["data"]
        meta = value["meta"]
        required_meta = {"count", "page", "pageSize", "totalPage"}
        if (
            not isinstance(data, list)
            or not isinstance(meta, dict)
            or set(meta) != required_meta
            or any(
                not isinstance(meta[key], int) or isinstance(meta[key], bool)
                for key in required_meta
            )
            or meta["count"] < 0
            or meta["page"] != expected_page
            or meta["pageSize"] != expected_page_size
            or meta["totalPage"] < 0
            or (meta["totalPage"] == 0 and meta["count"] != 0)
            or (meta["totalPage"] > 0 and expected_page > meta["totalPage"])
        ):
            raise M3Error("sink_contract_invalid", "NocoBase list response is invalid")
        expected_total_pages = (
            0
            if meta["count"] == 0
            else (meta["count"] + expected_page_size - 1) // expected_page_size
        )
        expected_row_count = (
            0
            if expected_total_pages == 0
            else (
                expected_page_size
                if expected_page < expected_total_pages
                else meta["count"] - (expected_page - 1) * expected_page_size
            )
        )
        if (
            meta["totalPage"] != expected_total_pages
            or len(data) != expected_row_count
        ):
            raise M3Error("sink_contract_invalid", "NocoBase list response is invalid")
        rows: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or any(
                field not in item for field in required_fields
            ):
                raise M3Error(
                    "sink_contract_invalid", "NocoBase list row is invalid"
                )
            rows.append(item)
        return rows, meta["totalPage"], meta["count"]

    def _request(
        self,
        method: str,
        collection: str,
        action: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | list[dict[str, Any]] | None = None,
        timeout: httpx.Timeout | None = None,
        parse: Callable[[object], Any],
    ) -> Any:
        collection = self._identifier(collection, "collection")

        def request_factory() -> httpx.Request:
            request_kwargs = {
                "params": params,
                "json": json_body,
                "headers": self._headers,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            return self._client.build_request(
                method,
                f"{self._base_url}/api/{collection}:{action}",
                **request_kwargs,
            )

        def consume(response: httpx.Response) -> Any:
            if response.status_code in (401, 403):
                raise M3Error("sink_unauthorized", "NocoBase authorization failed")
            if response.status_code >= 400:
                raise M3Error(
                    "sink_http_failed",
                    "NocoBase action failed",
                    {
                        "collection": collection,
                        "action": action,
                        "status_code": response.status_code,
                    },
                )
            body = self._read_limited_body(response)
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise M3Error(
                    "sink_contract_invalid", "NocoBase response is invalid"
                ) from error
            return parse(decoded)

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
                cause = error.__cause__
                failure_type = (
                    type(cause).__name__
                    if isinstance(cause, httpx.RequestError)
                    else "RequestError"
                )
                raise M3Error(
                    "sink_http_failed",
                    "NocoBase retry exhausted",
                    {
                        "collection": collection,
                        "action": action,
                        "failure_type": failure_type,
                    },
                ) from error
            raise

    def list_records(
        self,
        collection: str,
        *,
        filter: dict[str, Any],
        fields: list[str],
        sort: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not fields:
            raise M3Error("sink_contract_invalid", "NocoBase list arguments are invalid")
        checked_filter = self._json_mapping(filter, "filter", allow_empty=True)
        checked_fields = tuple(self._identifier(field, "field") for field in fields)
        checked_sort = tuple(self._sort_identifier(field) for field in (sort or ()))
        params = {
            "filter": json.dumps(
                checked_filter,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "fields": ",".join(checked_fields),
            "page": "1",
            "pageSize": str(LIST_PAGE_SIZE),
        }
        if checked_sort:
            params["sort"] = ",".join(checked_sort)
        return self._request(
            "GET",
            collection,
            "list",
            params=params,
            parse=lambda value: self._parse_list_envelope(value, checked_fields),
        )

    def list_first_record(
        self,
        collection: str,
        *,
        filter: dict[str, Any],
        fields: list[str],
        sort: list[str],
    ) -> dict[str, Any] | None:
        """Return only the first row from a strict, sorted one-row list page."""

        if not fields or not sort:
            raise M3Error(
                "sink_contract_invalid", "NocoBase first-record arguments are invalid"
            )
        checked_filter = self._json_mapping(filter, "filter", allow_empty=True)
        checked_fields = tuple(self._identifier(field, "field") for field in fields)
        checked_sort = tuple(self._sort_identifier(field) for field in sort)
        params = {
            "filter": json.dumps(
                checked_filter,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "fields": ",".join(checked_fields),
            "page": "1",
            "pageSize": "1",
            "sort": ",".join(checked_sort),
        }

        def parse(value: object) -> dict[str, Any] | None:
            rows, _, _ = self._parse_list_page(
                value,
                checked_fields,
                expected_page=1,
                expected_page_size=1,
            )
            return None if not rows else rows[0]

        return self._request(
            "GET",
            collection,
            "list",
            params=params,
            parse=parse,
        )

    def create_record(
        self, collection: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        checked_values = self._json_mapping(values, "values", allow_empty=False)
        return self._request(
            "POST",
            collection,
            "create",
            json_body=checked_values,
            parse=self._parse_record_envelope,
        )

    def create_records(
        self, collection: str, values: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if (
            type(values) is not list
            or not 1 <= len(values) <= MAX_CREATE_RECORDS
        ):
            raise M3Error(
                "sink_contract_invalid", "NocoBase bulk create arguments are invalid"
            )
        checked_values = [
            self._json_mapping(item, "values", allow_empty=False) for item in values
        ]
        return self._request(
            "POST",
            collection,
            "create",
            json_body=checked_values,
            timeout=BULK_CREATE_TIMEOUT,
            parse=lambda value: self._parse_records_envelope(
                value, len(checked_values)
            ),
        )

    def list_records_all(
        self,
        collection: str,
        *,
        filter: dict[str, Any],
        fields: list[str],
        sort: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not fields:
            raise M3Error("sink_contract_invalid", "NocoBase list arguments are invalid")
        checked_filter = self._json_mapping(filter, "filter", allow_empty=True)
        checked_fields = tuple(self._identifier(field, "field") for field in fields)
        checked_sort = tuple(self._sort_identifier(field) for field in (sort or ()))
        rows: list[dict[str, Any]] = []
        total_pages: int | None = None
        total_records: int | None = None
        for page in range(1, MAX_LIST_PAGES + 1):
            params = {
                "filter": json.dumps(
                    checked_filter,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "fields": ",".join(checked_fields),
                "page": str(page),
                "pageSize": str(LIST_PAGE_SIZE),
            }
            if checked_sort:
                params["sort"] = ",".join(checked_sort)
            page_rows, reported_pages, reported_records = self._request(
                "GET",
                collection,
                "list",
                params=params,
                parse=lambda value, expected_page=page: self._parse_list_page(
                    value,
                    checked_fields,
                    expected_page=expected_page,
                    expected_page_size=LIST_PAGE_SIZE,
                ),
            )
            if total_pages is None:
                total_pages = reported_pages
                total_records = reported_records
                if total_records > MAX_LIST_RECORDS:
                    raise M3Error(
                        "sink_contract_invalid",
                        "NocoBase list exceeded record limit",
                    )
            elif (
                reported_pages != total_pages
                or reported_records != total_records
            ):
                raise M3Error(
                    "sink_contract_invalid",
                    "NocoBase pagination changed during the read",
                )
            rows.extend(page_rows)
            if len(rows) > MAX_LIST_RECORDS:
                raise M3Error(
                    "sink_contract_invalid", "NocoBase list exceeded record limit"
                )
            if total_pages == 0 or page == total_pages:
                if len(rows) != total_records:
                    raise M3Error(
                        "sink_contract_invalid",
                        "NocoBase pagination is incomplete",
                    )
                return rows
        raise M3Error("sink_contract_invalid", "NocoBase list exceeded page limit")

    def update_record(
        self, collection: str, record_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not isinstance(record_id, int)
            or isinstance(record_id, bool)
            or record_id < 1
        ):
            raise M3Error("sink_contract_invalid", "NocoBase record_id is invalid")
        checked_values = self._json_mapping(values, "values", allow_empty=False)
        return self._request(
            "POST",
            collection,
            "update",
            params={"filterByTk": str(record_id)},
            json_body=checked_values,
            parse=self._parse_upsert_record_envelope,
        )

    def update_or_create(
        self, collection: str, filter: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        checked_filter = self._json_mapping(filter, "filter", allow_empty=False)
        checked_values = self._json_mapping(values, "values", allow_empty=False)
        filter_keys = self._filter_keys_for_values(checked_filter, checked_values)
        return self._request(
            "POST",
            collection,
            "updateOrCreate",
            params=[("filterKeys[]", key) for key in filter_keys],
            json_body=checked_values,
            parse=self._parse_upsert_record_envelope,
        )

    def first_or_create(
        self, collection: str, filter: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        checked_filter = self._json_mapping(filter, "filter", allow_empty=False)
        checked_values = self._json_mapping(values, "values", allow_empty=False)
        filter_keys = self._filter_keys_for_values(checked_filter, checked_values)
        return self._request(
            "POST",
            collection,
            "firstOrCreate",
            params=[("filterKeys[]", key) for key in filter_keys],
            json_body=checked_values,
            parse=self._parse_upsert_record_envelope,
        )
