"""Causal short-horizon PV calibration; original forecasts stay immutable."""
from datetime import datetime, timedelta
from collections import defaultdict
import hashlib
import json
import math
from statistics import median

POLICY = 'pv-recent-bias-v2'
MIN_POWER_KW = 20.0
MAX_ADJUSTMENT = 0.30
QUARTER = timedelta(minutes=15)


def timestamp(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('PV correction requires timezone-aware timestamps')
    return at


def number(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def correct_forecast(history, future, now, *, apply_recent_reserve=True):
    """Use only four closed quarters, with >=3 consistent, usable residuals.

    A 5% deadband rejects negligible residuals. Low-light points are excluded
    from ratios. Full correction lasts one hour, then decays to zero at two.
    This is an estimate, never permission to raise a device charging limit.
    """
    now = timestamp(now)
    closed = now.replace(minute=now.minute//15*15, second=0, microsecond=0)
    slots = {closed-QUARTER*i for i in range(1, 5)}
    usable = {}
    for item in history:
        at = timestamp(item['timestamp'])
        if at not in slots:
            continue
        if at in usable:
            raise ValueError('duplicate PV correction observation')
        f, a = item['forecast_kw'], item['actual_kw']
        if (number(f) and number(a) and f >= MIN_POWER_KW
                and type(item.get('valid_minutes')) is int and 12 <= item['valid_minutes'] <= 15):
            usable[at] = item
    status, ratio = 'unavailable', 0.0
    if len(usable) >= 3 and closed-QUARTER in usable:
        residuals = [(p['actual_kw']/p['forecast_kw']-1) for p in usable.values()]
        status = 'unchanged'
        if sum(r > .05 for r in residuals) >= 3 or sum(r < -.05 for r in residuals) >= 3:
            ratio = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, median(residuals)))
            status = 'applied'
    # Reserve against recent downside surprises separately from the central
    # estimate. Remove the bias already corrected to avoid double counting.
    # This empirical reserve is NOT a calibrated confidence bound.
    recent_overprediction = max((max(0.0, p['forecast_kw']*(1+min(ratio, 0.0))-p['actual_kw'])
        for p in usable.values()), default=0.0) if status != 'unavailable' else 0.0
    points = []
    for item in future:
        at, original = timestamp(item['timestamp']), item['forecast_kw']
        if at < now or not number(original):
            raise ValueError('invalid future PV correction point')
        hours = (at-now).total_seconds()/3600
        weight = min(1.0, max(0.0, 2-hours))
        corrected = original*(1+ratio*weight) if original >= MIN_POWER_KW else original
        reserve = min(MAX_ADJUSTMENT*original, recent_overprediction)*weight if original >= MIN_POWER_KW else 0.0
        candidate = max(0.0, min(original, corrected)-reserve)
        points.append(dict(timestamp=at.isoformat(), original_kw=original, corrected_kw=corrected,
            reserve_kw=reserve, reserve_candidate_kw=candidate,
            solver_kw=candidate if apply_recent_reserve else min(original, corrected)))
    return dict(policy=POLICY, status=status, as_of=now.isoformat(), ratio=ratio,
        reserve_applied=apply_recent_reserve,
        recent_overprediction_kw=recent_overprediction,
        observations=[{**usable[t], 'timestamp': t.isoformat()} for t in sorted(usable)], points=points)


def read_correction(client, station, inputs, now):
    """Read-only measurements; forecast failures never fabricate observations."""
    from shared.project import get_project
    config = get_project().station(station)
    if not config.has_pv:
        return None
    now = timestamp(now)
    closed = now.replace(minute=now.minute//15*15, second=0, microsecond=0)
    begin = closed-timedelta(hours=1)
    source = inputs['sources']['pv']
    generated = timestamp(source['generated_at'])
    forecast_start = timestamp(source['forecast_start'])
    minute, seen = defaultdict(list), set()
    raw = client.list_rows('t_es_data', fields='timestamp,es_sn,ac_solar_power',
        filters={'es_sn': {'$eq': config.source_code},
            'timestamp': {'$gte': begin.isoformat(), '$lt': closed.isoformat()}},
        sort='timestamp', page_size=2000, max_pages=2)
    for row in raw:
        at = timestamp(row['timestamp'])
        if row.get('es_sn') != config.source_code or not begin <= at < closed or at in seen:
            raise ValueError('invalid PV correction telemetry')
        seen.add(at)
        value = row.get('ac_solar_power')
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError('invalid PV correction power')
        value = float(value)
        if not number(value):
            raise ValueError('invalid PV correction power')
        minute[at.replace(second=0, microsecond=0)].append(value)
    means = {t: sum(v)/len(v) for t, v in minute.items()}
    history, future = [], []
    for point in inputs['request']['points']:
        at = timestamp(point['timestamp'])
        if begin <= at < closed and at >= forecast_start and generated <= at:
            values = [means[at+timedelta(minutes=i)] for i in range(15) if at+timedelta(minutes=i) in means]
            history.append(dict(timestamp=at, forecast_kw=point['pv_forecast_kw'],
                actual_kw=sum(values)/len(values) if values else None, valid_minutes=len(values)))
        if at >= closed+QUARTER:
            future.append(dict(timestamp=at, forecast_kw=point['pv_forecast_kw']))
    # The reserve remains a shadow candidate pending independent evaluation.
    result = correct_forecast(history, future, now, apply_recent_reserve=False)
    result.update(station_id=station, source_version=source['version'], run_id=source['run_id'],
        telemetry_sha256=hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
        solver_policy='downward-only-reserve-shadow-v1')
    result['version'] = POLICY+'-'+hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result
