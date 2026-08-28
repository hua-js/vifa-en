"""Fixed-target Node-RED operational alert client tests."""

from datetime import datetime
import json
import unittest

import httpx

from m3_worker.clients.alert_api import NodeRedAlertClient
from m3_worker.clients.http import RetryPolicy
from m3_worker.errors import M3Error


AT = datetime.fromisoformat("2026-08-25T01:17:00+08:00")


class AlertClientTests(unittest.TestCase):
    def test_posts_exact_fixed_path_bearer_and_minimal_body(self):
        """Alerts must not carry response bodies, URLs, credentials, or business data."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"status": "ok"})

        client = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        alerts = NodeRedAlertClient(
            "http://source.internal",
            "source-secret",
            client,
            RetryPolicy(max_attempts=1, base_delay_seconds=0),
        )

        alerts.send("station-1", "forecast", "forecast_failed", AT)

        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(
            str(request.url),
            "http://source.internal/internal/energy-forecast/v1/alerts",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer source-secret")
        self.assertEqual(
            json.loads(request.content),
            {
                "station_id": "station-1",
                "task": "forecast",
                "error_code": "forecast_failed",
                "at": "2026-08-25T01:17:00+08:00",
            },
        )

    def test_rejects_dynamic_target_and_unsafe_values_before_network(self):
        """Neither the configured origin nor alert fields may become SSRF path material."""
        calls = []
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: calls.append(request))
        )
        with self.assertRaises(ValueError):
            NodeRedAlertClient(
                "http://source.internal/prefix",
                "source-secret",
                client,
                RetryPolicy(max_attempts=1),
            )
        alerts = NodeRedAlertClient(
            "http://source.internal",
            "source-secret",
            client,
            RetryPolicy(max_attempts=1),
        )
        for supplied in (
            ("../station", "forecast", "forecast_failed", AT),
            ("station-1", "https://attacker.invalid", "forecast_failed", AT),
            ("station-1", "forecast", "token=secret", AT),
            ("station-1", "forecast", "forecast_failed", datetime(2026, 8, 25)),
        ):
            with self.assertRaises(M3Error):
                alerts.send(*supplied)
        self.assertEqual(calls, [])

    def test_does_not_follow_redirect_and_maps_auth_and_contract_failures_safely(self):
        """A redirect or malformed acknowledgement must never change target or leak body."""
        for response, code in (
            (httpx.Response(302, headers={"Location": "https://attacker.invalid"}), "alert_contract_invalid"),
            (httpx.Response(401, text="source-secret"), "alert_unauthorized"),
            (httpx.Response(400, text="https://internal?token=secret"), "alert_http_failed"),
            (httpx.Response(200, json={"status": "wrong", "secret": "x"}), "alert_contract_invalid"),
        ):
            calls = []
            client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, response=response: (calls.append(request), response)[1]
                ),
                follow_redirects=False,
            )
            alerts = NodeRedAlertClient(
                "http://source.internal",
                "source-secret",
                client,
                RetryPolicy(max_attempts=1, base_delay_seconds=0),
            )
            with self.assertRaises(M3Error) as caught:
                alerts.send("station-1", "forecast", "forecast_failed", AT)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", repr(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_retries_5xx_to_policy_bound_and_limits_response_body(self):
        """Transient Node-RED errors are bounded and oversized acknowledgements fail closed."""
        attempts = []

        def unavailable(request):
            attempts.append(request)
            return httpx.Response(503, text="unavailable")

        alerts = NodeRedAlertClient(
            "http://source.internal",
            "source-secret",
            httpx.Client(transport=httpx.MockTransport(unavailable)),
            RetryPolicy(max_attempts=3, base_delay_seconds=0),
        )
        with self.assertRaises(M3Error) as exhausted:
            alerts.send("station-1", "forecast", "forecast_failed", AT)
        self.assertEqual(exhausted.exception.code, "alert_http_failed")
        self.assertEqual(len(attempts), 3)

        oversized = NodeRedAlertClient(
            "http://source.internal",
            "source-secret",
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=b"x" * 33)
                )
            ),
            RetryPolicy(max_attempts=1),
            max_response_bytes=32,
        )
        with self.assertRaises(M3Error) as too_large:
            oversized.send("station-1", "forecast", "forecast_failed", AT)
        self.assertEqual(too_large.exception.code, "alert_contract_invalid")


if __name__ == "__main__":
    unittest.main()
