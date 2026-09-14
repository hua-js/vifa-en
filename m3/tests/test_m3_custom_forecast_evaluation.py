"""Regression coverage for custom forecast performance aggregates."""

from datetime import datetime, timedelta
import unittest

from m3.worker.api.models import CustomPerformanceResponse
from m3.worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomRunRecord,
)
from m3.worker.services.custom_forecast_evaluation_service import (
    CustomForecastEvaluationService,
)
from m3.worker.services.custom_forecast_repository import StoredCustomRun


NOW = datetime.fromisoformat("2026-08-31T00:00:00+08:00")


def make_run(
    run_id: str, model_manifest: object, *, history_days: int = 28
) -> StoredCustomRun:
    history_end = NOW - timedelta(days=1)
    config = CustomForecastConfig(
        history_start=history_end - timedelta(days=history_days),
        history_end=history_end,
        history_days=history_days,
        forecast_start=history_end,
        forecast_end=NOW,
        forecast_days=1,
        interval_seconds=900,
        points_per_day=96,
        expected_points_per_series=96,
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
            status="evaluated",
        ),
        model_manifest=model_manifest,
        source_manifest=None,
        content_hash=None,
        started_at=None,
        completed_at=NOW,
        evaluated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def evaluation(
    run_id: str,
    mape_percent: float,
    *,
    window_end: str = "2026-08-31T00:00:00+08:00",
    valid_count: int = 10,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "outcome": "available",
        "mape_percent": mape_percent,
        "baseline_mape_percent": 10.0,
        "valid_count": valid_count,
        "zero_actual_count": 0,
        "window_end": window_end,
    }


class InMemoryEvaluationRepository:
    def __init__(
        self,
        runs: list[StoredCustomRun],
        rows: list[dict[str, object]],
        *,
        reject_repeat_run_lookup: bool = False,
    ) -> None:
        self._runs = {run.run_id: run for run in runs}
        self._rows = rows
        self._reject_repeat_run_lookup = reject_repeat_run_lookup
        self._looked_up_run_ids: set[str] = set()

    def get_by_run_id(self, run_id: str) -> StoredCustomRun | None:
        if self._reject_repeat_run_lookup and run_id in self._looked_up_run_ids:
            raise AssertionError("performance looked up a run more than once")
        self._looked_up_run_ids.add(run_id)
        return self._runs.get(run_id)

    def list_comparable_evaluations(self, **_filters) -> list[dict[str, object]]:
        return list(self._rows)


class CustomForecastEvaluationServiceTests(unittest.TestCase):
    def test_saved_score_matches_live_score_without_replacing_standard_metrics(self):
        run = make_run("score-run", {"selection_policy": "weekly_load_v2"})
        points = [dict(target_time=(run.config.forecast_start + timedelta(minutes=15*i)).isoformat(),
                       forecast_value=109, actual_value=100, actual_quality="valid",
                       baseline_forecast_value=110) for i in range(96)]
        service = CustomForecastEvaluationService(None, None, lambda: NOW)
        saved = service._series_evaluation(run, "station_total_load", points, NOW)
        self.assertEqual(saved["current_score"], service.current_score(run, points))
        self.assertEqual(saved["mape_percent"], 9)
        self.assertLess(saved["current_score"]["mape_percent"], 9)
        self.assertIsNone(service._series_evaluation(run, "storage_soc", points, NOW)["current_score"])

    def test_performance_returns_seven_daily_buckets_ending_at_latest_evaluation(self):
        """Dropping daily bucketing would leave all seven frontend cards empty."""
        runs = [
            make_run(run_id, {"selection_policy": "weekly_load_v2"})
            for run_id in ("aug-28", "aug-30-a", "aug-30-b")
        ]
        repository = InMemoryEvaluationRepository(
            runs,
            [
                evaluation(
                    "aug-28",
                    6.0,
                    window_end="2026-08-29T00:00:00+08:00",
                ),
                evaluation(
                    "aug-30-a",
                    8.0,
                    window_end="2026-08-31T00:00:00+08:00",
                    valid_count=10,
                ),
                evaluation(
                    "aug-30-b",
                    4.0,
                    window_end="2026-08-31T00:00:00+08:00",
                    valid_count=30,
                ),
            ],
        )
        service = CustomForecastEvaluationService(
            repository, source=object(), now=lambda: NOW
        )

        performance = service.performance(
            station_id="ES01",
            interval_seconds=900,
            forecast_days=1,
            history_days=28,
            now=NOW,
        )

        load_daily = performance["series"][0]["daily"]
        self.assertEqual(
            [item["date"] for item in load_daily],
            [
                "2026-08-24",
                "2026-08-25",
                "2026-08-26",
                "2026-08-27",
                "2026-08-28",
                "2026-08-29",
                "2026-08-30",
            ],
        )
        self.assertEqual(load_daily[4], {
            "date": "2026-08-28",
            "mape_percent": 6.0,
            "scorable_point_count": 10,
            "run_count": 1,
        })
        self.assertEqual(load_daily[-1], {
            "date": "2026-08-30",
            "mape_percent": 5.0,
            "scorable_point_count": 40,
            "run_count": 2,
        })
        self.assertEqual(load_daily[0]["mape_percent"], None)
        response = CustomPerformanceResponse.model_validate(performance)
        self.assertEqual(response.series[0].daily[-1].mape_percent, 5.0)

    def test_performance_inherits_history_across_training_window_lengths(self):
        """Increasing training from 29 to 37 days keeps prior comparable evidence."""
        runs = [
            make_run(
                f"weekly-{history_days}",
                {"selection_policy": "weekly_load_v2"},
                history_days=history_days,
            )
            for history_days in (28, 29, 37)
        ]
        repository = InMemoryEvaluationRepository(
            runs,
            [
                evaluation("weekly-28", 3.0),
                evaluation("weekly-29", 6.0),
                evaluation("weekly-37", 9.0),
            ],
            reject_repeat_run_lookup=True,
        )
        service = CustomForecastEvaluationService(
            repository, source=object(), now=lambda: NOW
        )

        performance = service.performance(
            station_id="ES01",
            interval_seconds=900,
            forecast_days=1,
            history_days=37,
            now=NOW,
        )

        self.assertEqual(
            [(item["run_count"], item["mape_percent"]) for item in performance["series"]],
            [(3, 6.0), (3, 6.0)],
        )
        self.assertEqual(
            repository._looked_up_run_ids,
            {"weekly-28", "weekly-29", "weekly-37"},
        )

    def test_performance_excludes_old_daily_policy_evaluations(self):
        """Removing the run-manifest policy gate would mix the old 20% MAPE."""
        old_run = make_run("daily-run", {"series": {}})
        weekly_run = make_run("weekly-run", {"selection_policy": "weekly_load_v2"})
        repository = InMemoryEvaluationRepository(
            [old_run, weekly_run],
            [evaluation("daily-run", 20.0), evaluation("weekly-run", 8.0)],
        )
        service = CustomForecastEvaluationService(
            repository, source=object(), now=lambda: NOW
        )

        performance = service.performance(
            station_id="ES01",
            interval_seconds=900,
            forecast_days=1,
            history_days=28,
            now=NOW,
        )

        self.assertEqual(performance["series"][0]["run_count"], 1)
        self.assertEqual(performance["series"][0]["mape_percent"], 8.0)

    def test_performance_caches_run_lookups_and_ignores_invalid_manifest_runs(self):
        """A repeated lookup or unsafe manifest access would break aggregation."""
        weekly_run = make_run("weekly-run", {"selection_policy": "weekly_load_v2"})
        old_run = make_run("old-run", {"series": {}})
        null_manifest_run = make_run("null-manifest-run", None)
        malformed_manifest_run = make_run("malformed-manifest-run", [])
        wrong_policy_run = make_run("wrong-policy-run", {"selection_policy": "v0"})
        repository = InMemoryEvaluationRepository(
            [
                weekly_run,
                old_run,
                null_manifest_run,
                malformed_manifest_run,
                wrong_policy_run,
            ],
            [
                evaluation("weekly-run", 8.0),
                evaluation("old-run", 20.0),
                evaluation("missing-run", 20.0),
                evaluation("null-manifest-run", 20.0),
                evaluation("malformed-manifest-run", 20.0),
                evaluation("wrong-policy-run", 20.0),
            ],
            reject_repeat_run_lookup=True,
        )
        service = CustomForecastEvaluationService(
            repository, source=object(), now=lambda: NOW
        )

        performance = service.performance(
            station_id="ES01",
            interval_seconds=900,
            forecast_days=1,
            history_days=28,
            now=NOW,
        )

        self.assertEqual(
            [(item["run_count"], item["mape_percent"]) for item in performance["series"]],
            [(1, 8.0), (1, 8.0)],
        )


if __name__ == "__main__":
    unittest.main()
