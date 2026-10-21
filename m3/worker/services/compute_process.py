"""One owned spawn process for pure forecasting calls, never network or writes."""

import builtins
from dataclasses import fields
import logging
import multiprocessing
from threading import Event, Lock

from m3.worker.domain.production_schedule import ACTIVE_SCHEDULE, ProductionSchedule, use_schedule
from m3.worker.errors import M3Error


def _schedule_payload():
    schedule = ACTIVE_SCHEDULE.get()
    if schedule is None:
        return None
    values = {field.name: getattr(schedule, field.name) for field in fields(schedule)}
    for name in ("statuses", "overtime", "planned_hours"):
        values[name] = dict(values[name])
    return values


def _worker(connection):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    logger = logging.getLogger(__name__)
    logger.info("m3_forecast_compute_started pid=%s", multiprocessing.current_process().pid)
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            function, args, kwargs, schedule = request
            try:
                calendar = None if schedule is None else ProductionSchedule(**schedule)
                with use_schedule(calendar):
                    result = function(*args, **kwargs)
                response = ("ok", result)
            except M3Error as error:
                response = ("m3_error", (error.code, error.message, error.details))
            except Exception as error:
                response = ("python_error", (type(error).__name__, str(error)))
            connection.send(response)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class ForecastComputeProcess:
    """Serialize CPU work; never fall back to running it in the API process."""

    def __init__(self, *, stop_event: Event | None = None):
        self._context = multiprocessing.get_context("spawn")
        self._stop_event = stop_event or Event()
        self._call_lock = Lock()
        self._state_lock = Lock()
        self._process = None
        self._connection = None
        self._closed = False
        self._failed = False

    @property
    def healthy(self):
        with self._state_lock:
            return (
                not self._closed and not self._failed
                and not self._stop_event.is_set()
                and (self._process is None or self._process.is_alive())
            )

    def _start(self):
        with self._state_lock:
            if self._closed or self._failed or self._stop_event.is_set():
                raise M3Error("forecast_compute_unavailable", "Forecast computation is unavailable")
            if self._process is None:
                parent, child = self._context.Pipe()
                process = self._context.Process(
                    target=_worker, args=(child,), name="m3-forecast-compute", daemon=True,
                )
                try:
                    process.start()
                except BaseException:
                    self._failed = True
                    parent.close()
                    raise
                finally:
                    child.close()
                self._connection, self._process = parent, process
            if not self._process.is_alive():
                self._failed = True
                raise M3Error("forecast_compute_unavailable", "Forecast computation stopped")
            return self._connection, self._process

    def run(self, function, /, *args, **kwargs):
        with self._call_lock:
            connection, process = self._start()
            try:
                connection.send((function, args, kwargs, _schedule_payload()))
                while True:
                    if self._stop_event.is_set():
                        self.close()
                    if not self.healthy:
                        raise M3Error("forecast_compute_unavailable", "Forecast computation stopped")
                    if connection.poll(0.1):
                        kind, value = connection.recv()
                        break
                    if not process.is_alive():
                        raise EOFError("forecast process exited")
            except (EOFError, OSError, ValueError, TypeError) as error:
                with self._state_lock:
                    self._failed = True
                raise M3Error("forecast_compute_unavailable", "Forecast computation stopped") from error
            if kind == "m3_error":
                raise M3Error(*value)
            if kind == "python_error":
                name, message = value
                error_class = getattr(builtins, name, RuntimeError)
                if not isinstance(error_class, type) or not issubclass(error_class, Exception):
                    error_class = RuntimeError
                raise error_class(message)
            return value

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            process, connection = self._process, self._connection
        idle = self._call_lock.acquire(blocking=False)
        try:
            if process is not None:
                if idle and process.is_alive():
                    try:
                        connection.send(None)
                    except (EOFError, OSError):
                        pass
                    process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            if connection is not None:
                connection.close()
        finally:
            if idle:
                self._call_lock.release()
