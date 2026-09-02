"""Seven-day formal acceptance orchestration over Source and NocoBase HTTP APIs."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from threading import Lock, RLock
from typing import Any

from pydantic import ValidationError

from m3_worker.contracts import (
    AcceptanceContext,
    LatestSnapshot,
    ObservationPoint,
    SERIES_IDS,
    validate_latest_snapshot_origins,
    validate_shanghai_timestamp,
)
from m3_worker.domain.evaluation import MetricResult, evaluate_series, overall_outcome
from m3_worker.errors import M3Error
from m3_worker.json_contract import ExactJsonError, validate_exact_json_mapping
from m3_worker.sinks.forecast_sink import (
    POINT_HASH_FIELDS,
    acceptance_content_hash,
)


SERIES_SET = frozenset(SERIES_IDS)
METRIC_FIELDS = (
    "mape_percent",
    "mae",
    "smape_percent",
    "wape_percent",
    "median_ape_percent",
    "p90_ape_percent",
)
INTERVAL = timedelta(minutes=15)
ACCEPTANCE_DURATION = timedelta(days=7)
EXPECTED_DAILY_POINTS = len(SERIES_IDS) * 96
EXPECTED_SERIES_POINTS = 672
EXPECTED_OVERALL_POINTS = len(SERIES_IDS) * EXPECTED_SERIES_POINTS
ACTUAL_UPDATE_FIELDS = frozenset(
    {
        "actual_value",
        "actual_quality",
        "actual_source_revision",
        "actual_recorded_at",
        "evaluated_at",
        "absolute_percentage_error",
    }
)
BACKFILL_FIELDS = (
    "id",
    "batch_id",
    "unique_id",
    "data_time",
    "forecast_value",
    "actual_source_revision",
)
EVALUATION_FIELDS = (
    "batch_id",
    "unique_id",
    "data_time",
    "actual_value",
    "forecast_value",
    "actual_quality",
)
COMPLETE_BATCH_FIELDS = (
    "id",
    "station_id",
    "acceptance_run_id",
    "issued_at",
    "forecast_start_time",
    "forecast_end_time",
    "write_state",
)
WRITING_BATCH_FIELDS = (
    "id",
    "station_id",
    "acceptance_run_id",
    "issued_at",
    "forecast_start_time",
    "forecast_end_time",
    "status",
    "write_state",
    "model_manifest",
    "content_hash",
    "point_templates",
)
SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class CompleteAcceptanceBatch:
    record_id: int
    issued_at: datetime
    forecast_start: datetime
    forecast_end: datetime


def _safe_time(value: object, name: str, *, quarter_hour: bool) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise M3Error(
                "acceptance_points_incomplete", f"{name} is not a valid timestamp"
            ) from error
    else:
        raise M3Error(
            "acceptance_points_incomplete", f"{name} is not a valid timestamp"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise M3Error(
            "acceptance_points_incomplete", f"{name} must include a timezone"
        )
    local = parsed.astimezone(SHANGHAI)
    if quarter_hour and (local.minute % 15 or local.second or local.microsecond):
        raise M3Error(
            "acceptance_points_incomplete",
            f"{name} must be on a 15-minute boundary",
        )
    return local


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _exact_row(row: object, fields: tuple[str, ...], context: str) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != set(fields):
        raise M3Error(
            "acceptance_points_incomplete", f"{context} row is malformed"
        )
    return row


def _same_time(left: object, right: object, *, quarter_hour: bool) -> bool:
    try:
        return _safe_time(
            left, "response timestamp", quarter_hour=quarter_hour
        ) == _safe_time(right, "expected timestamp", quarter_hour=quarter_hour)
    except M3Error:
        return False


def _same_optional_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    return (
        left_number is not None
        and right_number is not None
        and left_number == right_number
    )


def build_acceptance_points(snapshot: LatestSnapshot) -> list[dict[str, Any]]:
    """Return sorted immutable point templates; mutable actual fields never enter."""

    templates = [
        {
            "unique_id": series.unique_id,
            "data_time": point.data_time.isoformat(),
            "target_time": point.target_time.isoformat(),
            "horizon_step": point.horizon_step,
            "model_name": series.model_name,
            "raw_forecast": point.raw_forecast,
            "forecast_value": point.forecast_value,
            "is_clipped": point.is_clipped,
        }
        for series in snapshot.series
        for point in series.points
    ]
    return sorted(
        templates, key=lambda point: (point["unique_id"], point["data_time"])
    )


class AcceptanceService:
    def __init__(
        self,
        *,
        run_service,
        observation_source,
        api,
        sink,
        forecast_service,
        now,
        evaluator=None,
    ):
        self._runs = run_service
        self._observation_source = observation_source
        self._api = api
        self._sink = sink
        self._forecast_service = forecast_service
        self._now = now
        self._evaluator = evaluator or evaluate_series
        self._lock_guard = Lock()
        self._station_locks: dict[str, RLock] = {}

    def _station_lock(self, station_id: str) -> RLock:
        if not isinstance(station_id, str) or not station_id:
            raise M3Error("station_not_configured", "Station is not configured")
        with self._lock_guard:
            return self._station_locks.setdefault(station_id, RLock())

    @staticmethod
    def _validate_operation_time(as_of: datetime) -> None:
        try:
            validate_shanghai_timestamp(as_of, "as_of", quarter_hour=False)
        except (TypeError, ValueError) as error:
            raise M3Error(
                "time_contract_invalid",
                "as_of must be an explicit Asia/Shanghai time",
            ) from error

    @classmethod
    def _validate_baseline_slot(cls, as_of: datetime) -> None:
        try:
            cls._validate_operation_time(as_of)
        except M3Error as error:
            raise M3Error(
                "acceptance_slot_invalid", "Formal baselines run only at 01:02"
            ) from error
        if (
            as_of.hour != 1
            or as_of.minute != 2
            or as_of.second != 0
            or as_of.microsecond != 0
        ):
            raise M3Error(
                "acceptance_slot_invalid", "Formal baselines run only at 01:02"
            )

    def _context(self, station_id: str, *, require_active: bool) -> AcceptanceContext:
        run = self._runs.active_run(station_id)
        if run is None:
            context = AcceptanceContext(active=False)
        else:
            try:
                context = AcceptanceContext(
                    active=True,
                    acceptance_run_id=run.acceptance_run_id,
                    window_start=run.window_start,
                    window_end=run.window_end,
                )
            except (ValidationError, TypeError, ValueError) as error:
                raise M3Error(
                    "acceptance_context_invalid", "Acceptance context is invalid"
                ) from error
        if not context.active:
            if require_active:
                raise M3Error("acceptance_inactive", "No active acceptance run")
            return context
        if (
            not isinstance(context.acceptance_run_id, str)
            or not context.acceptance_run_id
            or context.window_start is None
            or context.window_end is None
        ):
            raise M3Error(
                "acceptance_context_invalid", "Acceptance context is incomplete"
            )
        if context.window_end - context.window_start != ACCEPTANCE_DURATION:
            raise M3Error(
                "acceptance_window_invalid",
                "Acceptance window must be exactly seven days",
            )
        return context

    @staticmethod
    def _active_window(
        context: AcceptanceContext,
    ) -> tuple[str, datetime, datetime]:
        run_id = context.acceptance_run_id
        start = context.window_start
        end = context.window_end
        if (
            not context.active
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(start, datetime)
            or not isinstance(end, datetime)
            or end - start != ACCEPTANCE_DURATION
        ):
            raise M3Error(
                "acceptance_context_invalid",
                "Active acceptance window invariants are unavailable",
            )
        return run_id, start, end

    def _complete_batches(
        self, station_id: str, context: AcceptanceContext
    ) -> tuple[CompleteAcceptanceBatch, ...]:
        acceptance_run_id, window_start, window_end = self._active_window(context)
        rows = self._api.list_records(
            "energy_forecast_batches",
            filter={
                "station_id": station_id,
                "acceptance_run_id": acceptance_run_id,
                "write_state": "complete",
            },
            fields=list(COMPLETE_BATCH_FIELDS),
            sort=["issued_at"],
        )
        if not isinstance(rows, list):
            raise M3Error(
                "sink_contract_invalid", "Complete acceptance batch listing is invalid"
            )

        batches: list[CompleteAcceptanceBatch] = []
        seen_ids: set[int] = set()
        seen_dates: set[object] = set()
        for raw in rows:
            row = _exact_row(raw, COMPLETE_BATCH_FIELDS, "complete batch")
            record_id = row["id"]
            issued_at = _safe_time(row["issued_at"], "issued_at", quarter_hour=False)
            forecast_start = _safe_time(
                row["forecast_start_time"], "forecast_start_time", quarter_hour=True
            )
            forecast_end = _safe_time(
                row["forecast_end_time"], "forecast_end_time", quarter_hour=True
            )
            if (
                type(record_id) is not int
                or record_id < 1
                or record_id in seen_ids
                or row["station_id"] != station_id
                or row["acceptance_run_id"] != acceptance_run_id
                or row["write_state"] != "complete"
                or forecast_end - forecast_start != timedelta(days=1)
                or forecast_start < window_start
                or forecast_end > window_end
                or forecast_start.date() in seen_dates
                or any(
                    forecast_start < batch.forecast_end
                    and batch.forecast_start < forecast_end
                    for batch in batches
                )
            ):
                raise M3Error(
                    "acceptance_points_incomplete", "Complete acceptance batch is invalid"
                )
            seen_ids.add(record_id)
            seen_dates.add(forecast_start.date())
            batches.append(
                CompleteAcceptanceBatch(
                    record_id=record_id,
                    issued_at=issued_at,
                    forecast_start=forecast_start,
                    forecast_end=forecast_end,
                )
            )
        return tuple(sorted(batches, key=lambda batch: batch.forecast_start))

    def _validate_current_baseline_slot(self, as_of: datetime) -> None:
        current = self._now()
        try:
            validate_shanghai_timestamp(current, "now", quarter_hour=False)
        except (AttributeError, TypeError, ValueError) as error:
            raise M3Error(
                "acceptance_slot_invalid",
                "Worker clock is invalid for the formal baseline slot",
            ) from error
        if current < as_of or current >= as_of + INTERVAL:
            raise M3Error(
                "acceptance_slot_invalid",
                "Formal baseline execution is outside its scheduled slot",
            )

    def _now_iso(self) -> str:
        current = self._now()
        try:
            validate_shanghai_timestamp(current, "now", quarter_hour=False)
        except (AttributeError, TypeError, ValueError) as error:
            raise M3Error(
                "time_contract_invalid", "Worker clock must use Asia/Shanghai"
            ) from error
        return current.isoformat()

    @staticmethod
    def _baseline_start(as_of: datetime) -> datetime:
        return as_of.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _validate_snapshot(
        raw_snapshot: object,
        station_id: str,
        as_of: datetime,
        baseline_start: datetime,
    ) -> LatestSnapshot:
        try:
            validate_latest_snapshot_origins(raw_snapshot)
        except ExactJsonError as error:
            raise M3Error(
                "sink_contract_invalid", "Forecast model manifest is not exact JSON"
            ) from error
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise M3Error(
                "acceptance_write_incomplete", "Forecast snapshot is invalid"
            ) from error
        try:
            payload = (
                raw_snapshot.model_dump(mode="python")
                if type(raw_snapshot) is LatestSnapshot
                else raw_snapshot
            )
            snapshot = LatestSnapshot.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise M3Error(
                "acceptance_write_incomplete", "Forecast snapshot is invalid"
            ) from error
        if (
            snapshot.station_id != station_id
            or snapshot.as_of != as_of
            or snapshot.status not in {"ok", "degraded"}
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Forecast snapshot identity is invalid"
            )
        by_id = {series.unique_id: series for series in snapshot.series}
        if len(snapshot.series) != len(SERIES_IDS) or set(by_id) != SERIES_SET:
            raise M3Error(
                "acceptance_write_incomplete", "Formal baseline requires two series"
            )
        for unique_id in SERIES_IDS:
            series = by_id[unique_id]
            if (
                series.status not in {"ok", "degraded"}
                or len(series.points) != 96
                or not isinstance(series.model_name, str)
                or not series.model_name
            ):
                raise M3Error(
                    "acceptance_write_incomplete",
                    "Formal baseline requires 96 completed points per series",
                )
            for index, point in enumerate(series.points, start=1):
                expected_data_time = baseline_start + INTERVAL * (index - 1)
                if (
                    point.data_time != expected_data_time
                    or point.target_time != expected_data_time + INTERVAL
                    or type(point.horizon_step) is not int
                    or point.horizon_step != index
                    or not math.isfinite(point.raw_forecast)
                    or not math.isfinite(point.forecast_value)
                ):
                    raise M3Error(
                        "acceptance_write_incomplete",
                        "Formal baseline point topology is invalid",
                    )
        return snapshot

    @staticmethod
    def _build_baseline_payload(
        snapshot: LatestSnapshot,
        context: AcceptanceContext,
        station_id: str,
        as_of: datetime,
        baseline_start: datetime,
        baseline_end: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        points = build_acceptance_points(snapshot)
        if len(points) != EXPECTED_DAILY_POINTS or any(
            set(point) != set(POINT_HASH_FIELDS) for point in points
        ):
            raise M3Error(
                "acceptance_write_incomplete", "Formal baseline is incomplete"
            )
        batch: dict[str, Any] = {
            "station_id": station_id,
            "acceptance_run_id": context.acceptance_run_id,
            "issued_at": as_of.isoformat(),
            "forecast_start_time": baseline_start.isoformat(),
            "forecast_end_time": baseline_end.isoformat(),
            "status": snapshot.status,
            "model_manifest": snapshot.model_manifest,
        }
        try:
            validate_exact_json_mapping(
                {**batch, "point_templates": points},
                "formal acceptance payload",
                allow_empty=False,
            )
            content_hash = acceptance_content_hash(batch, points)
            batch["content_hash"] = content_hash
            batch["point_templates"] = points
            validate_exact_json_mapping(
                batch, "formal acceptance payload", allow_empty=False
            )
            if acceptance_content_hash(batch, points) != content_hash:
                raise M3Error(
                    "idempotency_conflict", "Formal baseline hash is unstable"
                )
        except M3Error:
            raise
        except Exception as error:
            raise M3Error(
                "sink_contract_invalid", "Formal acceptance payload is invalid"
            ) from error
        return batch, points

    def run_baseline(
        self, station_id: str, as_of: datetime
    ) -> dict[str, Any] | None:
        """Persist only the exact 01:02 baseline inside an active seven-day run."""

        self._validate_baseline_slot(as_of)
        with self._station_lock(station_id):
            self._validate_current_baseline_slot(as_of)
            context = self._context(station_id, require_active=False)
            if not context.active:
                return None
            acceptance_run_id, window_start, window_end = self._active_window(context)
            baseline_start = self._baseline_start(as_of)
            baseline_end = baseline_start + timedelta(days=1)
            if (
                baseline_start < window_start
                or baseline_end > window_end
            ):
                raise M3Error(
                    "acceptance_window_invalid",
                    "Baseline is outside the active seven-day window",
                )

            snapshot = self._validate_snapshot(
                self._forecast_service.run_forecast(station_id, as_of),
                station_id,
                as_of,
                baseline_start,
            )
            batch, points = self._build_baseline_payload(
                snapshot,
                context,
                station_id,
                as_of,
                baseline_start,
                baseline_end,
            )
            published = self._sink.publish_acceptance(batch, points)
            self._runs.sync_progress(station_id, acceptance_run_id)
            return published

    @staticmethod
    def _completed_boundary(as_of: datetime) -> datetime:
        return as_of.replace(
            minute=(as_of.minute // 15) * 15, second=0, microsecond=0
        )

    def _list_backfill_points(
        self,
        station_id: str,
        context: AcceptanceContext,
        completed_end: datetime,
    ) -> list[dict[str, Any]]:
        acceptance_run_id, window_start, _ = self._active_window(context)
        stored: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, datetime]] = set()
        seen_ids: set[int] = set()
        batches = self._complete_batches(station_id, context)
        for unique_id in SERIES_IDS:
            for batch in batches:
                lower = max(window_start, batch.forecast_start)
                upper = min(completed_end, batch.forecast_end)
                if lower >= upper:
                    continue
                rows = self._api.list_records(
                    "energy_forecast_points",
                    filter={
                        "batch_id": batch.record_id,
                        "unique_id": unique_id,
                        "data_time": {
                            "$gte": lower.isoformat(),
                            "$lt": upper.isoformat(),
                        },
                    },
                    fields=list(BACKFILL_FIELDS),
                    sort=["data_time"],
                )
                if not isinstance(rows, list):
                    raise M3Error(
                        "sink_contract_invalid", "Forecast point listing is invalid"
                    )
                previous: datetime | None = None
                for raw in rows:
                    row = _exact_row(raw, BACKFILL_FIELDS, "backfill")
                    record_id = row["id"]
                    revision = row["actual_source_revision"]
                    data_time = _safe_time(
                        row["data_time"], "data_time", quarter_hour=True
                    )
                    forecast = _finite_number(row["forecast_value"])
                    if (
                        type(record_id) is not int
                        or record_id < 1
                        or record_id in seen_ids
                        or row["batch_id"] != batch.record_id
                        or row["unique_id"] != unique_id
                        or data_time < lower
                        or data_time >= upper
                        or previous is not None
                        and data_time <= previous
                        or revision is not None
                        and (type(revision) is not int or revision < 0)
                        or forecast is None
                        or unique_id == "station_total_load"
                        and forecast < 0
                        or unique_id != "station_total_load"
                        and not 0 <= forecast <= 100
                    ):
                        raise M3Error(
                            "acceptance_points_incomplete",
                            "Persisted backfill point is invalid",
                        )
                    key = (unique_id, data_time)
                    if key in seen_keys:
                        raise M3Error(
                            "acceptance_points_incomplete",
                            "Persisted backfill points are duplicated",
                        )
                    seen_ids.add(record_id)
                    seen_keys.add(key)
                    previous = data_time
                    stored.append(
                        {
                            **row,
                            "data_time_parsed": data_time,
                            "forecast_value_parsed": forecast,
                        }
                    )
        return sorted(
            stored, key=lambda point: (point["unique_id"], point["data_time_parsed"])
        )

    def _pull_actuals(
        self, station_id: str, start: datetime, end: datetime
    ) -> dict[tuple[str, datetime], ObservationPoint]:
        actuals: dict[tuple[str, datetime], ObservationPoint] = {}
        cursor = start
        while cursor < end:
            segment_end = min(cursor + timedelta(days=7), end)
            raw_points = self._observation_source.list_observations(
                station_id, cursor, segment_end
            )
            if not isinstance(raw_points, list):
                raise M3Error(
                    "source_contract_invalid", "Source actual response is invalid"
                )
            for raw in raw_points:
                try:
                    payload = (
                        raw.model_dump(mode="python")
                        if isinstance(raw, ObservationPoint)
                        else raw
                    )
                    if (
                        not isinstance(payload, dict)
                        or type(payload.get("source_revision")) is not int
                    ):
                        raise ValueError("source revision is not exact")
                    point = ObservationPoint.model_validate(payload)
                except (ValidationError, TypeError, ValueError) as error:
                    raise M3Error(
                        "source_contract_invalid", "Source actual point is invalid"
                    ) from error
                if point.ds < cursor or point.ds >= segment_end:
                    raise M3Error(
                        "source_contract_invalid",
                        "Source actual falls outside the requested window",
                    )
                key = (point.unique_id, point.ds)
                if key in actuals:
                    raise M3Error(
                        "source_contract_invalid", "Source actuals are duplicated"
                    )
                actuals[key] = point
            cursor = segment_end
        return actuals

    @staticmethod
    def _validate_actual_update_response(
        response: object,
        stored: dict[str, Any],
        actual: ObservationPoint,
        values: dict[str, Any],
    ) -> None:
        required_fields = {
            "id",
            "unique_id",
            "data_time",
            "forecast_value",
            *ACTUAL_UPDATE_FIELDS,
        }
        if not isinstance(response, dict) or not required_fields.issubset(response):
            raise M3Error(
                "sink_contract_invalid", "Actual update response is invalid"
            )
        if (
            type(response["id"]) is not int
            or response["id"] < 1
            or response["id"] != stored["id"]
            or response["unique_id"] != stored["unique_id"]
            or not _same_time(
                response["data_time"],
                stored["data_time_parsed"],
                quarter_hour=True,
            )
            or not _same_optional_number(
                response["forecast_value"], stored["forecast_value_parsed"]
            )
        ):
            raise M3Error(
                "sink_contract_invalid", "Actual update target identity mismatch"
            )
        if (
            type(response["actual_source_revision"]) is not int
            or response["actual_source_revision"] != actual.source_revision
            or response["actual_source_revision"]
            != values["actual_source_revision"]
        ):
            raise M3Error(
                "sink_contract_invalid", "Actual update revision mismatch"
            )
        if response["actual_quality"] != values["actual_quality"]:
            raise M3Error(
                "sink_contract_invalid", "Actual update quality mismatch"
            )
        if not _same_optional_number(
            response["actual_value"], values["actual_value"]
        ) or not _same_optional_number(
            response["absolute_percentage_error"],
            values["absolute_percentage_error"],
        ):
            raise M3Error(
                "sink_contract_invalid", "Actual update numeric values mismatch"
            )
        if not _same_time(
            response["actual_recorded_at"],
            values["actual_recorded_at"],
            quarter_hour=False,
        ) or not _same_time(
            response["evaluated_at"],
            values["evaluated_at"],
            quarter_hour=False,
        ):
            raise M3Error(
                "sink_contract_invalid", "Actual update timestamps mismatch"
            )

    def _backfill_locked(
        self, station_id: str, as_of: datetime, context: AcceptanceContext
    ) -> int:
        if not context.active:
            return 0
        acceptance_run_id, window_start, window_end = self._active_window(context)
        completed_end = min(self._completed_boundary(as_of), window_end)
        if completed_end <= window_start:
            return 0
        stored = self._list_backfill_points(station_id, context, completed_end)
        if not stored:
            return 0
        pull_start = min(point["data_time_parsed"] for point in stored)
        actuals = self._pull_actuals(station_id, pull_start, completed_end)
        updated = 0
        for point in stored:
            actual = actuals.get((point["unique_id"], point["data_time_parsed"]))
            if actual is None:
                continue
            persisted_revision = point["actual_source_revision"]
            if (
                persisted_revision is not None
                and actual.source_revision <= persisted_revision
            ):
                continue
            recorded_at = self._now_iso()
            actual_value = actual.y
            ape = (
                None
                if actual.quality != "valid"
                or actual_value is None
                or actual_value == 0
                else 100
                * abs(actual_value - point["forecast_value_parsed"])
                / abs(actual_value)
            )
            values = {
                "actual_value": actual_value,
                "actual_quality": actual.quality,
                "actual_source_revision": actual.source_revision,
                "actual_recorded_at": recorded_at,
                "evaluated_at": recorded_at,
                "absolute_percentage_error": ape,
            }
            if set(values) != ACTUAL_UPDATE_FIELDS:
                raise M3Error(
                    "sink_contract_invalid", "Actual update field set is invalid"
                )
            response = self._api.update_record(
                "energy_forecast_points", point["id"], values
            )
            self._validate_actual_update_response(response, point, actual, values)
            updated += 1
        if updated and completed_end >= window_end:
            self._recalculate_locked(
                station_id, acceptance_run_id, context
            )
        return updated

    def backfill_actuals(self, station_id: str, as_of: datetime) -> int:
        """Fill only completed formal points with absent or higher-revision actuals."""

        self._validate_operation_time(as_of)
        with self._station_lock(station_id):
            context = self._context(station_id, require_active=False)
            return self._backfill_locked(station_id, as_of, context)

    @staticmethod
    def _validate_metric(metric: object) -> MetricResult:
        if not isinstance(metric, MetricResult):
            raise M3Error(
                "acceptance_evaluation_invalid", "Evaluator result is invalid"
            )
        numeric = tuple(getattr(metric, field) for field in METRIC_FIELDS)
        if (
            type(metric.expected_count) is not int
            or metric.expected_count != EXPECTED_SERIES_POINTS
            or type(metric.valid_count) is not int
            or not 0 <= metric.valid_count <= EXPECTED_SERIES_POINTS
            or type(metric.zero_actual_count) is not int
            or not 0 <= metric.zero_actual_count <= EXPECTED_SERIES_POINTS
            or metric.valid_count + metric.zero_actual_count > EXPECTED_SERIES_POINTS
            or metric.outcome not in {"passed", "failed", "insufficient_data"}
            or any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                )
                for value in numeric
            )
        ):
            raise M3Error(
                "acceptance_evaluation_invalid", "Evaluator result is invalid"
            )
        return metric

    def _series_evaluation(
        self,
        station_id: str,
        acceptance_run_id: str,
        unique_id: str,
        context: AcceptanceContext,
    ) -> tuple[MetricResult, dict[str, Any]]:
        _, window_start, window_end = self._active_window(context)
        rows: list[dict[str, Any]] = []
        previous: datetime | None = None
        for batch in self._complete_batches(station_id, context):
            lower = max(window_start, batch.forecast_start)
            upper = min(window_end, batch.forecast_end)
            batch_rows = self._api.list_records(
                "energy_forecast_points",
                filter={
                    "batch_id": batch.record_id,
                    "unique_id": unique_id,
                    "data_time": {
                        "$gte": lower.isoformat(),
                        "$lt": upper.isoformat(),
                    },
                },
                fields=list(EVALUATION_FIELDS),
                sort=["data_time"],
            )
            if not isinstance(batch_rows, list):
                raise M3Error(
                    "sink_contract_invalid", "Forecast point listing is invalid"
                )
            for raw in batch_rows:
                row = _exact_row(raw, EVALUATION_FIELDS, "evaluation")
                data_time = _safe_time(
                    row["data_time"], "data_time", quarter_hour=True
                )
                if (
                    row["batch_id"] != batch.record_id
                    or data_time < lower
                    or data_time >= upper
                ):
                    raise M3Error(
                        "acceptance_points_incomplete",
                        "Acceptance point batch identity is invalid",
                    )
                if previous is not None and data_time <= previous:
                    raise M3Error(
                        "acceptance_points_incomplete",
                        "Acceptance point ordering is invalid",
                    )
                previous = data_time
                rows.append(row)
        if len(rows) != EXPECTED_SERIES_POINTS:
            raise M3Error(
                "acceptance_points_incomplete",
                "Acceptance requires exactly 672 points per series",
            )
        actual_values: list[float | None] = []
        forecast_values: list[float] = []
        qualities: list[str | None] = []
        for index, raw in enumerate(rows):
            row = _exact_row(raw, EVALUATION_FIELDS, "evaluation")
            data_time = _safe_time(row["data_time"], "data_time", quarter_hour=True)
            expected_time = window_start + INTERVAL * index
            forecast = _finite_number(row["forecast_value"])
            quality = row["actual_quality"]
            actual = row["actual_value"]
            if (
                row["unique_id"] != unique_id
                or data_time != expected_time
                or data_time >= window_end
                or forecast is None
                or unique_id == "station_total_load"
                and forecast < 0
                or unique_id != "station_total_load"
                and not 0 <= forecast <= 100
                or quality not in {None, "valid", "invalid"}
            ):
                raise M3Error(
                    "acceptance_points_incomplete",
                    "Acceptance point topology or value is invalid",
                )
            if quality == "valid":
                actual_number = _finite_number(actual)
                if actual_number is None:
                    raise M3Error(
                        "acceptance_points_incomplete", "Valid actual is invalid"
                    )
                if (
                    unique_id == "station_total_load"
                    and actual_number < 0
                    or unique_id != "station_total_load"
                    and not 0 <= actual_number <= 100
                ):
                    raise M3Error(
                        "acceptance_points_incomplete",
                        "Actual value is outside its physical bounds",
                    )
                actual = actual_number
            elif actual is not None:
                raise M3Error(
                    "acceptance_points_incomplete",
                    "Unscored actual must remain null",
                )
            actual_values.append(actual)
            forecast_values.append(forecast)
            qualities.append(quality)
        try:
            metric = self._validate_metric(
                self._evaluator(actual_values, forecast_values, qualities)
            )
        except M3Error:
            raise
        except Exception as error:
            raise M3Error(
                "acceptance_evaluation_invalid", "Acceptance evaluation failed"
            ) from error
        return metric, {
            "station_id": station_id,
            "acceptance_run_id": acceptance_run_id,
            "evaluation_key": unique_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "expected_count": EXPECTED_SERIES_POINTS,
            "valid_count": metric.valid_count,
            "zero_actual_count": metric.zero_actual_count,
            **{field: getattr(metric, field) for field in METRIC_FIELDS},
            "outcome": metric.outcome,
        }

    @staticmethod
    def _validate_evaluation_response(
        response: object, expected: dict[str, Any]
    ) -> None:
        if not isinstance(response, dict) or not {
            "id",
            *expected,
        }.issubset(response):
            raise M3Error(
                "sink_contract_invalid", "Evaluation upsert response is incomplete"
            )
        if type(response["id"]) is not int or response["id"] < 1:
            raise M3Error(
                "sink_contract_invalid", "Evaluation upsert primary key is invalid"
            )
        for field in (
            "station_id",
            "acceptance_run_id",
            "evaluation_key",
            "outcome",
        ):
            if response[field] != expected[field]:
                raise M3Error(
                    "sink_contract_invalid", "Evaluation upsert identity mismatch"
                )
        for field in ("window_start", "window_end", "calculated_at"):
            if not _same_time(
                response[field], expected[field], quarter_hour=field != "calculated_at"
            ):
                raise M3Error(
                    "sink_contract_invalid", "Evaluation upsert timestamp mismatch"
                )
        for field in ("expected_count", "valid_count", "zero_actual_count"):
            if type(response[field]) is not int or response[field] != expected[field]:
                raise M3Error(
                    "sink_contract_invalid", "Evaluation upsert count mismatch"
                )
        for field in METRIC_FIELDS:
            if not _same_optional_number(response[field], expected[field]):
                raise M3Error(
                    "sink_contract_invalid", "Evaluation upsert metric mismatch"
                )

    def _recalculate_locked(
        self,
        station_id: str,
        acceptance_run_id: str | None,
        context: AcceptanceContext,
    ) -> dict[str, dict[str, Any]]:
        if (
            not isinstance(acceptance_run_id, str)
            or not acceptance_run_id
            or context.acceptance_run_id != acceptance_run_id
        ):
            raise M3Error(
                "acceptance_context_invalid", "Acceptance run identity is invalid"
            )
        metrics: list[MetricResult] = []
        results: dict[str, dict[str, Any]] = {}
        for unique_id in SERIES_IDS:
            metric, values = self._series_evaluation(
                station_id, acceptance_run_id, unique_id, context
            )
            metrics.append(metric)
            results[unique_id] = values

        calculated_at = self._now_iso()
        for unique_id in SERIES_IDS:
            results[unique_id]["calculated_at"] = calculated_at
        _, window_start, window_end = self._active_window(context)
        overall = {
            "station_id": station_id,
            "acceptance_run_id": acceptance_run_id,
            "evaluation_key": "overall",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "expected_count": EXPECTED_OVERALL_POINTS,
            "valid_count": sum(metric.valid_count for metric in metrics),
            "zero_actual_count": sum(
                metric.zero_actual_count for metric in metrics
            ),
            **dict.fromkeys(METRIC_FIELDS),
            "outcome": overall_outcome(metrics),
            "calculated_at": calculated_at,
        }
        for key in (*SERIES_IDS, "overall"):
            values = overall if key == "overall" else results[key]
            response = self._api.update_or_create(
                "energy_forecast_evaluations",
                {
                    "station_id": station_id,
                    "acceptance_run_id": acceptance_run_id,
                    "evaluation_key": key,
                },
                values,
            )
            self._validate_evaluation_response(response, values)
        self._runs.sync_result(
            station_id,
            acceptance_run_id,
            overall["outcome"],
            datetime.fromisoformat(calculated_at),
        )
        results["overall"] = overall
        return results

    def recalculate(
        self, station_id: str, acceptance_run_id: str
    ) -> dict[str, dict[str, Any]]:
        """Recalculate exact seven-day metrics from completed formal points only."""

        with self._station_lock(station_id):
            context = self._context(station_id, require_active=True)
            return self._recalculate_locked(station_id, acceptance_run_id, context)

    def reconcile_writing_batches(self, station_id: str) -> int:
        """Resume one station's writing batches without rerunning its model."""

        batches = self._api.list_records(
            "energy_forecast_batches",
            filter={"station_id": station_id, "write_state": "writing"},
            fields=list(WRITING_BATCH_FIELDS),
            sort=["issued_at"],
        )
        if not isinstance(batches, list):
            raise M3Error(
                "sink_contract_invalid", "Writing batch listing is invalid"
            )
        recovered = 0
        for raw in batches:
            batch = _exact_row(raw, WRITING_BATCH_FIELDS, "writing batch")
            if (
                type(batch["id"]) is not int
                or batch["id"] < 1
                or not isinstance(batch["station_id"], str)
                or batch["station_id"] != station_id
                or not isinstance(batch["acceptance_run_id"], str)
                or not batch["acceptance_run_id"]
                or batch["status"] not in {"ok", "degraded"}
                or batch["write_state"] != "writing"
                or not isinstance(batch["model_manifest"], dict)
                or not isinstance(batch["point_templates"], list)
                or len(batch["point_templates"]) != EXPECTED_DAILY_POINTS
            ):
                raise M3Error(
                    "acceptance_write_incomplete",
                    "Writing batch recovery payload is incomplete",
                )
            issued_at = _safe_time(
                batch["issued_at"], "issued_at", quarter_hour=False
            )
            start = _safe_time(
                batch["forecast_start_time"],
                "forecast_start_time",
                quarter_hour=True,
            )
            end = _safe_time(
                batch["forecast_end_time"],
                "forecast_end_time",
                quarter_hour=True,
            )
            if (
                start.hour != 1
                or start.minute != 0
                or start.second != 0
                or start.microsecond != 0
                or issued_at != start + timedelta(minutes=2)
                or end - start != timedelta(days=1)
            ):
                raise M3Error(
                    "acceptance_write_incomplete",
                    "Writing batch recovery window is invalid",
                )
            by_series: dict[str, list[tuple[datetime, dict[str, Any]]]] = {
                unique_id: [] for unique_id in SERIES_IDS
            }
            seen: set[tuple[str, datetime]] = set()
            for raw_point in batch["point_templates"]:
                try:
                    point = _exact_row(
                        raw_point, POINT_HASH_FIELDS, "writing batch template"
                    )
                except M3Error as error:
                    raise M3Error(
                        "acceptance_write_incomplete",
                        "Writing batch recovery template is invalid",
                    ) from error
                unique_id = point["unique_id"]
                data_time = _safe_time(
                    point["data_time"], "data_time", quarter_hour=True
                )
                target_time = _safe_time(
                    point["target_time"], "target_time", quarter_hour=True
                )
                raw_value = _finite_number(point["raw_forecast"])
                published = _finite_number(point["forecast_value"])
                key = (unique_id, data_time)
                if (
                    unique_id not in SERIES_SET
                    or key in seen
                    or type(point["horizon_step"]) is not int
                    or not 1 <= point["horizon_step"] <= 96
                    or not isinstance(point["model_name"], str)
                    or not point["model_name"]
                    or raw_value is None
                    or published is None
                    or type(point["is_clipped"]) is not bool
                    or point["is_clipped"] != (raw_value != published)
                    or target_time - data_time != INTERVAL
                    or unique_id == "station_total_load"
                    and published < 0
                    or unique_id != "station_total_load"
                    and not 0 <= published <= 100
                ):
                    raise M3Error(
                        "acceptance_write_incomplete",
                        "Writing batch recovery template is invalid",
                    )
                seen.add(key)
                by_series[unique_id].append((data_time, point))
            for points in by_series.values():
                points.sort(key=lambda item: item[0])
                if len(points) != 96 or any(
                    data_time != start + INTERVAL * (index - 1)
                    or point["horizon_step"] != index
                    for index, (data_time, point) in enumerate(points, start=1)
                ):
                    raise M3Error(
                        "acceptance_write_incomplete",
                        "Writing batch recovery topology is invalid",
                    )
            try:
                expected_hash = acceptance_content_hash(
                    batch, batch["point_templates"]
                )
            except M3Error as error:
                raise M3Error(
                    "acceptance_write_incomplete",
                    "Writing batch recovery payload is incomplete",
                ) from error
            if (
                not isinstance(batch["content_hash"], str)
                or batch["content_hash"] != expected_hash
            ):
                raise M3Error(
                    "idempotency_conflict", "Writing batch content hash mismatch"
                )
            with self._station_lock(station_id):
                response = self._sink.reconcile_acceptance(batch)
                if (
                    not isinstance(response, dict)
                    or type(response.get("id")) is not int
                    or response["id"] < 1
                    or response.get("id") != batch["id"]
                    or response.get("station_id") != batch["station_id"]
                    or response.get("acceptance_run_id")
                    != batch["acceptance_run_id"]
                    or response.get("content_hash") != batch["content_hash"]
                    or response.get("write_state") != "complete"
                ):
                    raise M3Error(
                        "sink_contract_invalid",
                        "Acceptance reconciliation response identity mismatch",
                    )
                recovered += 1
        return recovered

    def reconcile_run_summary(self, station_id: str) -> bool:
        with self._station_lock(station_id):
            return self._runs.reconcile_active(station_id) is not None
