"""Remaining-day advisory plans anchored to measured state; no dispatch client."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.metrics import calculate_metrics
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import validate_candidate
from .ems_simulation import simulate_ems_day
from .load_accuracy import require_gate
from .daily_comparison import REVENUE_GATE_VERSION, MIN_NET_SAVINGS_YUAN
from .pv_correction import read_correction, POLICY as PV_CORRECTION_POLICY

ZONE = ZoneInfo('Asia/Shanghai')
POLICY = 'remaining-day-pv-correction-v3'


def prepare_remaining_request(configuration, inputs, now):
    """Project a recent measurement to the next quarter; never use midnight SOC."""
    station = configuration.station_id
    if inputs.get('station_id') != station or not inputs.get('can_compare'):
        raise ValueError('预测与配置尚未就绪，暂停滚动建议。')
    source = inputs['sources']
    soc, power = source['current_soc'], source['current_power']
    max_age = min(configuration.parameters.max_input_age_seconds, 900)
    for sample in (soc, power):
        if sample.get('station_id') != station or sample.get('status') != 'ready':
            raise ValueError('当前SOC或储能功率不完整，暂停滚动建议。')
        at = datetime.fromisoformat(sample['observed_at']).astimezone(ZONE)
        if not 0 <= (now-at).total_seconds() <= max_age:
            raise ValueError('当前SOC或储能功率已过期，暂停滚动建议。')
    observed = datetime.fromisoformat(soc['observed_at']).astimezone(ZONE)
    power_at = datetime.fromisoformat(power['observed_at']).astimezone(ZONE)
    if abs((observed-power_at).total_seconds()) > min(max_age, 900):
        raise ValueError('SOC与功率采样时间不一致，暂停滚动建议。')
    require_gate(source['load'].get('accuracy_gate'), station, now)
    start = now.replace(second=0, microsecond=0, minute=now.minute//15*15)+timedelta(minutes=15)
    if start.date() != now.date():
        raise ValueError('当日已无完整的后续时段，等待次日日计划。')
    # Daily inputs are JSON-compatible snapshots; strict datetime fields must
    # be restored through JSON validation, as in DailyPlanService.latest.
    base = OptimizationRequest.model_validate_json(json.dumps(inputs['request']))
    measured_soc, measured_power = soc['soc_pct'], power['power_kw']
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (measured_soc, measured_power)):
        raise ValueError('当前实测数据无效，暂停滚动建议。')
    cap = base.capability.model_copy(deep=True)
    if (not base.constraints.soc_min_pct <= measured_soc <= base.constraints.soc_max_pct
            or measured_power < -cap.max_charge_kw or measured_power > cap.max_discharge_kw):
        raise ValueError('当前实测状态超出配置范围，暂停滚动建议。')
    hours = (start-observed).total_seconds()/3600
    # AC power convention: negative charge, positive discharge.
    delta = (-measured_power*cap.charge_efficiency if measured_power < 0
             else -measured_power/cap.discharge_efficiency)*hours
    projected = measured_soc+delta/cap.energy_capacity_kwh*100
    if not base.constraints.soc_min_pct <= projected <= base.constraints.soc_max_pct:
        raise ValueError('生效时刻预计SOC超出安全范围，暂停滚动建议。')
    cap.initial_soc_pct = projected
    cap.derating_reason = '站级建议能力；尚未接入设备下发。'
    points = [p for p in base.points if p.timestamp >= start]
    if not points or points[0].timestamp != start:
        raise ValueError('后续预测时段不完整，暂停滚动建议。')
    correction = source.get('pv_correction')
    if correction and correction.get('status') in ('applied', 'unchanged', 'unavailable'):
        if (correction.get('station_id') != station
                or correction.get('source_version') != source['pv']['version']
                or correction.get('policy') != PV_CORRECTION_POLICY
                or not 0 <= (now-datetime.fromisoformat(correction['as_of'])).total_seconds() <= 60):
            raise ValueError('光伏校正来源已变化，暂停滚动建议。')
        by_time = {datetime.fromisoformat(p['timestamp']): p for p in correction['points']}
        adjusted = []
        for point in points:
            value = by_time.get(point.timestamp)
            if (value is None or value['original_kw'] != point.pv_forecast_kw
                    or type(value['solver_kw']) not in (float, int)
                    or not math.isfinite(value['solver_kw'])
                    or not 0 <= value['solver_kw'] <= point.pv_forecast_kw):
                raise ValueError('光伏校正时段无效，暂停滚动建议。')
            adjusted.append(point.model_copy(update={'pv_forecast_kw': value['solver_kw']}))
        points = adjusted
    replay, simulation = simulate_ems_day(cap, base.constraints, points,
        inputs['baseline']['schedule'], pv_dispatch_policy=base.pv_dispatch_policy, remaining_day=True)
    terminal = replay[-1].expected_soc_pct
    policy = base.peak_reserve_policy.model_copy(deep=True) if base.peak_reserve_policy else None
    if policy:
        policy.terminal_soc_min_pct = max(terminal, base.constraints.preferred_soc_min_pct)
    raw = base.model_dump(mode='json')
    raw.update(request_id='m4-rolling-'+str(uuid4()), plan_start_at=start.isoformat(),
        input_observed_at=now.isoformat(), horizon_points=len(points),
        points=[p.model_dump(mode='json') for p in points], capability=cap.model_dump(mode='json'))
    if policy:
        raw['peak_reserve_policy'] = policy.model_dump(mode='json')
    raw['source_versions'].update(planning_basis=POLICY, capability=soc['observed_at'],
        terminal_target=format(terminal, '.17g'))
    if correction:
        raw['source_versions']['pv_correction'] = correction.get('version', PV_CORRECTION_POLICY+'-unavailable')
    request = OptimizationRequest.model_validate_json(json.dumps(raw))
    return request, replay, simulation, dict(observed_at=soc['observed_at'],
        measured_soc_pct=measured_soc, power_observed_at=power['observed_at'],
        measured_power_kw=measured_power, effective_at=start.isoformat(),
        projected_soc_pct=projected), terminal


class RollingPlanService:
    def __init__(self, daily):
        self.daily = daily
        self.root = daily.root / 'rolling'
        self.running, self.jobs = set(), {}
        self.lock = Lock()

    def latest(self, station):
        self.daily._path(station)  # validate station before filesystem access
        with self.lock:
            result = deepcopy(self.jobs.get(station))
        path = self.root / (station+'.json')
        if result is None and path.exists():
            if path.is_symlink():
                raise ValueError('invalid rolling result path')
            result = json.loads(path.read_text())
        if result is None:
            return dict(station_id=station, status='empty', result=None)
        if result.get('station_id') != station:
            raise ValueError('invalid rolling result')
        if result.get('policy_version') != POLICY:
            return dict(station_id=station, status='stale', result=None,
                message='滚动策略已更新，等待新的建议。')
        now = datetime.now(ZONE)
        if result['status'] == 'completed':
            payload = result['result']
            if (now >= datetime.fromisoformat(payload['valid_until'])
                    or payload['configuration_version'] != self.daily.store.get(station).version):
                return dict(station_id=station, status='stale', result=None,
                    message='滚动建议已过期，等待更新。')
            daily = self.daily.latest(station)
            if daily.get('run_id') != payload['daily_run_id']:
                return dict(station_id=station, status='stale', result=None,
                    message='日计划已更新，等待新的滚动建议。')
        elif result['status'] == 'running' and station not in self.running:
            return dict(station_id=station, status='failed', result=None,
                message='滚动任务已中断，等待更新。')
        return result

    def history(self, station):
        from .rolling_history import advisory_history
        self.daily._path(station)
        archive = self.root / station
        jobs, unreadable = [], 0
        if archive.is_symlink():
            raise ValueError('invalid rolling archive path')
        for path in sorted(archive.glob('*.json')):
            try:
                if path.is_symlink():
                    raise ValueError('invalid rolling archive file')
                jobs.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                unreadable += 1
        result = advisory_history(station, jobs, datetime.now(ZONE))
        result['skipped'] += unreadable
        return result

    def start(self, station):
        self.daily._path(station)
        with self.lock:
            if station in self.running:
                return deepcopy(self.jobs[station])
            job = dict(station_id=station, status='running', result=None,
                run_id=str(uuid4()), policy_version=POLICY, message='正在更新后续建议。')
            self.jobs[station] = job
            self.running.add(station)
        Thread(target=self._run, args=(station, job['run_id']), daemon=True).start()
        return deepcopy(job)

    def _run(self, station, run_id):
        try:
            now = datetime.now(ZONE)
            day = now.date().isoformat()
            daily = self.daily.latest(station)
            if daily.get('status') != 'completed' or daily['result']['record']['daily_comparison']['date'] != day:
                raise ValueError('当日日计划尚未生成，等待全天收益判断。')
            gate = daily['result']['record']['daily_comparison']
            configuration = self.daily.store.get(station)
            if daily['request']['source_versions']['configuration'] != configuration.version:
                raise ValueError('参数已变化，请先重新生成日计划。')
            inputs = self.daily.inputs.fetch(configuration, now.date())
            try:
                inputs['sources']['pv_correction'] = read_correction(
                    self.daily.inputs.live.client, station, inputs, datetime.now(ZONE))
            except Exception:
                # Optional calibration never turns unreadable telemetry into zero PV.
                inputs['sources']['pv_correction'] = dict(policy=PV_CORRECTION_POLICY,
                    status='read_failed', points=[], reason='近期光伏实测不可用，沿用原预测。')
            # Use the post-fetch clock for measurement freshness and effective time.
            now = datetime.now(ZONE)
            if now.date().isoformat() != day:
                raise ValueError('已跨日，等待次日日计划。')
            request, baseline, simulation, anchor, terminal = prepare_remaining_request(configuration, inputs, now)
            base_metrics = calculate_metrics(request, baseline)
            admitted = (gate.get('revenue_gate_version') == REVENUE_GATE_VERSION
                and gate['status'] == 'optimized'
                and gate.get('net_savings_yuan', 0) >= MIN_NET_SAVINGS_YUAN)
            chosen, candidates = None, []
            reason = '当日日计划未通过收益门禁，后续沿用EMS。'
            if admitted:
                result = M4Optimizer(model_version=POLICY).optimize(request, terminal_soc_target_pct=terminal)
                candidates = result.candidates
                viable = []
                for candidate in candidates:
                    if candidate.status not in ('optimal', 'feasible'):
                        continue
                    if request.peak_reserve_policy and 'PEAK_RESERVE_PREFERENCE_INCOMPLETE' in candidate.risk_codes:
                        continue
                    validate_candidate(request, candidate, terminal_soc_target_pct=terminal)
                    metrics = calculate_metrics(request, candidate.plan)
                    net = (base_metrics.energy_cost+base_metrics.cycle_cost
                        -metrics.energy_cost-metrics.cycle_cost)
                    if net > 1e-6:
                        viable.append((net, candidate.profile_id, candidate, metrics))
                if viable:
                    _, _, chosen, metrics = max(viable, key=lambda item: (item[0], item[1]))
                    reason = '后续建议已按最新实测状态更新。'
                else:
                    reason = '暂无更经济且可行的后续优化方案，沿用EMS。'
            points = chosen.plan if chosen else baseline
            metrics = calculate_metrics(request, points)
            current_configuration = self.daily.store.get(station)
            current_daily = self.daily.latest(station)
            current_controls = self.daily.inputs.live._controls(station)
            finished = datetime.now(ZONE)
            if (finished >= request.plan_start_at
                    or (finished-datetime.fromisoformat(anchor['observed_at'])).total_seconds() > min(configuration.parameters.max_input_age_seconds, 900)
                    or current_daily.get('run_id') != daily['run_id']
                    or current_configuration.version != configuration.version
                    or current_controls['version'] != request.source_versions['controls']):
                raise ValueError('生效窗口或配置已变化，本轮建议作废。')
            payload = dict(schema_version='m4-rolling-plan-v1', station_id=station, date=day,
                configuration_version=configuration.version, daily_run_id=daily['run_id'],
                daily_gate_passed=admitted, daily_net_savings_yuan=gate.get('net_savings_yuan'),
                effective_at=request.plan_start_at.isoformat(),
                valid_until=(request.plan_start_at+timedelta(minutes=15)).isoformat(),
                end_at=(request.plan_start_at+timedelta(minutes=15*len(points))).isoformat(),
                finished_at=finished.isoformat(), anchor=anchor, reason=reason,
                source='optimized' if chosen else 'ems', request=request.model_dump(mode='json'),
                plan=[p.model_dump(mode='json') for p in points],
                baseline=[p.model_dump(mode='json') for p in baseline], simulation=simulation,
                metrics=metrics.model_dump(mode='json'), baseline_metrics=base_metrics.model_dump(mode='json'),
                candidates=[c.model_dump(mode='json') for c in candidates],
                actual_load=inputs['sources'].get('actual_load'),
                pv_correction=inputs['sources'].get('pv_correction'),
                dispatch_status='not_dispatched', usage='remaining_day_advice_only')
            job = dict(station_id=station, run_id=run_id, status='completed', result=payload,
                policy_version=POLICY, message=reason)
        except Exception as error:
            job = dict(station_id=station, run_id=run_id, status='failed', result=None,
                policy_version=POLICY, message=str(error) if isinstance(error, ValueError) else '滚动建议生成失败，等待更新。')
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            archive = self.root / station
            archive.mkdir(exist_ok=True)
            encoded = json.dumps(job, ensure_ascii=False, allow_nan=False)
            archived = archive / (run_id+'.json')
            archive_temporary = archived.with_suffix('.tmp')
            archive_temporary.write_text(encoded)
            archive_temporary.replace(archived)
            path = self.root / (station+'.json')
            temporary = path.with_suffix('.tmp')
            temporary.write_text(encoded)
            temporary.replace(path)
        except OSError:
            job = dict(station_id=station, run_id=run_id, status='failed', result=None,
                policy_version=POLICY, message='滚动建议保存失败，等待更新。')
        finally:
            with self.lock:
                self.jobs[station] = job
                self.running.discard(station)


class PlanningCoordinator:
    """One daily admission, then quarter-hour state-anchored advice."""
    def __init__(self, daily, rolling):
        self.daily, self.rolling = daily, rolling

    @property
    def running(self):
        return self.daily.running | self.rolling.running

    def start(self, station):
        latest = self.daily.latest(station)
        comparison = (latest.get('result') or {}).get('record', {}).get('daily_comparison', {})
        if latest.get('status') != 'completed' or comparison.get('date') != datetime.now(ZONE).date().isoformat():
            return self.daily.start(station)
        return self.rolling.start(station)
