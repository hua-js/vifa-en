#!/usr/bin/env python3
"""Fetch an immutable ES02 Archive API batch containing only elapsed hours."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

spec = importlib.util.spec_from_file_location('archive_weather_shared', Path(__file__).with_name('fetch-open-meteo-weather.py'))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
history = shared.history
ENDPOINT = 'https://archive-api.open-meteo.com/v1/archive'
VERSION = 'open-meteo-elapsed-archive-v1'


def parameters(start_date, end_date):
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start < date(2026, 9, 1) or not 0 <= (end-start).days <= 366:
        raise ValueError('archive range must start September 2026 or later and span at most 367 days')
    return {'latitude': 23, 'longitude': 113, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
        'hourly': ','.join(shared.FIELD_MAP), 'timezone': 'Asia/Shanghai', 'temperature_unit': 'celsius',
        'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm', 'timeformat': 'iso8601'}


def prepare_snapshot(envelope, raw):
    if len(raw) > shared.MAX_BYTES or hashlib.sha256(raw).hexdigest() != envelope['response_sha256']:
        raise ValueError('archive response checksum/size mismatch')
    params = envelope['parameters']
    if (envelope['version'] != 1 or envelope['es_sn'] != 'ES02' or envelope['endpoint'] != ENDPOINT
            or params != parameters(params['start_date'], params['end_date'])):
        raise ValueError('archive station/endpoint/parameters mismatch')
    started = shared.timestamp(envelope['request_started_at'], explicit=True)
    received = shared.timestamp(envelope['received_at'], explicit=True)
    fetched = shared.timestamp(envelope['fetched_at'], explicit=True)
    if started > received or shared.database_time(received) != fetched or date.fromisoformat(params['end_date']) > started.date():
        raise ValueError('archive request chronology invalid')
    data = shared.load_json(raw)
    if data.get('error') or data.get('timezone') != 'Asia/Shanghai' or data.get('utc_offset_seconds') != 28800:
        raise ValueError('archive response error/timezone mismatch')
    for name, center in [('latitude', 23), ('longitude', 113)]:
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value-center) > 1:
            raise ValueError('archive grid mismatch')
    units, hourly = data.get('hourly_units', {}), data.get('hourly', {})
    if units.get('time') != 'iso8601' or any(units.get(k) != v for k, v in shared.UNITS.items()):
        raise ValueError('archive units mismatch')
    start = shared.timestamp(params['start_date']+'T00:00:00')
    count = ((date.fromisoformat(params['end_date'])-start.date()).days+1)*24
    times = [shared.timestamp(t) for t in hourly.get('time', [])]
    if times != [start+timedelta(hours=i) for i in range(count)]:
        raise ValueError('archive response contains gaps/duplicate/out-of-range hours')
    if any(not isinstance(hourly.get(k), list) or len(hourly[k]) != count for k in shared.FIELD_MAP):
        raise ValueError('archive column lengths mismatch')
    cutoff = started.replace(minute=0, second=0, microsecond=0)
    elapsed = [t for t in times if t <= cutoff]
    if not elapsed:
        raise ValueError('no elapsed archive hours')
    identity = {'es_sn': 'ES02', 'source_kind': 'historical_reanalysis', 'provider': 'open_meteo',
        'endpoint': ENDPOINT, 'parameters': params, 'response_sha256': envelope['response_sha256'],
        'fetched_at': fetched.isoformat(), 'elapsed_hour_cutoff': cutoff.isoformat(), 'mapping_version': VERSION}
    batch_id = history.digest(identity)
    metadata = {**identity, 'requested_model_selection': 'default', 'resolved_model': None,
        'response_grid': {k: str(data[k]) for k in ('latitude', 'longitude', 'elevation') if k in data},
        'request_started_at': started.isoformat(), 'response_received_at': received.isoformat(),
        'expected_batch_rows': len(elapsed), 'first_weather_time': elapsed[0].isoformat(), 'last_weather_time': elapsed[-1].isoformat(),
        'excluded_future_hours': count-len(elapsed), 'units': {v: shared.UNITS[k] for k,v in shared.FIELD_MAP.items()},
        'time_semantics': {'temperature_humidity_cloud_wind': 'instant_at_weather_time',
            'radiation': 'mean_over_preceding_hour', 'precipitation': 'sum_over_preceding_hour'},
        'quality_policy': 'elapsed_hours_finite_ranges_lossless_decimal_units_continuity_v1'}
    rows = []
    for i, at in enumerate(elapsed):
        values = {name: history.decimal_value(hourly[api][i], name) for api,name in shared.FIELD_MAP.items()}
        row = {'es_sn': 'ES02', 'source_kind': 'historical_reanalysis', 'provider': 'open_meteo',
            'source_batch_id': batch_id, 'weather_time': at.isoformat(), 'interval_minutes': 60,
            'latitude': '23.000000', 'longitude': '113.000000', 'timezone': 'Asia/Shanghai',
            'issued_at': None, 'fetched_at': fetched.isoformat(), **values,
            'quality_status': 'valid' if all(v is not None for v in values.values()) else 'incomplete', 'source_metadata': metadata}
        row['content_hash'] = history.digest(row)
        rows.append(row)
    summary = {k: metadata[k] for k in ('source_kind','fetched_at','first_weather_time','last_weather_time','excluded_future_hours','mapping_version')}
    summary.update(source_batch_id=batch_id, expected_rows=len(rows), response_sha256=envelope['response_sha256'],
        incomplete_hourly_rows=sum(r['quality_status'] != 'valid' for r in rows))
    history.compare_rows(rows, rows)
    return rows, summary


def fetch(directory, start_date='2026-09-01', end_date=None):
    if directory.exists():
        raise ValueError('archive directory already exists; replay its snapshot')
    started = datetime.now(history.TZ)
    params = parameters(start_date, end_date or started.date().isoformat())
    if date.fromisoformat(params['end_date']) > started.date():
        raise ValueError('cannot request a future historical date')
    try:
        with build_opener(history.deployment.base.NoRedirect()).open(
            Request(ENDPOINT+'?'+urlencode(params), headers={'Accept':'application/json'}), timeout=30) as response:
            raw = response.read(shared.MAX_BYTES+1)
    except HTTPError as error:
        raise ValueError(f'archive HTTP {error.code}; no automatic retry') from None
    except (URLError, TimeoutError):
        raise ValueError('archive connection failed; no automatic retry') from None
    received = datetime.now(history.TZ)
    envelope = {'version': 1, 'es_sn': 'ES02', 'endpoint': ENDPOINT, 'parameters': params,
        'request_started_at': started.isoformat(), 'received_at': received.isoformat(),
        'fetched_at': shared.database_time(received).isoformat(), 'response_sha256': hashlib.sha256(raw).hexdigest()}
    directory.mkdir(parents=True, exist_ok=False)
    (directory/'response.json').write_bytes(raw)
    history.save_report(directory/'envelope.json', envelope)
    rows, summary = prepare_snapshot(envelope, raw)
    history.save_report(directory/'weather_rows.json', rows)
    history.save_report(directory/'summary.json', summary)
    return rows, summary


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fetch-dir', type=Path); mode.add_argument('--snapshot', type=Path)
    p.add_argument('--start-date', default='2026-09-01'); p.add_argument('--end-date')
    p.add_argument('--execute', action='store_true'); p.add_argument('--report', type=Path)
    args=p.parse_args()
    try:
        directory=args.fetch_dir or args.snapshot
        if args.fetch_dir: rows,summary=fetch(directory,args.start_date,args.end_date)
        else: rows,summary=prepare_snapshot(shared.load_json((directory/'envelope.json').read_bytes()),(directory/'response.json').read_bytes())
        if args.execute: history.execute(rows,summary,args.report or directory/'import_verification.json')
        else: print(json.dumps(summary,ensure_ascii=False,indent=2))
    except Exception as error:
        print(json.dumps({'status':'failed','error':str(error) if isinstance(error,ValueError) else type(error).__name__}))
        sys.exit(1)
