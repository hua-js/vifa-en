"""Recover station 1 forecasts from real schedule-matched days, not fake history."""
from datetime import datetime, timedelta
from dataclasses import replace
import unittest

import pandas as pd

from m3.worker.custom_forecast_contracts import (
    CustomForecastRequest, CustomObservationPoint, build_custom_forecast_config,
)
from m3.worker.domain.custom_training_data import build_custom_training_dataset
from m3.worker.domain.custom_forecasting import select_custom_champion, forecast_custom_series
from m3.worker.domain.production_schedule import ProductionSchedule, use_schedule
from m3.worker.domain.load_history import recent_schedule_load_reference
from m3.worker.domain.training_data import build_training_dataset
from m3.worker.domain.forecasting import forecast_one, select_champion
from m3.worker.contracts import ObservationPoint
from m3.worker.errors import M3Error


ORIGIN = datetime.fromisoformat("2026-10-22T00:00:00+08:00")
START = ORIGIN - timedelta(days=19)
GRID = timedelta(minutes=15)
LOAD = "station_total_load"


def calendar(*, station="station_1", statuses=None, overtime=None):
    days = {
        (START + timedelta(days=i)).date(): (
            "REST" if (START + timedelta(days=i)).weekday() == 6 else "WORK"
        )
        for i in range(-1, 27)
    }
    days.update(statuses or {})
    return ProductionSchedule(
        "ES01" if station == "station_1" else "ES02", station,
        "86" if station == "station_1" else "113", (START - timedelta(days=1)).date(),
        (ORIGIN + timedelta(days=7)).date(), ORIGIN.date(), days, overtime or {}, "{}",
    )


def observations():
    result = []
    for timestamp in pd.date_range(START, end=ORIGIN - GRID, freq=GRID):
        complete_day = timestamp.date() in {
            (ORIGIN - timedelta(days=1)).date(), (ORIGIN - timedelta(days=2)).date(),
        }
        valid = complete_day or not 6 <= timestamp.hour < 18
        value = (100.0 if timestamp.day == 20 else 300.0) + timestamp.hour
        result.append(CustomObservationPoint(
            unique_id=LOAD, ds=timestamp.to_pydatetime(), y=value if valid else None,
            quality="valid" if valid else "invalid",
            source_state="valid" if valid else "negative", source_revision=0,
        ))
    return result


def config(*, days=1):
    return build_custom_forecast_config(CustomForecastRequest(
        history_start=START, history_end=ORIGIN, forecast_days=days,
        interval_seconds=900, idempotency_key="recent-schedule-unit",
    ))


class RecentScheduleLoadTests(unittest.TestCase):
    def reference(self, *, values=None, schedule=None, origin=ORIGIN, periods=96):
        if values is None:
            values = pd.Series({p.ds: p.y for p in observations()}, dtype=float)
        with use_schedule(schedule or calendar()):
            return recent_schedule_load_reference(
                values, history_end=origin, origin=origin, interval=GRID, periods=periods,
            )

    def test_custom_forecast_recovers_without_changing_training_or_observations(self):
        points = observations()
        before = [point.model_dump() for point in points]
        with use_schedule(calendar()):
            dataset = build_custom_training_dataset(points, LOAD, config())
            self.assertIsNotNone(getattr(dataset, "recent_schedule_reference", None))
            self.assertEqual(dataset.mode, "insufficient")
            self.assertEqual(len(dataset.usable_weeks), 0)
            champion = select_custom_champion(dataset, config())
            result = forecast_custom_series(dataset, champion, config())
        self.assertEqual(result.model_name, "RecentScheduleDayReference")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.fallback_reason, "recent_schedule_day_reference")
        self.assertEqual(len(result.points), 96)
        self.assertEqual(result.points[0].forecast_value, 200.0)
        self.assertEqual(result.points[40].forecast_value, 210.0)
        self.assertIsNone(champion.cv_mape_percent)
        self.assertEqual(champion.candidate_scores, ())
        self.assertEqual(before, [point.model_dump() for point in points])

    def test_manifest_reconstructs_only_recent_real_sources(self):
        reference = self.reference()
        reconstructed = []
        for segment in reference.manifest()["segments"]:
            target = datetime.fromisoformat(segment["target_start"])
            sources = [datetime.fromisoformat(value) for value in segment["source_starts"]]
            for step in range(segment["point_count"]):
                reconstructed.append((target + step * GRID, tuple(source + step * GRID for source in sources)))
        self.assertEqual(tuple(reconstructed), reference.donors)
        self.assertEqual(reference.manifest()["policy"], "recent-schedule-real-slots-median-v1")
        self.assertTrue(all(ORIGIN - timedelta(days=7) <= source < ORIGIN
                            for _, sources in reference.donors for source in sources))

    def test_incomplete_real_days_are_not_completed_with_imputed_donors(self):
        points = [p.model_copy(update={"quality": "invalid", "source_state": "negative", "y": None})
                  if p.ds.hour == 10 and p.ds.minute == 0 else p for p in observations()]
        with use_schedule(calendar()):
            dataset = build_custom_training_dataset(points, LOAD, config())
            self.assertGreater(len(dataset.imputed_keys), 0)
            self.assertIsNone(dataset.recent_schedule_reference)
            with self.assertRaises(M3Error):
                select_custom_champion(dataset, config())

    def test_other_station_and_missing_calendar_do_not_enable_new_policy(self):
        values = pd.Series({p.ds: p.y for p in observations()}, dtype=float)
        self.assertIsNone(self.reference(values=values, schedule=calendar(station="station_2")))
        with use_schedule(None):
            self.assertIsNone(recent_schedule_load_reference(
                values, history_end=ORIGIN, origin=ORIGIN, interval=GRID, periods=96,
            ))
        with use_schedule(calendar(station="station_2")):
            dataset = build_custom_training_dataset(observations(), LOAD, config())
            self.assertIsNone(dataset.load_reference)
            with self.assertRaises(M3Error):
                select_custom_champion(dataset, config())

    def test_overtime_mismatch_in_another_slot_rejects_whole_donor_day(self):
        schedule = calendar(
            statuses={ORIGIN.date(): "REST", (ORIGIN - timedelta(days=1)).date(): "REST"},
            overtime={ORIGIN.date(): ("morning",)},
        )
        self.assertIsNone(self.reference(schedule=schedule))

    def test_matching_rest_day_needs_explicit_equal_status_and_shift(self):
        donor = (ORIGIN - timedelta(days=1)).date()
        schedule = calendar(statuses={ORIGIN.date(): "REST", donor: "REST"})
        reference = self.reference(schedule=schedule)
        self.assertIsNotNone(reference)
        self.assertEqual(reference.values[0], 300.0)
        self.assertTrue(all(source.date() == donor for _, sources in reference.donors for source in sources))

    def test_unknown_future_day_and_unmatched_later_day_block_entire_horizon(self):
        schedule = calendar()
        statuses = dict(schedule.statuses)
        statuses.pop((ORIGIN + timedelta(days=1)).date())
        self.assertIsNone(self.reference(schedule=replace(schedule, statuses=statuses), periods=192))
        schedule = calendar(statuses={(ORIGIN + timedelta(days=1)).date(): "REST"})
        self.assertIsNone(self.reference(schedule=schedule, periods=192))

    def test_future_and_stale_complete_days_cannot_supply_the_reference(self):
        values = pd.Series({p.ds: p.y for p in observations()}, dtype=float)
        values.loc[values.index >= ORIGIN - timedelta(days=2)] = float("nan")
        stale = pd.date_range(ORIGIN - timedelta(days=8), periods=96, freq=GRID)
        future = pd.date_range(ORIGIN, periods=96, freq=GRID)
        values.loc[stale] = 900.0
        values = pd.concat([values, pd.Series(999.0, index=future)])
        self.assertIsNone(self.reference(values=values))

    def test_at_most_three_newest_matching_days_are_used(self):
        index = pd.date_range(ORIGIN - timedelta(days=7), end=ORIGIN - GRID, freq=GRID)
        values = pd.Series([float(t.day) for t in index], index=index)
        reference = self.reference(values=values)
        self.assertEqual(reference.values[0], 20.0)
        self.assertEqual(tuple(source.day for source in reference.donors[0][1]), (21, 20, 19))

    def test_regular_forecast_recovers_and_cached_reference_retires_on_recovery(self):
        points = [ObservationPoint(unique_id=LOAD, ds=p.ds, y=p.y, quality=p.quality, source_revision=0)
                  for p in observations()]
        with use_schedule(calendar()):
            dataset = build_training_dataset(points, LOAD, history_days=19)
            champion = select_champion(dataset)
            result = forecast_one(dataset, None, ORIGIN)
            self.assertEqual(result.status, "degraded")
            self.assertEqual(result.model_name, "RecentScheduleDayReference")
            self.assertEqual(result.points[0].forecast_value, 200.0)
            self.assertEqual(len(result.points), 96)
            # The old reference champion cannot persist after real history recovers.
            healthy = [p.model_copy(update={"y": 80.0, "quality": "valid"}) for p in points]
            recovered = build_training_dataset(healthy, LOAD, history_days=19)
            self.assertIsNone(recovered.load_reference)
            result = forecast_one(recovered, champion, ORIGIN)
            self.assertEqual(result.model_name, "SeasonalNaive")
            self.assertEqual(result.status, "warming_up")

    def test_normal_usable_weeks_and_soc_do_not_use_recent_reference(self):
        points = [p.model_copy(update={"y": 80.0, "quality": "valid", "source_state": "valid"})
                  for p in observations()]
        with use_schedule(calendar()):
            dataset = build_custom_training_dataset(points, LOAD, config())
            self.assertGreater(len(dataset.usable_weeks), 0)
            self.assertIsNone(dataset.load_reference)
            soc = [p.model_copy(update={"unique_id": "storage_soc", "y": 60.0}) for p in points]
            self.assertIsNone(build_custom_training_dataset(soc, "storage_soc", config()).load_reference)

    def test_fine_cadences_combine_only_exact_real_slots_and_never_interpolate(self):
        for seconds in [30, 60]:
            with self.subTest(interval=seconds):
                step = timedelta(seconds=seconds)
                index = pd.date_range(ORIGIN - timedelta(days=2), end=ORIGIN - step, freq=step)
                values = pd.Series([100.0 if t.day == 20 else 300.0 for t in index], index=index)
                values.loc[ORIGIN - timedelta(days=2) + timedelta(hours=1)] = float("nan")
                values.loc[ORIGIN - timedelta(days=1) + timedelta(hours=2)] = float("nan")
                before = values.copy()
                with use_schedule(calendar()):
                    ref = recent_schedule_load_reference(values, history_end=ORIGIN, origin=ORIGIN,
                                                         interval=step, periods=86400 // seconds)
                self.assertIsNotNone(ref)
                self.assertEqual(ref.values[3600 // seconds], 300.0)
                self.assertEqual(ref.values[7200 // seconds], 100.0)
                self.assertEqual(ref.values[0], 200.0)
                self.assertEqual(len(ref.donors[3600 // seconds][1]), 1)
                pd.testing.assert_series_equal(values, before)
                # Neither day can donate the same missing target clock time.
                values.loc[ORIGIN - timedelta(days=2) + timedelta(hours=2)] = float("nan")
                with use_schedule(calendar()):
                    self.assertIsNone(recent_schedule_load_reference(
                        values, history_end=ORIGIN, origin=ORIGIN, interval=step, periods=86400 // seconds,
                    ))

    def test_service_saves_degraded_primary_baseline_and_real_donor_manifest(self):
        from types import SimpleNamespace
        from m3.tests.test_m3_custom_forecast_service import make_run, InMemoryRepository
        from m3.worker.services.custom_forecast_service import CustomForecastService

        base = make_run()
        run = replace(base, record=base.record.model_copy(update={"config": config()}))
        repository = InMemoryRepository(run)
        points = observations() + [p.model_copy(update={
            "unique_id": "storage_soc", "y": 60.0, "quality": "valid", "source_state": "valid",
        }) for p in observations()]
        source = SimpleNamespace(list_custom_observations=lambda station, start, end, **kwargs: [
            p for p in points if start <= p.ds < end
        ])
        service = CustomForecastService(
            repository, source, now=lambda: ORIGIN, station_ids=("ES01",),
            schedule_provider=SimpleNamespace(load=lambda *args, **kwargs: calendar()),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))
        service._execute(run.run_id)
        self.assertEqual(repository.run.status, "succeeded", repository.run.record.error_code)
        load = repository.captured_series[LOAD]
        self.assertEqual(load.status, "degraded")
        self.assertEqual(repository.captured_baselines[LOAD], load)
        evidence = repository.run.model_manifest["series"][LOAD]
        self.assertEqual(evidence["realized_fallback_reason"], "recent_schedule_day_reference")
        self.assertEqual(evidence["selection_status"], "degraded")
        self.assertEqual(evidence["candidate_scores"], [])
        manifest = repository.run.source_manifest["series"][LOAD]
        self.assertEqual(manifest["usable_week_count"], 0)
        self.assertIn("recent_schedule_reference", manifest)
        self.assertNotIn("post_holiday_reference", manifest)

    def test_regular_reference_survives_spawn_and_state_response(self):
        from fastapi.testclient import TestClient
        from m3.worker.main import create_app
        from m3.worker.services.compute_process import ForecastComputeProcess
        from m3.tests.test_m3_worker_api import FakeResources, settings, ES01, AUTH

        points = [ObservationPoint(unique_id=LOAD, ds=p.ds, y=p.y, quality=p.quality, source_revision=0)
                  for p in observations()]
        compute = ForecastComputeProcess()
        self.addCleanup(compute.close)
        with use_schedule(calendar()):
            dataset = build_training_dataset(points, LOAD, history_days=19)
            champion = compute.run(select_champion, dataset)
            result = compute.run(forecast_one, dataset, champion, ORIGIN)
        self.assertEqual(result.model_name, "RecentScheduleDayReference")
        resources = FakeResources()
        resources._state.champions[LOAD] = champion
        resources._state.champions["storage_soc"].model_name = "SOCScheduleDelta"
        with TestClient(create_app(settings(), resources, start_scheduler=False)) as client:
            response = client.get(f"/v1/stations/{ES01}/state", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["champions"][LOAD]["selection_reason"],
                         "recent_schedule_day_reference")


if __name__ == "__main__":
    unittest.main()
