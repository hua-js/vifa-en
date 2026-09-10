"""Strict two-station live-dashboard backend tests."""

from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread as TestThread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from m3.worker.config import StationBinding
from m3.worker.contracts import ForecastPoint, ForecastSeries, ObservationPoint
from m3.worker.dashboard_contracts import (
    DashboardAcceptance,
    DashboardAcceptanceResult,
    DashboardEnvelope,
)
from m3.worker.live_dashboard_app import LiveDashboardResources, create_live_dashboard_app
from m3.worker.services.live_dashboard_service import (
    DashboardCache,
    DashboardStationResult,
    LiveDashboardProvider,
    PeriodicDashboardRefresher,
    build_dashboard_payload,
    floor_quarter_hour,
    forecast_station,
)
from m3.worker.errors import M3Error


STATION_1 = StationBinding("plant-alpha-ES01", "station_1", "1# 电站")
STATION_2 = StationBinding("plant-beta-ES02", "station_2", "2# 电站")
STATIONS = (STATION_1, STATION_2)
STATION_1_AS_OF = datetime.fromisoformat("2026-08-26T10:00:00+08:00")
STATION_2_AS_OF = datetime.fromisoformat("2026-08-26T09:45:00+08:00")
GENERATED_AT = datetime.fromisoformat("2026-08-26T10:01:00+08:00")
SERIES_IDS = ("station_total_load", "storage_soc")


def route_paths(app) -> list[str]:
    paths: list[str] = []
    for route in app.routes:
        if hasattr(route, "path"):
            paths.append(route.path)
        elif hasattr(route, "original_router"):
            paths.extend(item.path for item in route.original_router.routes)
    return sorted(paths)


def actual_points(as_of: datetime) -> list[ObservationPoint]:
    points: list[ObservationPoint] = []
    start = as_of - timedelta(hours=24)
    for unique_id in SERIES_IDS:
        for index in range(96):
            value = 400.0 + index if unique_id == "station_total_load" else 55.0
            points.append(ObservationPoint(
                unique_id=unique_id,
                ds=start + timedelta(minutes=15 * index),
                y=value,
                quality="valid",
                source_revision=index + 1,
            ))
    return points


def forecast_series(as_of: datetime) -> list[ForecastSeries]:
    output: list[ForecastSeries] = []
    for unique_id in SERIES_IDS:
        points: list[ForecastPoint] = []
        for index in range(96):
            value = 500.0 + index if unique_id == "station_total_load" else 60.0
            data_time = as_of + timedelta(minutes=15 * index)
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
            unit="kW" if unique_id == "station_total_load" else "%",
            model_name="SeasonalNaive",
            status="warming_up",
            points=points,
        ))
    return output


def station_result(
    binding: StationBinding,
    *, as_of: datetime | None = None,
    generated_at: datetime = GENERATED_AT,
) -> DashboardStationResult:
    station_as_of = as_of or (
        STATION_1_AS_OF if binding.station_key == "station_1" else STATION_2_AS_OF
    )
    return DashboardStationResult(
        binding=binding,
        as_of=station_as_of,
        generated_at=generated_at,
        actual=actual_points(station_as_of),
        forecasts=forecast_series(station_as_of),
    )


class ControlledProvider:
    def __init__(self) -> None:
        self.call_count = 0
        self.failed: set[str] = set()
        self.as_of = {
            STATION_1.station_id: STATION_1_AS_OF,
            STATION_2.station_id: STATION_2_AS_OF,
        }

    def build_station(self, binding: StationBinding) -> DashboardStationResult:
        self.call_count += 1
        if binding.station_id in self.failed:
            raise RuntimeError(f"unsafe {binding.station_id} Bearer should-not-log")
        return station_result(binding, as_of=self.as_of[binding.station_id])

    def fail_station(self, station_id: str) -> None:
        self.failed.add(station_id)

    def advance_station(self, station_id: str, *, minutes: int) -> None:
        self.as_of[station_id] += timedelta(minutes=minutes)


class DashboardContractTests(unittest.TestCase):
    def envelope(self) -> DashboardEnvelope:
        return build_dashboard_payload(
            [station_result(STATION_1), station_result(STATION_2)],
            generated_at=GENERATED_AT,
        )

    def test_dashboard_contains_two_public_stations_and_no_full_es_sn(self):
        envelope = self.envelope()
        payload = envelope.model_dump(mode="json")["data"]
        self.assertEqual(payload["operation"], "forecast_dashboard")
        self.assertEqual(
            [(item["station_key"], item["station_name"]) for item in payload["stations"]],
            [("station_1", "1# 电站"), ("station_2", "2# 电站")],
        )
        self.assertTrue(all(
            [series["unique_id"] for series in item["series"]]
            == ["station_total_load", "storage_soc"]
            for item in payload["stations"]
        ))
        rendered = envelope.model_dump_json()
        self.assertNotIn("plant-alpha-ES01", rendered)
        self.assertNotIn("plant-beta-ES02", rendered)

    def test_station_ranges_keep_independent_forecast_times(self):
        stations = self.envelope().model_dump(mode="json")["data"]["stations"]
        self.assertEqual(
            [item["range"]["forecast_start"] for item in stations],
            [STATION_1_AS_OF.isoformat(), STATION_2_AS_OF.isoformat()],
        )
        self.assertEqual(
            [item["range"]["forecast_end"] for item in stations],
            [(STATION_1_AS_OF + timedelta(hours=24)).isoformat(),
             (STATION_2_AS_OF + timedelta(hours=24)).isoformat()],
        )

    def test_explicit_degraded_result_keeps_forecasts_visible(self):
        result = station_result(STATION_1)
        degraded = DashboardStationResult(
            binding=result.binding,
            as_of=result.as_of,
            generated_at=result.generated_at,
            actual=[],
            forecasts=result.forecasts,
            degraded=True,
        )

        station = build_dashboard_payload(
            [degraded, station_result(STATION_2)], generated_at=GENERATED_AT
        ).data.stations[0]

        self.assertEqual(station.system.state, "degraded")
        self.assertEqual(station.system.mode, "degraded")
        self.assertEqual(len(station.series[0].forecast), 96)
        self.assertEqual(station.series[0].actual, [])

    def test_dashboard_accepts_operational_soc_safety_clipping(self):
        result = station_result(STATION_1)
        soc = result.forecasts[1]
        points = list(soc.points)
        points[0] = points[0].model_copy(update={
            "raw_forecast": 1.0,
            "forecast_value": 2.0,
            "is_clipped": True,
        })
        points[1] = points[1].model_copy(update={
            "raw_forecast": 100.0,
            "forecast_value": 99.0,
            "is_clipped": True,
        })
        result.forecasts[1] = ForecastSeries(
            unique_id=soc.unique_id,
            unit=soc.unit,
            model_name=soc.model_name,
            status=soc.status,
            points=points,
        )

        try:
            station = build_dashboard_payload(
                [result, station_result(STATION_2)],
                generated_at=GENERATED_AT,
            ).data.stations[0]
        except ValidationError as error:
            self.fail(f"valid operational SOC clipping was rejected: {error}")

        clipped = station.series[1].forecast[:2]
        self.assertEqual(
            [(point.raw_value, point.value, point.is_clipped) for point in clipped],
            [(1.0, 2.0, True), (100.0, 99.0, True)],
        )

    def test_dashboard_rejects_wrong_station_or_series_order(self):
        payload = self.envelope().model_dump(mode="python")
        for mutate in (
            lambda value: value["data"].__setitem__(
                "stations", value["data"]["stations"][::-1]
            ),
            lambda value: value["data"]["stations"][0].__setitem__(
                "series", value["data"]["stations"][0]["series"][::-1]
            ),
        ):
            with self.subTest(mutate=mutate):
                invalid = deepcopy(payload)
                mutate(invalid)
                with self.assertRaises(ValidationError):
                    DashboardEnvelope.model_validate(invalid)

    def test_dashboard_rejects_non_24_hour_ranges_and_broken_96_point_forecasts(self):
        payload = self.envelope().model_dump(mode="python")
        cases = []
        wrong_range = deepcopy(payload)
        wrong_range["data"]["stations"][0]["range"]["forecast_end"] -= timedelta(minutes=15)
        cases.append(wrong_range)
        short_forecast = deepcopy(payload)
        short_forecast["data"]["stations"][0]["series"][0]["forecast"].pop()
        cases.append(short_forecast)
        broken_continuity = deepcopy(payload)
        broken_continuity["data"]["stations"][0]["series"][0]["forecast"][1]["data_time"] += timedelta(minutes=15)
        cases.append(broken_continuity)
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                DashboardEnvelope.model_validate(invalid)

    def test_dashboard_rejects_untruthful_clipping_wrong_units_and_aggregate_severity(self):
        payload = self.envelope().model_dump(mode="python")
        cases = []
        clipped = deepcopy(payload)
        clipped_point = clipped["data"]["stations"][0]["series"][0]["forecast"][0]
        clipped_point.update(raw_value=-2.0, value=0.0, is_clipped=False)
        cases.append(clipped)
        wrong_unit = deepcopy(payload)
        wrong_unit["data"]["stations"][1]["series"][1]["unit"] = "kW"
        cases.append(wrong_unit)
        wrong_aggregate = deepcopy(payload)
        wrong_aggregate["data"]["system"]["state"] = "ready"
        cases.append(wrong_aggregate)
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                DashboardEnvelope.model_validate(invalid)

    def test_acceptance_requires_empty_in_progress_or_two_ordered_final_results(self):
        in_progress = DashboardAcceptance(
            acceptance_run_id="run-1", status="in_progress", completed_days=3, results=[]
        )
        self.assertEqual(in_progress.results, [])
        result_values = {
            "expected_count": 672, "valid_count": 672, "zero_actual_count": 0,
            "mape_percent": 2.0, "mae": 1.0, "smape_percent": 2.1,
            "wape_percent": 1.8, "median_ape_percent": 1.5,
            "p90_ape_percent": 3.2,
            "outcome": "passed",
        }
        ordered = [
            DashboardAcceptanceResult(unique_id=unique_id, **result_values)
            for unique_id in SERIES_IDS
        ]
        final = DashboardAcceptance(
            acceptance_run_id="run-2", status="passed", completed_days=7, results=ordered
        )
        self.assertEqual([item.unique_id for item in final.results], list(SERIES_IDS))
        sparse = DashboardAcceptanceResult(
            unique_id="station_total_load",
            expected_count=672,
            valid_count=12,
            zero_actual_count=20,
            mape_percent=4.0,
            mae=2.0,
            smape_percent=4.2,
            wape_percent=3.8,
            median_ape_percent=3.0,
            p90_ape_percent=7.5,
            outcome="insufficient_data",
        )
        self.assertEqual((sparse.valid_count, sparse.zero_actual_count), (12, 20))
        for status, results in (
            ("in_progress", ordered), ("passed", []), ("failed", ordered[::-1])
        ):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                DashboardAcceptance(
                    acceptance_run_id="run-invalid", status=status,
                    completed_days=7, results=results,
                )

    def test_public_dto_forbids_internal_station_identity_fields(self):
        payload = self.envelope().model_dump(mode="python")
        payload["data"]["stations"][0]["station_id"] = STATION_1.station_id
        with self.assertRaises(ValidationError):
            DashboardEnvelope.model_validate(payload)

    def test_internal_identity_cannot_leak_through_a_peer_display_name(self):
        unsafe_station = StationBinding(
            STATION_1.station_id, "station_1", STATION_2.station_id
        )
        with self.assertRaisesRegex(ValueError, "public station names"):
            build_dashboard_payload(
                [station_result(unsafe_station), station_result(STATION_2)],
                generated_at=GENERATED_AT,
            )

    def test_public_adapter_recursively_rejects_internal_ids_in_indirect_strings(self):
        base = station_result(STATION_1)
        unsafe_model = deepcopy(base)
        unsafe_model.forecasts[0] = unsafe_model.forecasts[0].model_copy(
            update={"model_name": STATION_2.station_id}
        )
        unsafe_fallback = deepcopy(base)
        unsafe_fallback.forecasts[0] = unsafe_fallback.forecasts[0].model_copy(
            update={"status": "degraded", "fallback_reason": STATION_1.station_id}
        )
        unsafe_acceptance = SimpleNamespace(
            binding=base.binding,
            as_of=base.as_of,
            generated_at=base.generated_at,
            actual=base.actual,
            forecasts=base.forecasts,
            acceptance=DashboardAcceptance(
                acceptance_run_id=f"run-{STATION_2.station_id}",
                status="in_progress",
                completed_days=1,
                results=[],
            ),
        )
        for label, unsafe in (
            ("model_name", unsafe_model),
            ("fallback_reason", unsafe_fallback),
            ("acceptance_run_id", unsafe_acceptance),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError, "public dashboard payload"
            ):
                build_dashboard_payload(
                    [unsafe, station_result(STATION_2)],
                    generated_at=GENERATED_AT,
                )


class DashboardCacheAndRefreshTests(unittest.TestCase):
    def make_refresher(self):
        provider = ControlledProvider()
        cache = DashboardCache(STATIONS)
        refresher = PeriodicDashboardRefresher(
            provider, cache, STATIONS, interval_seconds=3600
        )
        return provider, cache, refresher

    def test_floor_quarter_hour_uses_each_source_timestamp(self):
        self.assertEqual(
            floor_quarter_hour(datetime.fromisoformat("2026-08-26T10:14:59+08:00")),
            STATION_1_AS_OF,
        )

    def test_live_provider_caches_backtest_once_per_station_and_calendar_day(self):
        """Repeating a live refresh must not rerun the expensive daily backtest or share it across stations."""
        start = STATION_1_AS_OF - timedelta(days=8)
        source_points = []
        for index in range(8 * 96):
            data_time = start + timedelta(minutes=15 * index)
            source_points.extend((
                ObservationPoint(
                    unique_id="station_total_load", ds=data_time, y=100.0,
                    quality="valid", source_revision=index,
                ),
                ObservationPoint(
                    unique_id="storage_soc", ds=data_time, y=50.0,
                    quality="valid", source_revision=index,
                ),
            ))

        class Source:
            def latest_timestamp(self, _station_id):
                return STATION_1_AS_OF + timedelta(minutes=5)

            def list_observations(self, _station_id, start, end):
                return [point for point in source_points if start <= point.ds < end]

        calls: list[datetime] = []

        def backtest(_actual, backtest_as_of):
            calls.append(backtest_as_of)
            values = {
                "expected_count": 672, "valid_count": 672,
                "zero_actual_count": 0, "mape_percent": 0.0,
                "mae": 0.0, "smape_percent": 0.0,
                "wape_percent": 0.0, "median_ape_percent": 0.0,
                "p90_ape_percent": 0.0, "outcome": "passed",
            }
            return DashboardAcceptance(
                acceptance_run_id=f"historical-backtest-{backtest_as_of:%Y%m%d-%H%M}",
                status="passed", completed_days=7,
                results=[
                    DashboardAcceptanceResult(unique_id=unique_id, **values)
                    for unique_id in SERIES_IDS
                ],
            )

        provider = LiveDashboardProvider(
            Source(), clock=lambda: GENERATED_AT, history_days=8,
            backtest_builder=backtest,
        )

        first = provider.build_station(STATION_1)
        repeated = provider.build_station(STATION_1)
        peer = provider.build_station(STATION_2)

        self.assertEqual(first.acceptance.status, "passed")
        self.assertEqual(repeated.acceptance.status, "passed")
        self.assertEqual(peer.acceptance.status, "passed")
        self.assertEqual(
            calls,
            [
                STATION_1_AS_OF.replace(hour=0, minute=0),
                STATION_1_AS_OF.replace(hour=0, minute=0),
            ],
        )

    def test_forecast_station_keeps_valid_load_when_soc_has_no_valid_history(self):
        actual: list[ObservationPoint] = []
        start = STATION_1_AS_OF - timedelta(days=8)
        for index in range(8 * 96):
            data_time = start + timedelta(minutes=15 * index)
            actual.extend((
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=data_time,
                    y=400.0 + index % 96,
                    quality="valid",
                    source_revision=index,
                ),
                ObservationPoint(
                    unique_id="storage_soc",
                    ds=data_time,
                    y=None,
                    quality="invalid",
                    source_revision=index,
                ),
            ))

        forecasts = forecast_station(actual, STATION_1_AS_OF)

        self.assertEqual(
            [(item.unique_id, item.status, len(item.points)) for item in forecasts],
            [
                ("station_total_load", "warming_up", 96),
                ("storage_soc", "insufficient_history", 0),
            ],
        )

    def test_forecast_station_turns_known_training_error_into_one_error_series(self):
        actual: list[ObservationPoint] = []
        start = STATION_1_AS_OF - timedelta(days=8)
        for index in range(8 * 96):
            data_time = start + timedelta(minutes=15 * index)
            actual.extend((
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=data_time,
                    y=400.0,
                    quality="valid",
                    source_revision=index,
                ),
                ObservationPoint(
                    unique_id="storage_soc",
                    ds=data_time,
                    y=55.0,
                    quality="valid",
                    source_revision=index,
                ),
            ))
        duplicate = actual[0].model_copy()
        actual.append(duplicate)

        forecasts = forecast_station(actual, STATION_1_AS_OF)

        self.assertEqual(forecasts[0].status, "error")
        self.assertEqual(forecasts[0].points, [])
        self.assertEqual(forecasts[1].status, "warming_up")
        self.assertEqual(len(forecasts[1].points), 96)

    def test_failed_station_refresh_keeps_its_last_good_value_and_updates_peer(self):
        provider, cache, refresher = self.make_refresher()
        refresher.refresh_all()
        provider.fail_station(STATION_1.station_id)
        provider.advance_station(STATION_2.station_id, minutes=15)
        refresher.refresh_all()
        snapshot = cache.snapshot(GENERATED_AT + timedelta(minutes=15)).model_dump(mode="json")["data"]
        self.assertEqual(snapshot["stations"][0]["range"]["forecast_start"], STATION_1_AS_OF.isoformat())
        self.assertEqual(snapshot["stations"][1]["range"]["forecast_start"], (STATION_2_AS_OF + timedelta(minutes=15)).isoformat())
        self.assertEqual(snapshot["stations"][0]["system"]["state"], "degraded")
        self.assertEqual(snapshot["stations"][1]["system"]["state"], "initializing")

    def test_bad_recent_actual_topology_publishes_only_that_series_as_empty(self):
        actual: list[ObservationPoint] = []
        start = STATION_1_AS_OF - timedelta(days=8)
        for index in range(8 * 96):
            data_time = start + timedelta(minutes=15 * index)
            actual.extend((
                ObservationPoint(
                    unique_id="station_total_load",
                    ds=data_time,
                    y=400.0,
                    quality="valid",
                    source_revision=index,
                ),
                ObservationPoint(
                    unique_id="storage_soc",
                    ds=data_time,
                    y=55.0,
                    quality="valid",
                    source_revision=index,
                ),
            ))
        recent_load = next(
            point for point in actual
            if point.unique_id == "station_total_load"
            and point.ds == STATION_1_AS_OF - timedelta(hours=1)
        )
        actual.append(recent_load.model_copy())
        forecasts = forecast_station(actual, STATION_1_AS_OF)
        bad_station = DashboardStationResult(
            binding=STATION_1,
            as_of=STATION_1_AS_OF,
            generated_at=GENERATED_AT,
            actual=actual,
            forecasts=forecasts,
        )

        envelope = build_dashboard_payload(
            [bad_station, station_result(STATION_2)], generated_at=GENERATED_AT
        )
        cache = DashboardCache(STATIONS)
        cache.publish_station(bad_station)
        cache.publish_station(station_result(STATION_2))
        snapshot = cache.snapshot(GENERATED_AT)

        for candidate in (envelope, snapshot):
            station = candidate.model_dump(mode="python")["data"]["stations"][0]
            load, soc = station["series"]
            self.assertEqual(station["system"]["state"], "degraded")
            self.assertEqual((load["status"], load["actual"], load["forecast"]), ("error", [], []))
            self.assertEqual((soc["status"], len(soc["actual"]), len(soc["forecast"])), ("warming_up", 96, 96))

    def test_forecast_failure_keeps_valid_actual_and_real_actual_latest(self):
        for failed_ids in (
            {"station_total_load"},
            {"station_total_load", "storage_soc"},
        ):
            with self.subTest(failed_ids=failed_ids):
                result = station_result(STATION_1)
                for index, series in enumerate(result.forecasts):
                    if series.unique_id in failed_ids:
                        result.forecasts[index] = ForecastSeries(
                            unique_id=series.unique_id,
                            unit=series.unit,
                            model_name="SeasonalNaive",
                            status="error",
                            points=[],
                            fallback_reason="forecast_failed",
                        )

                station = build_dashboard_payload(
                    [result, station_result(STATION_2)],
                    generated_at=GENERATED_AT,
                ).model_dump(mode="python")["data"]["stations"][0]

                self.assertEqual(
                    [len(series["actual"]) for series in station["series"]],
                    [96, 96],
                )
                self.assertEqual(
                    station["range"]["actual_latest"],
                    STATION_1_AS_OF - timedelta(minutes=15),
                )
                for series in station["series"]:
                    expected_forecasts = 0 if series["unique_id"] in failed_ids else 96
                    self.assertEqual(len(series["forecast"]), expected_forecasts)

    def test_invalid_public_actual_forces_only_that_series_to_safe_error(self):
        def swap_first_two(points: list[ObservationPoint]) -> None:
            indexes = [
                index for index, point in enumerate(points)
                if point.unique_id == "station_total_load"
            ]
            points[indexes[0]], points[indexes[1]] = points[indexes[1]], points[indexes[0]]

        def remove_middle(points: list[ObservationPoint]) -> None:
            index = next(
                index for index, point in enumerate(points)
                if point.unique_id == "station_total_load"
                and point.ds == STATION_1_AS_OF - timedelta(hours=12)
            )
            points.pop(index)

        def invalidate_revision(points: list[ObservationPoint]) -> None:
            index = next(
                index for index, point in enumerate(points)
                if point.unique_id == "station_total_load"
            )
            points[index] = points[index].model_copy(
                update={"source_revision": "not-an-integer"}
            )

        for label, mutate in (
            ("source_order", swap_first_two),
            ("non_contiguous", remove_middle),
            ("pydantic_invalid", invalidate_revision),
        ):
            with self.subTest(label=label):
                result = station_result(STATION_1)
                mutate(result.actual)
                station = build_dashboard_payload(
                    [result, station_result(STATION_2)],
                    generated_at=GENERATED_AT,
                ).model_dump(mode="python")["data"]["stations"][0]

                load, soc = station["series"]
                self.assertEqual(result.forecasts[0].status, "warming_up")
                self.assertEqual(station["system"]["state"], "degraded")
                self.assertEqual(station["system"]["mode"], "degraded")
                self.assertEqual(load["status"], "error")
                self.assertIsNone(load["model_name"])
                self.assertEqual(load["fallback_reason"], "actual_data_invalid")
                self.assertEqual((load["actual"], load["forecast"]), ([], []))
                self.assertEqual(
                    (soc["status"], len(soc["actual"]), len(soc["forecast"])),
                    ("warming_up", 96, 96),
                )

    def test_two_invalid_actual_series_have_null_latest_and_degraded_aggregate(self):
        result = station_result(STATION_1)
        result.actual[:] = [
            point.model_copy(update={"source_revision": "invalid"})
            for point in result.actual
        ]

        payload = build_dashboard_payload(
            [result, station_result(STATION_2)], generated_at=GENERATED_AT
        ).model_dump(mode="python")["data"]
        station = payload["stations"][0]

        self.assertIsNone(station["range"]["actual_latest"])
        self.assertEqual(station["system"]["state"], "degraded")
        self.assertEqual(station["system"]["mode"], "degraded")
        self.assertEqual(payload["system"]["state"], "degraded")
        self.assertTrue(all(series["status"] == "error" for series in station["series"]))
        self.assertTrue(all(not series["actual"] and not series["forecast"] for series in station["series"]))

    def test_legally_empty_actual_does_not_force_forecast_series_to_error(self):
        result = station_result(STATION_1)
        result.actual.clear()

        station = build_dashboard_payload(
            [result, station_result(STATION_2)], generated_at=GENERATED_AT
        ).model_dump(mode="python")["data"]["stations"][0]

        self.assertIsNone(station["range"]["actual_latest"])
        self.assertEqual(station["system"]["state"], "initializing")
        self.assertEqual(
            [(series["status"], len(series["actual"]), len(series["forecast"])) for series in station["series"]],
            [("warming_up", 0, 96), ("warming_up", 0, 96)],
        )

    def test_invalid_actual_time_field_degrades_only_its_public_series(self):
        invalid_times = (
            STATION_1_AS_OF.replace(tzinfo=None),
            None,
            f"not-a-datetime-{STATION_1.station_id}",
        )
        for invalid_time in invalid_times:
            with self.subTest(invalid_time=invalid_time):
                result = station_result(STATION_1)
                load_index = next(
                    index for index, point in enumerate(result.actual)
                    if point.unique_id == "station_total_load"
                )
                result.actual[load_index] = result.actual[load_index].model_copy(
                    update={"ds": invalid_time}
                )

                envelope = build_dashboard_payload(
                    [result, station_result(STATION_2)],
                    generated_at=GENERATED_AT,
                )
                station = envelope.model_dump(mode="python")["data"]["stations"][0]
                load, soc = station["series"]

                self.assertEqual(result.forecasts[0].status, "warming_up")
                self.assertEqual(station["system"]["state"], "degraded")
                self.assertEqual(
                    (load["status"], load["fallback_reason"], load["actual"], load["forecast"]),
                    ("error", "actual_data_invalid", [], []),
                )
                self.assertEqual(
                    (soc["status"], len(soc["actual"]), len(soc["forecast"])),
                    ("warming_up", 96, 96),
                )
                rendered = envelope.model_dump_json()
                self.assertNotIn(STATION_1.station_id, rendered)
                self.assertNotIn(STATION_2.station_id, rendered)

    def test_startup_succeeds_with_one_station_and_returns_strict_empty_error_peer(self):
        provider, cache, refresher = self.make_refresher()
        provider.fail_station(STATION_1.station_id)
        refresher.start()
        try:
            stations = cache.snapshot(GENERATED_AT).model_dump(mode="python")["data"]["stations"]
        finally:
            refresher.stop()
        error_station = stations[0]
        self.assertEqual(error_station["system"]["state"], "error")
        nullable_range_keys = {
            "history_start", "history_end", "actual_latest", "forecast_start", "forecast_end"
        }
        self.assertTrue(all(
            value is None for key, value in error_station["range"].items()
            if key in nullable_range_keys
        ))
        self.assertTrue(all(item["model_name"] is None for item in error_station["series"]))
        self.assertTrue(all(not item["actual"] and not item["forecast"] for item in error_station["series"]))
        self.assertIsNone(error_station["acceptance"])
        self.assertEqual(stations[1]["system"]["state"], "initializing")

    def test_startup_fails_only_when_both_stations_have_never_succeeded(self):
        provider, _cache, refresher = self.make_refresher()
        provider.failed.update({STATION_1.station_id, STATION_2.station_id})
        with self.assertRaisesRegex(RuntimeError, "initial dashboard refresh failed"):
            refresher.start()
        refresher.stop()

    def test_stale_state_and_aggregate_health_are_recomputed_without_modeling(self):
        provider, cache, refresher = self.make_refresher()
        refresher.refresh_all()
        calls = provider.call_count
        snapshot = cache.snapshot(GENERATED_AT + timedelta(minutes=31)).model_dump(mode="python")["data"]
        self.assertEqual(provider.call_count, calls)
        self.assertEqual([item["system"]["state"] for item in snapshot["stations"]], ["stale", "stale"])
        self.assertEqual(snapshot["system"]["state"], "stale")
        self.assertEqual(snapshot["system"]["healthy_station_count"], 0)

    def test_refresh_log_does_not_include_full_identity_or_exception_text(self):
        provider, _cache, refresher = self.make_refresher()
        provider.fail_station(STATION_1.station_id)
        with self.assertLogs("m3.worker.live_dashboard", level="ERROR") as captured:
            refresher.refresh_all()
        rendered = "\n".join(captured.output)
        self.assertIn("station_key=station_1", rendered)
        self.assertNotIn(STATION_1.station_id, rendered)
        self.assertNotIn("should-not-log", rendered)

    def test_refresh_log_sanitizes_an_unsafe_m3_error_code(self):
        normal = ControlledProvider()

        def build_station(binding: StationBinding):
            if binding.station_key == "station_1":
                raise M3Error(f"failure_{binding.station_id}", "unsafe message")
            return normal.build_station(binding)

        cache = DashboardCache(STATIONS)
        refresher = PeriodicDashboardRefresher(
            SimpleNamespace(build_station=build_station),
            cache,
            STATIONS,
            interval_seconds=3600,
        )
        with self.assertLogs("m3.worker.live_dashboard", level="ERROR") as captured:
            refresher.refresh_all()
        rendered = "\n".join(captured.output)
        self.assertNotIn(STATION_1.station_id, rendered)
        self.assertIn("error_code=unexpected_error", rendered)

    def test_refresh_log_uses_fixed_error_type_for_dynamic_exception_class(self):
        unsafe_error_type = type(STATION_1.station_id, (RuntimeError,), {})
        normal = ControlledProvider()

        def build_station(binding: StationBinding):
            if binding.station_key == "station_1":
                raise unsafe_error_type("unsafe")
            return normal.build_station(binding)

        refresher = PeriodicDashboardRefresher(
            SimpleNamespace(build_station=build_station),
            DashboardCache(STATIONS),
            STATIONS,
            interval_seconds=3600,
        )
        with self.assertLogs("m3.worker.live_dashboard", level="ERROR") as captured:
            refresher.refresh_all()
        rendered = "\n".join(captured.output)
        self.assertNotIn(STATION_1.station_id, rendered)
        self.assertIn("error_type=unexpected_error", rendered)

    def test_stop_waits_without_timeout_until_refresh_thread_has_exited(self):
        _provider, _cache, refresher = self.make_refresher()

        class SlowThread:
            def __init__(self) -> None:
                self.alive = True
                self.join_timeout = object()

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout=None) -> None:
                self.join_timeout = timeout
                if timeout is None:
                    self.alive = False

        thread = SlowThread()
        refresher._thread = thread
        refresher._state = "running"

        refresher.stop()

        self.assertIsNone(thread.join_timeout)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(refresher._thread)

    def test_thread_start_failure_clears_state_and_allows_retry(self):
        provider, cache, refresher = self.make_refresher()

        class FailingThread:
            def __init__(self, **_kwargs) -> None:
                self.joined = False

            def is_alive(self) -> bool:
                return False

            def start(self) -> None:
                raise RuntimeError("thread start failed")

            def join(self, timeout=None) -> None:
                self.joined = True

        with patch("m3.worker.services.live_dashboard_service.Thread", FailingThread):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                refresher.start()
            self.assertIsNone(refresher._thread)
            refresher.stop()

        retry = PeriodicDashboardRefresher(
            provider, cache, STATIONS, interval_seconds=3600
        )
        retry.start()
        retry.stop()
        self.assertIsNone(retry._thread)

    def test_worker_can_self_stop_without_joining_itself(self):
        provider = ControlledProvider()
        cache = DashboardCache(STATIONS)
        enabled = Event()
        returned = Event()
        holder = SimpleNamespace(refresher=None)

        def build_station(binding: StationBinding):
            if enabled.is_set() and binding.station_key == "station_1":
                holder.refresher.stop()
                returned.set()
            return provider.build_station(binding)

        refresher = PeriodicDashboardRefresher(
            SimpleNamespace(build_station=build_station),
            cache,
            STATIONS,
            interval_seconds=0.01,
        )
        holder.refresher = refresher
        refresher.start()
        try:
            enabled.set()
            self.assertTrue(returned.wait(1), "worker self-stop did not return")
        finally:
            enabled.clear()
            refresher.stop()
        self.assertIsNone(refresher._thread)

    def test_start_is_rejected_while_stop_waits_for_active_refresh(self):
        provider = ControlledProvider()
        cache = DashboardCache(STATIONS)
        block = Event()
        entered = Event()
        release = Event()

        def build_station(binding: StationBinding):
            if block.is_set() and binding.station_key == "station_1":
                entered.set()
                release.wait(2)
            return provider.build_station(binding)

        refresher = PeriodicDashboardRefresher(
            SimpleNamespace(build_station=build_station),
            cache,
            STATIONS,
            interval_seconds=0.01,
        )
        refresher.start()
        block.set()
        self.assertTrue(entered.wait(1), "background refresh did not enter")
        stopper = TestThread(target=refresher.stop)
        stopper.start()
        deadline = time.monotonic() + 1
        while not refresher._stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        try:
            with self.assertRaisesRegex(RuntimeError, "stopping"):
                refresher.start()
            self.assertTrue(stopper.is_alive())
        finally:
            release.set()
            stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(refresher._thread)

    def test_repeated_start_and_stop_are_idempotent(self):
        _provider, _cache, refresher = self.make_refresher()
        refresher.start()
        first_thread = refresher._thread
        refresher.start()
        self.assertIs(refresher._thread, first_thread)
        refresher.stop()
        refresher.stop()
        self.assertIsNone(refresher._thread)


class DashboardRoutesAndLifecycleTests(unittest.TestCase):
    def test_two_gets_read_cache_without_additional_provider_calls(self):
        provider = ControlledProvider()
        cache = DashboardCache(STATIONS)
        refresher = PeriodicDashboardRefresher(provider, cache, STATIONS, interval_seconds=3600)
        resources = LiveDashboardResources(
            cache, refresher, SimpleNamespace(close=lambda: None), lambda: GENERATED_AT
        )
        with TemporaryDirectory() as directory:
            html_path = Path(directory) / "dashboard.html"
            html_path.write_text('<script>fetch("/energy-forecast-api")</script>', encoding="utf-8")
            app = create_live_dashboard_app(lambda: resources, html_path)
            with TestClient(app) as client:
                startup_calls = provider.call_count
                first = client.get("/energy-forecast-api")
                second = client.get("/energy-forecast-api")
                page = client.get("/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(provider.call_count, startup_calls)
        self.assertEqual(first.json()["data"]["stations"][0]["station_key"], "station_1")
        self.assertIn("/energy-forecast-api", page.text)

    def test_lifespan_stops_refresher_before_closing_owned_http_client(self):
        events: list[str] = []
        refresher = SimpleNamespace(
            start=lambda: events.append("start"), stop=lambda: events.append("stop")
        )
        http = SimpleNamespace(close=lambda: events.append("close"))
        resources = LiveDashboardResources(
            DashboardCache(STATIONS), refresher, http, lambda: GENERATED_AT
        )
        with TemporaryDirectory() as directory:
            html_path = Path(directory) / "dashboard.html"
            html_path.write_text("dashboard", encoding="utf-8")
            app = create_live_dashboard_app(lambda: resources, html_path)
            with TestClient(app):
                self.assertEqual(events, ["start"])
        self.assertEqual(events, ["start", "stop", "close"])
        self.assertEqual(route_paths(app), ["/", "/energy-forecast-api"])
        self.assertIsNone(app.docs_url)
        self.assertIsNone(app.openapi_url)

    def test_default_app_import_does_not_read_credentials_or_open_http(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("credential read")), patch.object(
            httpx, "Client", side_effect=AssertionError("HTTP opened")
        ):
            module = importlib.reload(importlib.import_module("m3.worker.live_dashboard_app"))
        self.assertEqual(module.app.title, "VIFA M3 Live Dashboard")
        self.assertEqual(route_paths(module.app), ["/", "/energy-forecast-api"])

    def test_resource_builder_uses_exact_configured_station_ids_only_inside_factory(self):
        from m3.worker import live_dashboard_app

        with TemporaryDirectory() as directory:
            secret = Path(directory) / "source-token.txt"
            secret.write_text("source-test-token\n", encoding="utf-8")
            environment = {
                "M3_STATIONS_JSON": json.dumps([
                    {"station_id": STATION_1.station_id, "station_key": "station_1", "station_name": "1# 电站"},
                    {"station_id": STATION_2.station_id, "station_key": "station_2", "station_name": "2# 电站"},
                ]),
                "M3_LIVE_SECRET_FILE": str(secret),
                "M3_LIVE_SOURCE_URL": "https://source.example/api/t_es_data:list",
                "M3_LIVE_HISTORY_DAYS": "8",
                "M3_LIVE_REFRESH_SECONDS": "60",
            }
            with patch.dict(os.environ, environment, clear=True):
                resources = live_dashboard_app.build_live_dashboard_resources_from_env()
        try:
            self.assertEqual(resources.cache.station_keys, ("station_1", "station_2"))
        finally:
            resources.refresher.stop()
            resources.source_http.close()


if __name__ == "__main__":
    unittest.main()
