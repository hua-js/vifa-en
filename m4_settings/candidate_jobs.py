"""Bounded, idempotent in-process candidate jobs; clients poll instead of holding HTTP."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock, Thread

from .candidates import CandidateError


class CandidateJobs:
    def __init__(self, candidates):
        self.candidates = candidates
        self.lock = Lock()
        self.jobs = {}

    def get(self, station_id, request_id):
        with self.lock:
            return deepcopy(self.jobs.get((station_id, request_id)))

    def start(self, station_id, request_id, version):
        key = (station_id, request_id)
        with self.lock:
            if key in self.jobs:
                if self.jobs[key]['configuration_version'] != version:
                    raise CandidateError(409, '同一请求标识的配置版本不一致')
                return deepcopy(self.jobs[key])
            if any(k[0] == station_id and j['status'] == 'running' for k, j in self.jobs.items()):
                raise CandidateError(409, '本站已有候选任务运行，请等待完成')
            if len(self.jobs) >= 40:
                for old in list(self.jobs):
                    if self.jobs[old]['status'] != 'running':
                        del self.jobs[old]
                        break
            job = dict(request_id=request_id, station_id=station_id, configuration_version=version,
                status='running', stage='inputs', message='正在准备输入', result=None,
                created_at=datetime.now(timezone.utc).isoformat())
            self.jobs[key] = job
            Thread(target=self._run, args=(key,), daemon=True, name='m4-candidate-'+request_id).start()
            return deepcopy(job)

    def _run(self, key):
        def progress(stage, message):
            with self.lock:
                self.jobs[key].update(stage=stage, message=message)
        try:
            job = self.get(*key)
            result = self.candidates.calculate(key[0], job['configuration_version'], progress=progress)
            with self.lock:
                self.jobs[key].update(status='completed', stage='completed', message='候选计算完成', result=result)
        except Exception as error:
            detail = error.detail if isinstance(error, CandidateError) else {'message': '候选任务失败，请检查输入后重试'}
            with self.lock:
                self.jobs[key].update(status='failed', stage='failed', message=detail['message'], error=detail)
