"""Persistent, idempotent NocoBase repository for custom M3 forecast runs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from m3_worker.custom_forecast_contracts import (
    CustomForecastConfig,
    CustomForecastRequest,
    CustomForecastSeries,
    CustomRunRecord,
    RunStatus,
    build_custom_forecast_config,
    validate_custom_series_for_config,
)
from m3_worker.errors import M3Error
from m3_worker.sinks.forecast_sink import canonical_hash


RUNS = "energy_forecast_manual_runs"
POINTS = "energy_forecast_manual_points"
EVALUATIONS = "energy_forecast_manual_evaluations"
POINT_BATCH_SIZE = 500
RUN_FIELDS = [
    "id",
    "run_id",
    "station_id",
    "idempotency_key",
    "history_start",
    "history_end",
    "history_days",
    "forecast_start",
    "forecast_end",
    "forecast_days",
    "interval_seconds",
    "points_per_day",
    "expected_points_per_series",
    "model_policy",
    "status",
    "model_manifest",
    "source_manifest",
    "content_hash",
    "error_code",
    "requested_by",
    "started_at",
    "completed_at",
    "evaluated_at",
    "createdAt",
    "updatedAt",
]
POINT_FIELDS = [
    "id",
    "run_pk",
    "unique_id",
    "target_time",
    "horizon_step",
    "model_name",
    "raw_forecast",
    "forecast_value",
    "baseline_forecast_value",
    "is_clipped",
    "actual_value",
    "actual_quality",
    "actual_source_revision",
    "actual_recorded_at",
    "evaluated_at",
    "absolute_percentage_error",
]
EVALUATION_FIELDS = [
    "id",
    "run_pk",
    "station_id",
    "run_id",
    "evaluation_key",
    "interval_seconds",
    "forecast_days",
    "model_policy",
    "window_start",
    "window_end",
    "expected_count",
    "valid_count",
    "zero_actual_count",
    "mape_percent",
    "mae",
    "smape_percent",
    "wape_percent",
    "median_ape_percent",
    "p90_ape_percent",
    "baseline_mape_percent",
    "relative_baseline_improvement_percent",
    "outcome",
    "calculated_at",
]
IMMUTABLE_POINT_FIELDS = (
    "run_pk",
    "unique_id",
    "target_time",
    "horizon_step",
    "model_name",
    "raw_forecast",
    "forecast_value",
    "baseline_forecast_value",
    "is_clipped",
)
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed"}),
    "running": frozenset({"running", "succeeded", "failed"}),
    "succeeded": frozenset({"evaluated"}),
    "evaluated": frozenset(),
    "failed": frozenset(),
}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _positive_id(value: object, context: str) -> int:
    if type(value) is not int or value < 1:
        raise M3Error("sink_contract_invalid", f"{context} primary key is invalid")
    return value


def _timestamp(value: object, field_name: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error(
                "sink_contract_invalid", f"Persisted {field_name} is invalid"
            ) from error
    else:
        raise M3Error("sink_contract_invalid", f"Persisted {field_name} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise M3Error(
            "sink_contract_invalid", f"Persisted {field_name} must include a timezone"
        )
    return parsed.astimezone(SHANGHAI)


def _optional_mapping(value: object, field_name: str) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is not dict:
        raise M3Error("sink_contract_invalid", f"Persisted {field_name} is invalid")
    canonical_hash(value)
    return value


@dataclass(frozen=True)
class StoredCustomRun:
    record_id: int
    record: CustomRunRecord
    model_manifest: dict[str, object] | None
    source_manifest: dict[str, object] | None
    content_hash: str | None
    started_at: datetime | None
    completed_at: datetime | None
    evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def station_id(self) -> str:
        return self.record.station_id

    @property
    def config(self) -> CustomForecastConfig:
        return self.record.config

    @property
    def status(self) -> RunStatus:
        return self.record.status


class CustomForecastRepository:
    def __init__(self, api: object) -> None:
        self._api = api

    @staticmethod
    def _parse_run(row: dict[str, Any]) -> StoredCustomRun:
        if type(row) is not dict or any(field not in row for field in RUN_FIELDS):
            raise M3Error("sink_contract_invalid", "Persisted custom run is incomplete")
        config = CustomForecastConfig(
            history_start=_timestamp(row["history_start"], "history_start"),
            history_end=_timestamp(row["history_end"], "history_end"),
            history_days=row["history_days"],
            forecast_start=_timestamp(row["forecast_start"], "forecast_start"),
            forecast_end=_timestamp(row["forecast_end"], "forecast_end"),
            forecast_days=row["forecast_days"],
            interval_seconds=row["interval_seconds"],
            points_per_day=row["points_per_day"],
            expected_points_per_series=row["expected_points_per_series"],
            model_policy=row["model_policy"],
        )
        record = CustomRunRecord(
            run_id=row["run_id"],
            station_id=row["station_id"],
            idempotency_key=row["idempotency_key"],
            config=config,
            status=row["status"],
            requested_by=row["requested_by"],
            error_code=row["error_code"],
        )
        content_hash = row["content_hash"]
        if content_hash is not None and (
            type(content_hash) is not str or len(content_hash) != 64
        ):
            raise M3Error("sink_contract_invalid", "Persisted content hash is invalid")
        return StoredCustomRun(
            record_id=_positive_id(row["id"], "custom run"),
            record=record,
            model_manifest=_optional_mapping(row["model_manifest"], "model_manifest"),
            source_manifest=_optional_mapping(row["source_manifest"], "source_manifest"),
            content_hash=content_hash,
            started_at=_timestamp(row["started_at"], "started_at", optional=True),
            completed_at=_timestamp(
                row["completed_at"], "completed_at", optional=True
            ),
            evaluated_at=_timestamp(
                row["evaluated_at"], "evaluated_at", optional=True
            ),
            created_at=_timestamp(row["createdAt"], "createdAt"),
            updated_at=_timestamp(row["updatedAt"], "updatedAt"),
        )

    @staticmethod
    def _create_values(
        station_id: str,
        request: CustomForecastRequest,
        requested_by: str | None,
    ) -> tuple[dict[str, object], CustomForecastConfig]:
        if type(station_id) is not str or not station_id or station_id != station_id.strip():
            raise M3Error("station_not_configured", "Station is not configured")
        if requested_by is not None and (
            type(requested_by) is not str
            or not requested_by
            or requested_by != requested_by.strip()
            or len(requested_by) > 128
        ):
            raise M3Error("request_identity_invalid", "Request identity is invalid")
        request = CustomForecastRequest.model_validate(request)
        config = build_custom_forecast_config(request)
        values: dict[str, object] = {
            "run_id": str(uuid4()),
            "station_id": station_id,
            "idempotency_key": request.idempotency_key,
            **config.model_dump(mode="json"),
            "status": "queued",
            "requested_by": requested_by,
        }
        return values, config

    def create_or_get(
        self,
        station_id: str,
        request: CustomForecastRequest,
        *,
        requested_by: str | None,
    ) -> StoredCustomRun:
        values, expected_config = self._create_values(
            station_id, request, requested_by
        )
        row = self._api.first_or_create(
            RUNS,
            {
                "station_id": station_id,
                "idempotency_key": request.idempotency_key,
            },
            values,
        )
        run = self.get_by_run_id(row.get("run_id"))
        if run is None:
            raise M3Error("sink_contract_invalid", "Custom run was not persisted")
        if (
            run.station_id != station_id
            or run.record.idempotency_key != request.idempotency_key
            or run.config != expected_config
            or run.record.requested_by != requested_by
        ):
            raise M3Error(
                "idempotency_conflict",
                "The idempotency key belongs to another custom request",
            )
        return run

    def get_by_run_id(self, run_id: object) -> StoredCustomRun | None:
        if type(run_id) is not str or not run_id:
            return None
        rows = self._api.list_records(
            RUNS, filter={"run_id": run_id}, fields=RUN_FIELDS
        )
        if len(rows) > 1:
            raise M3Error("sink_contract_invalid", "Custom run identity is duplicated")
        return None if not rows else self._parse_run(rows[0])

    def list_recoverable(self) -> list[StoredCustomRun]:
        runs: list[StoredCustomRun] = []
        seen: set[str] = set()
        for status in ("queued", "running"):
            rows = self._api.list_records_all(
                RUNS,
                filter={"status": status},
                fields=RUN_FIELDS,
                sort=["createdAt"],
            )
            for row in rows:
                run = self._parse_run(row)
                if run.run_id in seen:
                    raise M3Error(
                        "sink_contract_invalid", "Recoverable custom run is duplicated"
                    )
                seen.add(run.run_id)
                runs.append(run)
        return runs

    def list_succeeded(self, station_id: str) -> list[StoredCustomRun]:
        rows = self._api.list_records_all(
            RUNS,
            filter={"station_id": station_id, "status": "succeeded"},
            fields=RUN_FIELDS,
            sort=["createdAt"],
        )
        return [self._parse_run(row) for row in rows]

    def transition(
        self,
        run: StoredCustomRun,
        status: RunStatus,
        *,
        at: datetime,
        model_manifest: dict[str, object] | None = None,
        source_manifest: dict[str, object] | None = None,
        content_hash: str | None = None,
        error_code: str | None = None,
    ) -> StoredCustomRun:
        if status not in ALLOWED_TRANSITIONS[run.status]:
            raise M3Error("job_state_invalid", "Custom run transition is invalid")
        if at.tzinfo is None or at.utcoffset() is None:
            raise M3Error("time_contract_invalid", "Custom run time must be aware")
        values: dict[str, object] = {"status": status}
        if status == "running" and run.started_at is None:
            values["started_at"] = at.isoformat()
        elif status == "succeeded":
            if not content_hash or model_manifest is None or source_manifest is None:
                raise M3Error(
                    "job_state_invalid", "Successful custom run evidence is incomplete"
                )
            values.update(
                {
                    "completed_at": at.isoformat(),
                    "model_manifest": model_manifest,
                    "source_manifest": source_manifest,
                    "content_hash": content_hash,
                    "error_code": None,
                }
            )
        elif status == "evaluated":
            values["evaluated_at"] = at.isoformat()
        elif status == "failed":
            if type(error_code) is not str or not error_code:
                raise M3Error("job_state_invalid", "Failed custom run needs an error code")
            values.update({"error_code": error_code, "completed_at": at.isoformat()})
        self._api.update_record(RUNS, run.record_id, values)
        updated = self.get_by_run_id(run.run_id)
        if updated is None or updated.status != status:
            raise M3Error("sink_contract_invalid", "Custom run update is incomplete")
        return updated

    @staticmethod
    def _point_values(
        run: StoredCustomRun,
        series: list[CustomForecastSeries],
        baseline_series: list[CustomForecastSeries],
    ) -> list[dict[str, object]]:
        by_id = {item.unique_id: item for item in series}
        baseline_by_id = {item.unique_id: item for item in baseline_series}
        expected_ids = {"station_total_load", "storage_soc"}
        if (
            len(series) != 2
            or set(by_id) != expected_ids
            or len(baseline_series) != 2
            or set(baseline_by_id) != expected_ids
        ):
            raise M3Error(
                "forecast_incomplete", "Custom result requires load and SOC exactly once"
            )
        values: list[dict[str, object]] = []
        for unique_id in ("station_total_load", "storage_soc"):
            item = by_id[unique_id]
            baseline = baseline_by_id[unique_id]
            validate_custom_series_for_config(item, run.config)
            validate_custom_series_for_config(baseline, run.config)
            if (
                item.status in {"insufficient_history", "error"}
                or baseline.status in {"insufficient_history", "error"}
                or baseline.model_name != "SeasonalNaive"
            ):
                raise M3Error(
                    "forecast_incomplete", "Custom result series is unavailable"
                )
            values.extend(
                {
                    "run_pk": run.record_id,
                    "unique_id": unique_id,
                    "target_time": point.target_time.isoformat(),
                    "horizon_step": point.horizon_step,
                    "model_name": item.model_name,
                    "raw_forecast": point.raw_forecast,
                    "forecast_value": point.forecast_value,
                    "baseline_forecast_value": baseline.points[
                        point.horizon_step - 1
                    ].forecast_value,
                    "is_clipped": point.is_clipped,
                }
                for point in item.points
            )
        return values

    @staticmethod
    def _point_key(point: dict[str, Any]) -> tuple[str, str]:
        unique_id = point.get("unique_id")
        target = _timestamp(point.get("target_time"), "target_time")
        if unique_id not in {"station_total_load", "storage_soc"} or target is None:
            raise M3Error("sink_contract_invalid", "Persisted custom point is invalid")
        return unique_id, target.isoformat()

    @staticmethod
    def _immutable_point(point: dict[str, Any]) -> dict[str, object]:
        try:
            value = {field: point[field] for field in IMMUTABLE_POINT_FIELDS}
        except (KeyError, TypeError) as error:
            raise M3Error(
                "sink_contract_invalid", "Persisted custom point is incomplete"
            ) from error
        value["run_pk"] = _positive_id(value["run_pk"], "custom point run")
        value["target_time"] = _timestamp(
            value["target_time"], "target_time"
        ).isoformat()
        if type(value["horizon_step"]) is not int:
            raise M3Error("sink_contract_invalid", "Custom point horizon is invalid")
        if type(value["is_clipped"]) is not bool:
            raise M3Error("sink_contract_invalid", "Custom point clip flag is invalid")
        for field in (
            "raw_forecast",
            "forecast_value",
            "baseline_forecast_value",
        ):
            if type(value[field]) not in {int, float}:
                raise M3Error(
                    "sink_contract_invalid", "Custom point value is invalid"
                )
            value[field] = float(value[field])
        return value

    def list_points(self, run: StoredCustomRun) -> list[dict[str, Any]]:
        rows = self._api.list_records_all(
            POINTS,
            filter={"run_pk": run.record_id},
            fields=POINT_FIELDS,
            sort=["unique_id", "target_time"],
        )
        seen: set[tuple[str, str]] = set()
        for row in rows:
            _positive_id(row.get("id"), "custom point")
            key = self._point_key(row)
            if key in seen:
                raise M3Error("sink_contract_invalid", "Custom point is duplicated")
            seen.add(key)
            self._immutable_point(row)
        return rows

    def store_points(
        self,
        run: StoredCustomRun,
        series: list[CustomForecastSeries],
        baseline_series: list[CustomForecastSeries],
    ) -> str:
        expected_values = self._point_values(run, series, baseline_series)
        expected = {self._point_key(value): value for value in expected_values}
        if len(expected) != len(expected_values):
            raise M3Error("forecast_incomplete", "Custom forecast points are duplicated")
        existing_rows = self.list_points(run)
        existing = {self._point_key(row): row for row in existing_rows}
        if not set(existing) <= set(expected):
            raise M3Error(
                "idempotency_conflict", "Persisted custom points exceed the result"
            )
        for key, row in existing.items():
            if canonical_hash(self._immutable_point(row)) != canonical_hash(
                self._immutable_point(expected[key])
            ):
                raise M3Error(
                    "idempotency_conflict", "Persisted custom point does not match"
                )
        missing = [value for key, value in expected.items() if key not in existing]
        for offset in range(0, len(missing), POINT_BATCH_SIZE):
            chunk = missing[offset : offset + POINT_BATCH_SIZE]
            created = self._api.create_records(POINTS, chunk)
            if len(created) != len(chunk):
                raise M3Error(
                    "sink_contract_invalid", "Custom point batch write is incomplete"
                )
            expected_chunk = {self._point_key(value): value for value in chunk}
            for row in created:
                _positive_id(row.get("id"), "custom point")
                key = self._point_key(row)
                if key not in expected_chunk or canonical_hash(
                    self._immutable_point(row)
                ) != canonical_hash(self._immutable_point(expected_chunk[key])):
                    raise M3Error(
                        "sink_contract_invalid", "Custom point batch response is invalid"
                    )
        persisted = self.list_points(run)
        if len(persisted) != len(expected):
            raise M3Error("forecast_incomplete", "Custom point count is incomplete")
        immutable = [self._immutable_point(row) for row in persisted]
        if {self._point_key(row) for row in persisted} != set(expected):
            raise M3Error("forecast_incomplete", "Custom point identities are incomplete")
        return canonical_hash(
            {
                "run_id": run.run_id,
                "station_id": run.station_id,
                "config": run.config.model_dump(mode="json"),
                "points": immutable,
            }
        )

    def list_evaluations(self, run: StoredCustomRun) -> list[dict[str, Any]]:
        rows = self._api.list_records_all(
            EVALUATIONS,
            filter={"run_pk": run.record_id},
            fields=EVALUATION_FIELDS,
            sort=["calculated_at"],
        )
        keys = [row.get("evaluation_key") for row in rows]
        if len(keys) != len(set(keys)) or any(
            key not in {"station_total_load", "storage_soc", "overall"}
            for key in keys
        ):
            raise M3Error(
                "sink_contract_invalid", "Custom evaluations are duplicated or invalid"
            )
        return rows

    def save_evaluations(
        self, run: StoredCustomRun, evaluations: list[dict[str, object]]
    ) -> list[dict[str, Any]]:
        if type(evaluations) is not list or len(evaluations) != 3:
            raise M3Error(
                "evaluation_incomplete", "Custom evaluation set is incomplete"
            )
        expected_keys = {"station_total_load", "storage_soc", "overall"}
        actual_keys = {item.get("evaluation_key") for item in evaluations}
        if actual_keys != expected_keys:
            raise M3Error(
                "evaluation_incomplete", "Custom evaluation keys are incomplete"
            )
        for item in evaluations:
            expected_identity = {
                "run_pk": run.record_id,
                "station_id": run.station_id,
                "run_id": run.run_id,
                "interval_seconds": run.config.interval_seconds,
                "forecast_days": run.config.forecast_days,
                "model_policy": run.config.model_policy,
            }
            if any(item.get(key) != value for key, value in expected_identity.items()):
                raise M3Error(
                    "evaluation_incomplete", "Custom evaluation identity is invalid"
                )
            self._api.update_or_create(
                EVALUATIONS,
                {
                    "run_pk": run.record_id,
                    "evaluation_key": item["evaluation_key"],
                },
                item,
            )
        rows = self.list_evaluations(run)
        if len(rows) != 3:
            raise M3Error(
                "evaluation_incomplete", "Custom evaluations were not persisted"
            )
        return rows

    def list_comparable_evaluations(
        self,
        *,
        station_id: str,
        evaluation_key: str,
        interval_seconds: int,
        forecast_days: int,
        model_policy: str,
        calculated_since: datetime,
    ) -> list[dict[str, Any]]:
        return self._api.list_records_all(
            EVALUATIONS,
            filter={
                "station_id": station_id,
                "evaluation_key": evaluation_key,
                "interval_seconds": interval_seconds,
                "forecast_days": forecast_days,
                "model_policy": model_policy,
                "calculated_at": {"$gte": calculated_since.isoformat()},
            },
            fields=EVALUATION_FIELDS,
            sort=["calculated_at"],
        )
