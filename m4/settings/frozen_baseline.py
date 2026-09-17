"""Explicit per-project EMS reference; execution schedule never updates it."""
from copy import deepcopy
import hashlib
import json
import math

from shared.project import get_project
from .control_sources import _business_values
from .ems_simulation import _slot


def baseline_configuration(station_id):
    baseline = deepcopy(get_project().station(station_id).ems_baseline)
    if baseline is None:
        return None  # Legacy/custom projects retain their explicit live-source policy.
    required = {'version', 'confirmed_at', 'power_scope', 'soc_limits_source',
                'soc_min_pct', 'soc_max_pct', 'schedule'}
    if set(baseline) != required or baseline['power_scope'] not in ('cabinet', 'station'):
        raise ValueError('固定 EMS 基线配置格式无效。')
    if any(not isinstance(baseline[k], str) or not baseline[k].strip()
           for k in ('version', 'confirmed_at', 'soc_limits_source')):
        raise ValueError('固定 EMS 基线缺少版本或来源。')
    lo, hi = baseline['soc_min_pct'], baseline['soc_max_pct']
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (lo, hi))
            or not 0 <= lo < hi <= 100):
        raise ValueError('固定 EMS 基线 SOC 范围无效。')
    if not isinstance(baseline['schedule'], list) or not baseline['schedule']:
        raise ValueError('固定 EMS 基线时段缺失。')
    occupied = set()
    for row in baseline['schedule']:
        if not isinstance(row, dict) or set(row) != {'id', 'start_time', 'end_time', 'mode', 'power_kw', 'repeat'}:
            raise ValueError('固定 EMS 基线时段格式无效。')
        a, b = _slot(row['start_time']), _slot(row['end_time'])
        power = row['power_kw']
        if (a == b or a == 96 or row['repeat'] != 'daily'
                or row['mode'] not in ('charge', 'discharge')
                or type(power) not in (int, float) or not math.isfinite(power) or power < 0):
            raise ValueError('固定 EMS 基线时段或功率无效。')
        slots = set(range(a, b)) if b > a else set(range(a, 96)) | set(range(b))
        if slots & occupied:
            raise ValueError('固定 EMS 基线时段重叠。')
        occupied |= slots
    return baseline


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()[:20]


def baseline_version(station_id):
    baseline = baseline_configuration(station_id)
    return 'fixed-ems-'+_digest(baseline) if baseline is not None else None


def planning_controls(live):
    """Bind frozen schedule/power to current demand/capacity safety inputs.

    Callers retain the unmodified live controls separately for inspection.
    Hash only planning dependencies; execution edits must not invalidate results.
    """
    baseline = baseline_configuration(live['station_id'])
    if baseline is None:
        return deepcopy(live)
    result = deepcopy(live)
    result.update(schedule=deepcopy(baseline['schedule']), power_scope=baseline['power_scope'],
                  baseline_source='fixed_configuration', baseline_version=baseline_version(live['station_id']),
                  baseline_configuration=baseline)
    for row in result['schedule']:
        row.update(soc_min_pct=baseline['soc_min_pct'], soc_max_pct=baseline['soc_max_pct'])
    result['source_health'] = {**result.get('source_health', {}), 'schedule': 'ready'}
    content = {k: result.get(k) for k in ('station_id', 'source_station_id', 'status',
        'power_scope', 'storage_capacity', 'configured_cabinet_count', 'control_policy_version',
        'demand', 'reverse_flow', 'baseline_version', 'schedule')}
    result['version'] = live['station_id']+'-planning-'+_digest(_business_values(content))
    # Live schedule diagnostics belong to execution_controls, not this reference.
    result['warnings'] = [w for w in result.get('warnings', []) if '每日充放电计划' not in w]
    return result
