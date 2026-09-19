"""Fixed repository for seven-day acceptance-run summaries."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Literal
from zoneinfo import ZoneInfo

from m3.worker.contracts import SERIES_IDS, validate_shanghai_timestamp
from m3.worker.errors import M3Error
from m3.worker.services.acceptance_batch_window import (
    is_valid_formal_batch_window,
)


CONTROL_STATES = frozenset({"active", "completed", "cancelled"})
RESULT_STATES = frozenset(
    {"pending", "in_progress", "passed", "failed", "insufficient_data"}
)
TERMINAL_RESULTS = frozenset({"passed", "failed", "insufficient_data"})
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
RUN_FIELDS = (
    "id",
    "station_id",
    "acceptance_run_id",
    "window_start",
    "window_end",
    "control_state",
    "completed_days",
    "result_state",
    "calculated_at",
)
RUN_COLLECTION = "energy_forecast_acceptance_runs"
AUDIT_FIELDS = frozenset({"createdAt", "updatedAt", "createdById", "updatedById"})
BATCH_FIELDS = (
    "station_id",
    "acceptance_run_id",
    "issued_at",
    "forecast_start_time",
    "forecast_end_time",
    "write_state",
)
EVALUATION_FIELDS = (
    "station_id",
    "acceptance_run_id",
    "evaluation_key",
    "window_start",
    "window_end",
    "outcome",
    "calculated_at",
)
EVALUATION_KEYS = (*SERIES_IDS, "overall")
EVALUATION_KEY_SET = frozenset(EVALUATION_KEYS)
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AcceptanceRunRecord:
    id: int
    station_id: str
    acceptance_run_id: str
    window_start: datetime
    window_end: datetime
    control_state: Literal["active", "completed", "cancelled"]
    completed_days: int
    result_state: Literal[
        "pending", "in_progress", "passed", "failed", "insufficient_data"
    ]
    calculated_at: datetime | None


def _context_error() -> M3Error:
    return M3Error("acceptance_context_invalid", "Acceptance run record is invalid")


def _summary_error() -> M3Error:
    return M3Error("acceptance_summary_invalid", "Acceptance run summary is invalid")


def _require_active(run: AcceptanceRunRecord) -> None:
    if run.control_state != "active":
        raise M3Error(
            "acceptance_run_update_incomplete",
            "Acceptance run is no longer active",
        )


def _timestamp(
    value: object,
    field_name: str,
    *,
    quarter_hour: bool,
    error_factory: Callable[[], M3Error],
) -> datetime:
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise error_factory() from error
    else:
        raise error_factory()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error_factory()
    parsed = parsed.astimezone(SHANGHAI)
    try:
        validate_shanghai_timestamp(
            parsed, field_name, quarter_hour=quarter_hour
        )
    except (TypeError, ValueError) as error:
        raise error_factory() from error
    return parsed


def _exact_string(value: object) -> str:
    if type(value) is not str:
        raise _context_error()
    return value


def _run_record(raw: object) -> AcceptanceRunRecord:
    if (type(raw) is not dict or not set(RUN_FIELDS).issubset(raw)
            or set(raw) - set(RUN_FIELDS) - AUDIT_FIELDS):
        raise _context_error()
    # Resource updates may return audit metadata even when list queries project fields.
    row: dict[str, object] = {field: raw[field] for field in RUN_FIELDS}
    if type(row["id"]) is not int or row["id"] < 1:
        raise _context_error()
    station_id = _exact_string(row["station_id"])
    acceptance_run_id = _exact_string(row["acceptance_run_id"])
    control_state = _exact_string(row["control_state"])
    if (
        not station_id
        or RUN_ID_PATTERN.fullmatch(acceptance_run_id) is None
        or control_state not in CONTROL_STATES
    ):
        raise _context_error()
    window_start = _timestamp(
        row["window_start"],
        "window_start",
        quarter_hour=True,
        error_factory=_context_error,
    )
    window_end = _timestamp(
        row["window_end"],
        "window_end",
        quarter_hour=True,
        error_factory=_context_error,
    )
    if (
        window_start.hour != 1
        or window_start.minute != 0
        or window_start.second != 0
        or window_start.microsecond != 0
        or window_end.hour != 1
        or window_end.minute != 0
        or window_end.second != 0
        or window_end.microsecond != 0
        or window_end - window_start != timedelta(days=7)
    ):
        raise _context_error()
    if type(row["completed_days"]) is not int or not 0 <= row["completed_days"] <= 7:
        raise _summary_error()
    result_state = row["result_state"]
    if type(result_state) is not str or result_state not in RESULT_STATES:
        raise _summary_error()
    calculated_at_raw = row["calculated_at"]
    calculated_at = (
        None
        if calculated_at_raw is None
        else _timestamp(
            calculated_at_raw,
            "calculated_at",
            quarter_hour=False,
            error_factory=_context_error,
        )
    )
    if (
        result_state == "pending"
        and (row["completed_days"] != 0 or calculated_at is not None)
        or result_state == "in_progress"
        and (not 1 <= row["completed_days"] <= 7 or calculated_at is not None)
        or result_state in TERMINAL_RESULTS
        and (
            row["completed_days"] != 7
            or calculated_at is None
            or calculated_at < window_end
        )
        or control_state == "completed"
        and result_state not in TERMINAL_RESULTS
    ):
        raise _summary_error()
    return AcceptanceRunRecord(
        id=row["id"],
        station_id=station_id,
        acceptance_run_id=acceptance_run_id,
        window_start=window_start,
        window_end=window_end,
        control_state=control_state,
        completed_days=row["completed_days"],
        result_state=result_state,
        calculated_at=calculated_at,
    )


class AcceptanceRunService:
    def __init__(self, api) -> None:
        self._api = api

    def active_run(self, station_id: str) -> AcceptanceRunRecord | None:
        rows = self._api.list_records(
            RUN_COLLECTION,
            filter={"station_id": station_id, "control_state": "active"},
            fields=list(RUN_FIELDS),
        )
        if type(rows) is not list or len(rows) > 1:
            raise M3Error(
                "acceptance_context_invalid",
                "Station has multiple active acceptance runs",
            )
        if not rows:
            return None
        run = _run_record(rows[0])
        if run.station_id != station_id or run.control_state != "active":
            raise _context_error()
        return run

    def _identity_run(
        self, station_id: str, acceptance_run_id: str
    ) -> AcceptanceRunRecord:
        rows = self._api.list_records(
            RUN_COLLECTION,
            filter={
                "station_id": station_id,
                "acceptance_run_id": acceptance_run_id,
            },
            fields=list(RUN_FIELDS),
        )
        if type(rows) is not list or len(rows) != 1:
            raise M3Error(
                "acceptance_context_invalid",
                "Acceptance run identity is unavailable",
            )
        run = _run_record(rows[0])
        if (
            run.station_id != station_id
            or run.acceptance_run_id != acceptance_run_id
        ):
            raise _context_error()
        return run

    def completed_days(self, run: AcceptanceRunRecord) -> int:
        rows = self._api.list_records(
            "energy_forecast_batches",
            filter={
                "station_id": run.station_id,
                "acceptance_run_id": run.acceptance_run_id,
                "write_state": "complete",
            },
            fields=list(BATCH_FIELDS),
        )
        if type(rows) is not list or len(rows) > 7:
            raise _summary_error()
        starts: set[datetime] = set()
        for raw in rows:
            if type(raw) is not dict or set(raw) != set(BATCH_FIELDS):
                raise _summary_error()
            row: dict[str, object] = raw
            if (
                type(row["station_id"]) is not str
                or type(row["acceptance_run_id"]) is not str
                or type(row["write_state"]) is not str
                or row["station_id"] != run.station_id
                or row["acceptance_run_id"] != run.acceptance_run_id
                or row["write_state"] != "complete"
            ):
                raise _summary_error()
            issued_at = _timestamp(
                row["issued_at"],
                "issued_at",
                quarter_hour=False,
                error_factory=_summary_error,
            )
            forecast_start = _timestamp(
                row["forecast_start_time"],
                "forecast_start_time",
                quarter_hour=True,
                error_factory=_summary_error,
            )
            forecast_end = _timestamp(
                row["forecast_end_time"],
                "forecast_end_time",
                quarter_hour=True,
                error_factory=_summary_error,
            )
            if (
                not is_valid_formal_batch_window(
                    issued_at=issued_at,
                    forecast_start=forecast_start,
                    forecast_end=forecast_end,
                    window_start=run.window_start,
                    window_end=run.window_end,
                )
                or forecast_start in starts
            ):
                raise _summary_error()
            starts.add(forecast_start)
        return len(starts)

    def _update_summary(
        self,
        run: AcceptanceRunRecord,
        *,
        completed_days: int,
        result_state: str,
        calculated_at: datetime | None,
    ) -> AcceptanceRunRecord:
        _require_active(run)
        current = self._identity_run(run.station_id, run.acceptance_run_id)
        if (
            current.id != run.id
            or current.window_start != run.window_start
            or current.window_end != run.window_end
            or current.control_state != "active"
        ):
            raise M3Error(
                "acceptance_run_update_incomplete",
                "Acceptance run changed before its summary update",
            )
        values = {
            "completed_days": completed_days,
            "result_state": result_state,
            "calculated_at": (
                None if calculated_at is None else calculated_at.isoformat()
            ),
        }
        response = self._api.update_record(RUN_COLLECTION, current.id, values)
        try:
            updated = _run_record(response)
        except M3Error as error:
            raise M3Error(
                "acceptance_run_update_incomplete",
                "Acceptance run summary update is incomplete",
            ) from error
        if (
            updated.id != current.id
            or updated.station_id != current.station_id
            or updated.acceptance_run_id != current.acceptance_run_id
            or updated.window_start != current.window_start
            or updated.window_end != current.window_end
            or updated.control_state != current.control_state
            or updated.completed_days != completed_days
            or updated.result_state != result_state
            or updated.calculated_at != calculated_at
        ):
            raise M3Error(
                "acceptance_run_update_incomplete",
                "Acceptance run summary update is incomplete",
            )
        return updated

    def sync_progress(
        self, station_id: str, acceptance_run_id: str
    ) -> AcceptanceRunRecord:
        run = self._identity_run(station_id, acceptance_run_id)
        _require_active(run)
        days = self.completed_days(run)
        state = "pending" if days == 0 else "in_progress"
        return self._update_summary(
            run,
            completed_days=days,
            result_state=state,
            calculated_at=None,
        )

    def sync_result(
        self,
        station_id: str,
        acceptance_run_id: str,
        outcome: str,
        calculated_at: datetime,
    ) -> AcceptanceRunRecord:
        run = self._identity_run(station_id, acceptance_run_id)
        _require_active(run)
        if outcome not in TERMINAL_RESULTS or type(calculated_at) is not datetime:
            raise _summary_error()
        try:
            validate_shanghai_timestamp(
                calculated_at, "calculated_at", quarter_hour=False
            )
        except (TypeError, ValueError) as error:
            raise _summary_error() from error
        if calculated_at < run.window_end:
            raise _summary_error()
        days = self.completed_days(run)
        if days != 7:
            raise _summary_error()
        return self._update_summary(
            run,
            completed_days=days,
            result_state=outcome,
            calculated_at=calculated_at,
        )

    def reconcile_active(self, station_id: str) -> AcceptanceRunRecord | None:
        run = self.active_run(station_id)
        if run is None:
            return None
        days = self.completed_days(run)
        rows = self._api.list_records(
            "energy_forecast_evaluations",
            filter={
                "station_id": run.station_id,
                "acceptance_run_id": run.acceptance_run_id,
            },
            fields=list(EVALUATION_FIELDS),
        )
        if type(rows) is not list:
            raise _summary_error()
        if not rows:
            return self._update_summary(
                run,
                completed_days=days,
                result_state="pending" if days == 0 else "in_progress",
                calculated_at=None,
            )
        if len(rows) != len(EVALUATION_KEYS):
            raise _summary_error()
        outcomes: dict[str, str] = {}
        calculated_at: datetime | None = None
        for raw in rows:
            if type(raw) is not dict or set(raw) != set(EVALUATION_FIELDS):
                raise _summary_error()
            row: dict[str, object] = raw
            key = row["evaluation_key"]
            outcome = row["outcome"]
            if (
                type(row["station_id"]) is not str
                or type(row["acceptance_run_id"]) is not str
                or type(key) is not str
                or type(outcome) is not str
                or row["station_id"] != run.station_id
                or row["acceptance_run_id"] != run.acceptance_run_id
                or key not in EVALUATION_KEY_SET
                or key in outcomes
                or outcome not in TERMINAL_RESULTS
            ):
                raise _summary_error()
            window_start = _timestamp(
                row["window_start"],
                "window_start",
                quarter_hour=True,
                error_factory=_summary_error,
            )
            window_end = _timestamp(
                row["window_end"],
                "window_end",
                quarter_hour=True,
                error_factory=_summary_error,
            )
            row_calculated_at = _timestamp(
                row["calculated_at"],
                "calculated_at",
                quarter_hour=False,
                error_factory=_summary_error,
            )
            if (
                window_start != run.window_start
                or window_end != run.window_end
                or row_calculated_at < run.window_end
                or calculated_at is not None
                and row_calculated_at != calculated_at
            ):
                raise _summary_error()
            calculated_at = row_calculated_at
            outcomes[key] = outcome
        if set(outcomes) != EVALUATION_KEY_SET or calculated_at is None:
            raise _summary_error()
        series_outcomes = [outcomes[key] for key in SERIES_IDS]
        expected_overall = (
            "insufficient_data"
            if "insufficient_data" in series_outcomes
            else "failed" if "failed" in series_outcomes else "passed"
        )
        if outcomes["overall"] != expected_overall:
            raise _summary_error()
        return self.sync_result(
            run.station_id,
            run.acceptance_run_id,
            outcomes["overall"],
            calculated_at,
        )
