"""Actual overlay, persistent evaluation, and comparable MAPE for custom runs."""

from datetime import datetime, timedelta
import math
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from m3_worker.contracts import SERIES_IDS
from m3_worker.custom_forecast_contracts import ALLOWED_INTERVAL_SECONDS
from m3_worker.domain.custom_load_profiles import LOAD_SELECTION_POLICY
from m3_worker.errors import M3Error
from m3_worker.services.custom_forecast_repository import (
    CustomForecastRepository,
    StoredCustomRun,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINIMUM_COVERAGE = 0.8


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error(
                "evaluation_incomplete", "Custom point timestamp is invalid"
            ) from error
    else:
        raise M3Error("evaluation_incomplete", "Custom point timestamp is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise M3Error("evaluation_incomplete", "Custom point timestamp must be aware")
    return parsed.astimezone(SHANGHAI)


def _number(value: object, field_name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise M3Error("evaluation_incomplete", f"{field_name} is invalid")
    return float(value)


class CustomForecastEvaluationService:
    def __init__(
        self,
        repository: CustomForecastRepository,
        source: object,
        now,
    ) -> None:
        self._repository = repository
        self._source = source
        self._now = now

    @staticmethod
    def _aligned_end(run: StoredCustomRun, now: datetime) -> datetime:
        now = now.astimezone(SHANGHAI)
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        aligned_seconds = seconds - seconds % run.config.interval_seconds
        aligned = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            seconds=aligned_seconds
        )
        return min(aligned, run.config.forecast_end)

    def _actuals(
        self, run: StoredCustomRun, *, through: datetime
    ) -> dict[tuple[str, datetime], object]:
        end = self._aligned_end(run, through)
        if end <= run.config.forecast_start:
            return {}
        points = []
        cursor = run.config.forecast_start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=7), end)
            chunk = self._source.list_custom_observations(
                run.station_id,
                cursor,
                chunk_end,
                interval_seconds=run.config.interval_seconds,
            )
            if type(chunk) is not list:
                raise M3Error(
                    "source_contract_invalid", "Custom actual batch is invalid"
                )
            points.extend(chunk)
            cursor = chunk_end
        actuals: dict[tuple[str, datetime], object] = {}
        for point in points:
            key = (point.unique_id, point.ds.astimezone(SHANGHAI))
            if key in actuals:
                raise M3Error("evaluation_incomplete", "Custom actual is duplicated")
            actuals[key] = point
        return actuals

    def overlay_actuals(
        self,
        run: StoredCustomRun,
        points: list[dict[str, Any]],
        *,
        through: datetime | None = None,
    ) -> list[dict[str, Any]]:
        actuals = self._actuals(run, through=through or self._now())
        overlaid = []
        for row in points:
            value = dict(row)
            key = (row.get("unique_id"), _timestamp(row.get("target_time")))
            actual = actuals.get(key)
            if actual is None:
                value.update(
                    {
                        "actual_value": None,
                        "actual_quality": None,
                        "actual_source_revision": None,
                        "absolute_percentage_error": None,
                    }
                )
            elif actual.quality != "valid" or actual.y is None:
                value.update(
                    {
                        "actual_value": None,
                        "actual_quality": "invalid",
                        "actual_source_revision": actual.source_revision,
                        "absolute_percentage_error": None,
                    }
                )
            else:
                forecast = _number(row.get("forecast_value"), "forecast_value")
                actual_value = float(actual.y)
                value.update(
                    {
                        "actual_value": actual_value,
                        "actual_quality": "valid",
                        "actual_source_revision": actual.source_revision,
                        "absolute_percentage_error": (
                            100 * abs(actual_value - forecast) / abs(actual_value)
                            if actual_value != 0
                            else None
                        ),
                    }
                )
            overlaid.append(value)
        return overlaid

    @staticmethod
    def _series_evaluation(
        run: StoredCustomRun,
        unique_id: str,
        rows: list[dict[str, Any]],
        calculated_at: datetime,
    ) -> dict[str, object]:
        expected = run.config.expected_points_per_series
        if len(rows) != expected:
            raise M3Error(
                "evaluation_incomplete", "Custom result point count is incomplete"
            )
        valid = [row for row in rows if row.get("actual_quality") == "valid"]
        zero_actual = [row for row in valid if row.get("actual_value") == 0]
        scorable = [row for row in valid if row.get("actual_value") != 0]
        errors = [
            abs(
                _number(row.get("actual_value"), "actual_value")
                - _number(row.get("forecast_value"), "forecast_value")
            )
            for row in valid
        ]
        apes = [
            abs(
                _number(row.get("actual_value"), "actual_value")
                - _number(row.get("forecast_value"), "forecast_value")
            )
            / abs(_number(row.get("actual_value"), "actual_value"))
            * 100
            for row in scorable
        ]
        baseline_apes = [
            abs(
                _number(row.get("actual_value"), "actual_value")
                - _number(
                    row.get("baseline_forecast_value"),
                    "baseline_forecast_value",
                )
            )
            / abs(_number(row.get("actual_value"), "actual_value"))
            * 100
            for row in scorable
        ]
        smape_parts = []
        for row in valid:
            actual = _number(row.get("actual_value"), "actual_value")
            forecast = _number(row.get("forecast_value"), "forecast_value")
            denominator = abs(actual) + abs(forecast)
            if denominator:
                smape_parts.append(200 * abs(actual - forecast) / denominator)
        actual_sum = sum(
            abs(_number(row.get("actual_value"), "actual_value")) for row in valid
        )
        mape = float(np.mean(apes)) if apes else None
        baseline_mape = float(np.mean(baseline_apes)) if baseline_apes else None
        relative = (
            (baseline_mape - mape) / baseline_mape * 100
            if baseline_mape not in {None, 0} and mape is not None
            else None
        )
        available = len(valid) >= math.ceil(expected * MINIMUM_COVERAGE) and mape is not None
        return {
            "run_pk": run.record_id,
            "station_id": run.station_id,
            "run_id": run.run_id,
            "evaluation_key": unique_id,
            "interval_seconds": run.config.interval_seconds,
            "forecast_days": run.config.forecast_days,
            "model_policy": run.config.model_policy,
            "window_start": run.config.forecast_start.isoformat(),
            "window_end": run.config.forecast_end.isoformat(),
            "expected_count": expected,
            "valid_count": len(valid),
            "zero_actual_count": len(zero_actual),
            "mape_percent": mape,
            "mae": float(np.mean(errors)) if errors else None,
            "smape_percent": float(np.mean(smape_parts)) if smape_parts else None,
            "wape_percent": sum(errors) / actual_sum * 100 if actual_sum else None,
            "median_ape_percent": float(median(apes)) if apes else None,
            "p90_ape_percent": float(np.percentile(apes, 90)) if apes else None,
            "baseline_mape_percent": baseline_mape,
            "relative_baseline_improvement_percent": relative,
            "outcome": "available" if available else "insufficient_data",
            "calculated_at": calculated_at.isoformat(),
        }

    def evaluate_run(
        self, run: StoredCustomRun, *, calculated_at: datetime
    ) -> StoredCustomRun:
        if run.status != "succeeded" or calculated_at < run.config.forecast_end:
            return run
        persisted = self._repository.list_points(run)
        points = self.overlay_actuals(run, persisted, through=run.config.forecast_end)
        grouped = {
            unique_id: [row for row in points if row.get("unique_id") == unique_id]
            for unique_id in SERIES_IDS
        }
        evaluations = [
            self._series_evaluation(run, unique_id, grouped[unique_id], calculated_at)
            for unique_id in SERIES_IDS
        ]
        outcomes = {item["outcome"] for item in evaluations}
        evaluations.append(
            {
                "run_pk": run.record_id,
                "station_id": run.station_id,
                "run_id": run.run_id,
                "evaluation_key": "overall",
                "interval_seconds": run.config.interval_seconds,
                "forecast_days": run.config.forecast_days,
                "model_policy": run.config.model_policy,
                "window_start": run.config.forecast_start.isoformat(),
                "window_end": run.config.forecast_end.isoformat(),
                "expected_count": run.config.expected_points_per_series * 2,
                "valid_count": sum(item["valid_count"] for item in evaluations),
                "zero_actual_count": sum(
                    item["zero_actual_count"] for item in evaluations
                ),
                "mape_percent": None,
                "mae": None,
                "smape_percent": None,
                "wape_percent": None,
                "median_ape_percent": None,
                "p90_ape_percent": None,
                "baseline_mape_percent": None,
                "relative_baseline_improvement_percent": None,
                "outcome": (
                    "available" if outcomes == {"available"} else "insufficient_data"
                ),
                "calculated_at": calculated_at.isoformat(),
            }
        )
        self._repository.save_evaluations(run, evaluations)
        return self._repository.transition(run, "evaluated", at=calculated_at)

    def evaluate_station(self, station_id: str, now: datetime) -> int:
        completed = 0
        for run in self._repository.list_succeeded(station_id):
            if run.config.forecast_end <= now:
                self.evaluate_run(run, calculated_at=now)
                completed += 1
        return completed

    def performance(
        self,
        *,
        station_id: str,
        interval_seconds: int,
        forecast_days: int,
        history_days: int,
        now: datetime | None = None,
    ) -> dict[str, object]:
        if interval_seconds not in ALLOWED_INTERVAL_SECONDS:
            raise M3Error("request_invalid", "Performance interval is invalid")
        if type(forecast_days) is not int or not 1 <= forecast_days <= 7:
            raise M3Error("request_invalid", "Performance forecast_days is invalid")
        if type(history_days) is not int or not 7 <= history_days <= 90:
            raise M3Error("request_invalid", "Performance history_days is invalid")
        at = now or self._now()
        policy = "seasonal_naive_only" if history_days < 28 else "full_selection"
        since = at - timedelta(days=8)
        run_cache: dict[str, StoredCustomRun | None] = {}

        def uses_comparable_weekly_run(row: dict[str, Any]) -> bool:
            run_id = row.get("run_id")
            if type(run_id) is not str:
                return False
            if run_id not in run_cache:
                run_cache[run_id] = self._repository.get_by_run_id(run_id)
            run = run_cache[run_id]
            manifest = run.model_manifest if run else None
            return (
                type(manifest) is dict
                and manifest.get("selection_policy") == LOAD_SELECTION_POLICY
                and run.config.history_days == history_days
            )

        usable_by_series: dict[str, list[dict[str, Any]]] = {}
        for unique_id in SERIES_IDS:
            rows = self._repository.list_comparable_evaluations(
                station_id=station_id,
                evaluation_key=unique_id,
                interval_seconds=interval_seconds,
                forecast_days=forecast_days,
                model_policy=policy,
                calculated_since=since,
            )
            rows = [row for row in rows if uses_comparable_weekly_run(row)]
            usable_by_series[unique_id] = [
                row
                for row in rows
                if row.get("outcome") == "available"
                and row.get("mape_percent") is not None
                and row.get("baseline_mape_percent") is not None
            ]

        def evaluation_day(row: dict[str, Any]):
            return (
                _timestamp(row.get("window_end")) - timedelta(microseconds=1)
            ).date()

        evaluation_days = [
            evaluation_day(row)
            for rows in usable_by_series.values()
            for row in rows
        ]
        latest_day = (
            max(evaluation_days)
            if evaluation_days
            else at.date() - timedelta(days=1)
        )
        daily_dates = [
            latest_day - timedelta(days=offset) for offset in range(6, -1, -1)
        ]

        def aggregate(
            rows: list[dict[str, Any]],
        ) -> tuple[float | None, float | None, int]:
            weights = [
                int(row["valid_count"]) - int(row["zero_actual_count"])
                for row in rows
            ]
            total_weight = sum(max(weight, 0) for weight in weights)
            if total_weight:
                mape = sum(
                    float(row["mape_percent"]) * max(weight, 0)
                    for row, weight in zip(rows, weights)
                ) / total_weight
                baseline = sum(
                    float(row["baseline_mape_percent"]) * max(weight, 0)
                    for row, weight in zip(rows, weights)
                ) / total_weight
            else:
                mape = None
                baseline = None
            return mape, baseline, total_weight

        series_results = []
        for unique_id in SERIES_IDS:
            usable = usable_by_series[unique_id]
            rows_by_day = {
                day: [row for row in usable if evaluation_day(row) == day]
                for day in daily_dates
            }
            included = [row for rows in rows_by_day.values() for row in rows]
            mape, baseline, total_weight = aggregate(included)
            daily = []
            for day in daily_dates:
                daily_mape, _daily_baseline, daily_weight = aggregate(
                    rows_by_day[day]
                )
                daily.append(
                    {
                        "date": day.isoformat(),
                        "mape_percent": daily_mape,
                        "scorable_point_count": daily_weight,
                        "run_count": len(rows_by_day[day]),
                    }
                )
            improvement = (
                (baseline - mape) / baseline * 100
                if baseline not in {None, 0} and mape is not None
                else None
            )
            series_results.append(
                {
                    "unique_id": unique_id,
                    "mape_percent": mape,
                    "baseline_mape_percent": baseline,
                    "relative_baseline_improvement_percent": improvement,
                    "scorable_point_count": total_weight,
                    "run_count": len(included),
                    "daily": daily,
                }
            )
        return {
            "station_id": station_id,
            "lookback_days": 7,
            "interval_seconds": interval_seconds,
            "forecast_days": forecast_days,
            "model_policy": policy,
            "calculated_at": at,
            "series": series_results,
        }
