"""Reconstruct advisory coverage from immutable rolling archives, never execution."""
from datetime import datetime, timedelta
import math

STEP = timedelta(minutes=15)


def advisory_history(station, jobs, now):
    day = now.date().isoformat()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    versions, skipped = [], 0
    for job in jobs:
        try:
            if job.get('station_id') != station or job.get('status') != 'completed':
                continue
            p = job['result']
            if p['date'] != day:
                continue
            if (p['station_id'] != station or p['schema_version'] != 'm4-rolling-plan-v1'
                    or p['dispatch_status'] != 'not_dispatched'):
                raise ValueError('invalid identity')
            effective, finished, until = (datetime.fromisoformat(p[k]) for k in
                ('effective_at', 'finished_at', 'valid_until'))
            if any(t.tzinfo is None for t in (effective, finished, until)):
                raise ValueError('naive timestamp')
            if (not midnight <= effective < midnight+timedelta(days=1)
                    or finished > now or finished >= effective or until <= effective
                    or (effective-midnight).total_seconds() % 900):
                raise ValueError('invalid window')
            inputs = {v['timestamp']: v for v in p.get('request', {}).get('points', [])}
            points = []
            for i, point in enumerate(p['plan']):
                at = datetime.fromisoformat(point['timestamp'])
                if at != effective+i*STEP or at+STEP > midnight+timedelta(days=1):
                    raise ValueError('invalid timeline')
                if point['mode'] not in ('charge', 'discharge', 'idle'):
                    raise ValueError('invalid action')
                for key in ('target_power_kw', 'grid_import_kw', 'expected_soc_pct'):
                    v = point[key]
                    if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                        raise ValueError('invalid value')
                if point['expected_soc_pct'] > 100:
                    raise ValueError('invalid SOC')
                item = {k: point[k] for k in ('timestamp', 'mode', 'target_power_kw',
                    'grid_import_kw', 'expected_soc_pct')}
                export = point.get('grid_export_kw')
                if type(export) in (int, float) and math.isfinite(export) and export >= 0:
                    item['grid_export_kw'] = export
                item['timestamp'] = at.astimezone(now.tzinfo).isoformat()
                source = inputs.get(point['timestamp'], {})
                for key in ('load_forecast_kw', 'pv_forecast_kw'):
                    value = source.get(key)
                    if type(value) in (int, float) and math.isfinite(value):
                        item[key] = value
                points.append(item)
            if not points or not isinstance(job['run_id'], str):
                raise ValueError('empty archive')
            versions.append(dict(run_id=job['run_id'], daily_run_id=p['daily_run_id'],
                source=p['source'], finished_at=finished.isoformat(),
                effective_at=effective.isoformat(), valid_until=until.isoformat(),
                plan=points, _effective=effective, _finished=finished, _until=until))
        except (KeyError, ValueError, TypeError, AttributeError):
            skipped += 1
    versions.sort(key=lambda v: (v['_effective'], v['_finished'], v['run_id']))
    # A newer version supersedes only at its effective time, never retroactively.
    winners = {}
    for version in versions:
        for point in version['plan']:
            winners[point['timestamp']] = version['run_id']
    history = []
    for version in versions:
        until = version['_until']
        for point in version['plan']:
            at = datetime.fromisoformat(point['timestamp'])
            if winners[point['timestamp']] != version['run_id']:
                state = 'superseded'
            elif at >= until and now >= until:
                state = 'expired'
            elif at+STEP <= now:
                state = 'ended_unverified'
            elif at <= now:
                state = 'active_unverified'
            else:
                state = 'pending'
            point['state'] = state
            if state in ('ended_unverified', 'active_unverified'):
                history.append(dict(point, run_id=version['run_id'], source=version['source']))
        for key in ('_effective', '_finished', '_until'):
            del version[key]
    return dict(schema_version='m4-rolling-history-v1', station_id=station, date=day,
        dispatch_status='not_dispatched', usage='advisory_history_only',
        points=sorted(history, key=lambda p: p['timestamp']),
        versions=list(reversed(versions)), skipped=skipped)
