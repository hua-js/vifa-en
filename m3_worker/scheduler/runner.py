"""Single-process deterministic scheduling for M3 station operations."""

from collections import OrderedDict
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, current_thread
from typing import Callable
from zoneinfo import ZoneInfo

from m3_worker.contracts import validate_shanghai_timestamp
from m3_worker.errors import M3Error


SHANGHAI = ZoneInfo("Asia/Shanghai")
ALLOWED_MANUAL_TASKS = frozenset({"forecast", "model_selection"})
ALLOWED_SLOTS = frozenset({*ALLOWED_MANUAL_TASKS, "acceptance_baseline"})


def _operation_time(value: datetime) -> datetime:
    try:
        validate_shanghai_timestamp(value, "operation time", quarter_hour=False)
    except (TypeError, ValueError) as error:
        raise M3Error(
            "time_contract_invalid",
            "operation time must be an explicit Asia/Shanghai time",
        ) from error
    return value


def _canonical_minute(value: datetime) -> datetime:
    return _operation_time(value).replace(second=0, microsecond=0)


def _completed_quarter(value: datetime) -> datetime:
    value = _operation_time(value)
    return value.replace(
        minute=(value.minute // 15) * 15,
        second=0,
        microsecond=0,
    )


def due_slots(now: datetime) -> tuple[str, ...]:
    """Return the exact operations due in one explicit Shanghai minute."""

    minute = _canonical_minute(now)
    if (minute.hour, minute.minute) == (0, 30):
        return ("model_selection",)
    if (minute.hour, minute.minute) == (1, 2):
        return ("acceptance_baseline",)
    if minute.minute in (2, 17, 32, 47):
        return ("forecast",)
    return ()


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, M3Error):
        code = error.code
        if (
            isinstance(code, str)
            and 1 <= len(code) <= 64
            and code[0].islower()
            and all(
                character.islower()
                or character.isdigit()
                or character == "_"
                for character in code
            )
        ):
            return code
    return "internal_error"


class SchedulerRunner:
    """Own one bounded scheduler thread and station-scoped operation locks."""

    def __init__(
        self,
        station_ids,
        forecast_service,
        acceptance_service,
        alert_sink: Callable[[str, str, str, datetime], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        poll_seconds: float = 15.0,
        stop_timeout_seconds: float | None = None,
        max_seen: int = 8192,
        acceptance_enabled: bool = True,
    ) -> None:
        stations = tuple(station_ids)
        if (
            not stations
            or any(not isinstance(item, str) or not item for item in stations)
            or len(stations) != len(set(stations))
        ):
            raise ValueError("station_ids must be unique non-empty strings")
        if (
            poll_seconds <= 0
            or (
                stop_timeout_seconds is not None
                and stop_timeout_seconds <= 0
            )
            or max_seen < 1
            or type(acceptance_enabled) is not bool
        ):
            raise ValueError("scheduler bounds must be positive")
        self._station_ids = stations
        self._station_set = frozenset(stations)
        self._forecast = forecast_service
        self._acceptance = acceptance_service
        self._acceptance_enabled = acceptance_enabled
        self._alert_sink = alert_sink or (lambda *_args: None)
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._poll_seconds = poll_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._max_seen = max_seen
        self._seen: OrderedDict[tuple[str, str, datetime], None] = OrderedDict()
        self._seen_lock = Lock()
        self._station_locks = {station_id: Lock() for station_id in stations}
        self._stop = Event()
        self._lifecycle_lock = Lock()
        self._thread: Thread | None = None
        self._lifecycle_state = "new"
        self._healthy = True
        self._last_error_code: str | None = None

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def lifecycle_state(self) -> str:
        with self._lifecycle_lock:
            return self._lifecycle_state

    @property
    def healthy(self) -> bool:
        with self._lifecycle_lock:
            return self._healthy

    @property
    def last_error_code(self) -> str | None:
        with self._lifecycle_lock:
            return self._last_error_code

    def _transition_loop_health(self, healthy: bool, code: str | None) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "running" or self._stop.is_set():
                return
            self._healthy = healthy
            self._last_error_code = code

    def _record_stop_timeout(self) -> None:
        with self._lifecycle_lock:
            self._healthy = False
            self._last_error_code = "scheduler_stop_timeout"

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "new" or self._stop.is_set():
                raise RuntimeError("scheduler already started")
            self._lifecycle_state = "starting"
            thread = Thread(target=self.run, name="m3-scheduler", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._stop.set()
                self._lifecycle_state = "stopped"
                raise
            self._lifecycle_state = "running"

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "new":
                self._stop.set()
                self._lifecycle_state = "stopped"
                return
            if self._lifecycle_state == "stopped":
                return
            self._lifecycle_state = "stopping"
            self._stop.set()
            thread = self._thread
        if thread is None:
            with self._lifecycle_lock:
                self._lifecycle_state = "stopped"
            return
        if thread is current_thread():
            self._record_stop_timeout()
            raise M3Error(
                "scheduler_stop_timeout",
                "Scheduler cannot synchronously stop its own thread",
            )
        thread.join(timeout=self._stop_timeout_seconds)
        if thread.is_alive():
            self._record_stop_timeout()
            raise M3Error(
                "scheduler_stop_timeout",
                "Scheduler did not stop before the configured timeout",
            )
        with self._lifecycle_lock:
            self._lifecycle_state = "stopped"

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick(self._clock())
                except Exception as error:
                    code = _safe_error_code(error)
                    self._transition_loop_health(False, code)
                    fallback = datetime.now(SHANGHAI).replace(
                        second=0, microsecond=0
                    )
                    for station_id in self._station_ids:
                        self._safe_alert(
                            station_id, "scheduler_loop", code, fallback
                        )
                else:
                    self._transition_loop_health(True, None)
                self._stop.wait(self._poll_seconds)
        finally:
            with self._lifecycle_lock:
                self._lifecycle_state = "stopped"

    def _reserve(self, key: tuple[str, str, datetime]) -> bool:
        with self._seen_lock:
            cutoff = key[2] - timedelta(days=2)
            while self._seen:
                oldest = next(iter(self._seen))
                if oldest[2] >= cutoff:
                    break
                self._seen.popitem(last=False)
            if key in self._seen:
                return False
            while len(self._seen) >= self._max_seen:
                self._seen.popitem(last=False)
            self._seen[key] = None
            return True

    def _validate_operation(self, station_id: str, task: str, at: datetime) -> datetime:
        if station_id not in self._station_set:
            raise M3Error("station_not_configured", "Station is not configured")
        if task not in ALLOWED_SLOTS:
            raise M3Error("job_task_invalid", "Manual task is not allowed")
        return _operation_time(at)

    def _operation_body(self, station_id: str, task: str, at: datetime) -> None:
        if task == "forecast":
            self._forecast.run_forecast(station_id, at)
            if self._acceptance_enabled:
                self._acceptance.backfill_actuals(station_id, at)
        elif task == "model_selection":
            self._forecast.bootstrap(station_id, _completed_quarter(at))
            self._forecast.select_models(station_id)
        elif task == "acceptance_baseline":
            self._acceptance.run_baseline(station_id, at)
        else:  # pragma: no cover - protected by _validate_operation
            raise M3Error("job_task_invalid", "Manual task is not allowed")

    def _run_locked(self, station_id: str, task: str, at: datetime) -> None:
        checked_at = self._validate_operation(station_id, task, at)
        with self._station_locks[station_id]:
            self._operation_body(station_id, task, checked_at)

    def run_manual(self, station_id: str, task: str, now: datetime) -> None:
        """Run one allowlisted manual operation through the scheduled operation body."""

        if task not in ALLOWED_MANUAL_TASKS:
            raise M3Error("job_task_invalid", "Manual task is not allowed")
        self._run_locked(station_id, task, now)

    def _safe_alert(self, station_id: str, task: str, code: str, at: datetime) -> None:
        try:
            self._alert_sink(station_id, task, code, at)
        except Exception:
            pass

    def tick(self, now: datetime) -> None:
        """Attempt every due station/slot once for the canonical minute."""

        minute = _canonical_minute(now)
        slots = due_slots(minute)
        for station_id in self._station_ids:
            for task in slots:
                if task == "acceptance_baseline" and not self._acceptance_enabled:
                    continue
                key = (station_id, task, minute)
                if not self._reserve(key):
                    continue
                try:
                    self._run_locked(station_id, task, minute)
                except Exception as error:
                    self._safe_alert(
                        station_id, task, _safe_error_code(error), minute
                    )
