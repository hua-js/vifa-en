"""Versioned operating floor and conservative end-inventory cost adjustment."""
import math

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
