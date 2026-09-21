"""Build a natural-day baseline from the EMS schedule, demand and SOC replay."""
from shared.project import get_project
from datetime import datetime, timedelta
import hashlib
import json

from m4.optimizer.contracts import (CapabilitySnapshot, ForecastPoint, PeakReservePolicy, GRID_CHARGING_POLICY,
    OptimizationConstraints, OptimizationRequest)
from m4.optimizer.metrics import calculate_metrics
from .daily_comparison import comparison_input_sha256, compare_daily_plan
from .objectives import get_daily_profiles
from .daily_policy import daily_policy_version, physical_grid_policy
from .terminal_policy import POLICY as TERMINAL_POLICY, target as terminal_target
from .startup_admission import require_admission, POLICY as STARTUP_POLICY
from .forecast_source import validate_forecast_values
from .daily_pv_policy import POLICY as DAILY_PV_POLICY
from .timeseries import tariff_export_price
from .ems_simulation import EMS_BASELINE_POLICY, simulate_ems_day
from .schedule_power import effective_station_power, station_schedule, station_energy_capacity


def prepare_ems_day(configuration, bundle):
    """Bind the original EMS replay and the station's explicit daily planning rule.

    Capability is configured whole-station capability for retrospective comparison,
    never today's live participating subset or permission to dispatch devices.
    """
    parameters = configuration.parameters
    if parameters is None or not configuration.version:
        raise ValueError('请先保存本站效率和 SOC 参数。')
    if bundle['station_id'] != configuration.station_id or bundle['configuration_version'] != configuration.version:
        raise ValueError('全天输入与本站参数版本不一致。')
    sources = bundle['sources']
    if any(sources.get(k, {}).get('status') != 'ready' for k in ('load', 'pv', 'tariff', 'controls', 'initial_soc')):
        raise ValueError('全天预测、电价、零点 SOC 或 EMS 时段尚未就绪。')
    if get_project().station(configuration.station_id).has_pv and sources['pv'].get('gap_policy') != DAILY_PV_POLICY:
        raise ValueError('光伏预测完整性规则已更新，请重新读取输入。')
    startup = require_admission(sources['load'].get('accuracy_gate'), configuration.station_id,
        sources['tariff'].get('period_types', []), datetime.fromisoformat(bundle['fetched_at']))
    control, initial = sources['controls'], sources['initial_soc']
    if control.get('power_scope') not in ('station', 'cabinet') or control.get('source_health', {}).get('schedule') != 'ready' or not control.get('schedule'):
        raise ValueError('尚未读取到完整的站级 EMS 原计划。')
    start = datetime.fromisoformat(bundle['plan_start_at'])
    if start.utcoffset() != timedelta(hours=8) or (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0):
        raise ValueError('日基线必须从北京时间零点开始。')
    if initial.get('scope') != 'whole_station_reference' or datetime.fromisoformat(initial['observed_at']) != start:
        raise ValueError('零点 SOC 的时刻或全站范围不一致。')
    points = [ForecastPoint.model_validate_json(json.dumps(p)) for p in bundle['points']]
    if len(points) != 96:
        raise ValueError('日基线需要完整96点输入。')
    if any(point.sell_price_per_kwh != tariff_export_price(sources['tariff']) for point in points):
        raise ValueError('上网电价口径已更新，请重新读取全天输入。')
    validate_forecast_values(sources['load'], points)
    capability = CapabilitySnapshot(available=True, initial_soc_pct=initial['initial_soc_pct'],
        **{k: getattr(parameters, k) for k in ('charge_efficiency', 'discharge_efficiency')},
        energy_capacity_kwh=station_energy_capacity(configuration, control),
        **effective_station_power(configuration, control),
        derating_reason='同日回算使用配置的全站范围与零点站级 SOC，不表示设备当前可下发能力。')
    constraints = OptimizationConstraints(
        **{k: getattr(parameters, k) for k in ('soc_min_pct', 'soc_max_pct',
           'preferred_soc_min_pct', 'preferred_soc_max_pct', 'terminal_soc_tolerance_pct', 'cycle_cost_per_kwh')},
        demand_limit_kw=control['demand']['need_kw'],
        grid_import_limit_kw=(parameters.grid_import_limit_kw if parameters.grid_import_limit_kw is not None else control['demand']['need_kw']),
        grid_export_enabled=True, grid_export_limit_kw=max(p.pv_forecast_kw for p in points))
    schedule = station_schedule(control)
    plan, simulation = simulate_ems_day(capability, constraints, points, schedule, pv_dispatch_policy=parameters.pv_dispatch_policy)
    terminal = plan[-1].expected_soc_pct
    policy_version = daily_policy_version(configuration.station_id)
    reserve_policy = (PeakReservePolicy(version='peak-reserve-v4', terminal_soc_min_pct=max(
        constraints.soc_min_pct, constraints.preferred_soc_min_pct)) if policy_version else None)
    terminal_versions = {}
    if policy_version:
        valley_prices = [p.buy_price_per_kwh for p in points if p.tariff_period == 'gu']
        if not valley_prices:
            raise ValueError('谷段电价缺失，不能比较日末库存费用。')
        terminal_versions = dict(terminal_policy=TERMINAL_POLICY,
            terminal_inventory_price=format(max(valley_prices), '.17g'))
    profiles = get_daily_profiles(configuration.station_id)
    content = {**terminal_versions, 'grid_charging_policy': GRID_CHARGING_POLICY,
               'configuration': configuration.model_dump(mode='json'), 'points': bundle['points'],
               'controls_version': control['version'], 'initial': initial, 'terminal': terminal,
               'baseline_policy': EMS_BASELINE_POLICY, 'pv_midday_economic': True,
               'profiles': [profile.model_dump(mode='json') for profile in profiles]}
    if policy_version:
        content.update(daily_policy=policy_version,
            peak_reserve_policy=reserve_policy.model_dump(mode='json'),
            profiles=[profile.model_dump(mode='json') for profile in profiles])
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
    request = OptimizationRequest(request_id='m4-daily-'+digest, station_id=configuration.station_id,
        plan_start_at=start, input_observed_at=start, max_input_age_seconds=parameters.max_input_age_seconds,
        interval_minutes=15, horizon_points=96, points=points, capability=capability, constraints=constraints,
        profiles=profiles, pv_dispatch_policy=parameters.pv_dispatch_policy, pv_midday_economic=True,
        peak_reserve_policy=reserve_policy,
        solver_time_limit_seconds=30.0, solver_mip_rel_gap=0.01,
        source_versions={**terminal_versions, **(dict(startup_policy=STARTUP_POLICY, startup_window_end=startup['end_at'],
                startup_tariff_periods=json.dumps(sources['tariff']['period_types'])) if startup else {}),
            **{k: sources[k]['version'] for k in ('load', 'pv', 'tariff')},
            'pv_export_policy': sources['tariff']['export_price_policy'],
            'pv_export_price': format(tariff_export_price(sources['tariff']), '.17g'),
            'load_policy': sources['load']['policy'], 'load_run_id': sources['load']['run_id'],
            'controls': control['version'], 'configuration': configuration.version,
            **({'ems_baseline': control['baseline_version']} if control.get('baseline_version') else {}),
            'project_configuration': get_project().fingerprint,
            'grid_charging_policy': GRID_CHARGING_POLICY,
            'capability': initial['version'], 'planning_basis': 'whole-station-retrospective-v1',
            **({'pv_gap_policy': DAILY_PV_POLICY,
                'pv_zero_filled_points': str(sources['pv'].get('zero_filled_points', 0))}
               if get_project().station(configuration.station_id).has_pv else {}),
            **({'physical_grid_policy': physical_grid_policy(configuration.station_id), 'economic_policy': 'station-1-cost-first-v1'} if get_project().station(configuration.station_id).policy == 'cost_first' else {}),
            'terminal_target': format(reserve_policy.terminal_soc_min_pct if reserve_policy else terminal, '.17g'), 'baseline_policy': EMS_BASELINE_POLICY,
            **({'daily_policy': policy_version} if policy_version else {})})
    baseline = dict(schema_version='m4-ems-daily-baseline-v1', station_id=configuration.station_id,
        basis='ems_rule_simulation', schedule=schedule, source_schedule=control['schedule'], source_power_scope=control['power_scope'], simulation=simulation, input_sha256=comparison_input_sha256(request),
        controls_version=control['version'], controller_version=EMS_BASELINE_POLICY,
        reference_source=control.get('baseline_source', 'live_ems_schedule'),
        reference_version=control.get('baseline_version'),
        reference_configuration=control.get('baseline_configuration'),
        initial_state_version=initial['version'], initial_state_at=start.isoformat(),
        initial_soc_pct=initial['initial_soc_pct'], terminal_soc_pct=terminal,
        plan=[p.model_dump(mode='json') for p in plan])
    comparison = compare_daily_plan(request, None, baseline=baseline, controls_version=control['version'],
                                    terminal_soc_target_pct=terminal_target(request, terminal))
    if comparison['status'] == 'unavailable':
        raise ValueError(comparison['reason'])
    comparison['reason'] = 'EMS 全天模拟基线已计算，已计入限充、削峰及 SOC 到限待机；请重新生成优化日计划比较费用。'
    baseline['metrics'] = calculate_metrics(request, plan).model_dump(mode='json')
    return request, baseline, comparison
