"""Construct optimizer requests from manual limits and a real observed state."""
from datetime import datetime
import hashlib
import json

from m4.optimizer.contracts import (
    CapabilitySnapshot, ForecastPoint, ObjectiveProfile,
    OptimizationConstraints, OptimizationRequest,
)
from .models import LiveStationState, ResolvedControlLimits, StationConfiguration
from .roster import CABINET_ISOLATION_POLICY, STATION_CABINETS


def build_request(
    configuration: StationConfiguration,
    live_state: LiveStationState,
    *,
    request_id: str,
    plan_start_at: datetime,
    points: list[ForecastPoint],
    source_versions: dict[str, str],
    profiles: list[ObjectiveProfile],
    control_limits: ResolvedControlLimits | None = None,
    solver_time_limit_seconds: float = 30.0,
    solver_mip_rel_gap: float = 0.01,
) -> OptimizationRequest:
    configuration = StationConfiguration.model_validate(configuration.model_dump())
    live_state = LiveStationState.model_validate(live_state.model_dump())
    parameters = configuration.parameters
    if parameters is None or not configuration.version:
        raise ValueError('本站调度参数尚未配置')
    if configuration.station_id != live_state.station_id:
        raise ValueError('实时状态与配置必须属于同一电站')
    if not live_state.available:
        raise ValueError('本站无可参与柜，不生成新候选，EMS 当前计划保持不变')
    if control_limits is None:
        raise ValueError('控制来源尚未读取，不能使用旧手填值生成计划')
    control_limits = ResolvedControlLimits.model_validate(control_limits.model_dump())
    if control_limits.station_id != configuration.station_id:
        raise ValueError('控制配置与实时状态必须属于同一电站')
    resolved = control_limits.model_dump()
    if resolved['grid_import_limit_kw'] is None:
        # Saved inputs before the explicit need_kw hard-limit policy retain
        # their original manual boundary when independently reconstructed.
        resolved.pop('grid_import_limit_kw')
    merged = {**parameters.model_dump(), **resolved}
    constraints = OptimizationConstraints(**{
        name: merged[name] for name in OptimizationConstraints.model_fields
    })
    roster = STATION_CABINETS[configuration.station_id][1]
    participants = live_state.participating_cabinet_ids
    excluded = [cabinet_id for cabinet_id in roster if cabinet_id not in participants]
    # Preserve the original per-cabinet allocation. An excluded cabinet's share
    # must not be redistributed to the remaining cabinets.
    fraction = len(participants) / len(roster)
    capability_limits = {
        name: getattr(parameters, name) * fraction
        for name in ('energy_capacity_kwh', 'max_charge_kw', 'max_discharge_kw')
    }
    if control_limits.station_energy_capacity_kwh is not None:
        capability_limits['energy_capacity_kwh'] = control_limits.station_energy_capacity_kwh*fraction
    for name in ('max_charge_kw', 'max_discharge_kw'):
        cabinet_limit = getattr(control_limits, 'cabinet_'+name)
        if cabinet_limit is not None:
            capability_limits[name] = min(capability_limits[name], cabinet_limit*len(participants))
    scope = {
        'policy': CABINET_ISOLATION_POLICY,
        'station_id': configuration.station_id,
        'configured_cabinet_ids': roster,
        'participating_cabinet_ids': participants,
        'excluded_cabinet_ids': excluded,
        'capability_limits': capability_limits,
    }
    scope_version = hashlib.sha256(json.dumps(
        scope, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()[:24]
    capacity_label = '接口' if control_limits.station_energy_capacity_kwh is not None else '配置'
    capability = CapabilitySnapshot(
        initial_soc_pct=live_state.initial_soc_pct, available=live_state.available,
        **capability_limits,
        charge_efficiency=parameters.charge_efficiency,
        discharge_efficiency=parameters.discharge_efficiency,
        derating_reason=(
            f'{capacity_label}容量及功率按固定 {len(roster)} 柜等分，参与 {len(participants)} 柜；'
            f'参与：{", ".join(participants)}；排除：{", ".join(excluded) or "无"}。'
            '本候选仅适用于参与柜，不包含排除柜的停机指令；'
            + ('容量及单柜功率优先使用接口来源，人工功率限值可进一步收紧；未接 EMS 动态可调度能力'
               if control_limits.station_energy_capacity_kwh is not None
               else '使用人工配置能力边界，未接 EMS 动态可调度能力')
        ),
    )
    return OptimizationRequest(
        request_id=f'{request_id}/{configuration.version}/{control_limits.source_version}/{scope_version}',
        station_id=configuration.station_id,plan_start_at=plan_start_at,
        input_observed_at=live_state.observed_at,
        max_input_age_seconds=parameters.max_input_age_seconds,
        interval_minutes=15,horizon_points=len(points),points=points,
        capability=capability,constraints=constraints,profiles=profiles,
        pv_dispatch_policy=parameters.pv_dispatch_policy,
        source_versions={**source_versions,
            'constraints':f'{configuration.version}/{control_limits.source_version}',
            'capability':f'manual-limits/{configuration.version}/live/{live_state.source_version}/scope/{scope_version}',
            'cabinet_policy':CABINET_ISOLATION_POLICY,
            'participating_cabinets':json.dumps(participants, separators=(',', ':')),
            'excluded_cabinets':json.dumps(excluded, separators=(',', ':'))},
        solver_time_limit_seconds=solver_time_limit_seconds,
        solver_mip_rel_gap=solver_mip_rel_gap,
    )
