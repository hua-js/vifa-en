"""On-demand, read-only forward PV evaluation; reports stay on local disk."""
from shared.project import get_project
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from .forecast_source import _load_power, _time
from .upstream import NocoBaseClient


def metrics(pairs):
    errors = [p['forecast_kw']-p['actual_kw'] for p in pairs if p['actual_kw'] is not None]
    return {'matched_points': len(errors), 'mae_kw': sum(map(abs, errors))/len(errors) if errors else None,
            'rmse_kw': math.sqrt(sum(e*e for e in errors)/len(errors)) if errors else None}


def evaluate(client, run, now):
    allowed_sources = {s.source_code for s in get_project().stations if s.has_pv}
    start, end = _time(run['forecast_start']), _time(run['forecast_end'])
    if (run['es_sn'] not in allowed_sources or run['run_kind'] != 'operational' or run['status'] != 'completed'
            or end > now or end-start != timedelta(days=1) or run['expected_points'] != 96
            or run['interval_minutes'] != 15 or not _time(run['as_of']) <= _time(run['generated_at']) < start):
        raise ValueError('invalid completed forward forecast')
    points = client.list_rows('energy_pv_forecast_points', fields='run_pk,target_time,horizon_step,forecast_kw',
        filters={'run_pk': {'$eq': run['id']}}, sort='horizon_step', page_size=100)
    if len(points) != 96:
        raise ValueError('incomplete forecast')
    raw = client.list_rows('t_es_data', fields='timestamp,es_sn,ac_solar_power',
        filters={'es_sn': {'$eq': run['es_sn']}, 'timestamp': {'$gte': start.isoformat(), '$lt': end.isoformat()}},
        sort='timestamp', page_size=2000, max_pages=100)
    seen, minute = set(), defaultdict(list)
    for row in raw:
        at = _time(row['timestamp'])
        if row['es_sn'] != run['es_sn'] or not start <= at < end or at in seen:
            raise ValueError('invalid or duplicate measured timestamp')
        seen.add(at)
        if row.get('ac_solar_power') is not None:
            minute[at.replace(second=0, microsecond=0)].append(_load_power(row['ac_solar_power']))
    actual = {at: sum(values)/len(values) for at, values in minute.items()}
    pairs = []
    for index, point in enumerate(points):
        at = start+timedelta(minutes=15*index)
        if (str(point['run_pk']) != str(run['id']) or type(point['horizon_step']) is not int
                or point['horizon_step'] != index+1 or _time(point['target_time']) != at):
            raise ValueError('invalid forecast association/grid')
        values = [actual[at+timedelta(minutes=i)] for i in range(15) if at+timedelta(minutes=i) in actual]
        pairs.append({'target_time': at.isoformat(), 'forecast_kw': _load_power(point['forecast_kw']),
            'actual_kw': sum(values)/len(values) if len(values) >= 12 else None, 'valid_minutes': len(values)})
    complete = all(p['actual_kw'] is not None for p in pairs)
    energy = {'forecast_kwh': sum(p['forecast_kw'] for p in pairs)/4,
              'actual_kwh': sum(p['actual_kw'] for p in pairs)/4 if complete else None}
    energy['error_kwh'] = energy['forecast_kwh']-energy['actual_kwh'] if complete else None
    days = defaultdict(list)
    for point in pairs:
        days[_time(point['target_time']).date().isoformat()].append(point)
    daily = {day: {'complete': len(items) == 96 and all(p['actual_kw'] is not None for p in items),
                  'error_kwh': sum(p['forecast_kw']-p['actual_kw'] for p in items)/4
                  if len(items) == 96 and all(p['actual_kw'] is not None for p in items) else None}
             for day, items in days.items()}
    return {'policy': 'pv-forward-accuracy-v1', 'run_id': run['run_id'], 'model_version': run['model_version'],
        'evaluated_at': now.isoformat(), 'forecast_start': start.isoformat(), 'forecast_end': end.isoformat(),
        'quality_rule': 'equal-weight minute means; >=12 valid minutes per quarter; no zero fill',
        'daytime_definition': '06:00 <= interval start < 18:00 Asia/Shanghai; fixed clock proxy, not solar elevation',
        'all_points': metrics(pairs), 'daytime': metrics([p for p in pairs if 6 <= _time(p['target_time']).hour < 18]),
        'coverage_pct': 100*sum(p['actual_kw'] is not None for p in pairs)/96,
        'window_energy': energy, 'natural_days': daily, 'pairs': pairs,
        'source_sha256': hashlib.sha256(json.dumps({'run': run, 'points': points, 'actual': raw}, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New immutable local report directory')
    parser.add_argument('--station', required=True, choices=[s.id for s in get_project().stations if s.has_pv])
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    from .api import upstream_token
    client = NocoBaseClient(upstream_token())
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    from .pv_forecast_source import RUN_FIELDS
    runs = client.list_rows('energy_pv_forecast_runs', fields=RUN_FIELDS,
        filters={'es_sn': {'$eq': get_project().station(args.station).source_code}, 'status': {'$eq': 'completed'}, 'run_kind': {'$eq': 'operational'},
                 'forecast_end': {'$lte': now.isoformat()}}, sort='-forecast_end,-id', page_size=20, limit=20)
    args.output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for index, run in enumerate(runs):
        report = evaluate(client, run, now)
        filename = f'{index:02d}.json'
        (args.output/filename).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        summaries.append({k: report[k] for k in ('run_id', 'model_version', 'coverage_pct', 'daytime', 'window_energy')})
    (args.output/'summary.json').write_text(json.dumps({'reports': summaries, 'count': len(summaries)}, ensure_ascii=False, indent=2)+'\n')
    print(f'Saved {len(summaries)} matured forecast reports to {args.output}')


if __name__ == '__main__':
    main()
