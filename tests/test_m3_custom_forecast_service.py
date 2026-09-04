"""Custom forecast persistence at the synchronous execution boundary."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import pandas as pd

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastPoint,
    CustomForecastSeries,
    CustomObservationPoint,
    CustomRunRecord,
)
from m3_worker.domain.custom_forecasting import CustomChampion
from m3_worker.domain.custom_load_profiles import (
    LoadCandidateScore,
    weekly_profile_values as real_weekly_profile_values,
)
from m3_worker.domain.custom_training_data import CustomWeekSummary
from m3_worker.domain.custom_training_data import CustomTrainingDataset
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)
from m3_worker.services.custom_forecast_service import CustomForecastService


HISTORY_START = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
HISTORY_END = HISTORY_START + timedelta(days=7)
NOW = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_run(
    *,
    history_days: int = 7,
    run_id: str = "weekly-evidence-run",
    interval_seconds: int = 3600,
) -> StoredCustomRun:
    history_start = HISTORY_END - timedelta(days=history_days)
    points_per_day = 86_400 // interval_seconds
    config = CustomForecastConfig(
        history_start=history_start,
        history_end=HISTORY_END,
        history_days=history_days,
        forecast_start=HISTORY_END,
        forecast_end=HISTORY_END + timedelta(days=1),
        forecast_days=1,
        interval_seconds=interval_seconds,
        points_per_day=points_per_day,
        expected_points_per_series=points_per_day,
        model_policy=(
            "seasonal_naive_only" if history_days < 28 else "full_selection"
        ),
    )
    return StoredCustomRun(
        record_id=1,
        record=CustomRunRecord(
            run_id=run_id,
            station_id="ES01",
            idempotency_key=f"{run_id}-key",
            config=config,
            status="queued",
        ),
        model_manifest=None,
        source_manifest=None,
        content_hash=None,
        started_at=None,
        completed_at=None,
        evaluated_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def make_observations(
    *,
    history_days: int = 7,
    load_week_values: list[float] | None = None,
    missing_load_index: int | None = None,
    missing_load_indices: set[int] | None = None,
) -> list[CustomObservationPoint]:
    history_start = HISTORY_END - timedelta(days=history_days)
    if load_week_values is not None and len(load_week_values) * 7 != history_days:
        raise ValueError("load_week_values must cover complete history weeks")
    points: list[CustomObservationPoint] = []
    missing_indices = set(missing_load_indices or ())
    if missing_load_index is not None:
        missing_indices.add(missing_load_index)
    for index in range(history_days * 24):
        timestamp = history_start + timedelta(hours=index)
        if index not in missing_indices:
            load_value = (
                load_week_values[index // (7 * 24)]
                if load_week_values is not None
                else 800.0 + index
            )
            points.append(
                CustomObservationPoint(
                    unique_id="station_total_load",
                    ds=timestamp,
                    y=load_value,
                    quality="valid",
                    source_state="valid",
                    source_revision=1,
                )
            )
        points.append(
            CustomObservationPoint(
                unique_id="storage_soc",
                ds=timestamp,
                y=55.0,
                quality="valid",
                source_state="valid",
                source_revision=1,
            )
        )
    return points


class InMemorySource:
    def __init__(self, observations: list[CustomObservationPoint]) -> None:
        self._observations = observations

    def list_custom_observations(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        *,
        interval_seconds: int,
    ) -> list[CustomObservationPoint]:
        if station_id != "ES01" or interval_seconds != 3600:
            raise AssertionError("unexpected custom source request")
        return [
            point for point in self._observations if start <= point.ds < end
        ]


class RecordingMultiIntervalSource:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def list_custom_observations(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        *,
        interval_seconds: int,
    ) -> list[CustomObservationPoint]:
        if station_id != "ES01":
            raise AssertionError("unexpected station")
        self.calls.append(interval_seconds)
        return [
            CustomObservationPoint(
                unique_id=unique_id,
                ds=start,
                y=value,
                quality="valid",
                source_state="valid",
                source_revision=1,
            )
            for unique_id, value in (
                ("station_total_load", 500.0),
                ("storage_soc", 10.0),
            )
        ]


def stub_dataset(unique_id: str, config: CustomForecastConfig) -> CustomTrainingDataset:
    timestamp = config.history_end - config.interval
    frame = pd.DataFrame(
        {"unique_id": [unique_id], "ds": [timestamp], "y": [10.0]}
    )
    return CustomTrainingDataset(
        frame=frame,
        imputed_keys=frozenset(),
        source_available_start=timestamp,
        source_available_points=1,
        leading_no_data_points=0,
        invalid_points=0,
        negative_invalid_points=0,
        start=timestamp,
        end=timestamp,
        mode="full",
        interval_seconds=config.interval_seconds,
        points_per_day=config.points_per_day,
    )


def stub_champion(dataset: CustomTrainingDataset, config: CustomForecastConfig) -> CustomChampion:
    unique_id = dataset.frame["unique_id"].iloc[0]
    return CustomChampion(
        model_name=("WeeklyNaive" if unique_id == "station_total_load" else "SOCWeeklyDelta"),
        cv_mape_percent=0.0,
        selected_at=NOW,
        training_start=dataset.start,
        training_end=dataset.end,
        statsforecast_version="test",
    )


def stub_forecast(
    dataset: CustomTrainingDataset,
    champion: CustomChampion,
    config: CustomForecastConfig,
) -> CustomForecastSeries:
    unique_id = dataset.frame["unique_id"].iloc[0]
    values = (
        [500.0] * config.expected_points_per_series
        if unique_id == "station_total_load"
        else [10.0 + index / 100 for index in range(config.expected_points_per_series)]
    )
    return CustomForecastSeries(
        unique_id=unique_id,
        unit="kW" if unique_id == "station_total_load" else "%",
        model_name=champion.model_name,
        status="ok",
        points=[
            CustomForecastPoint(
                target_time=config.forecast_start + index * config.interval,
                horizon_step=index + 1,
                raw_forecast=value,
                forecast_value=value,
                is_clipped=False,
            )
            for index, value in enumerate(values)
        ],
        fallback_reason=None,
    )


class InMemoryRepository:
    def __init__(self, run: StoredCustomRun) -> None:
        self.run = run
        self.captured_baselines = {}
        self.captured_series = {}
        self.latest_query = None

    def get_by_run_id(self, run_id: str) -> StoredCustomRun | None:
        return self.run if run_id == self.run.run_id else None

    def latest_usable(self, station_id: str, **query) -> StoredCustomRun | None:
        self.latest_query = (station_id, query)
        return self.run

    def transition(self, run: StoredCustomRun, status: str, *, at: datetime, **values):
        record = run.record.model_copy(
            update={"status": status, "error_code": values.get("error_code")}
        )
        self.run = replace(
            run,
            record=record,
            model_manifest=values.get("model_manifest", run.model_manifest),
            source_manifest=values.get("source_manifest", run.source_manifest),
            content_hash=values.get("content_hash", run.content_hash),
            started_at=at if status == "running" else run.started_at,
            completed_at=at if status in {"succeeded", "failed"} else run.completed_at,
            updated_at=at,
        )
        return self.run

    def store_points(self, run, series, baseline_series) -> str:
        CustomForecastRepository._point_values(run, series, baseline_series)
        self.captured_series = {item.unique_id: item for item in series}
        self.captured_baselines = {
            item.unique_id: item for item in baseline_series
        }
        return "a" * 64


class RecoveryRepository:
    def __init__(self, runs: list[StoredCustomRun]) -> None:
        self.runs = runs
        self.transitions: list[tuple[str, str, datetime, str | None]] = []

    def list_recoverable(self) -> list[StoredCustomRun]:
        return self.runs

    def transition(
        self,
        run: StoredCustomRun,
        status: str,
        *,
        at: datetime,
        error_code: str | None = None,
    ) -> StoredCustomRun:
        self.transitions.append((run.run_id, status, at, error_code))
        return replace(
            run,
            record=run.record.model_copy(
                update={"status": status, "error_code": error_code}
            ),
            completed_at=at,
            updated_at=at,
        )


class CustomForecastServiceTests(unittest.TestCase):
    def test_recover_fails_orphaned_running_run_and_schedules_queued_run(self):
        queued = make_run(run_id="queued-run")
        running = make_run(run_id="running-run")
        running = replace(
            running,
            record=running.record.model_copy(update={"status": "running"}),
            started_at=NOW - timedelta(minutes=5),
        )
        repository = RecoveryRepository([queued, running])
        service = CustomForecastService(
            repository,
            InMemorySource(make_observations()),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        scheduled: list[StoredCustomRun] = []

        with patch.object(service, "_schedule", side_effect=scheduled.append):
            recovered = service.recover()

        self.assertEqual(recovered, 2)
        self.assertEqual([run.run_id for run in scheduled], ["queued-run"])
        self.assertEqual(
            repository.transitions,
            [("running-run", "failed", NOW, "worker_interrupted")],
        )

    def test_recover_fails_running_runs_before_queued_scheduling_can_fail(self):
        first_queued = make_run(run_id="first-queued-run")
        second_queued = make_run(run_id="second-queued-run")
        running = make_run(run_id="running-run")
        running = replace(
            running,
            record=running.record.model_copy(update={"status": "running"}),
            started_at=NOW - timedelta(minutes=5),
        )
        repository = RecoveryRepository([first_queued, second_queued, running])
        service = CustomForecastService(
            repository,
            InMemorySource(make_observations()),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        scheduled: list[str] = []

        def schedule(run: StoredCustomRun) -> None:
            scheduled.append(run.run_id)
            if run.run_id == "second-queued-run":
                raise RuntimeError("queue capacity unavailable")

        with (
            patch.object(service, "_schedule", side_effect=schedule),
            self.assertRaisesRegex(RuntimeError, "queue capacity unavailable"),
        ):
            service.recover()

        self.assertEqual(
            repository.transitions,
            [("running-run", "failed", NOW, "worker_interrupted")],
        )
        self.assertEqual(scheduled, ["first-queued-run", "second-queued-run"])

    def test_one_minute_run_models_soc_on_five_minutes_then_interpolates_output(self):
        """One-minute output must not select SOC models on sparse minute buckets."""
        run = make_run(
            history_days=28,
            run_id="one-minute-soc-run",
            interval_seconds=60,
        )
        repository = InMemoryRepository(run)
        source = RecordingMultiIntervalSource()
        service = CustomForecastService(
            repository,
            source,
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        with (
            patch(
                "m3_worker.services.custom_forecast_service.build_custom_training_dataset",
                side_effect=lambda _points, unique_id, config: stub_dataset(
                    unique_id, config
                ),
            ),
            patch(
                "m3_worker.services.custom_forecast_service.select_custom_champion",
                side_effect=stub_champion,
            ),
            patch(
                "m3_worker.services.custom_forecast_service.forecast_custom_series",
                side_effect=stub_forecast,
            ),
        ):
            service._execute(run.run_id)

        self.assertEqual(repository.run.status, "succeeded")
        self.assertEqual(source.calls.count(60), 4)
        self.assertEqual(source.calls.count(300), 4)
        soc = repository.captured_series["storage_soc"]
        self.assertEqual(soc.model_name, "SOCWeeklyDelta5mLinear")
        self.assertEqual(len(soc.points), 1440)
        self.assertEqual(
            [soc.points[index].forecast_value for index in range(0, 30, 5)],
            [10.0, 10.01, 10.02, 10.03, 10.04, 10.05],
        )
        self.assertEqual(
            repository.captured_baselines["storage_soc"].model_name,
            "SeasonalNaive5mLinear",
        )
        soc_model = repository.run.model_manifest["series"]["storage_soc"]
        self.assertEqual(soc_model["model_interval_seconds"], 300)
        self.assertEqual(soc_model["output_interval_seconds"], 60)
        self.assertEqual(soc_model["output_interpolation"], "linear")
        self.assertEqual(
            repository.run.source_manifest["series"]["storage_soc"][
                "interval_seconds"
            ],
            300,
        )

    def test_latest_uses_station_output_configuration_and_current_policy(self):
        """Cross-browser lookup must not depend on the task's training window."""
        repository = InMemoryRepository(make_run())
        service = CustomForecastService(
            repository,
            InMemorySource(make_observations()),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)

        run = service.latest("ES01", interval_seconds=3600, forecast_days=1)

        self.assertIs(run, repository.run)
        self.assertEqual(
            repository.latest_query,
            (
                "ES01",
                {
                    "interval_seconds": 3600,
                    "forecast_days": 1,
                    "selection_policy": "weekly_load_v2",
                },
            ),
        )

    def test_evidence_serializers_keep_only_safe_candidate_values(self):
        """Non-finite metrics and arbitrary skip text must not enter run JSON."""
        score = LoadCandidateScore(
            "AutoARIMA", float("nan"), float("inf"), 12.5, 3, "RuntimeError"
        )
        week = CustomWeekSummary(
            start=HISTORY_START,
            end=HISTORY_END,
            point_count=168,
            real_point_count=167,
            imputed_point_count=1,
            imputation_ratio=1 / 168,
        )

        self.assertEqual(
            CustomForecastService._candidate_score_manifest(score),
            {
                "model_name": "AutoARIMA",
                "wape_percent": None,
                "mae": None,
                "mape_percent": 12.5,
                "scorable_point_count": 3,
                "skip_reason": "RuntimeError",
            },
        )
        self.assertEqual(
            CustomForecastService._candidate_score_manifest(
                replace(score, skip_reason="RuntimeError: secret detail")
            )["skip_reason"],
            "unknown",
        )
        self.assertEqual(
            CustomForecastService._candidate_score_manifest(
                replace(score, skip_reason=[])
            )["skip_reason"],
            "unknown",
        )
        self.assertEqual(
            CustomForecastService._candidate_score_manifest(
                replace(score, skip_reason="")
            )["skip_reason"],
            "unknown",
        )
        self.assertEqual(
            CustomForecastService._week_manifest(week),
            {
                "start": "2026-08-24T00:00:00+08:00",
                "end": "2026-08-31T00:00:00+08:00",
                "point_count": 168,
                "real_point_count": 167,
                "imputed_point_count": 1,
                "imputation_ratio": 1 / 168,
            },
        )

    def test_execute_persists_weekly_load_evidence_and_weekly_baseline(self):
        """A daily load baseline or missing weekly evidence must fail this contract."""
        repository = InMemoryRepository(make_run())
        service = CustomForecastService(
            repository,
            InMemorySource(make_observations()),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        with self.assertLogs("m3_worker.custom_forecast", level="INFO") as logs:
            service._execute("weekly-evidence-run")

        run = repository.run
        self.assertEqual(run.status, "succeeded")
        for unique_id in ("station_total_load", "storage_soc"):
            self.assertTrue(
                any(
                    "m3_custom_forecast_selection_started "
                    f"run_id=weekly-evidence-run station_id=ES01 series={unique_id}"
                    in message
                    for message in logs.output
                )
            )
            self.assertTrue(
                any(
                    "m3_custom_forecast_selection_finished "
                    f"run_id=weekly-evidence-run station_id=ES01 series={unique_id}"
                    in message
                    for message in logs.output
                )
            )
        self.assertEqual(run.model_manifest["selection_policy"], "weekly_load_v2")
        self.assertEqual(
            run.model_manifest["series"]["station_total_load"]["model_name"],
            "WeeklyNaive",
        )
        self.assertEqual(
            set(run.model_manifest["series"]["station_total_load"]),
            {
                "model_name",
                "selection_metric",
                "selection_reason",
                "selection_status",
                "candidate_scores",
                "realized_model_name",
                "realized_status",
                "realized_fallback_reason",
                "training_start",
                "training_end",
                "statsforecast_version",
            },
        )
        self.assertEqual(
            run.model_manifest["series"]["station_total_load"]["candidate_scores"],
            [],
        )
        self.assertEqual(
            set(run.model_manifest["series"]["storage_soc"]),
            {
                "model_name",
                "realized_model_name",
                "model_interval_seconds",
                "output_interval_seconds",
                "output_interpolation",
                "cv_mape_percent",
                "selected_at",
                "selection_metric",
                "selection_reason",
                "candidate_scores",
                "training_start",
                "training_end",
                "statsforecast_version",
            },
        )
        self.assertIsNone(
            run.model_manifest["series"]["storage_soc"]["selection_metric"]
        )
        self.assertEqual(
            run.model_manifest["series"]["storage_soc"]["candidate_scores"], []
        )
        self.assertEqual(
            run.source_manifest["series"]["station_total_load"]["usable_week_count"],
            1,
        )
        self.assertEqual(
            run.source_manifest["series"]["station_total_load"],
            {
                "interval_seconds": 3600,
                "source_available_start": "2026-08-24T00:00:00+08:00",
                "training_start": "2026-08-24T00:00:00+08:00",
                "source_available_points": 168,
                "leading_no_data_points": 0,
                "invalid_points": 0,
                "negative_invalid_points": 0,
                "retained_points": 168,
                "imputed_points": 0,
                "mode": "warming_up",
                "usable_week_count": 1,
                "weeks": [
                    {
                        "start": "2026-08-24T00:00:00+08:00",
                        "end": "2026-08-31T00:00:00+08:00",
                        "point_count": 168,
                        "real_point_count": 168,
                        "imputed_point_count": 0,
                        "imputation_ratio": 0.0,
                    }
                ],
            },
        )
        self.assertEqual(
            run.source_manifest["series"]["station_total_load"]["weeks"],
            [
                {
                    "start": "2026-08-24T00:00:00+08:00",
                    "end": "2026-08-31T00:00:00+08:00",
                    "point_count": 168,
                    "real_point_count": 168,
                    "imputed_point_count": 0,
                    "imputation_ratio": 0.0,
                }
            ],
        )
        self.assertEqual(
            repository.captured_baselines["station_total_load"].model_name,
            "WeeklyNaive",
        )
        self.assertEqual(
            repository.captured_baselines["storage_soc"].model_name,
            "SeasonalNaive",
        )

    def test_execute_accepts_one_imputation_in_minimum_weekly_load_baseline(self):
        """The baseline must share the primary load path's usable-week warming status."""
        run = make_run(run_id="minimum-week-run")
        repository = InMemoryRepository(run)
        service = CustomForecastService(
            repository,
            InMemorySource(make_observations(missing_load_index=72)),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        service._execute(run.run_id)

        self.assertEqual(repository.run.status, "succeeded")
        self.assertEqual(
            repository.run.source_manifest["series"]["station_total_load"][
                "usable_week_count"
            ],
            1,
        )
        self.assertEqual(
            repository.run.source_manifest["series"]["station_total_load"]["mode"],
            "insufficient",
        )
        baseline = repository.captured_baselines["station_total_load"]
        self.assertEqual(baseline.model_name, "WeeklyNaive")
        self.assertEqual(baseline.status, "warming_up")
        self.assertEqual(len(baseline.points), 24)

    def test_execute_persists_degraded_weekly_naive_when_latest_week_is_reconstructed(self):
        """A complete reconstructed week must forecast instead of failing at 5%."""
        run = make_run(run_id="degraded-reconstructed-week-run")
        repository = InMemoryRepository(run)
        service = CustomForecastService(
            repository,
            InMemorySource(
                make_observations(
                    missing_load_indices={24, 37, 50, 63, 76, 89, 102, 115, 128}
                )
            ),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        service._execute(run.run_id)

        self.assertEqual(repository.run.status, "succeeded")
        load_manifest = repository.run.model_manifest["series"]["station_total_load"]
        self.assertEqual(load_manifest["model_name"], "WeeklyNaive")
        self.assertEqual(
            load_manifest["selection_reason"], "latest_week_high_imputation"
        )
        self.assertEqual(load_manifest["selection_status"], "degraded")
        self.assertEqual(load_manifest["realized_status"], "degraded")
        self.assertEqual(
            load_manifest["realized_fallback_reason"],
            "latest_week_high_imputation",
        )
        self.assertEqual(
            repository.run.source_manifest["series"]["station_total_load"][
                "usable_week_count"
            ],
            0,
        )
        self.assertEqual(
            repository.captured_baselines["station_total_load"].status,
            "degraded",
        )

    def test_execute_persists_selected_and_realized_load_fallback_evidence(self):
        """Selection evidence must not overwrite the model that produced stored points."""
        run = make_run(history_days=21, run_id="realized-fallback-run")
        repository = InMemoryRepository(run)

        def fail_only_selected_final_forecast(dataset, model_name, *, origin, periods):
            if model_name == "WeeklyWeighted2" and origin == run.config.forecast_start:
                raise RuntimeError("database password must stay private")
            return real_weekly_profile_values(
                dataset, model_name, origin=origin, periods=periods
            )

        service = CustomForecastService(
            repository,
            InMemorySource(
                make_observations(
                    history_days=21, load_week_values=[50.0, 80.0, 70.0]
                )
            ),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        with patch(
            "m3_worker.domain.custom_forecasting.weekly_profile_values",
            side_effect=fail_only_selected_final_forecast,
        ):
            service._execute(run.run_id)

        self.assertEqual(repository.run.status, "succeeded")
        load_manifest = repository.run.model_manifest["series"]["station_total_load"]
        self.assertEqual(load_manifest["model_name"], "WeeklyWeighted2")
        self.assertEqual(load_manifest["realized_model_name"], "WeeklyNaive")
        self.assertEqual(load_manifest["realized_status"], "degraded")
        self.assertEqual(load_manifest["realized_fallback_reason"], "RuntimeError")
        self.assertNotIn("password", str(load_manifest))
        realized = repository.captured_series["station_total_load"]
        self.assertEqual(realized.model_name, "WeeklyNaive")
        self.assertEqual(realized.status, "degraded")
        self.assertEqual(realized.fallback_reason, "RuntimeError")

    def test_execute_persists_mae_when_holdout_wape_is_unavailable(self):
        """A zero-load holdout is a scored MAE selection, not a skipped candidate set."""
        run = make_run(history_days=21, run_id="mae-fallback-run")
        repository = InMemoryRepository(run)
        service = CustomForecastService(
            repository,
            InMemorySource(
                make_observations(
                    history_days=21, load_week_values=[10.0, 20.0, 0.0]
                )
            ),
            now=lambda: NOW,
            station_ids=("ES01",),
        )
        self.addCleanup(service.close)
        self.assertTrue(service._capacity.acquire(blocking=False))

        service._execute(run.run_id)

        self.assertEqual(repository.run.status, "succeeded")
        load_manifest = repository.run.model_manifest["series"]["station_total_load"]
        self.assertEqual(load_manifest["selection_metric"], "mae")
        self.assertEqual(load_manifest["selection_reason"], "wape_unavailable")
        self.assertTrue(load_manifest["candidate_scores"])
        self.assertTrue(
            all(score["mae"] is not None for score in load_manifest["candidate_scores"])
        )
        self.assertTrue(
            all(
                score["skip_reason"] is None
                for score in load_manifest["candidate_scores"]
            )
        )


if __name__ == "__main__":
    unittest.main()
