"""Whole-day comparison against the user-confirmed EMS plan; never redispatch."""
from shared.project import get_project
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from zoneinfo import ZoneInfo

from m4.optimizer.contracts import CandidateResult, PlanPoint
from m4.optimizer.metrics import calculate_metrics
from m4.optimizer.validation import validate_candidate, validate_peak_grid_charging
from .ems_simulation import EMS_BASELINE_POLICY, simulate_ems_day


SHANGHAI = ZoneInfo('Asia/Shanghai')
POWER_TOLERANCE_KW = 1e-5
ENERGY_TOLERANCE_KWH = 1e-5
COST_TOLERANCE_YUAN = 1e-6
MIN_NET_SAVINGS_YUAN = 100.0
REVENUE_GATE_VERSION = 'daily-net-savings-100-v1'


def comparison_input_sha256(request):
    """Bind an effective baseline to all inputs, including capability/efficiency."""
    raw = json.dumps(request.model_dump(mode='json'), sort_keys=True,
                     separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return sha256(raw.encode()).hexdigest()


def unavailable_comparison(station_id, reason='尚未完成本次全天费用比较，请读取全天数据。'):
    return dict(schema_version='m4-daily-comparison-v1', station_id=station_id,
        status='unavailable', recommended_source='ems', usage='preview_only',
        dispatch_status='not_dispatched', reason=reason, date=None,
        start_at=None, end_at=None, candidate_plan_version=None,
        controls_version=None, baseline_policy_version=None,
        input_sha256=None, baseline_cost_yuan=None, optimized_cost_yuan=None,
        savings_yuan=None, net_savings_yuan=None,
        revenue_gate_version=REVENUE_GATE_VERSION,
        minimum_net_savings_yuan=MIN_NET_SAVINGS_YUAN, baseline=None, recommended=None)


def _aware(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('baseline timestamp requires timezone')
    return parsed


def _baseline_plan(request, baseline, controls_version):
    if (not isinstance(baseline, dict)
            or baseline.get('schema_version') != 'm4-ems-daily-baseline-v1'
            or baseline.get('basis') != 'ems_rule_simulation'
            or baseline.get('controller_version') != EMS_BASELINE_POLICY
            or request.source_versions.get('baseline_policy') != EMS_BASELINE_POLICY
            or baseline.get('station_id') != request.station_id
            or baseline.get('input_sha256') != comparison_input_sha256(request)
            or not controls_version or baseline.get('controls_version') != controls_version
            or not isinstance(baseline.get('controller_version'), str)
            or not baseline['controller_version'].strip()
            or not isinstance(baseline.get('initial_state_version'), str)
            or not baseline['initial_state_version'].strip()
            or _aware(baseline['initial_state_at']) != request.plan_start_at
            or type(baseline.get('initial_soc_pct')) not in (int, float)
            or not math.isfinite(baseline['initial_soc_pct'])
            or abs(baseline['initial_soc_pct'] - request.capability.initial_soc_pct) > 1e-9):
        raise ValueError('baseline provenance mismatch')
    if request.ems_schedule_modes is not None:
        from .schedule_power import schedule_modes
        if request.ems_schedule_modes != schedule_modes(baseline['schedule']):
            raise ValueError('EMS schedule boundary provenance mismatch')
    plan = [PlanPoint.model_validate_json(json.dumps(point, allow_nan=False))
            for point in baseline['plan']]
    replay, simulation = simulate_ems_day(request.capability, request.constraints, request.points, baseline['schedule'], pv_dispatch_policy=request.pv_dispatch_policy)
    if ([p.model_dump(mode='json') for p in replay] != baseline['plan']
            or simulation != baseline.get('simulation')):
        raise ValueError('EMS replay does not match saved baseline')
    # A forecast can leave unmet demand after the available energy is exhausted.
    # Keep that modeled gap visible, never invent energy to force baseline feasibility.
    # The NEW optimizer still has its unchanged hard demand/grid constraint.
    validation_request = request.model_copy(deep=True)
    validation_request.constraints.terminal_soc_tolerance_pct = 100.0
    validation_request.constraints.grid_import_limit_kw = None
    # EMS is replayed under its original controller, not the optimizer's new
    # non-peak discharge restriction or terminal reserve. Keep its PV policy.
    validation_request.peak_reserve_policy = None
    validation_request.ems_schedule_modes = None
    metrics = calculate_metrics(validation_request, plan)
    checked = CandidateResult(profile_id='balanced', profile_version='ems-baseline-v1',
        plan_version='ems/'+baseline['controller_version']+'/'+baseline['input_sha256'], status='feasible',
        solver_message='EMS demand/SOC forecast replay', solve_seconds=0.0,
        plan=plan, metrics=metrics, layers=[], risk_codes=[], risk_messages=[])
    validate_candidate(validation_request, checked, tolerance=POWER_TOLERANCE_KW,
                       historical_baseline=True)
    return checked


def compare_daily_plan(request, candidate, *, baseline=None, controls_version=None,
                       terminal_soc_target_pct=None):
    """Keep EMS comparison evidence but never recommend prohibited peak charging."""
    output = _compare_daily_plan(request, candidate, baseline=baseline,
        controls_version=controls_version, terminal_soc_target_pct=terminal_soc_target_pct)
    recommended = output.get('recommended')
    if recommended is not None:
        plan = [PlanPoint.model_validate_json(json.dumps(point)) for point in recommended['plan']]
        try:
            validate_peak_grid_charging(request, plan, POWER_TOLERANCE_KW)
        except ValueError:
            output.update(status='blocked', recommended_source=None, recommended=None,
                reason='当前方案包含尖、峰段电网充电，暂停推荐，请重新生成合规方案。')
    return output


def _compare_daily_plan(request, candidate, *, baseline=None, controls_version=None,
                        terminal_soc_target_pct=None):
    """Choose one complete day, never an assortment of cheaper quarter hours.

    Called only after the saved request and candidate evidence is verified.
    The EMS baseline includes modeled demand controls and SOC-limited active time.
    """
    output = unavailable_comparison(request.station_id)
    reserve_policy = request.peak_reserve_policy
    if reserve_policy is not None:
        output.update(daily_policy_version=request.source_versions.get('daily_policy'),
            terminal_energy_rule='not_less_than_baseline',
            baseline_terminal_soc_pct=None, optimized_terminal_soc_pct=None,
            retained_energy_kwh=None)
    output['controls_version'] = controls_version
    start = request.plan_start_at.astimezone(SHANGHAI)
    if (request.horizon_points != 96 or len(request.points) != 96
            or request.interval_minutes != 15
            or (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0)
            or any(point.timestamp != request.plan_start_at + timedelta(minutes=15*i)
                   for i, point in enumerate(request.points))):
        output['reason'] = '当前求解结果不是 00:00–24:00 的完整日计划，暂不作为全天优化推荐；沿用 EMS 原计划。'
        return output
    output.update(date=start.date().isoformat(), start_at=start.isoformat(),
                  end_at=(start+timedelta(days=1)).isoformat(),
                  input_sha256=comparison_input_sha256(request))
    if baseline is None:
        output['reason'] = '本次尚未生成全天基线，请读取 EMS 原计划及全天数据。'
        return output
    try:
        ems = _baseline_plan(request, baseline, controls_version)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        output['reason'] = 'EMS 日基线的来源、日初状态或有效功率尚未校验通过，暂无法比较；沿用原策略。'
        return output
    if reserve_policy is not None:
        expected_floor = max(ems.metrics.terminal_soc_pct,
                             request.constraints.preferred_soc_min_pct)
        if abs(reserve_policy.terminal_soc_min_pct - expected_floor) > 1e-9:
            output['reason'] = '峰段保电的日末 SOC 下限与 EMS 基线及推荐下限不一致，请重新生成。'
            return output
        output['baseline_terminal_soc_pct'] = ems.metrics.terminal_soc_pct
    ems_view = dict(profile_id='ems', plan_version=ems.plan_version,
                    plan=[point.model_dump(mode='json') for point in ems.plan],
                    metrics=ems.metrics.model_dump(mode='json'), simulation=baseline['simulation'])
    output.update(status='ems', baseline_policy_version=EMS_BASELINE_POLICY, baseline_cost_yuan=ems.metrics.energy_cost,
                  baseline=ems_view, recommended=ems_view,
                  reason='暂无费用更低且满足需量约束的完整日方案，沿用 EMS 原计划。')
    if candidate is None or candidate.status not in ('optimal', 'feasible'):
        return output
    if reserve_policy is not None and (
            'PEAK_RESERVE_PREFERENCE_INCOMPLETE' in candidate.risk_codes
            or not any(layer.name == 'peak-reserve-shortfall' for layer in candidate.layers)):
        output['reason'] = '峰前储能目标尚未确认完成，未采用保电优化方案；沿用 EMS 原计划。'
        return output
    output['candidate_plan_version'] = candidate.plan_version
    try:
        validate_candidate(request, candidate, tolerance=POWER_TOLERANCE_KW,
                           terminal_soc_target_pct=terminal_soc_target_pct)
        metrics = calculate_metrics(request, candidate.plan)
    except (ValueError, TypeError, AttributeError, OverflowError):
        output['reason'] = '优化日计划未通过独立校验，沿用 EMS 原计划。'
        return output
    soft_demand = (get_project().station(request.station_id).policy == 'cost_first'
        and ((request.source_versions.get('physical_grid_policy') == 'station-1-550-v1'
              and request.constraints.grid_import_limit_kw == 550.0)
             or (request.source_versions.get('physical_grid_policy') == 'configured-grid-import-v1'
                 and request.constraints.grid_import_limit_kw is not None)))
    if metrics.peak_demand_exceed_kw > POWER_TOLERANCE_KW and not soft_demand:
        output['reason'] = '优化日计划未满足需量目标，沿用 EMS 原计划。'
        return output
    cost_first = soft_demand and request.source_versions.get('economic_policy') == 'station-1-cost-first-v1'
    if soft_demand and not cost_first and (metrics.peak_demand_exceed_kw > ems.metrics.peak_demand_exceed_kw + POWER_TOLERANCE_KW
            or (abs(metrics.peak_demand_exceed_kw - ems.metrics.peak_demand_exceed_kw) <= POWER_TOLERANCE_KW
                and metrics.demand_exceed_energy_kwh > ems.metrics.demand_exceed_energy_kwh + ENERGY_TOLERANCE_KWH)):
        output['reason'] = '候选方案未改善需量控制目标，沿用 EMS 原计划。'
        return output
    terminal_difference = (metrics.terminal_soc_pct - ems.metrics.terminal_soc_pct) * request.capability.energy_capacity_kwh / 100
    if reserve_policy is not None:
        output.update(optimized_terminal_soc_pct=metrics.terminal_soc_pct,
                      retained_energy_kwh=terminal_difference)
    if ((reserve_policy is None and abs(terminal_difference) > ENERGY_TOLERANCE_KWH)
            or (reserve_policy is not None and terminal_difference < -ENERGY_TOLERANCE_KWH)):
        output['reason'] = ('保电优化方案的期末电量低于 EMS 基线，未采用；沿用 EMS 原计划。'
            if reserve_policy is not None else '两套日计划的期末电量不一致，费用不可直接比较；沿用 EMS 原计划。')
        return output
    savings = ems.metrics.energy_cost - metrics.energy_cost
    net_savings = (ems.metrics.energy_cost + ems.metrics.cycle_cost
                   - metrics.energy_cost - metrics.cycle_cost)
    output.update(optimized_cost_yuan=metrics.energy_cost, savings_yuan=savings,
                  net_savings_yuan=net_savings)
    if not math.isfinite(net_savings) or net_savings < MIN_NET_SAVINGS_YUAN:
        output['reason'] = '全天预计净节省未达到100元，沿用 EMS 原计划。'
        return output
    if savings > COST_TOLERANCE_YUAN:
        output.update(status='optimized', recommended_source='optimized',
            reason=('峰段保电方案满足需量与安全约束，日末电量不低于 EMS，全天预计购电费用更低；未用电量保留到日末。'
                if reserve_policy is not None else '优化方案满足需量与安全约束，且在相同期末电量下全天预计费用更低。'),
            recommended=dict(profile_id=candidate.profile_id, plan_version=candidate.plan_version,
                plan=[point.model_dump(mode='json') for point in candidate.plan],
                metrics=metrics.model_dump(mode='json')))
    if output['status'] == 'optimized' and soft_demand and metrics.peak_demand_exceed_kw > POWER_TOLERANCE_KW:
        output['reason'] = (f'优化方案严格满足{request.constraints.grid_import_limit_kw:g} kW物理购电上限，{('费用优先，需量目标仅作同成本择优参考' if cost_first else '优先减少需量目标超限')}；'
            f'仍有约{metrics.peak_demand_exceed_kw:.2f} kW目标偏差，全天预计费用低于 EMS。')
    return output
