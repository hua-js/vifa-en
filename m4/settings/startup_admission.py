"""Station-2 early valley exception; never a general quality-gate bypass."""
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

from shared.project import get_project
from .ems_simulation import _slot
from .frozen_baseline import baseline_configuration, baseline_version
from .load_accuracy import assess, require_gate

POLICY = 'night-valley-ems-startup-v1'
ZONE = ZoneInfo('Asia/Shanghai')


def window(station, periods, now):
    """Intersect the midnight-connected valley with configured charging slots."""
    now = now.astimezone(ZONE)
    if (get_project().id != 'vifa' or station != 'station-2'
            or not isinstance(periods, list) or len(periods) != 96
            or any(p not in ('gu', 'ping', 'feng', 'jian') for p in periods)):
        return None
    end = next((i for i, period in enumerate(periods) if period != 'gu'), 96)
    # An all-day valley does not establish a distinct early-morning window.
    if not 0 < end <= 48:
        return None
    baseline = baseline_configuration(station)
    if baseline is None:
        return None
    slots = set()
    for row in baseline['schedule']:
        if row['mode'] != 'charge' or row['power_kw'] <= 0:
            continue
        a, b = _slot(row['start_time']), _slot(row['end_time'])
        slots.update(range(a, b) if a < b else [*range(a, 96), *range(b)])
    current = now.hour*4 + now.minute//15
    if current >= end or current not in slots or current+1 >= end or current+1 not in slots:
        return None
    stop = current+1
    while stop < end and stop in slots:
        stop += 1
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return dict(policy=POLICY, effective_at=(midnight+timedelta(minutes=15*(current+1))).isoformat(),
                end_at=(midnight+timedelta(minutes=15*stop)).isoformat())


def require_admission(gate, station, periods, now):
    checked = assess(gate.get('evidence') if isinstance(gate, dict) else None, station, now)
    if not isinstance(gate, dict) or gate.get('version') != checked['version']:
        raise ValueError('负荷质量证据已变化，请重新读取。')
    if checked['status'] == 'ready':
        require_gate(gate, station, now)
        return None
    eligible = window(station, periods, now)
    if checked['startup_fallback_eligible'] and eligible:
        return eligible
    require_gate(gate, station, now)


def request_window(request, now):
    """Recheck the bounded exception at the write boundary, independently."""
    versions = request.get('source_versions', {})
    if versions.get('startup_policy') != POLICY:
        raise ValueError('凌晨保底策略版本无效。')
    station = request.get('station_id')
    periods = json.loads(versions.get('startup_tariff_periods', 'null'))
    if not isinstance(periods, list) or versions.get('ems_baseline') != baseline_version(station):
        raise ValueError('凌晨保底时段或基线已变化。')
    allowed = window(station, periods, now)
    if (not allowed or allowed['effective_at'] != request.get('plan_start_at')
            or allowed['end_at'] != versions.get('startup_window_end')):
        raise ValueError('已离开凌晨谷电充电窗口，不允许保底下发。')
    points = request.get('points', [])
    start, end = (datetime.fromisoformat(allowed[k]) for k in ('effective_at', 'end_at'))
    expected = [start+timedelta(minutes=15*i) for i in range(int((end-start).total_seconds()/900))]
    if ([datetime.fromisoformat(p['timestamp']) for p in points] != expected
            or any(p.get('tariff_period') != 'gu' for p in points)):
        raise ValueError('凌晨保底计划超出谷电充电窗口。')
    return end
