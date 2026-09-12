"""Interpret upstream schedule power using the user-confirmed cabinet scope."""
import math

from .roster import STATION_CABINETS


SCHEDULE_POWER_POLICY = 'm4-control-policy-v5-need-import-limit'


def cabinet_power_limits(control):
    """Keep old evidence semantics; new inputs require both cabinet mode limits."""
    if control.get('power_scope') != 'cabinet':
        if control.get('control_policy_version') == SCHEDULE_POWER_POLICY:
            raise ValueError('充放模式表的单柜功率口径缺失。')
        return None
    if control.get('source_health', {}).get('schedule') != 'ready':
        raise ValueError('充放模式表尚未就绪，无法取得单柜功率上限。')
    limits = {}
    for mode in ('charge', 'discharge'):
        values = [p.get('power_kw') for p in control.get('schedule', []) if p.get('mode') == mode]
        if not values or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError('充放模式表须提供有效的单柜充电、放电功率，不能以固定值补齐。')
        limits['max_'+mode+'_kw'] = max(values)
    return limits


def station_energy_capacity(configuration, control):
    if control.get('station_id') != configuration.station_id:
        raise ValueError('容量来源与当前电站不一致。')
    capacity = control.get('storage_capacity')
    if capacity is None and control.get('control_policy_version') != SCHEDULE_POWER_POLICY:
        # Only old saved evidence may retain its original manual-capacity basis.
        return configuration.parameters.energy_capacity_kwh
    expected_sn = STATION_CABINETS[configuration.station_id][0]
    if (not isinstance(capacity, dict) or capacity.get('scope') != 'station'
            or capacity.get('source_station_id') != expected_sn
            or capacity.get('source_table') != 't_es' or capacity.get('source_field') != 'es_power_storage'
            or type(capacity.get('energy_capacity_kwh')) not in (int, float)
            or not math.isfinite(capacity['energy_capacity_kwh']) or capacity['energy_capacity_kwh'] <= 0):
        raise ValueError('电站表 es_power_storage 必须提供本站有效的正数容量，不能使用手填值替代。')
    return capacity['energy_capacity_kwh']


def effective_station_power(configuration, control, cabinet_count=None):
    count = len(STATION_CABINETS[configuration.station_id][1])
    active = count if cabinet_count is None else cabinet_count
    if type(active) is not int or not 0 <= active <= count or control.get('station_id') != configuration.station_id:
        raise ValueError('功率来源电站或参与柜数量不一致。')
    limits = cabinet_power_limits(control)
    return {name: min(getattr(configuration.parameters, name)*active/count, limits[name]*active)
        if limits is not None else getattr(configuration.parameters, name)*active/count
        for name in ('max_charge_kw', 'max_discharge_kw')}


def station_schedule(control):
    count = len(STATION_CABINETS[control['station_id']][1])
    cabinet_power_limits(control)
    if control.get('power_scope') not in ('station', 'cabinet'):
        raise ValueError('充放模式表的功率作用范围未知。')
    multiplier = count if control['power_scope'] == 'cabinet' else 1
    return [{**p, 'power_kw': p['power_kw']*multiplier} for p in control['schedule']]


def schedule_modes(schedule):
    """Expand original configured directions, never the SOC-limited EMS replay."""
    from .ems_simulation import _slot
    modes = ['idle'] * 96
    occupied = set()
    for item in schedule:
        start, end = _slot(item['start_time']), _slot(item['end_time'])
        if (start == 96 or start == end or item.get('repeat') != 'daily'
                or item.get('mode') not in ('charge', 'discharge')):
            raise ValueError('Invalid EMS schedule direction')
        indices = range(start, end) if end > start else [*range(start, 96), *range(end)]
        for i in indices:
            if i in occupied:
                raise ValueError('Overlapping EMS schedule')
            occupied.add(i)
            modes[i] = item['mode']
    if not occupied:
        raise ValueError('Missing EMS schedule')
    return modes
