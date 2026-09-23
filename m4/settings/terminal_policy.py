"""Versioned operating floor and conservative end-inventory cost adjustment."""
import math
from shared.project import get_project

POLICY = 'daily-operating-floor-v1'


def enabled(request):
    return request.source_versions.get('terminal_policy') == POLICY


def target(request, baseline_terminal):
    if enabled(request):
        if request.peak_reserve_policy is None:
            raise ValueError('日末目标缺失，暂停优化。')
        return request.peak_reserve_policy.terminal_soc_min_pct
    return baseline_terminal


def inventory_cost(request, baseline_soc, candidate_soc):
    if not enabled(request):
        return 0.0
    price = float(request.source_versions['terminal_inventory_price'])
    if not math.isfinite(price) or price < 0:
        raise ValueError('日末库存估值无效，暂停优化。')
    deficit = max(baseline_soc-candidate_soc, 0.0)*request.capability.energy_capacity_kwh/100.0
    return deficit*price/request.capability.charge_efficiency


ROLLING_REFERENCE_POLICY = 'remaining-ems-operating-floor-v2'
IDLE_SOC_TOLERANCE_PCT = get_project().m4['idle_soc_tolerance_pct']


def remaining_reference(request, capability, points, schedule):
    """Replay a conservative EMS reference without spending the operating floor.

    The saved EMS schedule is never mutated. The operating floor limits this
    fallback's discharge, not the optimizer's soft intermediate reserve.
    """
    from .ems_simulation import simulate_ems_day
    reference = schedule
    if enabled(request):
        floor = target(request, None)
        reference = [dict(row, soc_min_pct=max(
            row.get('soc_min_pct', request.constraints.soc_min_pct), floor))
            for row in schedule]
    plan, simulation = simulate_ems_day(capability, request.constraints, points,
        reference, pv_dispatch_policy=request.pv_dispatch_policy, remaining_day=True)
    if enabled(request):
        simulation = dict(simulation, reference_policy=ROLLING_REFERENCE_POLICY,
                          operating_soc_floor_pct=floor)
    return plan, simulation


def near_operating_floor(initial_soc, floor):
    return floor-IDLE_SOC_TOLERANCE_PCT-1e-6 <= initial_soc <= floor+1e-6


def idle_floor_tolerance(request, plan):
    """Only a zero-power idle plan may retain the small observed SOC deficit."""
    if not enabled(request) or not plan:
        return False
    floor = target(request, None)
    initial = request.capability.initial_soc_pct
    return (near_operating_floor(initial, floor)
            and initial >= request.constraints.soc_min_pct
            and all(p.mode == 'idle' and p.target_power_kw == 0.0 for p in plan)
            and plan[-1].expected_soc_pct >= initial-1e-6)


def spends_floor_from_empty(request, plan):
    """Near the operating floor, tolerance never becomes usable inventory."""
    if not enabled(request):
        return False
    floor = target(request, None)
    initial = request.capability.initial_soc_pct
    return (near_operating_floor(initial, floor)
            and any(p.expected_soc_pct < min(initial, floor)-1e-6
                    or (p.mode == 'discharge' and p.expected_soc_pct < floor-1e-6)
                    for p in plan))
