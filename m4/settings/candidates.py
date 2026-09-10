"""Local, on-demand candidate previews built from server-owned live inputs.

There is no AI or device client here. The process keeps only the most recent
result per station, including the exact input snapshot used by the optimizer.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock

from m4.orchestrator import M4Orchestrator
from m4.orchestrator.contracts import StationInput

from .live_inputs import request_from_inputs
from .objectives import get_profiles, get_profile_metadata


class CandidateError(Exception):
    def __init__(self, status_code: int, message: str, **details):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {'message': message, **details}


class CandidateService:
    def __init__(self, *, store, fetch_inputs, read_controls, optimizer=None, clock=None):
        self.store = store
        self.fetch_inputs = fetch_inputs
        self.read_controls = read_controls
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.orchestrator = M4Orchestrator(model_version='m4-milp-v2-early-valley',
            orchestrator_version='m4-live-candidates-v1', optimizer=optimizer, clock=self.clock)
        self._locks = {station_id: Lock() for station_id in ('station-1', 'station-2')}
        self._results_lock = Lock()
        self._results = {}

    def _configuration(self, station_id):
        try:
            return self.store.get(station_id)
        except Exception:
            raise CandidateError(503, '无法读取本站调度参数，请重试') from None

    def _assert_configuration(self, configuration):
        current = self._configuration(configuration.station_id)
        if (current.version, current.revision) != (configuration.version, configuration.revision):
            raise CandidateError(409, '调度参数在计算期间已更新，请刷新输入后重新计算')

    def latest(self, station_id):
        with self._results_lock:
            result = deepcopy(self._results.get(station_id))
        if result is None:
            return None
        configuration = self._configuration(station_id)
        reason = ''
        if configuration.version != result['configuration_version']:
            reason = '调度参数已变化，此结果仅保留为历史预览，请重新计算'
        elif self.clock() >= datetime.fromisoformat(result['expires_at']):
            reason = '输入快照或计划起点已过期，此结果仅供历史预览，请重新计算'
        result.update(stale=bool(reason), stale_reason=reason)
        return result

    def calculate(self, station_id, configuration_version, *, progress=lambda *_: None):
        lock = self._locks.get(station_id)
        if lock is None:
            raise CandidateError(404, '未知电站')
        if not lock.acquire(blocking=False):
            raise CandidateError(429, '本站候选正在计算，请等待本轮完成')
        try:
            with self._results_lock:
                self._results.pop(station_id, None)
            configuration = self._configuration(station_id)
            if configuration.parameters is None or not configuration.version:
                raise CandidateError(422, '本站调度参数尚未配置或保存')
            if configuration.version != configuration_version:
                raise CandidateError(409, '页面参数版本已变化，请刷新参数后重新计算')
            try:
                inputs = self.fetch_inputs(configuration)
            except Exception:
                raise CandidateError(502, '真实输入读取失败，请重新读取后计算') from None
            self._assert_configuration(configuration)
            if inputs.get('status') != 'ready' or inputs.get('issues'):
                raise CandidateError(422, '真实输入未通过校验，未启动候选计算',
                    issues=inputs.get('issues', []), inputs=inputs)
            profiles = get_profiles(station_id)
            profile_metadata = get_profile_metadata(station_id)
            try:
                request = request_from_inputs(configuration, inputs, profiles, now=self.clock())
            except (ValueError, TypeError, KeyError, OverflowError):
                raise CandidateError(422, '输入快照已失效或不完整，请刷新真实输入后计算',
                    issues=['输入时间、来源版本或参与柜校验未通过'], inputs=inputs) from None
            progress('solving', '光伏与其他输入已就绪，正在计算候选')
            result = self.orchestrator.run([StationInput(input_ref='live-'+station_id, request=request)])
            # A preview must not be presented as a new valid result if the
            # configuration changed or its snapshot expired during solving.
            try:
                controls = self.read_controls(station_id)
            except Exception:
                raise CandidateError(502, '无法复核控制配置，请刷新输入后重新计算') from None
            if (controls.get('station_id') != station_id or controls.get('status') != 'ready'
                    or controls.get('version') != inputs['sources']['controls']['version']):
                raise CandidateError(409, '控制配置在计算期间已变化，请刷新输入后重新计算')
            self._assert_configuration(configuration)
            now = self.clock()
            expires_at = min(request.plan_start_at,
                request.input_observed_at + timedelta(seconds=request.max_input_age_seconds))
            if now >= expires_at:
                raise CandidateError(409, '本轮输入或计划起点已过期，请重新计算')
            # Reuse the full cabinet/source validation rather than duplicating it.
            try:
                request_from_inputs(configuration, inputs, profiles, now=now)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise CandidateError(409, '本轮输入已过期或失效，请重新计算') from None
            envelope = dict(schema_version='m4-live-candidates-v1', station_id=station_id,
                configuration_version=configuration.version, generated_at=now.isoformat(),
                expires_at=expires_at.isoformat(), usage='preview_only', stale=False, stale_reason='',
                profile_metadata=profile_metadata, inputs=inputs,
                request=request.model_dump(mode='json'), result=result.model_dump(mode='json'))
            with self._results_lock:
                self._results[station_id] = deepcopy(envelope)
            return envelope
        finally:
            lock.release()
