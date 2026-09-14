"""Automatic local plan generation; no device dispatch or browser dependency."""
from datetime import datetime, timezone
import fcntl
import json
import logging
from pathlib import Path
from threading import Event, Thread
import time

LOG = logging.getLogger(__name__)


class AutomaticPlans:
    interval_seconds = 900

    def __init__(self, service, root, *, enabled=True):
        self.service, self.root, self.enabled = service, Path(root), enabled
        self.stopped = Event()
        self.thread = None

    def tick(self, now=None):
        if not self.enabled:
            return
        now = time.time() if now is None else now
        window = int(now // self.interval_seconds)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.automatic.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            path = self.root / '.automatic.json'
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(state, dict):
                    raise ValueError('Invalid automatic plan state')
            except (ValueError, OSError):
                LOG.exception('Cannot read automatic plan state; skipping this check')
                return
            for station in ('station-1', 'station-2'):
                if state.get(station) == window or station in self.service.running:
                    continue
                # Persist the claim under the cross-process lock before dispatch.
                state[station] = window
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(state))
                temporary.replace(path)
                try:
                    self.service.start(station)
                except Exception:
                    LOG.exception('Automatic plan start failed for %s', station)

    def start(self):
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stopped.clear()
        self.thread = Thread(target=self._loop, name='m4-automatic-plans', daemon=True)
        self.thread.start()

    def _loop(self):
        # Hold leadership across windows, including long-running jobs.
        while not self.stopped.is_set():
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with (self.root / '.automatic-leader.lock').open('a') as leader:
                    try:
                        fcntl.flock(leader, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.stopped.wait(15)
                        continue
                    while not self.stopped.is_set():
                        try:
                            self.tick()
                        except Exception:
                            LOG.exception('Automatic plan check failed')
                        self.stopped.wait(15)
            except Exception:
                LOG.exception('Automatic plan leadership failed')
                self.stopped.wait(15)

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)

    def metadata(self):
        return {'enabled': self.enabled, 'interval_minutes': 15,
                'next_check_at': datetime.fromtimestamp(
                    (int(time.time() // self.interval_seconds) + 1) * self.interval_seconds,
                    timezone.utc).isoformat() if self.enabled else None,
                'usage': 'daily_admission_and_remaining_day_advice', 'dispatch_status': 'not_dispatched'}
