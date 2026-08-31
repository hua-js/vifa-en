"""Custom forecast persistence at the synchronous execution boundary."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomObservationPoint,
    CustomRunRecord,
)
from m3_worker.domain.custom_load_profiles import LoadCandidateScore
from m3_worker.domain.custom_training_data import CustomWeekSummary
from m3_worker.services.custom_forecast_repository import StoredCustomRun
from m3_worker.services.custom_forecast_service import CustomForecastService


HISTORY_START = datetime.fromisoformat("2026-08-24T00:00:00+08:00")
HISTORY_END = HISTORY_START + timedelta(days=7)
NOW = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_run() -> StoredCustomRun:
    config = CustomForecastConfig(
        history_start=HISTORY_START,
        history_end=HISTORY_END,
        history_days=7,
        forecast_start=HISTORY_END,
        forecast_end=HISTORY_END + timedelta(days=1),
        forecast_days=1,
        interval_seconds=3600,
        points_per_day=24,
        expected_points_per_series=24,
        model_policy="seasonal_naive_only",
    )
    return StoredCustomRun(
        record_id=1,
        record=CustomRunRecord(
            run_id="weekly-evidence-run",
            station_id="ES01",
            idempotency_key="weekly-evidence-key",
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


def make_observations() -> list[CustomObservationPoint]:
    points: list[CustomObservationPoint] = []
    for index in range(7 * 24):
        timestamp = HISTORY_START + timedelta(hours=index)
        points.extend(
            (
                CustomObservationPoint(
                    unique_id="station_total_load",
                    ds=timestamp,
                    y=800.0 + index,
                    quality="valid",
                    source_revision=1,
                ),
                CustomObservationPoint(
                    unique_id="storage_soc",
                    ds=timestamp,
                    y=55.0,
                    quality="valid",
                    source_revision=1,
                ),
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

        service._execute("weekly-evidence-run")

        run = repository.run
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.model_manifest["selection_policy"], "weekly_load_v1")
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
            run.source_manifest["series"]["station_total_load"]["usable_week_count"],
            1,
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


if __name__ == "__main__":
    unittest.main()
