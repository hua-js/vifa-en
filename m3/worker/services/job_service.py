"""Bounded in-process execution and retention for manual M3 jobs."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import BoundedSemaphore, Event, Lock
from typing import Callable
from uuid import uuid4

from m3.worker.contracts import JobState, validate_shanghai_timestamp
from m3.worker.errors import M3Error


ALLOWED_TASKS = frozenset({"forecast", "model_selection"})


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


class JobService:
    """Keep both executor admission and observable terminal history finite."""

    def __init__(
        self,
        run_task: Callable[[str, str, datetime], None],
        now: Callable[[], datetime],
        *,
        station_ids,
        max_workers: int,
        max_pending: int = 16,
        max_retained: int = 256,
    ) -> None:
        stations = tuple(station_ids)
        if (
            not stations
            or any(not isinstance(item, str) or not item for item in stations)
            or len(stations) != len(set(stations))
        ):
            raise ValueError("station_ids must be unique non-empty strings")
        if max_workers < 1 or max_pending < max_workers or max_retained < 1:
            raise ValueError(
                "job bounds require workers >= 1, pending >= workers, retained >= 1"
            )
        self._run_task = run_task
        self._now = now
        self._station_ids = frozenset(stations)
        self._states: dict[str, JobState] = {}
        self._terminal_ids: deque[str] = deque()
        self._max_retained = max_retained
        self._lock = Lock()
        self._capacity = BoundedSemaphore(max_pending)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="m3-manual"
        )
        self._closing = False
        self._closed = False
        self._closed_event = Event()

    @staticmethod
    def _copy(job: JobState) -> JobState:
        return job.model_copy(deep=True)

    def _validate_submission(self, station_id: str, task: str) -> None:
        if station_id not in self._station_ids:
            raise M3Error("station_not_configured", "Station is not configured")
        if task not in ALLOWED_TASKS:
            raise M3Error("job_task_invalid", "Manual task is not allowed")

    def submit(self, station_id: str, task: str) -> JobState:
        self._validate_submission(station_id, task)
        with self._lock:
            if self._closing or self._closed:
                raise M3Error("job_service_closed", "Manual job service is closed")
            if not self._capacity.acquire(blocking=False):
                raise M3Error(
                    "job_capacity_exceeded", "Manual job capacity is exhausted"
                )
            job = JobState(
                job_id=str(uuid4()),
                station_id=station_id,
                task=task,
                status="queued",
                error_code=None,
            )
            self._states[job.job_id] = job
            try:
                self._pool.submit(self._execute, job.job_id)
            except Exception as error:
                self._states.pop(job.job_id, None)
                self._capacity.release()
                raise M3Error(
                    "job_service_closed", "Manual job service is unavailable"
                ) from error
            return self._copy(job)

    def _execute(self, job_id: str) -> None:
        try:
            with self._lock:
                queued = self._states.get(job_id)
                if queued is None:
                    return
                self._states[job_id] = queued.model_copy(
                    update={"status": "running"}
                )
                running = self._states[job_id]
            try:
                at = self._now()
                validate_shanghai_timestamp(at, "job time", quarter_hour=False)
                self._run_task(running.station_id, running.task, at)
            except Exception as error:
                update = {
                    "status": "failed",
                    "error_code": _safe_error_code(error),
                }
            else:
                update = {"status": "succeeded", "error_code": None}
            with self._lock:
                current = self._states.get(job_id)
                if current is not None:
                    self._states[job_id] = current.model_copy(update=update)
                    self._terminal_ids.append(job_id)
                    while len(self._terminal_ids) > self._max_retained:
                        expired = self._terminal_ids.popleft()
                        state = self._states.get(expired)
                        if state is not None and state.status in {
                            "succeeded",
                            "failed",
                        }:
                            self._states.pop(expired, None)
        finally:
            self._capacity.release()

    def get(self, job_id: str) -> JobState | None:
        if not isinstance(job_id, str) or not job_id:
            return None
        with self._lock:
            state = self._states.get(job_id)
            return None if state is None else self._copy(state)

    def close(self) -> None:
        owner = False
        with self._lock:
            if self._closed:
                return
            if not self._closing:
                self._closing = True
                owner = True
        if not owner:
            self._closed_event.wait()
            return
        try:
            self._pool.shutdown(wait=True, cancel_futures=False)
        finally:
            with self._lock:
                self._closed = True
            self._closed_event.set()
