"""Forecast replay of EMS schedule and limits, not measured device execution."""
from datetime import timedelta
import math
import re

from m4.optimizer.contracts import PlanPoint


EMS_BASELINE_POLICY = 'ems-demand-soc-duration-v6-pv-priority'
INTERVAL_HOURS = 0.25
EPSILON = 1e-9


def _slot(value):
    if value == '24:00:00':
        return 96
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):(?:00|15|30|45):00', value):
        raise ValueError('EMS 时段需要对齐15分钟边界，才能与预测逐点计算。')
    h, m, _ = map(int, value.split(':'))
    return h*4 + m//15


def simulate_ems_day(capability, constraints, points, schedule, *, pv_dispatch_policy="legacy"):
    """Apply demand first, then equipment/SOC limits, integrating active time.

    Forecast load and prices are constant within each quarter hour. Once SOC
    reaches a boundary, the rest of that interval is idle. Public PlanPoints
    contain interval-average power; intervals retain the actual modeled duration.
    """
    customer_pv = pv_dispatch_policy in ('load_first_export_priority', 'load_first_storage_priority')
    if pv_dispatch_policy not in ('legacy', 'load_first_economic', 'load_first_export_priority', 'load_first_storage_priority'):
        raise ValueError('未知光伏余电策略。')
    if len(points) != 96 or not schedule:
        raise ValueError('EMS 模拟需要完整96点输入及原时段配置。')
    slots = [None]*96
    for item in schedule:
        a, b = _slot(item['start_time']), _slot(item['end_time'])
        power = item.get('power_kw')
        if (a == b or a == 96 or item.get('repeat') != 'daily'
                or item.get('mode') not in ('charge', 'discharge')
                or type(power) not in (int, float) or not math.isfinite(power) or power < 0):
            raise ValueError('EMS 时段、模式或配置功率无法解释。')
        indices = range(a, b) if b > a else [*range(a, 96), *range(b)]
        for i in indices:
            if slots[i] is not None:
                raise ValueError('EMS 计划时段重叠，请核对原计划。')
            slots[i] = item

    capacity = capability.energy_capacity_kwh
    lower = capacity*constraints.soc_min_pct/100
    upper = capacity*constraints.soc_max_pct/100
    energy = capacity*capability.initial_soc_pct/100
    if not lower-EPSILON <= energy <= upper+EPSILON:
        raise ValueError('零点 SOC 位于当前安全范围之外，请核对历史状态与参数。')
    grid_limit = min(constraints.demand_limit_kw, constraints.grid_import_limit_kw
        if constraints.grid_import_limit_kw is not None else constraints.demand_limit_kw)
    plan, intervals, events = [], [], []
    for index, point in enumerate(points):
        item = slots[index]
        requested_mode, requested_kw = (item['mode'], item['power_kw']) if item else ('idle', 0.0)
        net_load = point.load_forecast_kw-point.pv_forecast_kw
        peak_discharge = max(net_load-grid_limit, 0.0)
        reasons = []
        if customer_pv and net_load < 0:
            surplus = -net_load
            export_quota = (min(surplus, constraints.grid_export_limit_kw)
                if pv_dispatch_policy == 'load_first_export_priority' and constraints.grid_export_enabled else 0.0)
            mode, power = 'charge', surplus-export_quota
            reasons.append('pv_export_priority' if pv_dispatch_policy == 'load_first_export_priority' else 'pv_storage_priority')
        elif peak_discharge > EPSILON:
            mode, power = 'discharge', max(peak_discharge, requested_kw if requested_mode == 'discharge' else 0.0)
            if requested_mode != 'discharge' or power > requested_kw+EPSILON:
                reasons.append('demand_peak_shaving')
        elif requested_mode == 'charge':
            mode, power = 'charge', min(requested_kw, max(grid_limit-net_load, 0.0))
            if power < requested_kw-EPSILON:
                reasons.append('demand_charge_limit')
        else:
            mode, power = requested_mode, requested_kw
        power_limit = capability.max_charge_kw if mode == 'charge' else capability.max_discharge_kw
        if power > power_limit+EPSILON:
            reasons.append('equipment_power_limit')
        power = min(power, power_limit) if capability.available else 0.0
        if mode == 'discharge' and power > max(net_load, 0.0):
            # No storage export revenue is modeled by the existing input contract.
            power = max(net_load, 0.0)
            reasons.append('load_limit')
        headroom = max(upper-energy, 0.0) if mode == 'charge' else max(energy-lower, 0.0)
        battery_rate = power*capability.charge_efficiency if mode == 'charge' else power/capability.discharge_efficiency
        active_hours = min(INTERVAL_HOURS, headroom/battery_rate) if power > EPSILON else 0.0
        average_kw = power*active_hours/INTERVAL_HOURS
        if mode in ('charge', 'discharge') and power > EPSILON and headroom <= battery_rate*INTERVAL_HOURS+EPSILON:
            reason = 'soc_upper_limit' if mode == 'charge' else 'soc_lower_limit'
            reasons.append(reason)
            if active_hours > EPSILON:
                events.append(dict(at=(point.timestamp+timedelta(hours=active_hours)).isoformat(),
                    reason=reason, soc_pct=constraints.soc_max_pct if mode == 'charge' else constraints.soc_min_pct))
        actual_mode = mode if average_kw > EPSILON else 'idle'
        if actual_mode == 'idle':
            average_kw, active_hours = 0.0, 0.0
        charge = average_kw if actual_mode == 'charge' else 0.0
        discharge = average_kw if actual_mode == 'discharge' else 0.0
        energy += INTERVAL_HOURS*(charge*capability.charge_efficiency-discharge/capability.discharge_efficiency)
        grid = net_load+charge-discharge
        excess = max(grid-constraints.demand_limit_kw, 0.0)
        if excess > EPSILON:
            reasons.append('predicted_demand_shortfall')
        export = max(-grid, 0.0)
        curtailed = 0.0
        if customer_pv:
            export_limit = constraints.grid_export_limit_kw if constraints.grid_export_enabled else 0.0
            curtailed = max(export-export_limit, 0.0)
            export = min(export, export_limit)
        plan.append(PlanPoint(timestamp=point.timestamp, mode=actual_mode, target_power_kw=average_kw,
            expected_soc_pct=max(constraints.soc_min_pct, min(constraints.soc_max_pct, energy/capacity*100)),
            grid_import_kw=max(grid, 0.0), grid_export_kw=export,
            pv_unabsorbed_kw=curtailed, demand_exceed_kw=excess))
        intervals.append(dict(timestamp=point.timestamp.isoformat(), requested_mode=requested_mode,
            requested_power_kw=requested_kw, mode=actual_mode, setpoint_power_kw=power if active_hours else 0.0,
            average_power_kw=average_kw, active_minutes=active_hours*60,
            idle_minutes=(INTERVAL_HOURS-active_hours)*60, reasons=reasons))
    summary = dict(charge_hours=sum(p['active_minutes']/60 for p in intervals if p['mode']=='charge'),
        discharge_hours=sum(p['active_minutes']/60 for p in intervals if p['mode']=='discharge'),
        idle_hours=sum(p['idle_minutes']/60 for p in intervals),
        demand_shortfall_points=sum('predicted_demand_shortfall' in p['reasons'] for p in intervals))
    return plan, dict(policy_version=EMS_BASELINE_POLICY, usage='forecast_simulation',
        intervals=intervals, events=events, summary=summary)
