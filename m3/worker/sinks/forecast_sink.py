"""Deterministic latest and recoverable acceptance publication over HTTP."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any

from pydantic_core import to_jsonable_python

from m3.worker.contracts import (
    SERIES_IDS,
    LatestSnapshot,
    validate_shanghai_timestamp,
)
from m3.worker.errors import M3Error


BATCH_HASH_FIELDS = (
    "station_id",
    "acceptance_run_id",
    "issued_at",
    "forecast_start_time",
    "forecast_end_time",
    "status",
    "model_manifest",
)
POINT_HASH_FIELDS = (
    "unique_id",
    "data_time",
    "target_time",
    "horizon_step",
    "model_name",
    "raw_forecast",
    "forecast_value",
    "is_clipped",
)
EXPECTED_DAILY_POINTS = len(SERIES_IDS) * 96
LATEST_MODEL_FIELDS = ("station_id", "model_manifest")


def canonical_hash(value: object) -> str:
    """Hash JSON-compatible business content independent of mapping order."""

    try:
        normalized = to_jsonable_python(value)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise M3Error(
            "sink_contract_invalid", "Content is not canonically serializable"
        ) from error
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: object, field_name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error(
                "sink_contract_invalid", f"{field_name} is not a valid timestamp"
            ) from error
    else:
        raise M3Error("sink_contract_invalid", f"{field_name} is not a valid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise M3Error("sink_contract_invalid", f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def acceptance_content_hash(batch: dict, points: list[dict]) -> str:
    """Hash only the immutable acceptance batch and prediction fields."""

    try:
        immutable_points = [
            {key: point[key] for key in POINT_HASH_FIELDS} for point in points
        ]
        immutable_batch = {key: batch[key] for key in BATCH_HASH_FIELDS}
    except (KeyError, TypeError) as error:
        raise M3Error(
            "acceptance_write_incomplete", "Acceptance content is missing immutable fields"
        ) from error
    for field in ("issued_at", "forecast_start_time", "forecast_end_time"):
        immutable_batch[field] = _canonical_timestamp(immutable_batch[field], field)
    for point in immutable_points:
        point["data_time"] = _canonical_timestamp(point["data_time"], "data_time")
        point["target_time"] = _canonical_timestamp(point["target_time"], "target_time")
    return canonical_hash(
        {
            "batch": immutable_batch,
            "points": sorted(
                immutable_points,
                key=lambda point: (point["unique_id"], point["data_time"]),
            ),
        }
    )


def _timestamp(
    value: object, name: str, *, quarter_hour: bool, storage_response: bool = False
) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error(
                "acceptance_write_incomplete", f"{name} is not a valid timestamp"
            ) from error
    else:
        raise M3Error("acceptance_write_incomplete", f"{name} is not a valid timestamp")
    if storage_response:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise M3Error("acceptance_write_incomplete", f"{name} must include a timezone")
        if quarter_hour and (parsed.minute % 15 or parsed.second or parsed.microsecond):
            raise M3Error(
                "acceptance_write_incomplete", f"{name} must be on a 15-minute boundary"
            )
    else:
        try:
            validate_shanghai_timestamp(parsed, name, quarter_hour=quarter_hour)
        except ValueError as error:
            raise M3Error("acceptance_write_incomplete", str(error)) from error
    return parsed


def _positive_primary_key(row: dict[str, Any], *, context: str) -> int:
    record_id = row.get("id")
    if not isinstance(record_id, int) or isinstance(record_id, bool) or record_id < 1:
        raise M3Error("sink_contract_invalid", f"NocoBase {context} primary key is invalid")
    return record_id


def _same_timestamp(left: object, right: object) -> bool:
    try:
        left_time = (
            datetime.fromisoformat(left.replace("Z", "+00:00"))
            if isinstance(left, str)
            else left
        )
        right_time = (
            datetime.fromisoformat(right.replace("Z", "+00:00"))
            if isinstance(right, str)
            else right
        )
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(left_time, datetime)
        or not isinstance(right_time, datetime)
        or left_time.tzinfo is None
        or right_time.tzinfo is None
        or left_time.utcoffset() is None
        or right_time.utcoffset() is None
    ):
        return False
    left_millisecond = left_time.astimezone(timezone.utc).replace(
        microsecond=(left_time.microsecond // 1000) * 1000
    )
    right_millisecond = right_time.astimezone(timezone.utc).replace(
        microsecond=(right_time.microsecond // 1000) * 1000
    )
    return left_millisecond == right_millisecond


def _aware_timestamp(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
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
    return parsed


def _same_immutable_point(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    if any(field not in actual for field in POINT_HASH_FIELDS):
        return False
    if not _same_timestamp(
        actual["data_time"], expected["data_time"]
    ) or not _same_timestamp(actual["target_time"], expected["target_time"]):
        return False
    non_time_fields = tuple(
        field for field in POINT_HASH_FIELDS if field not in {"data_time", "target_time"}
    )
    return canonical_hash(
        {field: actual[field] for field in non_time_fields}
    ) == canonical_hash({field: expected[field] for field in non_time_fields})


class ForecastSink:
    def __init__(self, api: object) -> None:
        self._api = api

    def load_latest_model_manifest(self, station_id: str) -> dict | None:
        if type(station_id) is not str or not station_id:
            raise M3Error("sink_contract_invalid", "Station identity is invalid")
        rows = self._api.list_records(
            "energy_forecast_latest",
            filter={"station_id": station_id},
            fields=list(LATEST_MODEL_FIELDS),
        )
        if type(rows) is not list or len(rows) > 1:
            raise M3Error(
                "sink_contract_invalid", "Latest model manifest listing is invalid"
            )
        if not rows:
            return None
        row = rows[0]
        if (
            type(row) is not dict
            or set(row) != set(LATEST_MODEL_FIELDS)
            or row["station_id"] != station_id
            or type(row["model_manifest"]) is not dict
        ):
            raise M3Error(
                "sink_contract_invalid", "Latest model manifest row is invalid"
            )
        return deepcopy(row["model_manifest"])

    def publish_latest(self, snapshot: LatestSnapshot) -> dict[str, Any]:
        try:
            snapshot = LatestSnapshot.model_validate(snapshot)
        except Exception as error:
            raise M3Error("sink_contract_invalid", "Latest snapshot is invalid") from error
        values = snapshot.model_dump(mode="json")
        values["series_payload"] = values.pop("series")
        existing = self._api.list_records(
            "energy_forecast_latest",
            filter={"station_id": snapshot.station_id},
            fields=["as_of", "content_hash"],
        )
        if len(existing) > 1 or any(
            not isinstance(row, dict)
            or "as_of" not in row
            or not isinstance(row.get("content_hash"), str)
            for row in existing
        ):
            raise M3Error("sink_contract_invalid", "Latest snapshot lookup is invalid")
        if existing:
            persisted_as_of = _aware_timestamp(existing[0]["as_of"], "latest as_of")
            if snapshot.as_of < persisted_as_of:
                raise M3Error(
                    "idempotency_conflict",
                    "older latest snapshot cannot replace a newer persisted snapshot",
                )
            if (
                snapshot.as_of == persisted_as_of
                and existing[0]["content_hash"] != values["content_hash"]
            ):
                raise M3Error(
                    "idempotency_conflict",
                    "latest snapshot hash mismatch for the same as_of",
                )
        persisted = self._api.update_or_create(
            "energy_forecast_latest", {"station_id": snapshot.station_id}, values
        )
        if not isinstance(persisted, dict):
            raise M3Error("sink_contract_invalid", "Latest snapshot response is invalid")
        _positive_primary_key(persisted, context="latest")
        if (
            persisted.get("station_id") != snapshot.station_id
            or not _same_timestamp(persisted.get("as_of"), values["as_of"])
            or persisted.get("content_hash") != values["content_hash"]
        ):
            raise M3Error(
                "sink_contract_invalid", "Latest snapshot response identity mismatch"
            )
        return persisted

    def _validate_acceptance(
        self, batch: dict, points: list[dict], *, storage_response: bool = False
    ) -> None:
        if not isinstance(batch, dict) or not isinstance(points, list):
            raise M3Error("acceptance_write_incomplete", "Acceptance payload is invalid")
        if any(
            field not in batch
            for field in (*BATCH_HASH_FIELDS, "content_hash", "point_templates")
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Acceptance batch fields are incomplete"
            )
        if (
            not isinstance(batch["station_id"], str)
            or not batch["station_id"]
            or not isinstance(batch["acceptance_run_id"], str)
            or not batch["acceptance_run_id"]
            or batch["status"] not in {"ok", "degraded"}
            or not isinstance(batch["model_manifest"], dict)
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Acceptance batch context is invalid"
            )
        issued_at = _timestamp(
            batch["issued_at"],
            "issued_at",
            quarter_hour=False,
            storage_response=storage_response,
        )
        start = _timestamp(
            batch["forecast_start_time"],
            "forecast_start_time",
            quarter_hour=True,
            storage_response=storage_response,
        )
        end = _timestamp(
            batch["forecast_end_time"],
            "forecast_end_time",
            quarter_hour=True,
            storage_response=storage_response,
        )
        if issued_at > end or end - start != timedelta(days=1):
            raise M3Error("acceptance_write_incomplete", "Acceptance batch window is invalid")
        if (
            len(points) != EXPECTED_DAILY_POINTS
            or not isinstance(batch["point_templates"], list)
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Acceptance batch requires 192 points"
            )
        by_series: dict[str, list[tuple[datetime, dict[str, Any]]]] = {
            unique_id: [] for unique_id in SERIES_IDS
        }
        seen_keys: set[tuple[str, datetime]] = set()
        for point in points:
            if not isinstance(point, dict) or set(point) != set(POINT_HASH_FIELDS):
                raise M3Error(
                    "acceptance_write_incomplete",
                    "Acceptance point templates must contain only immutable fields",
                )
            unique_id = point["unique_id"]
            if unique_id not in SERIES_IDS:
                raise M3Error("acceptance_write_incomplete", "Acceptance series is invalid")
            data_time = _timestamp(
                point["data_time"],
                "data_time",
                quarter_hour=True,
                storage_response=storage_response,
            )
            target_time = _timestamp(
                point["target_time"],
                "target_time",
                quarter_hour=True,
                storage_response=storage_response,
            )
            horizon = point["horizon_step"]
            raw = point["raw_forecast"]
            published = point["forecast_value"]
            clipped = point["is_clipped"]
            if (
                not isinstance(horizon, int)
                or isinstance(horizon, bool)
                or not 1 <= horizon <= 96
                or not isinstance(point["model_name"], str)
                or not point["model_name"]
                or isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(raw)
                or isinstance(published, bool)
                or not isinstance(published, (int, float))
                or not math.isfinite(published)
                or not isinstance(clipped, bool)
                or clipped != (raw != published)
                or target_time - data_time != timedelta(minutes=15)
                or (unique_id == "station_total_load" and published < 0)
                or (unique_id != "station_total_load" and not 0 <= published <= 100)
            ):
                raise M3Error("acceptance_write_incomplete", "Acceptance point is invalid")
            key = (unique_id, data_time)
            if key in seen_keys:
                raise M3Error(
                    "acceptance_write_incomplete", "Acceptance points are duplicated"
                )
            seen_keys.add(key)
            by_series[unique_id].append((data_time, point))

        for series_points in by_series.values():
            series_points.sort(key=lambda item: item[0])
            if len(series_points) != 96:
                raise M3Error(
                    "acceptance_write_incomplete", "Acceptance requires 96 points per series"
                )
            for index, (data_time, point) in enumerate(series_points, start=1):
                expected_time = start + timedelta(minutes=15 * (index - 1))
                if data_time != expected_time or point["horizon_step"] != index:
                    raise M3Error(
                        "acceptance_write_incomplete", "Acceptance points are not contiguous"
                    )

        templates = batch["point_templates"]
        if points is not templates:
            self._validate_acceptance(
                batch, templates, storage_response=storage_response
            )

        supplied_hash = batch["content_hash"]
        if not isinstance(supplied_hash, str):
            raise M3Error("idempotency_conflict", "Acceptance content hash is invalid")
        template_hash = acceptance_content_hash(batch, batch["point_templates"])
        points_hash = acceptance_content_hash(batch, points)
        if supplied_hash != template_hash or supplied_hash != points_hash:
            raise M3Error("idempotency_conflict", "Acceptance content hash mismatch")

    def _validate_batch_response(
        self, persisted: object, expected: dict
    ) -> dict[str, Any]:
        if not isinstance(persisted, dict):
            raise M3Error("sink_contract_invalid", "Acceptance batch response is invalid")
        _positive_primary_key(persisted, context="batch")
        missing = [
            field
            for field in (*BATCH_HASH_FIELDS, "content_hash", "point_templates", "write_state")
            if field not in persisted
        ]
        if missing or persisted["write_state"] not in {"writing", "complete"}:
            raise M3Error(
                "sink_contract_invalid", "Acceptance batch response is incomplete"
            )
        mismatched = (
            persisted["station_id"] != expected["station_id"]
            or persisted["acceptance_run_id"] != expected["acceptance_run_id"]
            or any(
                not _same_timestamp(persisted[field], expected[field])
                for field in ("issued_at", "forecast_start_time", "forecast_end_time")
            )
            or persisted["status"] != expected["status"]
            or canonical_hash(persisted["model_manifest"])
            != canonical_hash(expected["model_manifest"])
            or persisted["content_hash"] != expected["content_hash"]
        )
        if mismatched:
            raise M3Error(
                "idempotency_conflict",
                "Acceptance batch hash or business key mismatch",
            )
        try:
            self._validate_acceptance(
                persisted, persisted["point_templates"], storage_response=True
            )
        except M3Error as error:
            raise M3Error(
                "idempotency_conflict", "Acceptance batch templates are invalid"
            ) from error
        return persisted

    def publish_acceptance(self, batch: dict, points: list[dict]) -> dict[str, Any]:
        return self._publish_acceptance(batch, points, storage_response=False)

    def _publish_acceptance(
        self, batch: dict, points: list[dict], *, storage_response: bool
    ) -> dict[str, Any]:
        recovery_batch_id: int | None = None
        if isinstance(batch, dict) and "id" in batch:
            supplied_id = batch["id"]
            if (
                not isinstance(supplied_id, int)
                or isinstance(supplied_id, bool)
                or supplied_id < 1
            ):
                raise M3Error(
                    "sink_contract_invalid",
                    "Acceptance recovery batch primary key is invalid",
                )
            recovery_batch_id = supplied_id
        self._validate_acceptance(
            batch, points, storage_response=storage_response
        )
        batch_values = {
            key: batch[key]
            for key in (*BATCH_HASH_FIELDS, "content_hash", "point_templates")
        }
        business_filter = {
            key: batch[key] for key in ("station_id", "acceptance_run_id", "issued_at")
        }
        persisted = self._validate_batch_response(
            self._api.first_or_create(
                "energy_forecast_batches",
                business_filter,
                {**batch_values, "write_state": "writing"},
            ),
            batch,
        )
        batch_id = persisted["id"]
        if recovery_batch_id is not None and batch_id != recovery_batch_id:
            raise M3Error(
                "idempotency_conflict",
                "Acceptance recovery batch primary key mismatch",
            )

        for template in points:
            point = {**template, "batch_id": batch_id}
            returned = self._api.first_or_create(
                "energy_forecast_points",
                {
                    key: point[key]
                    for key in ("batch_id", "unique_id", "data_time")
                },
                point,
            )
            if not isinstance(returned, dict):
                raise M3Error(
                    "sink_contract_invalid", "Acceptance point response is invalid"
                )
            _positive_primary_key(returned, context="point")
            returned_batch_id = returned.get("batch_id")
            if (
                type(returned_batch_id) is not int
                or returned_batch_id < 1
                or returned_batch_id != batch_id
                or not _same_immutable_point(returned, point)
            ):
                raise M3Error(
                    "idempotency_conflict",
                    "Acceptance point immutable fields mismatch",
                )

        stored = self._api.list_records(
            "energy_forecast_points",
            filter={"batch_id": batch_id},
            fields=list(POINT_HASH_FIELDS),
            sort=["unique_id", "data_time"],
        )
        try:
            self._validate_acceptance(batch, stored, storage_response=True)
            stored_hash = acceptance_content_hash(persisted, stored)
        except M3Error as error:
            if error.code == "idempotency_conflict":
                stored_hash = ""
            else:
                raise M3Error(
                    "acceptance_write_incomplete", "Acceptance point verification failed"
                ) from error
        if (
            len(stored) != EXPECTED_DAILY_POINTS
            or stored_hash != batch["content_hash"]
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Acceptance point verification failed"
            )

        completed = self._api.update_record(
            "energy_forecast_batches", batch_id, {"write_state": "complete"}
        )
        completed = self._validate_batch_response(completed, batch)
        if completed["id"] != batch_id:
            raise M3Error(
                "sink_contract_invalid", "Acceptance completion primary key mismatch"
            )
        if completed["write_state"] != "complete":
            raise M3Error(
                "sink_contract_invalid", "Acceptance completion response is invalid"
            )
        return completed

    def reconcile_acceptance(
        self, batch: dict, points: list[dict] | None = None
    ) -> dict[str, Any]:
        if not isinstance(batch, dict) or "point_templates" not in batch:
            raise M3Error(
                "acceptance_write_incomplete", "Acceptance templates are unavailable"
            )
        original_templates = batch["point_templates"]
        if points is not None and acceptance_content_hash(
            batch, points
        ) != acceptance_content_hash(batch, original_templates):
            raise M3Error("idempotency_conflict", "Acceptance reconcile templates mismatch")
        return self._publish_acceptance(
            batch, original_templates, storage_response=True
        )
