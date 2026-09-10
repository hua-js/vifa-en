"""One explicit manual operation at a time; no scheduler or automatic replay."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import threading
import uuid

from m3.worker.services.pv_hourly import atomic_json


def now():
    return datetime.now(timezone.utc).isoformat()


class ManualJobs:
    def __init__(self, root: Path, operation):
        self.root = root
        self.operation = operation
        root.mkdir(parents=True, exist_ok=True)
        self._file = (root/'service.lock').open('a+')
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._file.close()
            raise
        try:
            self._mutex = threading.RLock()
            self._latest = json.loads((root/'latest.json').read_text()) if (root/'latest.json').exists() else None
            if self._latest and self._latest['status'] in ('queued', 'running'):
                self._latest.update(status='interrupted', finished_at=now(), error_code='manual_review_required')
                self._save()
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pv-manual')
        except BaseException:
            self._file.close()
            raise

    def _save(self):
        atomic_json(self.root/'latest.json', self._latest)
        directory = self.root/'jobs'/self._latest['job_id']
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory/'job.json', self._latest)

    def latest(self):
        with self._mutex:
            return deepcopy(self._latest)

    def submit(self, kind):
        if kind not in ('weather', 'forecast'):
            raise ValueError('unsupported manual operation')
        with self._mutex:
            if self._latest and self._latest['status'] in ('queued', 'running'):
                return False, self.latest()
            self._latest = {'job_id': str(uuid.uuid4()), 'operation': kind, 'status': 'queued',
                            'created_at': now(), 'finished_at': None, 'result': None, 'error_code': None}
            self._save()
            accepted = self.latest()
            self._pool.submit(self._run)
            return True, accepted

    def _run(self):
        with self._mutex:
            self._latest['status'] = 'running'
            self._save()
            kind = self._latest['operation']
            directory = self.root/'jobs'/self._latest['job_id']
        try:
            result = self.operation(kind, directory)
            if not isinstance(result, dict) or result.get('status') != 'completed':
                raise ValueError('manual operation has not completed')
        except Exception:
            with self._mutex:
                self._latest.update(status='failed', finished_at=now(), error_code='manual_review_required')
                self._save()
        else:
            with self._mutex:
                self._latest.update(status='completed', finished_at=now(), result=result)
                self._save()

    def close(self):
        self._pool.shutdown(wait=True)
        self._file.close()
