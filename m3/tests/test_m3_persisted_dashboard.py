"""Read-only dashboard assembly from persisted M3 forecast evidence."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from m3.worker.config import StationBinding
from m3.worker.contracts import ForecastPoint, ForecastSeries, LatestSnapshot, ObservationPoint
from m3.worker.errors import M3Error
from m3.worker.services.persisted_dashboard_service import PersistedDashboardService


STATION_1 = StationBinding("plant-alpha-ES01", "station_1", "1# 电站")
STATION_2 = StationBinding("plant-beta-ES02", "station_2", "2# 电站")
STATIONS = (STATION_1, STATION_2)
FORECAST_START = datetime.fromisoformat("2026-08-26T10:00:00+08:00")
NOW = datetime.fromisoformat("2026-08-26T10:05:00+08:00")
RUN_ID = "run-20260820"


def _series() -> list[ForecastSeries]:
    output = []
    for unique_id, unit, value in (
        ("station_total_load", "kW", 500.0),
        ("storage_soc", "%", 60.0),
    ):
        points = []
        for index in range(96):
            data_time = FORECAST_START + timedelta(minutes=15 * index)
            points.append(ForecastPoint(
                data_time=data_time,
                target_time=data_time + timedelta(minutes=15),
                horizon_step=index + 1,
                raw_forecast=value,
                forecast_value=value,
                is_clipped=False,
            ))
        output.append(ForecastSeries(
            unique_id=unique_id,
            unit=unit,
            model_name="SeasonalNaive",
            status="ok",
            points=points,
        ))
    return output


def _latest_row(station_id: str) -> dict:
    snapshot = LatestSnapshot(
        station_id=station_id,
        as_of=datetime.fromisoformat("2026-08-26T10:02:00+08:00"),
        generated_at=datetime.fromisoformat("2026-08-26T10:03:00+08:00"),
        source_data_end=FORECAST_START,
        status="ok",
        series=_series(),
        model_manifest={
            "statsforecast_version": "2.1.1",
            "readiness": {
                "required_days": 28,
                "required_points": 2688,
                "series": {
                    "station_total_load": {"real_points": 2688},
                    "storage_soc": {"real_points": 2688},
                },
            },
        },
        content_hash=f"hash-{station_id}",
    )
    values = snapshot.model_dump(mode="json")
    values["series_payload"] = values.pop("series")
    return values


def _batches(station_id: str, count: int = 7) -> list[dict]:
    rows = []
    for offset in range(count):
        start = FORECAST_START - timedelta(days=offset)
        rows.append({
            "station_id": station_id,
            "acceptance_run_id": RUN_ID,
            "issued_at": (start + timedelta(minutes=2)).isoformat(),
            "forecast_start_time": start.isoformat(),
            "write_state": "complete",
        })
    return rows


def _evaluations(station_id: str) -> list[dict]:
    common = {
        "station_id": station_id,
        "acceptance_run_id": RUN_ID,
        "expected_count": 672,
        "valid_count": 672,
        "zero_actual_count": 0,
        "mape_percent": 2.5,
        "mae": 1.25,
        "smape_percent": 2.6,
        "wape_percent": 2.4,
        "median_ape_percent": 1.9,
        "p90_ape_percent": 5.2,
        "outcome": "passed",
    }
    series = [
        {**common, "evaluation_key": "station_total_load"},
        {**common, "evaluation_key": "storage_soc"},
    ]
    return [
        *series,
        {
            **common,
            "evaluation_key": "overall",
            "expected_count": 1344,
            "valid_count": 1344,
            "mape_percent": None,
            "mae": None,
            "smape_percent": None,
            "wape_percent": None,
            "median_ape_percent": None,
            "p90_ape_percent": None,
        },
    ]


def _actual(start: datetime, end: datetime) -> list[ObservationPoint]:
    points = []
    for unique_id, value in (("station_total_load", 450.0), ("storage_soc", 55.0)):
        cursor = start
        while cursor < end:
            points.append(ObservationPoint(
                unique_id=unique_id,
                ds=cursor,
                y=value,
                quality="valid",
                source_revision=1,
            ))
            cursor += timedelta(minutes=15)
    return points


class FakeApi:
    def __init__(self) -> None:
        self.latest = {binding.station_id: _latest_row(binding.station_id) for binding in STATIONS}
        self.batches = {binding.station_id: _batches(binding.station_id) for binding in STATIONS}
        self.evaluations = {binding.station_id: _evaluations(binding.station_id) for binding in STATIONS}
        self.calls: list[SimpleNamespace] = []

    def list_records(self, collection, *, filter, fields, sort=None):
        self.calls.append(SimpleNamespace(
            collection=collection, filter=filter, fields=fields, sort=sort
        ))
        station_id = filter["station_id"]
        if collection == "energy_forecast_latest":
            value = self.latest[station_id]
            if isinstance(value, Exception):
                raise value
            return [] if value is None else [dict(value)]
        if collection == "energy_forecast_batches":
            value = self.batches[station_id]
            if isinstance(value, Exception):
                raise value
            return [dict(row) for row in value]
        if collection == "energy_forecast_evaluations":
            value = self.evaluations[station_id]
            if isinstance(value, Exception):
                raise value
            return [dict(row) for row in value]
        raise AssertionError(collection)


class FakeSource:
    def __init__(self) -> None:
        self.failed: set[str] = set()
        self.calls: list[SimpleNamespace] = []

    def list_observations(self, station_id, start, end):
        self.calls.append(SimpleNamespace(station_id=station_id, start=start, end=end))
        if station_id in self.failed:
            raise M3Error("source_http_failed", "source unavailable")
        return _actual(start, end)


class PersistedDashboardTests(unittest.TestCase):
    def make_service(self, *, api=None, source=None):
        api = api or FakeApi()
        source = source or FakeSource()
        return (
            PersistedDashboardService(
                source=source,
                api=api,
                bindings=STATIONS,
                clock=lambda: NOW,
            ),
            api,
            source,
        )

    def test_builds_two_station_dashboard_from_latest_batches_and_evaluations(self):
        service, api, source = self.make_service()

        envelope = service.build()

        payload = envelope.model_dump(mode="json")["data"]
        self.assertEqual(payload["system"]["state"], "ready")
        self.assertEqual(
            [station["station_key"] for station in payload["stations"]],
            ["station_1", "station_2"],
        )
        self.assertTrue(all(station["acceptance"]["status"] == "passed" for station in payload["stations"]))
        first_result = payload["stations"][0]["acceptance"]["results"][0]
        self.assertEqual(
            (
                first_result["mape_percent"],
                first_result["wape_percent"],
                first_result["median_ape_percent"],
                first_result["p90_ape_percent"],
            ),
            (2.5, 2.4, 1.9, 5.2),
        )
        self.assertEqual(len(payload["stations"][0]["series"][0]["forecast"]), 96)
        self.assertEqual(len(payload["stations"][0]["series"][0]["actual"]), 96)
        self.assertIn("readiness", payload["stations"][0])
        self.assertEqual(
            payload["stations"][0]["readiness"],
            {
                "required_days": 28,
                "available_days": 28.0,
                "remaining_days": 0.0,
            },
        )
        self.assertEqual(len(source.calls), 2)
        self.assertTrue(all(call.end - call.start == timedelta(hours=24) for call in source.calls))
        self.assertFalse(hasattr(api, "create_record"))
        self.assertNotIn("plant-alpha-ES01", envelope.model_dump_json())

    def test_warming_station_exposes_shorter_series_readiness_progress(self):
        api = FakeApi()
        row = api.latest[STATION_1.station_id]
        row["status"] = "warming_up"
        for series in row["series_payload"]:
            series["status"] = "warming_up"
        row["model_manifest"]["readiness"]["series"] = {
            "station_total_load": {"real_points": 2160},
            "storage_soc": {"real_points": 2112},
        }
        service, _, _ = self.make_service(api=api)

        station = service.build().model_dump(mode="python")["data"]["stations"][0]

        self.assertEqual(station["system"]["state"], "initializing")
        self.assertIn("readiness", station)
        self.assertEqual(
            station["readiness"],
            {
                "required_days": 28,
                "available_days": 22.0,
                "remaining_days": 6.0,
            },
        )

    def test_missing_latest_is_initializing_without_failing_healthy_peer(self):
        api = FakeApi()
        api.latest[STATION_1.station_id] = None
        service, _, source = self.make_service(api=api)

        stations = service.build().data.stations

        self.assertEqual(stations[0].system.state, "initializing")
        self.assertEqual(stations[1].system.state, "ready")
        self.assertEqual([call.station_id for call in source.calls], [STATION_2.station_id])

    def test_one_nocobase_failure_is_isolated_and_both_failures_are_fatal(self):
        api = FakeApi()
        api.latest[STATION_1.station_id] = M3Error("sink_http_failed", "unavailable")
        service, _, _ = self.make_service(api=api)

        envelope = service.build()

        self.assertEqual(envelope.data.stations[0].system.state, "error")
        self.assertEqual(envelope.data.stations[1].system.state, "ready")
        self.assertEqual(envelope.data.system.state, "degraded")

        api.latest[STATION_2.station_id] = M3Error("sink_http_failed", "unavailable")
        service, _, _ = self.make_service(api=api)
        with self.assertRaises(M3Error) as raised:
            service.build()
        self.assertEqual(raised.exception.code, "sink_http_failed")

    def test_one_station_public_projection_failure_is_isolated(self):
        api = FakeApi()
        point = api.latest[STATION_1.station_id]["series_payload"][0]["points"][0]
        point.update({
            "raw_forecast": -2.0,
            "forecast_value": 1.0,
            "is_clipped": True,
        })
        service, _, _ = self.make_service(api=api)

        try:
            envelope = service.build()
        except ValidationError as error:
            self.fail(f"one invalid station projection was not isolated: {error}")

        self.assertEqual(envelope.data.stations[0].system.state, "error")
        self.assertEqual(envelope.data.stations[1].system.state, "ready")
        self.assertEqual(envelope.data.system.state, "degraded")

    def test_raw_actual_failure_retains_forecast_and_marks_station_degraded(self):
        source = FakeSource()
        source.failed.add(STATION_1.station_id)
        service, _, _ = self.make_service(source=source)

        station = service.build().data.stations[0]

        self.assertEqual(station.system.state, "degraded")
        self.assertEqual(station.system.mode, "degraded")
        self.assertEqual(len(station.series[0].forecast), 96)
        self.assertEqual(station.series[0].actual, [])

    def test_partial_acceptance_stays_in_progress_without_metric_rows(self):
        api = FakeApi()
        api.batches[STATION_1.station_id] = _batches(STATION_1.station_id, 3)
        service, api, _ = self.make_service(api=api)

        acceptance = service.build().data.stations[0].acceptance

        self.assertEqual(acceptance.status, "in_progress")
        self.assertEqual(acceptance.completed_days, 3)
        self.assertEqual(acceptance.results, [])
        station_calls = [
            call.collection
            for call in api.calls
            if call.filter.get("station_id") == STATION_1.station_id
        ]
        self.assertNotIn("energy_forecast_evaluations", station_calls)


if __name__ == "__main__":
    unittest.main()
