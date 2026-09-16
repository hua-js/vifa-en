"""On-demand, local whole-day comparison jobs. No EMS client or dispatch path."""
from shared.project import get_project
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from m4.optimizer.contracts import OptimizationRequest, CandidateResult
from m4.optimizer.service import M4Optimizer
from .daily_baseline import prepare_ems_day
from .daily_comparison import compare_daily_plan, REVENUE_GATE_VERSION
from .ems_simulation import EMS_BASELINE_POLICY
from .forecast_source import LOAD_POLICY
from .daily_pv_policy import POLICY as DAILY_PV_POLICY
from .daily_policy import matches_current_daily_policy, PEAK_RESERVE_SELECTOR


class DailyPlanService:
    def __init__(self, store, inputs, root):
        self.store, self.inputs, self.root = store, inputs, Path(root)
        self.lock = Lock()
        self.running = set()
        self.jobs = {}

    def _path(self, station):
        if station not in tuple(s.id for s in get_project().stations):
            raise ValueError('未知电站')
        from .project_storage import bind_root
        bind_root(self.root)
        return self.root / (station+'.json')

    def latest(self, station):
        path = self._path(station)
        with self.lock:
            raw = deepcopy(self.jobs.get(station))
        if raw is not None and raw.get('status') in ('running', 'failed'):
            return raw
        if raw is None:
            if not path.exists():
                return dict(station_id=station, status='empty', result=None)
            if path.is_symlink():
                raise ValueError('invalid daily result path')
            raw = json.loads(path.read_text())
        if raw['station_id'] != station or raw.get('status') != 'completed':
            raise ValueError('invalid saved daily result')
        if get_project().station(station).has_pv and raw.get('request', {}).get('source_versions', {}).get('pv_gap_policy') != DAILY_PV_POLICY:
            return dict(station_id=station, status='empty', result=None,
                message='旧日计划的光伏预测不完整，请等待完整输入后重新生成。')
        comparison = raw.get('result', {}).get('record', {}).get('daily_comparison', {})
        if comparison.get('revenue_gate_version') != REVENUE_GATE_VERSION:
            return dict(station_id=station, status='empty', result=None,
                message='收益门槛已更新为100元，请重新生成计划。')
        if raw.get('request', {}).get('source_versions', {}).get('load_policy') != LOAD_POLICY:
            return dict(station_id=station, status='empty', result=None,
                message='负荷预测来源已更新，请重新生成计划。')
        if raw.get('baseline', {}).get('controller_version') != EMS_BASELINE_POLICY:
            return dict(station_id=station, status='empty', result=None,
                message='EMS 基线规则已更新，旧费用比较失效；请按当前光伏余电设置重新生成。')
        if not matches_current_daily_policy(station, raw.get('request')):
            return dict(station_id=station, status='empty', result=None,
                message='本站日优化策略已更新，旧计划不再作为当前方案；请重新生成峰段保电计划。')
        # Recalculate the recommendation and its explicit terminal-energy rule,
        # instead of trusting a saved savings value alone.
        request = OptimizationRequest.model_validate_json(json.dumps(raw['request']))
        if request.station_id != station:
            raise ValueError('invalid daily request station')
        chosen = CandidateResult.model_validate_json(json.dumps(raw['selected_candidate'])) if raw['selected_candidate'] else None
        expected = compare_daily_plan(request, chosen, baseline=raw['baseline'],
            controls_version=request.source_versions['controls'],
            terminal_soc_target_pct=raw['baseline']['terminal_soc_pct'])
        if expected != raw['result']['record']['daily_comparison']:
            raise ValueError('saved daily comparison mismatch')
        return raw

    def start(self, station):
        self._path(station)
        with self.lock:
            if station in self.running:
                return deepcopy(self.jobs[station])
            job = dict(station_id=station, run_id=str(uuid4()), status='running',
                       started_at=datetime.now(timezone.utc).isoformat(), result=None,
                       message='正在读取全天输入并计算，与 EMS 原计划比较。')
            self.running.add(station)
            self.jobs[station] = job
        Thread(target=self._run, args=(station, job['run_id']), daemon=True).start()
        return deepcopy(job)

    def _run(self, station, run_id):
        try:
            from .daily_inputs import SHANGHAI
            configuration = self.store.get(station)
            day = datetime.now(SHANGHAI).date()
            inputs = self.inputs.fetch(configuration, day)
            if not inputs['can_compare']:
                raise ValueError('；'.join(c['detail'] for c in inputs['checks'] if c['status'] != 'ready'))
            request, baseline, _ = prepare_ems_day(configuration, inputs)
            from .load_accuracy import require_gate
            require_gate(inputs['sources']['load'].get('accuracy_gate'), station, datetime.now(timezone.utc))
            result = M4Optimizer(model_version='m4-daily-ems-baseline-v1').optimize(
                request, terminal_soc_target_pct=baseline['terminal_soc_pct'])
            usable = [c for c in result.candidates if c.status in ('optimal', 'feasible')
                and (request.peak_reserve_policy is None
                     or ('PEAK_RESERVE_PREFERENCE_INCOMPLETE' not in c.risk_codes
                         and any(layer.name == 'peak-reserve-shortfall' for layer in c.layers)))]
            chosen = min(usable, key=lambda c: (c.metrics.energy_cost, c.profile_id)) if usable else None
            comparison = compare_daily_plan(request, chosen, baseline=baseline,
                controls_version=request.source_versions['controls'], terminal_soc_target_pct=baseline['terminal_soc_pct'])
            if self.store.get(station).version != configuration.version:
                raise ValueError('计算期间参数已变化，请重新计算。')
            if not matches_current_daily_policy(station, request.model_dump(mode='json')):
                raise ValueError('计算期间日优化策略已变化，请重新计算。')
            controls = self.inputs.live._controls(station)
            if controls['version'] != request.source_versions['controls'] or datetime.now(SHANGHAI).date() != day:
                raise ValueError('计算期间 EMS 原计划或日期变化，请重新计算。')
            finished = datetime.now(timezone.utc).isoformat()
            record = dict(station_id=station, run_id=run_id,
                status='completed' if chosen else 'blocked_model_solver',
                started_at=self.jobs[station]['started_at'], finished_at=finished,
                checked_at=finished, expires_at=inputs['plan_end_at'],
                plan_start_at=request.plan_start_at.isoformat(),
                candidate_run_id=run_id, input_sha256=comparison['input_sha256'],
                selected=dict(profile_id=chosen.profile_id, plan_version=chosen.plan_version) if chosen else None,
                reason=comparison['reason'], comparison=[], issues=[], stages=[],
                policy=None, peak_preparation=None,
                solver_name=result.solver_name, solver_version=result.solver_version,
                model_version=result.model_version,
                selector_version=PEAK_RESERVE_SELECTOR if request.peak_reserve_policy is not None else 'daily-cost-gate-v1',
                solve_seconds=sum(c.solve_seconds for c in result.candidates),
                input_summary=dict(configuration_version=configuration.version, horizon_points=96,
                    source_versions=request.source_versions, input_observed_at=request.input_observed_at.isoformat(),
                    **{k: getattr(request.capability, k) for k in ('initial_soc_pct','energy_capacity_kwh','max_charge_kw','max_discharge_kw')},
                    demand_limit_kw=request.constraints.demand_limit_kw),
                candidates=[dict(profile_id=c.profile_id, status=c.status, plan_version=c.plan_version,
                    metrics=c.metrics.model_dump(mode='json') if c.metrics else None,
                    plan=[p.model_dump(mode='json') for p in c.plan]) for c in result.candidates],
                daily_comparison=comparison, daily_points=[p.model_dump(mode='json') for p in request.points])
            output = dict(station_id=station, run_id=run_id, status='completed', message=comparison['reason'],
                request=request.model_dump(mode='json'), baseline=baseline,
                selected_candidate=chosen.model_dump(mode='json') if chosen else None,
                result=dict(schema_version='m4-decision-results-v1', station_id=station,
                    usage='historical_preview_only', dispatch_status='not_dispatched', status='available', record=record))
            path = self._path(station)
            path.parent.mkdir(parents=True, exist_ok=True)
            archive = self.root / station
            archive.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(output, ensure_ascii=False, allow_nan=False)
            (archive / (run_id+'.json')).write_text(encoded)
            temporary = path.with_suffix('.'+run_id+'.tmp')
            temporary.write_text(encoded)
            temporary.replace(path)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else '全天计算失败，请检查输入后重试。'
            output = dict(station_id=station, run_id=run_id, status='failed', message=message, result=None)
        finally:
            with self.lock:
                self.jobs[station] = output
                self.running.discard(station)
