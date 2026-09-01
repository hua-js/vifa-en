"""Custom forecast persistence at the synchronous execution boundary."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomObservationPoint,
    CustomRunRecord,
)
from m3_worker.domain.custom_load_profiles import (
    LoadCandidateScore,
    weekly_profile_values as real_weekly_profile_values,
)
from m3_worker.domain.custom_training_data import CustomWeekSummary
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)
from m3_worker.services.custom_forecast_service import CustomForecastService


HISTORY_START = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
HISTORY_END = HISTORY_START + timedelta(days=7)
NOW = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_run(
    *, history_days: int = 7, run_id: str = "weekly-evidence-run"
) -> StoredCustomRun:
    history_start = HISTORY_END - timedelta(days=history_days)
    config = CustomForecastConfig(
        history_start=history_start,
        history_end=HISTORY_END,
        history_days=history_days,
        forecast_start=HISTORY_END,
        forecast_end=HISTORY_END + timedelta(days=1),
        forecast_days=1,
        interval_seconds=3600,
        points_per_day=24,
        expected_points_per_series=24,
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


class InMemoryRepository:
    def __init__(self, run: StoredCustomRun) -> None:
        self.run = run
        self.captured_baselines = {}
        self.captured_series = {}

    def get_by_run_id(self, run_id: str) -> StoredCustomRun | None:
        return self.run if run_id == self.run.run_id else None

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


class CustomForecastServiceTests(unittest.TestCase):
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
