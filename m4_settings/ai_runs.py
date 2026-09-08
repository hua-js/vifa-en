"""User-triggered background previews using the existing station-1 chain."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from threading import Lock, Thread

from m4_selection.ai_config import DEFAULT_CONFIG_PATH, load_model_config
from m4_selection.live_chain import BASE, run_chain
from m4_selection.ollama import GATE_DIR, OllamaSelector


class AiRunError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class AiRunManager:
    def __init__(self, root, api):
        self.root = Path(root)
        self.jobs = self.root / '.web-jobs'
        self.api = api
        self.mutex = Lock()

    def _write(self, job):
        path = self.jobs / (job['run_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False))
        temporary.replace(path)

    def _lock(self):
        self.jobs.mkdir(parents=True, exist_ok=True)
        lock = (self.jobs / 'run.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise AiRunError(409, '已有 AI 预览正在运行，请等待完成') from None
        return lock

    def latest(self, station_id):
        with self.mutex:
            result = dict(station_id=station_id, usage='preview_only', dispatch_status='not_dispatched', job=None)
            if station_id != 'station-1' or not self.jobs.exists():
                return result
            paths = list(self.jobs.glob('*.json'))
            if not paths:
                return result
            job = json.loads(max(paths, key=lambda p: p.stat().st_mtime_ns).read_text())
            if job['status'] == 'running':
                try:
                    lock = self._lock()
                except AiRunError:
                    pass
                else:
                    # A stopped local process cannot resume a remote model request.
                    try:
                        job.update(status='interrupted', message='上次本地运行已中断，未自动重试；请核对模型完成状态')
                        self._write(job)
                    finally:
                        lock.close()
            result['job'] = job
            return result

    def start(self, station_id, request_id):
        if station_id != 'station-1':
            raise AiRunError(409, '电站 2 尚未接入 AI 全链路预览')
        with self.mutex:
            lock = self._lock()
            try:
                path = self.jobs / (request_id + '.json')
                if path.exists():
                    # The browser may resend the same explicit click after losing its response.
                    return json.loads(path.read_text())
                if (GATE_DIR / 'server-completion-unknown.json').exists():
                    raise AiRunError(409, '模型存在在途或完成状态未知的请求，请核对后再运行')
                try:
                    config = load_model_config(os.environ.get('M4_AI_CONFIG') or DEFAULT_CONFIG_PATH)
                except ValueError:
                    raise AiRunError(422, 'AI 配置无效，请检查模型配置文件') from None
                if not config.enabled:
                    raise AiRunError(409, 'AI 已在配置中禁用')
                model = OllamaSelector.from_config(config)
                job = dict(run_id=request_id, station_id=station_id, status='running', stage='configuration',
                           model=config.model, started_at=datetime.now(timezone.utc).isoformat(),
                           finished_at=None, message='正在读取调度参数与选择偏好')
                self._write(job)
                thread = Thread(target=self._run, args=(job, config, model, lock), daemon=True,
                                name='m4-ai-preview-' + request_id)
                thread.start()
                lock = None  # The worker owns the cross-process lock until completion.
                return dict(job)
            finally:
                if lock is not None:
                    lock.close()

    def _run(self, job, config, model, lock):
        manager = self
        selection_count = 0

        def progress(stage, message):
            with self.mutex:
                job.update(stage=stage, message=message)
                self._write(job)

        class Api:
            def request(self, method, path, data=None):
                nonlocal selection_count
                route = path.removeprefix(BASE)
                if route == 'inputs':
                    progress('inputs', '正在获取并校验真实输入')
                elif route == 'candidates':
                    progress('model_solver', '正在建模并求解三套候选')
                elif route == 'selection':
                    selection_count += 1
                    progress('pre_ai_validation' if selection_count == 1 else 'post_ai_validation',
                             '正在复核候选与实时状态' if selection_count == 1 else 'AI 已返回，正在再次复核实时状态')
                return manager.api.request(method, path, data)

        def select(payload, output):
            progress('ai', '正在等待模型选择，请保持本轮运行，勿重复提交')
            return model(payload, output)

        try:
            report = run_chain(Api(), select, self.root / job['run_id'], config)
            with self.mutex:
                job.update(status='finished', stage=report['status'], finished_at=report['finished_at'],
                           message='本轮已结束，请查看下方 AI 结果记录')
                self._write(job)
        except Exception:
            with self.mutex:
                job.update(status='failed', finished_at=datetime.now(timezone.utc).isoformat(),
                           message='本地运行失败，请检查联调记录；未自动重试')
                self._write(job)
        finally:
            lock.close()
