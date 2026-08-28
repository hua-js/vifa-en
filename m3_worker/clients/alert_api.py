"""Fixed-target Node-RED operational alert delivery."""

from datetime import datetime
import json
import re

import httpx

from m3_worker.clients.http import RetryPolicy, send_with_retry
from m3_worker.clients.source_api import STATION_ID_PATTERN
from m3_worker.contracts import validate_shanghai_timestamp
from m3_worker.errors import M3Error


ALERT_PATH = "/internal/energy-forecast/v1/alerts"
DEFAULT_MAX_RESPONSE_BYTES = 65_536
SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class NodeRedAlertClient:
    """POST the minimal alert contract to one configured Node-RED origin."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.Client,
        retry: RetryPolicy,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be an exact HTTP(S) origin")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("token must be a bounded non-empty string")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one")
        self._url = str(parsed.copy_with(path=ALERT_PATH))
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client
        self._retry = retry
        self._max_response_bytes = max_response_bytes

    def _read_limited(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise M3Error(
                        "alert_contract_invalid",
                        "Node-RED alert acknowledgement is too large",
                    )
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(chunk) > self._max_response_bytes - len(body):
                raise M3Error(
                    "alert_contract_invalid",
                    "Node-RED alert acknowledgement is too large",
                )
            body.extend(chunk)
        return bytes(body)

    def _consume(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise M3Error(
                "alert_unauthorized", "Node-RED alert authorization failed"
            )
        if response.status_code >= 400:
            raise M3Error("alert_http_failed", "Node-RED alert request failed")
        if not 200 <= response.status_code < 300:
            raise M3Error(
                "alert_contract_invalid", "Node-RED alert response is invalid"
            )
        body = self._read_limited(response)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise M3Error(
                "alert_contract_invalid", "Node-RED alert response is invalid"
            ) from error
        if type(payload) is not dict or payload != {"status": "ok"}:
            raise M3Error(
                "alert_contract_invalid", "Node-RED alert response is invalid"
            )

    @staticmethod
    def _payload(
        station_id: str, task: str, error_code: str, at: datetime
    ) -> dict[str, str]:
        if (
            not isinstance(station_id, str)
            or STATION_ID_PATTERN.fullmatch(station_id) is None
            or not isinstance(task, str)
            or SAFE_NAME.fullmatch(task) is None
            or not isinstance(error_code, str)
            or SAFE_NAME.fullmatch(error_code) is None
        ):
            raise M3Error("alert_contract_invalid", "Alert fields are invalid")
        try:
            validate_shanghai_timestamp(at, "alert time", quarter_hour=False)
        except (TypeError, ValueError) as error:
            raise M3Error(
                "alert_contract_invalid",
                "Alert time must use Asia/Shanghai",
            ) from error
        return {
            "station_id": station_id,
            "task": task,
            "error_code": error_code,
            "at": at.isoformat(),
        }

    def send(
        self, station_id: str, task: str, error_code: str, at: datetime
    ) -> None:
        payload = self._payload(station_id, task, error_code, at)

        def request_factory() -> httpx.Request:
            return self._client.build_request(
                "POST", self._url, headers=self._headers, json=payload
            )

        try:
            send_with_retry(
                self._client,
                request_factory,
                self._retry,
                stream=True,
                response_consumer=self._consume,
            )
        except M3Error as error:
            if error.code == "http_retry_exhausted":
                raise M3Error(
                    "alert_http_failed", "Node-RED alert retry exhausted"
                ) from error
            raise
