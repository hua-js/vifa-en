"""Station-isolated, idempotent background scheduling decisions."""
from datetime import datetime, timezone
import fcntl
from pathlib import Path
from threading import Lock, Thread

from m4.selection.decision_chain import read_json, run_chain, validate_identity, write_json


class DecisionRunError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DecisionRunManager:
    def __init__(self, root, api):
        self.root = Path(root)
        self.api = api
        self.mutex = Lock()

    def _directory(self, station_id):
        validate_identity(station_id)
        return self.root / station_id / '.web-jobs'

    def _write(self, job):
        directory = self._directory(job['station_id'])
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / (job['run_id'] + '.json'), job)

    def _lock(self, station_id):
        directory = self._directory(station_id)
        directory.mkdir(parents=True, exist_ok=True)
        lock = (directory / 'run.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise DecisionRunError(409, '本站已有求解器决策正在运行，请等待完成') from None
        return lock

    def _read(self, path, station_id):
        job = read_json(path)
        validate_identity(job['station_id'], job['run_id'])
        if (job['station_id'] != station_id or path.stem != job['run_id']
                or job['status'] not in {'running', 'finished', 'failed', 'interrupted'}):
            raise ValueError('invalid decision job')
        return job

    def _interrupt_if_unlocked(self, job):
        if job['status'] != 'running':
            return job
        try:
            lock = self._lock(job['station_id'])
        except DecisionRunError:
            return job
        try:
            # Another process may have completed after our initial read and
            # released the station lock. Never overwrite its finished record.
            path = self._directory(job['station_id']) / (job['run_id'] + '.json')
            job = self._read(path, job['station_id'])
            if job['status'] != 'running':
                return job
            job.update(status='interrupted', finished_at=datetime.now(timezone.utc).isoformat(),
                       message='上次本地决策运行已中断；未自动重试，请重新启动计算')
            self._write(job)
        finally:
            lock.close()
        return job

    def latest(self, station_id):
        with self.mutex:
            directory = self._directory(station_id)
            result = dict(station_id=station_id, usage='preview_only', dispatch_status='not_dispatched', job=None)
            if not directory.exists():
                return result
            paths = list(directory.glob('*.json'))
            if paths:
                path = max(paths, key=lambda item: (item.stat().st_mtime_ns, item.name))
                result['job'] = self._interrupt_if_unlocked(self._read(path, station_id))
            return result

    def start(self, station_id, request_id):
        validate_identity(station_id, request_id)
        with self.mutex:
            path = self._directory(station_id) / (request_id + '.json')
            # A lost HTTP response can cause the browser to resend one click.
            # Return the same job even while its station lock is still held.
            if path.exists():
                return self._interrupt_if_unlocked(self._read(path, station_id))
            lock = self._lock(station_id)
            try:
                # Another process may have persisted the same click first.
                if path.exists():
                    return self._read(path, station_id)
                job = dict(run_id=request_id, station_id=station_id, status='running',
                    stage='configuration', started_at=datetime.now(timezone.utc).isoformat(),
                    finished_at=None, message='正在读取调度参数与经营偏好')
                self._write(job)
                thread = Thread(target=self._run, args=(job, lock), daemon=True,
                                name='m4-decision-' + request_id)
                thread.start()
                lock = None
                return dict(job)
            finally:
                if lock is not None:
                    lock.close()

    def _run(self, job, lock):
        def progress(stage, message):
            with self.mutex:
                job.update(stage=stage, message=message)
                self._write(job)
        try:
            report = run_chain(self.api, self.root / job['station_id'] / job['run_id'],
                station_id=job['station_id'], run_id=job['run_id'], progress=progress)
            with self.mutex:
                job.update(status='finished', stage=report['status'], finished_at=report['finished_at'],
                           message='本轮已结束，请查看决策记录')
                self._write(job)
        except Exception:
            with self.mutex:
                job.update(status='failed', finished_at=datetime.now(timezone.utc).isoformat(),
                           message='本地决策运行失败；未自动重试，请检查记录后重新计算')
                self._write(job)
        finally:
            lock.close()
