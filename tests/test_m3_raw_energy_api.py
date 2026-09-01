from datetime import datetime, timedelta
import json
import unittest

import httpx

from m3_worker.clients.raw_energy_api import RawEnergySourceClient
from m3_worker.errors import M3Error


STATION_1 = "plant-alpha-ES01"
STATION_2 = "plant-beta-ES02"
START = datetime.fromisoformat("2026-08-26T09:00:00+08:00")
END = START + timedelta(minutes=15)


def minute_rows(
    *, es_sn: str, load_power: object, solar_power: object, emus_soc: object,
    start: str = "2026-08-26T01:00:00.000Z", count: int = 15,
) -> list[dict[str, object]]:
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {
            "timestamp": (first + timedelta(minutes=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "es_sn": es_sn,
            "load_power": load_power,
            "solar_power": solar_power,
            "emus_soc": emus_soc,
        }
        for index in range(count)
    ]


def envelope(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"data": rows, "meta": {"hasNext": False, "page": 1, "pageSize": 1000}}


class RawEnergySourceClientTests(unittest.TestCase):
    def make_client(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return RawEnergySourceClient(
            "https://source.example/api/t_es_data:list", "source-token", http,
            allowed_station_ids=(STATION_1, STATION_2),
        )

    def test_station_query_uses_full_es_sn_and_load_power_only(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=envelope(minute_rows(
                es_sn=STATION_1, load_power="40", solar_power=999, emus_soc=61,
            )))

        points = self.make_client(handler).list_observations(STATION_1, START, END)

        self.assertEqual([point.unique_id for point in points], ["station_total_load", "storage_soc"])
        self.assertEqual(points[0].y, 40.0)
        self.assertEqual(points[1].y, 61.0)
        encoded_filter = json.loads(seen[0].url.params["filter"])
        self.assertEqual(encoded_filter["es_sn"], {"$eq": STATION_1})
        self.assertNotIn("source-token", str(seen[0].url))

    def test_aggregation_uses_real_load_mean_and_last_soc_value(self):
        rows = minute_rows(
            es_sn=STATION_1, load_power=0, solar_power=0, emus_soc=0,
        )
        rows = [
            {**row, "load_power": 5 + index * 3, "emus_soc": 31 + index}
            for index, row in enumerate(rows)
        ]
        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows))
        ).list_observations(STATION_1, START, END)

        self.assertEqual(points[0].y, 26.0)
        self.assertEqual(points[1].y, 45.0)

    def test_malformed_values_are_series_local_and_last_valid_soc_wins(self):
        rows = minute_rows(
            es_sn=STATION_1, load_power=0, solar_power=0, emus_soc=0,
        )
        rows = [
            {**row, "load_power": 5 + index * 3, "emus_soc": 31 + index}
            for index, row in enumerate(rows)
        ]
        rows[1]["load_power"] = " 8"
        rows[2]["load_power"] = True
        rows[3]["load_power"] = "NaN"
        rows[4].pop("load_power")
        rows[5]["emus_soc"] = "not-a-number"
        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows))
        ).list_observations(STATION_1, START, END)

        self.assertEqual(points[0].quality, "invalid")
        self.assertIsNone(points[0].y)
        self.assertEqual(points[1].quality, "valid")
        self.assertEqual(points[1].y, 45.0)

    def test_negative_load_samples_are_filtered_without_poisoning_covered_bucket(self):
        rows = minute_rows(
            es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61,
        )
        rows[0]["load_power"] = -90
        rows[1]["load_power"] = -89

        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows))
        ).list_observations(STATION_1, START, END)

        self.assertEqual(points[0].quality, "valid")
        self.assertEqual(points[0].y, 40.0)

    def test_custom_aggregation_accepts_mixed_one_and_two_minute_cadence(self):
        rows = minute_rows(
            es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61,
            count=30,
        )
        mixed_cadence_rows = [
            row for index, row in enumerate(rows)
            if index < 10 or index % 2 == 0
        ]

        points = self.make_client(
            lambda _request: httpx.Response(
                200, json=envelope(mixed_cadence_rows)
            )
        ).list_custom_observations(
            STATION_1, START, START + timedelta(minutes=30),
            interval_seconds=1800,
        )

        self.assertEqual([point.quality for point in points], ["valid", "valid"])
        self.assertEqual([point.y for point in points], [40.0, 61.0])

    def test_custom_aggregation_rejects_long_internal_sampling_gap(self):
        rows = minute_rows(
            es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61,
            count=30,
        )
        rows_with_gap = [
            row for index, row in enumerate(rows) if not 12 <= index <= 17
        ]

        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows_with_gap))
        ).list_custom_observations(
            STATION_1, START, START + timedelta(minutes=30),
            interval_seconds=1800,
        )

        self.assertEqual([point.quality for point in points], ["invalid", "invalid"])
        self.assertEqual([point.y for point in points], [None, None])

    def test_custom_load_points_distinguish_leading_no_rows_from_negative_load(self):
        rows = minute_rows(
            es_sn=STATION_2,
            load_power=-40,
            solar_power=0,
            emus_soc=61,
            start="2026-08-26T01:30:00.000Z",
            count=15,
        )

        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows))
        ).list_custom_observations(
            STATION_2, START, START + timedelta(minutes=45),
            interval_seconds=900,
        )

        load_points = [
            point for point in points if point.unique_id == "station_total_load"
        ]
        self.assertEqual(
            [point.source_state for point in load_points],
            ["no_rows", "no_rows", "negative"],
        )
        self.assertTrue(all(point.quality == "invalid" for point in load_points))

    def test_earlier_out_of_range_soc_does_not_poison_final_soc(self):
        rows = minute_rows(
            es_sn=STATION_2, load_power=0, solar_power=0, emus_soc=0,
        )
        rows = [
            {**row, "load_power": 5 + index * 3, "emus_soc": 31 + index}
            for index, row in enumerate(rows)
        ]
        rows[5]["emus_soc"] = 101
        points = self.make_client(
            lambda _request: httpx.Response(200, json=envelope(rows))
        ).list_observations(STATION_2, START, END)

        self.assertEqual(points[0].y, 26.0)
        self.assertEqual(points[1].quality, "valid")
        self.assertEqual(points[1].y, 45.0)

    def test_redirect_target_is_never_requested_when_client_allows_redirects(self):
        seen_paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            if request.url.path == "/api/t_es_data:list":
                return httpx.Response(302, headers={"Location": "/redirected"})
            self.fail("redirect target must not be requested")

        http = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        )
        self.addCleanup(http.close)
        client = RawEnergySourceClient(
            "https://source.example/api/t_es_data:list", "source-token", http,
            allowed_station_ids=(STATION_1, STATION_2),
        )
        with self.assertRaises(M3Error) as raised:
            client.list_observations(STATION_1, START, END)

        self.assertEqual(raised.exception.code, "source_contract_invalid")
        self.assertEqual(seen_paths, ["/api/t_es_data:list"])

    def test_cross_station_row_is_rejected_before_any_point_is_returned(self):
        client = self.make_client(lambda _request: httpx.Response(
            200, json=envelope(minute_rows(
                es_sn=STATION_2, load_power=40, solar_power=0, emus_soc=61,
            ))
        ))
        with self.assertRaises(M3Error) as raised:
            client.list_observations(STATION_1, START, END)
        self.assertEqual(raised.exception.code, "source_contract_invalid")

    def test_aggregation_is_station_isolated_and_ignores_solar_power(self):
        rows_1 = minute_rows(es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61)
        rows_2 = minute_rows(es_sn=STATION_2, load_power=70, solar_power=0, emus_soc=72)

        def points_for(station_id: str, rows: list[dict[str, object]]):
            return self.make_client(lambda _request: httpx.Response(200, json=envelope(rows))).list_observations(station_id, START, END)

        baseline_2 = points_for(STATION_2, rows_2)
        altered_solar = [{**row, "solar_power": 9999} for row in rows_1]
        altered_1 = [{**row, "load_power": 400} for row in altered_solar]
        self.assertEqual(points_for(STATION_1, rows_1), points_for(STATION_1, altered_solar))
        self.assertEqual(baseline_2, points_for(STATION_2, rows_2))
        self.assertNotEqual(points_for(STATION_1, rows_1), points_for(STATION_1, altered_1))

    def test_invalid_coverage_and_values_affect_only_their_station_series(self):
        cases = (
            (STATION_1, minute_rows(es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61, count=11), {"station_total_load", "storage_soc"}),
            (STATION_2, minute_rows(es_sn=STATION_2, load_power=40, solar_power=0, emus_soc=61)[3:], {"station_total_load", "storage_soc"}),
            (STATION_1, minute_rows(es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61)[:-3], {"station_total_load", "storage_soc"}),
            (STATION_1, [{**row, "load_power": -1} for row in minute_rows(es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61)], {"station_total_load"}),
            (STATION_2, [{**row, "emus_soc": 101} for row in minute_rows(es_sn=STATION_2, load_power=40, solar_power=0, emus_soc=61)], {"storage_soc"}),
        )
        for station_id, rows, invalid_series in cases:
            with self.subTest(station_id=station_id, invalid_series=invalid_series):
                points = self.make_client(lambda _request: httpx.Response(200, json=envelope(rows))).list_observations(station_id, START, END)
                self.assertEqual(
                    {point.unique_id for point in points if point.quality == "invalid"}, invalid_series
                )
                self.assertTrue(all(point.y is None for point in points if point.quality == "invalid"))

    def test_latest_timestamp_filters_to_exact_station_and_does_not_choose_peer(self):
        seen = []
        records = [
            minute_rows(es_sn=STATION_1, load_power=40, solar_power=0, emus_soc=61)[-1],
            {**minute_rows(es_sn=STATION_2, load_power=70, solar_power=0, emus_soc=72)[-1], "timestamp": "2026-08-26T01:15:00.000Z"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            station_id = json.loads(request.url.params["filter"])["es_sn"]["$eq"]
            return httpx.Response(200, json=envelope([
                row for row in records if row["es_sn"] == station_id
            ]))

        latest = self.make_client(handler).latest_timestamp(STATION_1)
        self.assertEqual(latest, datetime.fromisoformat("2026-08-26T09:14:00+08:00"))
        encoded_filter = json.loads(seen[0].url.params["filter"])
        self.assertEqual(encoded_filter["es_sn"], {"$eq": STATION_1})

    def test_unconfigured_station_fails_before_http(self):
        client = self.make_client(lambda _request: self.fail("HTTP must not be called"))
        with self.assertRaises(M3Error) as raised:
            client.list_observations("unknown-ES01", START, END)
        self.assertEqual(raised.exception.code, "source_contract_invalid")
