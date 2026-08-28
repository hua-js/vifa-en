"""Assemble the browser dashboard from persisted forecasts and read-only HTTP data."""

from datetime import datetime, timedelta
import math
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from m3_worker.config import StationBinding
from m3_worker.contracts import LatestSnapshot, ObservationPoint, SERIES_IDS
from m3_worker.dashboard_contracts import (
    DashboardAcceptance,
    DashboardAcceptanceResult,
    DashboardEnvelope,
    DashboardReadiness,
)
from m3_worker.domain.training_data import (
    POINTS_PER_DAY,
    READY_HISTORY_DAYS,
    READY_REQUIRED_POINTS,
)
from m3_worker.errors import M3Error
from m3_worker.services.live_dashboard_service import (
    DashboardCache,
    DashboardStationResult,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
LATEST_FIELDS = (
    "station_id",
    "as_of",
    "generated_at",
    "source_data_end",
    "status",
    "series_payload",
    "model_manifest",
    "content_hash",
)
BATCH_FIELDS = (
    "station_id",
    "acceptance_run_id",
    "issued_at",
    "forecast_start_time",
    "write_state",
)
METRIC_FIELDS = (
    "mape_percent",
    "mae",
    "smape_percent",
    "wape_percent",
    "median_ape_percent",
    "p90_ape_percent",
)
EVALUATION_FIELDS = (
    "station_id",
    "acceptance_run_id",
    "evaluation_key",
    "expected_count",
    "valid_count",
    "zero_actual_count",
    *METRIC_FIELDS,
    "outcome",
)


def _contract_error(message: str) -> M3Error:
    return M3Error("dashboard_contract_invalid", message)


def _exact_row(value: object, fields: tuple[str, ...], context: str) -> dict:
    if type(value) is not dict or set(value) != set(fields):
        raise _contract_error(f"Persisted {context} row is malformed")
    return value


def _timestamp(value: object, field_name: str, *, quarter_hour: bool) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise _contract_error(f"Persisted {field_name} is invalid") from error
    else:
        raise _contract_error(f"Persisted {field_name} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _contract_error(f"Persisted {field_name} must include a timezone")
    local = parsed.astimezone(SHANGHAI)
    if quarter_hour and (local.minute % 15 or local.second or local.microsecond):
        raise _contract_error(
            f"Persisted {field_name} must use a 15-minute boundary"
        )
    return local


def _safe_identity(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _contract_error(f"Persisted {field_name} is invalid")
    return value


def _metric(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _contract_error(f"Persisted {field_name} is invalid")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise _contract_error(f"Persisted {field_name} is invalid")
    return converted


def _readiness(model_manifest: dict[str, object]) -> DashboardReadiness | None:
    if "readiness" not in model_manifest:
        return None
    value = model_manifest["readiness"]
    if type(value) is not dict or set(value) != {
        "required_days",
        "required_points",
        "series",
    }:
        raise _contract_error("Persisted readiness manifest is invalid")
    if (
        type(value["required_days"]) is not int
        or value["required_days"] != READY_HISTORY_DAYS
        or type(value["required_points"]) is not int
        or value["required_points"] != READY_REQUIRED_POINTS
    ):
        raise _contract_error("Persisted readiness manifest is invalid")
    series = value["series"]
    if type(series) is not dict or set(series) != set(SERIES_IDS):
        raise _contract_error("Persisted readiness series are invalid")
    real_points = []
    for unique_id in SERIES_IDS:
        item = series[unique_id]
        if (
            type(item) is not dict
            or set(item) != {"real_points"}
            or type(item["real_points"]) is not int
            or not 0 <= item["real_points"] <= READY_REQUIRED_POINTS
        ):
            raise _contract_error("Persisted readiness series are invalid")
        real_points.append(item["real_points"])
    available_days = round(min(real_points) / POINTS_PER_DAY, 1)
    remaining_days = round(max(0.0, READY_HISTORY_DAYS - available_days), 1)
    try:
        return DashboardReadiness(
            required_days=READY_HISTORY_DAYS,
            available_days=available_days,
            remaining_days=remaining_days,
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise _contract_error("Persisted readiness progress is invalid") from error


class PersistedDashboardService:
    """Read forecasts/evaluations and current actuals without running a model."""

    def __init__(
        self,
        *,
        source,
        api,
        bindings: tuple[StationBinding, StationBinding],
        clock: Callable[[], datetime],
    ) -> None:
        if [binding.station_key for binding in bindings] != [
            "station_1",
            "station_2",
        ]:
            raise ValueError("persisted dashboard requires two ordered stations")
        self._source = source
        self._api = api
        self._bindings = bindings
        self._clock = clock

    def _latest(self, binding: StationBinding) -> LatestSnapshot | None:
        rows = self._api.list_records(
            "energy_forecast_latest",
            filter={"station_id": binding.station_id},
            fields=list(LATEST_FIELDS),
        )
        if not isinstance(rows, list) or len(rows) > 1:
            raise _contract_error("Persisted latest forecast listing is invalid")
        if not rows:
            return None
        row = _exact_row(rows[0], LATEST_FIELDS, "latest forecast")
        if row["station_id"] != binding.station_id:
            raise _contract_error("Persisted latest forecast station mismatch")
        values = {
            "station_id": row["station_id"],
            "as_of": _timestamp(row["as_of"], "as_of", quarter_hour=False),
            "generated_at": _timestamp(
                row["generated_at"], "generated_at", quarter_hour=False
            ),
            "source_data_end": _timestamp(
                row["source_data_end"], "source_data_end", quarter_hour=True
            ),
            "status": row["status"],
            "series": row["series_payload"],
            "model_manifest": row["model_manifest"],
            "content_hash": row["content_hash"],
        }
        try:
            snapshot = LatestSnapshot.model_validate(values)
        except (ValidationError, TypeError, ValueError) as error:
            raise _contract_error("Persisted latest forecast payload is invalid") from error
        for series in snapshot.series:
            if series.points and series.points[0].data_time != snapshot.source_data_end:
                raise _contract_error("Persisted forecast start is misaligned")
        return snapshot

    def _acceptance(self, binding: StationBinding) -> DashboardAcceptance | None:
        rows = self._api.list_records(
            "energy_forecast_batches",
            filter={"station_id": binding.station_id, "write_state": "complete"},
            fields=list(BATCH_FIELDS),
            sort=["-issued_at"],
        )
        if not isinstance(rows, list):
            raise _contract_error("Persisted acceptance batch listing is invalid")
        if not rows:
            return None

        normalized: list[tuple[dict, datetime, datetime]] = []
        for value in rows:
            row = _exact_row(value, BATCH_FIELDS, "acceptance batch")
            station_id = _safe_identity(row["station_id"], "batch station_id")
            run_id = _safe_identity(
                row["acceptance_run_id"], "acceptance_run_id"
            )
            if station_id != binding.station_id or row["write_state"] != "complete":
                raise _contract_error("Persisted acceptance batch identity is invalid")
            normalized.append((
                {**row, "acceptance_run_id": run_id},
                _timestamp(row["issued_at"], "issued_at", quarter_hour=False),
                _timestamp(
                    row["forecast_start_time"],
                    "forecast_start_time",
                    quarter_hour=True,
                ),
            ))
        if any(
            current[1] > previous[1]
            for previous, current in zip(normalized, normalized[1:])
        ):
            raise _contract_error("Persisted acceptance batches are not newest first")

        run_id = normalized[0][0]["acceptance_run_id"]
        run_starts = {
            forecast_start
            for row, _, forecast_start in normalized
            if row["acceptance_run_id"] == run_id
        }
        completed_days = min(len(run_starts), 7)
        if completed_days < 1:
            raise _contract_error("Persisted acceptance progress is invalid")
        if completed_days < 7:
            return DashboardAcceptance(
                acceptance_run_id=run_id,
                status="in_progress",
                completed_days=completed_days,
                results=[],
            )

        evaluations = self._api.list_records(
            "energy_forecast_evaluations",
            filter={
                "station_id": binding.station_id,
                "acceptance_run_id": run_id,
            },
            fields=list(EVALUATION_FIELDS),
        )
        if not isinstance(evaluations, list):
            raise _contract_error("Persisted evaluation listing is invalid")
        if not evaluations:
            return DashboardAcceptance(
                acceptance_run_id=run_id,
                status="in_progress",
                completed_days=7,
                results=[],
            )

        by_key: dict[str, dict] = {}
        for value in evaluations:
            row = _exact_row(value, EVALUATION_FIELDS, "evaluation")
            if (
                row["station_id"] != binding.station_id
                or row["acceptance_run_id"] != run_id
            ):
                raise _contract_error("Persisted evaluation identity is invalid")
            key = row["evaluation_key"]
            if key not in {*SERIES_IDS, "overall"} or key in by_key:
                raise _contract_error("Persisted evaluation key is invalid")
            by_key[key] = row

        overall = by_key.get("overall")
        if overall is None or overall["outcome"] == "in_progress":
            return DashboardAcceptance(
                acceptance_run_id=run_id,
                status="in_progress",
                completed_days=7,
                results=[],
            )
        if set(by_key) != {*SERIES_IDS, "overall"}:
            raise _contract_error("Persisted final evaluations are incomplete")
        if overall["outcome"] not in {"passed", "failed", "insufficient_data"}:
            raise _contract_error("Persisted overall outcome is invalid")
        if any(overall[field] is not None for field in METRIC_FIELDS):
            raise _contract_error("Persisted overall metrics must be null")
        if (
            type(overall["expected_count"]) is not int
            or overall["expected_count"] != 1344
            or type(overall["valid_count"]) is not int
            or type(overall["zero_actual_count"]) is not int
        ):
            raise _contract_error("Persisted overall counts are invalid")

        results = []
        outcomes = []
        for unique_id in SERIES_IDS:
            row = by_key[unique_id]
            if (
                type(row["expected_count"]) is not int
                or row["expected_count"] != 672
                or type(row["valid_count"]) is not int
                or type(row["zero_actual_count"]) is not int
                or row["outcome"] not in {"passed", "failed", "insufficient_data"}
            ):
                raise _contract_error("Persisted series evaluation is invalid")
            metrics = {field: _metric(row[field], field) for field in METRIC_FIELDS}
            try:
                result = DashboardAcceptanceResult(
                    unique_id=unique_id,
                    expected_count=672,
                    valid_count=row["valid_count"],
                    zero_actual_count=row["zero_actual_count"],
                    outcome=row["outcome"],
                    **metrics,
                )
            except (ValidationError, TypeError, ValueError) as error:
                raise _contract_error(
                    "Persisted series evaluation is invalid"
                ) from error
            outcomes.append(result.outcome)
            results.append(result)
        expected_overall = (
            "insufficient_data"
            if "insufficient_data" in outcomes
            else "failed"
            if "failed" in outcomes
            else "passed"
        )
        if overall["outcome"] != expected_overall:
            raise _contract_error("Persisted overall outcome is inconsistent")
        return DashboardAcceptance(
            acceptance_run_id=run_id,
            status=overall["outcome"],
            completed_days=7,
            results=results,
        )

    def _station(self, binding: StationBinding) -> DashboardStationResult | None:
        snapshot = self._latest(binding)
        if snapshot is None:
            return None
        readiness = _readiness(snapshot.model_manifest)
        acceptance = self._acceptance(binding)
        forecast_start = snapshot.source_data_end
        degraded = False
        try:
            actual = self._source.list_observations(
                binding.station_id,
                forecast_start - timedelta(hours=24),
                forecast_start,
            )
            if not isinstance(actual, list) or any(
                not isinstance(point, ObservationPoint) for point in actual
            ):
                raise M3Error(
                    "source_contract_invalid", "Raw source observations are invalid"
                )
        except M3Error:
            actual = []
            degraded = True
        return DashboardStationResult(
            binding=binding,
            as_of=forecast_start,
            generated_at=snapshot.generated_at,
            actual=actual,
            forecasts=snapshot.series,
            acceptance=acceptance,
            readiness=readiness,
            degraded=degraded,
        )

    def build(self) -> DashboardEnvelope:
        """Build stations independently; fail only when both persisted reads fail."""

        now = self._clock()
        cache = DashboardCache(self._bindings)
        failures: list[M3Error] = []
        for binding in self._bindings:
            try:
                result = self._station(binding)
                if result is not None:
                    cache.publish_station(result)
            except M3Error as error:
                failures.append(error)
                cache.mark_station_failure(binding)
        if len(failures) == len(self._bindings):
            if any(error.code == "dashboard_contract_invalid" for error in failures):
                raise _contract_error("Both persisted station payloads are invalid")
            raise failures[0]
        return cache.snapshot(now)
