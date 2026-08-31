"""Regression coverage for custom forecast performance aggregates."""

from datetime import datetime, timedelta
import unittest

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomRunRecord,
)
from m3_worker.services.custom_forecast_evaluation_service import (
    CustomForecastEvaluationService,
)
from m3_worker.services.custom_forecast_repository import StoredCustomRun


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


def evaluation(run_id: str, mape_percent: float) -> dict[str, object]:
    return {
        "run_id": run_id,
        "outcome": "available",
        "mape_percent": mape_percent,
        "baseline_mape_percent": 10.0,
        "valid_count": 10,
        "zero_actual_count": 0,
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
    def test_performance_matches_exact_history_days_and_caches_mixed_run_lookups(self):
        """The broad full-selection policy must not mix 28/60/90-day evidence."""
        runs = [
            make_run(
                f"weekly-{history_days}",
                {"selection_policy": "weekly_load_v1"},
                history_days=history_days,
            )
            for history_days in (28, 60, 90)
        ]
        repository = InMemoryEvaluationRepository(
            runs,
            [
                evaluation("weekly-28", 28.0),
                evaluation("weekly-60", 6.0),
                evaluation("weekly-90", 9.0),
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
            history_days=60,
            now=NOW,
        )

        self.assertEqual(
            [(item["run_count"], item["mape_percent"]) for item in performance["series"]],
            [(1, 6.0), (1, 6.0)],
        )
        self.assertEqual(
            repository._looked_up_run_ids,
            {"weekly-28", "weekly-60", "weekly-90"},
        )

    def test_performance_excludes_old_daily_policy_evaluations(self):
        """Removing the run-manifest policy gate would mix the old 20% MAPE."""
        old_run = make_run("daily-run", {"series": {}})
        weekly_run = make_run("weekly-run", {"selection_policy": "weekly_load_v1"})
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
        weekly_run = make_run("weekly-run", {"selection_policy": "weekly_load_v1"})
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
