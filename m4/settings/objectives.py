"""Versioned server-owned objectives for real-data candidate debugging only.

The approved ordering comes from CODEX_HANDOFF.md. The normalization references,
weights and lock tolerances below are explicit debugging defaults, not approved
production tuning and not copied from the offline Mock profiles. A station may
partially override these numeric settings with its own version. Neither that
configuration nor these functions can change safety limits or objective order.
"""
import hashlib
import json
from typing import Annotated

from pydantic import Field

from m4.optimizer.contracts import ObjectiveLayer, ObjectiveProfile, StrictModel
from .roster import STATION_CABINETS


Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ObjectiveSettings(StrictModel):
    version: Annotated[str, Field(min_length=1, pattern=r'\S')]
    balanced_cost_reference_cny: Positive
    balanced_pv_reference_kwh: Positive
    balanced_cost_weight: Positive
    balanced_pv_weight: Positive
    absolute_tolerance: NonNegative
    relative_tolerance: NonNegative


# Service-owned configuration, intentionally separate from station parameters.
# Change this version for a reviewed configuration update; the content hash also
# prevents changed weights or tolerances from retaining the same output version.
GLOBAL_OBJECTIVE_DEFAULTS = {
    'version': 'candidate-debug-v2-early-valley',
    'balanced_cost_reference_cny': 1000.0,
    'balanced_pv_reference_kwh': 1000.0,
    'balanced_cost_weight': 1.0,
    'balanced_pv_weight': 1.0,
    'absolute_tolerance': 1e-6,
    'relative_tolerance': 0.0,
}
# Example shape: {'station-2': {'version': 's2-debug-v2', 'balanced_pv_weight': 2.0}}
# Empty means both stations inherit the same global configuration.
STATION_OBJECTIVE_OVERRIDES: dict[str, dict] = {}


# Only numeric tuning is overridable. Each objective occurs exactly once;
# Demand stays first; early charging is only a final tie-break after throughput.
ORDERS = {
    'balanced': ('demand_peak', 'demand_duration', 'soc_preferred_deviation',
                 'balanced_value', 'throughput', 'valley_charge_delay'),
    'cost': ('demand_peak', 'demand_duration', 'energy_cost', 'pv_unused',
             'soc_preferred_deviation', 'throughput', 'valley_charge_delay'),
    'pv': ('demand_peak', 'demand_duration', 'pv_unused', 'energy_cost',
           'soc_preferred_deviation', 'throughput', 'valley_charge_delay'),
}


def _resolve(station_id: str) -> tuple[ObjectiveSettings, str]:
    if not isinstance(station_id, str) or station_id not in STATION_CABINETS:
        raise ValueError('目标配置仅支持本站已知电站')
    base = ObjectiveSettings.model_validate(GLOBAL_OBJECTIVE_DEFAULTS)
    if station_id not in STATION_OBJECTIVE_OVERRIDES:
        return base, 'global'
    override = STATION_OBJECTIVE_OVERRIDES[station_id]
    if not isinstance(override, dict) or 'version' not in override:
        raise ValueError('电站目标覆盖配置必须提供独立版本')
    return ObjectiveSettings.model_validate({**base.model_dump(), **override}), station_id


def _configuration_version(settings: ObjectiveSettings, scope: str) -> str:
    digest = hashlib.sha256(json.dumps({
        'usage': 'candidate_debug', 'scope': scope,
        'settings': settings.model_dump(), 'orders': ORDERS,
    }, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()[:24]
    return f'{settings.version}/{scope}/{digest}'


def _build_profiles(settings: ObjectiveSettings, scope: str) -> list[ObjectiveProfile]:
    version = _configuration_version(settings, scope)
    combined = {
        'energy_cost': settings.balanced_cost_weight / settings.balanced_cost_reference_cny,
        'pv_unused': settings.balanced_pv_weight / settings.balanced_pv_reference_kwh,
    }
    return [ObjectiveProfile(
        profile_id=profile_id,
        profile_version=f'{version}/{profile_id}',
        objective_order=[ObjectiveLayer(
            name=objective.replace('_', '-'),
            terms=combined.copy() if objective == 'balanced_value' else {objective: 1.0},
            absolute_tolerance=settings.absolute_tolerance,
            relative_tolerance=settings.relative_tolerance,
        ) for objective in order],
    ) for profile_id, order in ORDERS.items()]


def get_profiles(station_id: str) -> list[ObjectiveProfile]:
    """Return fresh validated profiles; callers cannot mutate shared defaults."""
    settings, scope = _resolve(station_id)
    return _build_profiles(settings, scope)


def get_daily_profiles(station_id: str) -> list[ObjectiveProfile]:
    """Versioned daily overrides: station 1 cost first; station 2 peak reserve."""
    from .daily_policy import daily_policy_version
    profiles = get_profiles(station_id)
    if station_id == 'station-1':
        settings, _ = _resolve(station_id)
        for profile in profiles:
            profile.profile_version += '/station-1-cost-first-v1/station-1-continuity-v2'
            profile.objective_order = [ObjectiveLayer(
                name=term.replace('_', '-'), terms={term: 1.0},
                absolute_tolerance=settings.absolute_tolerance,
                relative_tolerance=settings.relative_tolerance)
                for term in ('energy_cost', 'demand_peak', 'demand_duration',
                             'pv_unused', 'throughput', 'power_variation',
                             'soc_preferred_deviation', 'valley_charge_delay')]
        return profiles
    policy_version = daily_policy_version(station_id)
    if policy_version is None:
        return profiles
    settings, _ = _resolve(station_id)
    for profile in profiles:
        profile.profile_version += '/' + policy_version + '/station-2-continuity-v1'
        # Economic objectives precede reserve preparation; reserve is a tie-break.
        position = next(i for i, layer in enumerate(profile.objective_order) if layer.name == 'throughput')
        profile.objective_order.insert(position, ObjectiveLayer(
            name='peak-reserve-shortfall', terms={'peak_reserve_shortfall': 1.0},
            absolute_tolerance=settings.absolute_tolerance,
            relative_tolerance=settings.relative_tolerance))
        # Preserve all previous priorities, then prefer smoother contiguous power.
        profile.objective_order.insert(len(profile.objective_order)-1, ObjectiveLayer(
            name='power-variation', terms={'power_variation': 1.0},
            absolute_tolerance=settings.absolute_tolerance,
            relative_tolerance=settings.relative_tolerance))
    return profiles


def get_profile_metadata(station_id: str) -> dict:
    """Expose the tuning basis and versions without implying production approval."""
    settings, scope = _resolve(station_id)
    profiles = _build_profiles(settings, scope)
    return {
        'usage': 'candidate_debug',
        'production_approved': False,
        'scope': scope,
        'configuration_version': _configuration_version(settings, scope),
        'profile_versions': {profile.profile_id: profile.profile_version for profile in profiles},
        'normalization': {
            'cost_reference_cny': settings.balanced_cost_reference_cny,
            'pv_reference_kwh': settings.balanced_pv_reference_kwh,
            'cost_weight': settings.balanced_cost_weight,
            'pv_weight': settings.balanced_pv_weight,
        },
        'tolerances': {
            'absolute': settings.absolute_tolerance,
            'relative': settings.relative_tolerance,
        },
        'description': (
            '目标顺序沿用已确认优先级；数值配置仅用于真实数据候选调试，尚非已审批生产策略。'
            f'均衡综合层按 {settings.balanced_cost_reference_cny:g} 元、'
            f'{settings.balanced_pv_reference_kwh:g} kWh 固定参考尺度归一化，'
            f'成本与光伏未利用量权重为 {settings.balanced_cost_weight:g}、{settings.balanced_pv_weight:g}。'
            '站级覆盖不能改变需量优先顺序或设备、SOC、功率安全约束。'
            '末级偏好为凌晨谷段同价优先早充：按真实谷段标签识别零点起的连续谷段，'
            '在各自然日同价区间内保持充电总量、全部放电及区间外充电不变。'
            '未完成末级求解或仅获得可行解时，提示早充偏好尚未确认最优。'
        ),
    }
