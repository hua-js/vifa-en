"""Explicit strategy requests may refresh PV; no timers or production DB writes here."""
import http.client
import json
import os
import socket
import time
from threading import Lock
from uuid import UUID


class PVPreparationError(ValueError):
    pass


class SocketConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=15)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class PVService:
    def __init__(self, token):
        self.token = token
        self.path = os.environ.get('M4_PV_SOCKET_PATH', '/run/vifa-pv/pv.sock')

    def request(self, method):
        if not self.token or any(c.isspace() for c in self.token):
            raise PVPreparationError('光伏服务凭据未配置')
        connection = SocketConnection(self.path)
        try:
            path = '/api/pv/ES02/runs' if method == 'POST' else '/api/pv/ES02/job'
            connection.request(method, path, headers={'Authorization': 'Bearer '+self.token, 'Accept': 'application/json'})
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError
            value = json.loads(body)
            allowed = (202, 409) if method == 'POST' else (200,)
            if response.status not in allowed or not isinstance(value, dict):
                raise ValueError
            job = value.get('data')
            if not isinstance(job, dict):
                raise ValueError
            UUID(job['job_id'])
            if job.get('operation') != 'forecast':
                raise PVPreparationError('光伏服务正在处理其他操作，请等待完成后重新启动策略')
            return job
        except PVPreparationError:
            raise
        except Exception:
            raise PVPreparationError('光伏服务请求失败或结果不确定；未自动重发，请查看光伏任务状态') from None
        finally:
            connection.close()

    def generate(self, progress):
        # One POST only. A busy forecast can be joined, never replaced.
        job = self.request('POST')
        identity = job['job_id']
        deadline = time.monotonic()+600
        while True:
            if job['job_id'] != identity:
                raise PVPreparationError('光伏任务已被其他任务替换，未继续本轮计算')
            state = job.get('status')
            if state == 'completed':
                result = job.get('result') or {}
                if (result.get('status') != 'completed' or result.get('es_sn') != 'ES02'
                        or result.get('verified_points') != 96 or not result.get('run_id')):
                    raise PVPreparationError('光伏任务完成信息不完整，未继续计算')
                return identity, result['run_id']
            if state not in ('queued', 'running'):
                raise PVPreparationError('光伏预测任务失败或中断，未继续计算，请查看光伏任务记录')
            if time.monotonic() >= deadline:
                raise PVPreparationError('等待光伏预测超时；任务可能仍在运行，请查看任务状态，未自动重发')
            progress('pv_refresh', '正在更新天气和光伏预测，等待入库完成')
            time.sleep(2)
            job = self.request('GET')


class PreparedInputs:
    def __init__(self, fetch, service):
        self.fetch = fetch
        self.service = service
        self.lock = Lock()

    def __call__(self, configuration, progress=lambda *_: None):
        progress('inputs', '正在检查真实输入与光伏覆盖')
        inputs = self.fetch(configuration)
        pv = inputs.get('sources', {}).get('pv', {})
        other_ready = all(inputs.get('sources', {}).get(key, {}).get('status') == 'ready'
                          for key in ('load', 'tariff', 'controls', 'realtime'))
        if configuration.station_id != 'station-2' or not pv.get('refresh_required') or not other_ready:
            return inputs
        if not self.lock.acquire(blocking=False):
            raise PVPreparationError('光伏准备正在进行，请等待当前任务完成')
        try:
            # Another explicit request may have completed while the first read ran.
            inputs = self.fetch(configuration)
            if (not inputs.get('sources', {}).get('pv', {}).get('refresh_required')
                    or not all(inputs.get('sources', {}).get(key, {}).get('status') == 'ready'
                               for key in ('load', 'tariff', 'controls', 'realtime'))):
                return inputs
            job_id, run_id = self.service.generate(progress)
            progress('inputs', '光伏预测已完成，重新对齐负荷、电价和实时输入')
            inputs = self.fetch(configuration)
            pv = inputs.get('sources', {}).get('pv', {})
            if pv.get('run_id') != run_id or pv.get('status') != 'ready':
                raise PVPreparationError('新光伏批次未覆盖本轮窗口或批次已变化，未继续计算，请重新启动策略')
            inputs['pv_preparation'] = {'job_id': job_id, 'run_id': run_id, 'status': 'completed'}
            return inputs
        finally:
            self.lock.release()
