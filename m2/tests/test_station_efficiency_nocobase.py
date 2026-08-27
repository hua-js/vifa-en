import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from m2.station_efficiency_nocobase import (
    StationEfficiencyStoreError,
    delete_device_points_before,
    fetch_active_events,
    fetch_dashboard_events,
    fetch_device_points,
    fetch_minute_points,
    save_bottleneck_event,
    save_device_point,
    save_minute_point,
)


CONFIG = {
    "nocobase_base_url": "https://vifa.hlszh.com",
    "nocobase_token": "test-secret",
    "timeout_seconds": 7,
}

MINUTE_POINT = {
    "station_id": "ES02",
    "data_time": "2026-08-26T09:30:00+08:00",
    "pv_storage_efficiency": 88.5,
    "storage_load_efficiency": None,
    "pv_load_efficiency": 95.2,
    "pv_storage_input_kw": 100.0,
    "pv_storage_output_kw": 88.5,
    "storage_load_input_kw": None,
    "storage_load_output_kw": None,
    "pv_load_input_kw": 80.0,
    "pv_load_output_kw": 76.16,
    "formula_version": "energy-chain-v1",
    "calculated_at": "2026-08-26T09:30:03+08:00",
}

BOTTLENECK_EVENT = {
    "station_id": "ES02",
    "event_type": "inverter_low_load",
    "device_id": "pv-inverter-1",
    "device_name": "1#逆变器",
    "start_time": "2026-08-26T09:25:00+08:00",
    "end_time": None,
    "last_seen_time": "2026-08-26T09:30:00+08:00",
    "status": "active",
    "observed_value": 12.4,
    "threshold_value": 20.0,
    "observed_unit": "%",
    "evidence": {"display_text": "负载率最低 12.40%", "rule": {"version": 1}},
    "impact_chain": ["pv_storage", "pv_load"],
    "rule_version": 1,
}

DEVICE_POINT = {
    "station_id": "ES02",
    "device_type": "inverter",
    "device_id": "pv-inverter-1",
    "device_name": "1#逆变器",
    "data_time": "2026-08-27T10:00:00+08:00",
    "active_power_kw": 12.4,
    "rated_power_kw": 100.0,
}


class StationEfficiencyNocoBaseTests(unittest.TestCase):
    def test_device_save_uses_four_field_identity(self):
        calls = []

        save_device_point(
            DEVICE_POINT,
            CONFIG,
            request_json=lambda *args: calls.append(args) or {"data": {"id": 7}},
        )

        parsed = urlsplit(calls[0][0])
        self.assertEqual(parsed.path, "/api/t_efficiency_device_points:updateOrCreate")
        self.assertEqual(
            parse_qs(parsed.query)["filterKeys[]"],
            ["station_id", "device_type", "device_id", "data_time"],
        )

    def test_device_save_canonicalizes_data_time_and_rejects_naive_time(self):
        calls = []

        save_device_point(
            DEVICE_POINT,
            CONFIG,
            request_json=lambda *args: calls.append(args) or {"data": {"id": 7}},
        )

        self.assertEqual(calls[0][3]["data_time"], "2026-08-27T02:00:00+00:00")
        naive_point = {**DEVICE_POINT, "data_time": "2026-08-27T10:00:00"}
        with self.assertRaisesRegex(
            StationEfficiencyStoreError, "data_time 必须包含时区",
        ):
            save_device_point(
                naive_point,
                CONFIG,
                request_json=lambda *args: self.fail("不能请求 NocoBase"),
            )

    def test_fetch_device_points_filters_station_and_time_range(self):
        rows = fetch_device_points(
            "ES02",
            "2026-08-27T09:50:00+08:00",
            "2026-08-27T10:01:00+08:00",
            CONFIG,
            request_json=lambda *args: {"data": [DEVICE_POINT]},
        )

        self.assertEqual(rows, [DEVICE_POINT])

    def test_fetch_active_events_queries_all_active_events_for_station(self):
        calls = []

        fetch_active_events(
            "ES02",
            CONFIG,
            request_json=lambda *args: calls.append(args) or {
                "data": [], "meta": {"totalPage": 1},
            },
        )

        query = parse_qs(urlsplit(calls[0][0]).query)
        self.assertEqual(json.loads(query["filter"][0]), {
            "$and": [
                {"station_id": {"$eq": "ES02"}},
                {"status": {"$eq": "active"}},
            ]
        })

    def test_fetch_active_events_aggregates_all_pages(self):
        calls = []

        def request_json(url, *args):
            calls.append(url)
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return {"data": [{"id": page}], "meta": {"totalPage": 2}}

        rows = fetch_active_events("ES02", CONFIG, request_json=request_json)

        self.assertEqual(rows, [{"id": 1}, {"id": 2}])
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["page"] for url in calls],
            [["1"], ["2"]],
        )
        self.assertTrue(all(
            parse_qs(urlsplit(url).query)["pageSize"] == ["100"]
            for url in calls
        ))

    def test_event_queries_reject_missing_pagination_metadata(self):
        event_queries = (
            (fetch_active_events, ("ES02", CONFIG)),
            (
                fetch_dashboard_events,
                (
                    "ES02",
                    "2026-08-27T00:00:00+08:00",
                    "2026-08-28T00:00:00+08:00",
                    CONFIG,
                ),
            ),
        )

        for fetch_events, arguments in event_queries:
            with self.subTest(fetch_events=fetch_events.__name__):
                with self.assertRaisesRegex(
                    StationEfficiencyStoreError, "NocoBase 未返回合法分页信息",
                ):
                    fetch_events(
                        *arguments,
                        request_json=lambda *args: {"data": []},
                    )

    def test_empty_first_event_page_with_zero_total_pages_is_complete(self):
        event_queries = (
            (fetch_active_events, ("ES02", CONFIG)),
            (
                fetch_dashboard_events,
                (
                    "ES02",
                    "2026-08-27T00:00:00+08:00",
                    "2026-08-28T00:00:00+08:00",
                    CONFIG,
                ),
            ),
        )

        for fetch_events, arguments in event_queries:
            with self.subTest(fetch_events=fetch_events.__name__):
                self.assertEqual(
                    fetch_events(
                        *arguments,
                        request_json=lambda *args: {
                            "data": [], "meta": {"totalPage": 0},
                        },
                    ),
                    [],
                )

    def test_event_queries_reject_non_integer_empty_zero_page_values(self):
        event_queries = (
            (fetch_active_events, ("ES02", CONFIG)),
            (
                fetch_dashboard_events,
                (
                    "ES02",
                    "2026-08-27T00:00:00+08:00",
                    "2026-08-28T00:00:00+08:00",
                    CONFIG,
                ),
            ),
        )

        for total_pages in (False, 0.0):
            for fetch_events, arguments in event_queries:
                with self.subTest(
                    total_pages=total_pages,
                    fetch_events=fetch_events.__name__,
                ):
                    with self.assertRaisesRegex(
                        StationEfficiencyStoreError, "NocoBase 未返回合法分页信息",
                    ):
                        fetch_events(
                            *arguments,
                            request_json=lambda *args: {
                                "data": [], "meta": {"totalPage": total_pages},
                            },
                        )

    def test_event_queries_reject_nonempty_zero_total_pages(self):
        event_queries = (
            (fetch_active_events, ("ES02", CONFIG)),
            (
                fetch_dashboard_events,
                (
                    "ES02",
                    "2026-08-27T00:00:00+08:00",
                    "2026-08-28T00:00:00+08:00",
                    CONFIG,
                ),
            ),
        )

        for fetch_events, arguments in event_queries:
            with self.subTest(fetch_events=fetch_events.__name__):
                with self.assertRaisesRegex(
                    StationEfficiencyStoreError, "NocoBase 未返回合法分页信息",
                ):
                    fetch_events(
                        *arguments,
                        request_json=lambda *args: {
                            "data": [{"id": 1}], "meta": {"totalPage": 0},
                        },
                    )

    def test_event_queries_reject_zero_page_after_nonempty_first_page(self):
        calls = []

        def request_json(url, *args):
            calls.append(url)
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            if page == 1:
                return {"data": [{"id": 1}], "meta": {"totalPage": 2}}
            return {"data": [], "meta": {"totalPage": 0}}

        with self.assertRaisesRegex(
            StationEfficiencyStoreError, "NocoBase 未返回合法分页信息",
        ):
            fetch_active_events("ES02", CONFIG, request_json=request_json)

        self.assertEqual(
            [parse_qs(urlsplit(url).query)["page"] for url in calls],
            [["1"], ["2"]],
        )

    def test_dashboard_events_include_active_and_recovered_in_day(self):
        calls = []

        fetch_dashboard_events(
            "ES02",
            "2026-08-27T00:00:00+08:00",
            "2026-08-28T00:00:00+08:00",
            CONFIG,
            request_json=lambda *args: calls.append(args) or {
                "data": [], "meta": {"totalPage": 1},
            },
        )

        filter_value = json.loads(parse_qs(urlsplit(calls[0][0]).query)["filter"][0])
        self.assertEqual(filter_value["$and"][0], {"station_id": {"$eq": "ES02"}})
        self.assertIn("$or", filter_value["$and"][1])

    def test_dashboard_events_aggregates_all_pages(self):
        calls = []

        def request_json(url, *args):
            calls.append(url)
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return {"data": [{"id": page}], "meta": {"totalPage": 2}}

        rows = fetch_dashboard_events(
            "ES02",
            "2026-08-27T00:00:00+08:00",
            "2026-08-28T00:00:00+08:00",
            CONFIG,
            request_json=request_json,
        )

        self.assertEqual(rows, [{"id": 1}, {"id": 2}])
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["page"] for url in calls],
            [["1"], ["2"]],
        )

    def test_cleanup_destroys_only_old_device_points(self):
        calls = []

        deleted = delete_device_points_before(
            "2026-07-28T10:00:00+08:00",
            CONFIG,
            request_json=lambda *args: calls.append(args) or {"data": 123},
        )

        parsed = urlsplit(calls[0][0])
        self.assertEqual(parsed.path, "/api/t_efficiency_device_points:destroy")
        self.assertEqual(json.loads(parse_qs(parsed.query)["filter"][0]), {
            "data_time": {"$lt": "2026-07-28T02:00:00+00:00"}
        })
        self.assertEqual(deleted, 123)

    def test_fetches_one_station_calendar_day_in_time_order(self):
        calls = []

        def request_json(url, token, timeout):
            calls.append((url, token, timeout))
            return {"data": [MINUTE_POINT]}

        rows = fetch_minute_points(
            "ES02",
            "2026-08-26T00:00:00+08:00",
            "2026-08-27T00:00:00+08:00",
            CONFIG,
            request_json=request_json,
        )

        self.assertEqual(rows, [MINUTE_POINT])
        self.assertEqual(calls[0][1:], ("test-secret", 7.0))
        parsed = urlsplit(calls[0][0])
        self.assertEqual(parsed.path, "/api/t_efficiency_points:list")
        query = parse_qs(parsed.query)
        self.assertEqual(query["sort[]"], ["data_time"])
        self.assertEqual(query["pageSize"], ["1440"])
        self.assertEqual(
            json.loads(query["filter"][0]),
            {
                "$and": [
                    {"station_id": {"$eq": "ES02"}},
                    {
                        "data_time": {
                            "$gte": "2026-08-25T16:00:00+00:00",
                            "$lt": "2026-08-26T16:00:00+00:00",
                        }
                    },
                ]
            },
        )

    def test_minute_save_uses_fixed_update_or_create_request(self):
        response = io.BytesIO(json.dumps({"data": {"id": 11}}).encode("utf-8"))

        with patch(
            "m2.station_efficiency_nocobase.urlopen",
            return_value=response,
        ) as mocked_urlopen:
            saved = save_minute_point(MINUTE_POINT, CONFIG)

        request = mocked_urlopen.call_args.args[0]
        parsed_url = urlsplit(request.full_url)
        self.assertEqual(parsed_url.scheme, "https")
        self.assertEqual(parsed_url.netloc, "vifa.hlszh.com")
        self.assertEqual(
            parsed_url.path,
            "/api/t_efficiency_points:updateOrCreate",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(mocked_urlopen.call_args.kwargs, {"timeout": 7.0})
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            MINUTE_POINT,
        )
        self.assertEqual(
            parse_qs(parsed_url.query)["filterKeys[]"],
            ["station_id", "data_time"],
        )
        self.assertEqual(saved, {"id": 11})
        self.assertNotIn("test-secret", request.full_url)
        self.assertNotIn(b"test-secret", request.data)

    def test_event_save_uses_event_identity_as_update_key(self):
        calls = []

        def request_json(url, token, timeout, body):
            calls.append((url, token, timeout, body))
            return {"data": {"id": 21, **body}}

        saved = save_bottleneck_event(
            BOTTLENECK_EVENT,
            CONFIG,
            request_json=request_json,
        )

        self.assertEqual(calls[0][0], (
            "https://vifa.hlszh.com/api/"
            "t_efficiency_bottleneck_events:updateOrCreate"
            "?filterKeys%5B%5D=station_id"
            "&filterKeys%5B%5D=event_type"
            "&filterKeys%5B%5D=device_id"
            "&filterKeys%5B%5D=start_time"
        ))
        self.assertEqual(
            calls[0][3],
            BOTTLENECK_EVENT,
        )
        self.assertEqual(saved["id"], 21)

    def test_network_error_does_not_expose_token(self):
        with patch(
            "m2.station_efficiency_nocobase.urlopen",
            side_effect=URLError("connection failed with test-secret"),
        ):
            with self.assertRaises(StationEfficiencyStoreError) as caught:
                save_minute_point(MINUTE_POINT, CONFIG)

        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(str(caught.exception), "NocoBase 写入失败")


if __name__ == "__main__":
    unittest.main()
