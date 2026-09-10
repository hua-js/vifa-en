#!/usr/bin/env python3
"""Fetch ES02 current + hourly weather once; replay/import the saved snapshot.

Open-Meteo receives no NocoBase credentials. Current conditions are preserved
as model-based batch metadata; physical weather rows remain hourly forecasts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('weather_import_shared', ROOT/'scripts/import-weather-history.py')
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)

ENDPOINT = 'https://api.open-meteo.com/v1/forecast'
VERSION = 'open-meteo-current-hourly-v1'
MAX_BYTES = 4*1024*1024
FIELD_MAP = dict(zip(
    ('temperature_2m', 'relative_humidity_2m', 'cloud_cover', 'cloud_cover_low', 'cloud_cover_mid', 'cloud_cover_high',
     'shortwave_radiation', 'direct_radiation', 'diffuse_radiation', 'precipitation', 'wind_speed_10m'),
    ('temperature_c', 'relative_humidity_pct', 'cloud_cover_pct', 'cloud_cover_low_pct', 'cloud_cover_mid_pct', 'cloud_cover_high_pct',
     'ghi_wm2', 'direct_horizontal_wm2', 'dhi_wm2', 'precipitation_mm', 'wind_speed_kmh')))
UNITS = {'temperature_2m': '°C', 'relative_humidity_2m': '%', 'cloud_cover': '%', 'cloud_cover_low': '%',
         'cloud_cover_mid': '%', 'cloud_cover_high': '%', 'shortwave_radiation': 'W/m²',
         'direct_radiation': 'W/m²', 'diffuse_radiation': 'W/m²', 'precipitation': 'mm', 'wind_speed_10m': 'km/h'}


def parameters():
    return {'latitude': 23, 'longitude': 113, 'timezone': 'Asia/Shanghai',
            'current': ','.join([*FIELD_MAP, 'is_day', 'weather_code']), 'hourly': ','.join(FIELD_MAP),
            'past_hours': 1, 'forecast_hours': 49, 'temperature_unit': 'celsius',
            'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm', 'timeformat': 'iso8601'}


def timestamp(value, *, explicit=False):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.utcoffset() is None:
        if explicit:
            raise ValueError('snapshot timestamps need explicit timezone')
        at = at.replace(tzinfo=history.TZ)
    return at.astimezone(history.TZ)


def database_time(at):
    """Ceil to milliseconds, never claiming availability before receipt."""
    return at + timedelta(microseconds=(-at.microsecond) % 1000)


def reject_constant(value):
    raise ValueError('nonfinite JSON value')


def load_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique)


def prepare_snapshot(envelope, raw):
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != envelope.get('response_sha256'):
        raise ValueError('response checksum/size mismatch')
    if (envelope.get('version') != 1 or envelope.get('es_sn') != 'ES02'
            or envelope.get('endpoint') != ENDPOINT or envelope.get('parameters') != parameters()):
        raise ValueError('snapshot endpoint/station/request does not match ES02 configuration')
    started = timestamp(envelope['request_started_at'], explicit=True)
    received = timestamp(envelope['received_at'], explicit=True)
    fetched = timestamp(envelope['fetched_at'], explicit=True)
    if started > received or fetched != database_time(received):
        raise ValueError('snapshot fetch chronology/precision invalid')
    response = load_json(raw)
    if not isinstance(response, dict) or response.get('error'):
        raise ValueError('Open-Meteo returned no usable weather object')
    if response.get('timezone') != 'Asia/Shanghai' or response.get('utc_offset_seconds') != 28800:
        raise ValueError('unexpected response timezone/offset')
    for key, center in [('latitude', 23), ('longitude', 113)]:
        value = response.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value-center) > 1:
            raise ValueError('response grid is not near requested station')
    hourly, current = response.get('hourly'), response.get('current')
    hourly_units, current_units = response.get('hourly_units'), response.get('current_units')
    if not all(isinstance(v, dict) for v in [hourly, current, hourly_units, current_units]):
        raise ValueError('current/hourly data or units missing')
    for units in [hourly_units, current_units]:
        if units.get('time') != 'iso8601' or any(units.get(name) != unit for name, unit in UNITS.items()):
            raise ValueError('unexpected weather units')
    if current_units.get('interval') != 'seconds' or current_units.get('is_day') != '' or current_units.get('weather_code') != 'wmo code':
        raise ValueError('unexpected current units')
    if not isinstance(hourly.get('time'), list) or len(hourly['time']) != 50:
        raise ValueError('hourly response must cover 50 requested timestamps')
    times = [timestamp(t) for t in hourly['time']]
    if any(t.minute or t.second or t.microsecond for t in times) or any(b-a != timedelta(hours=1) for a, b in zip(times, times[1:])):
        raise ValueError('hourly response has gaps, duplicates or non-hourly timestamps')
    if any(not isinstance(hourly.get(name), list) or len(hourly[name]) != len(times) for name in FIELD_MAP):
        raise ValueError('hourly weather columns do not match time count')
    current_time = timestamp(current['time'])
    interval = current.get('interval')
    if type(interval) is not int or interval <= 0 or interval > 3600:
        raise ValueError('invalid current aggregation interval')
    if not timedelta(0) <= received-current_time <= timedelta(hours=1):
        raise ValueError('current timestamp is in the future or over one hour old')
    if type(current.get('is_day')) is not int or current['is_day'] not in (0, 1):
        raise ValueError('invalid current day/night flag')
    if type(current.get('weather_code')) is not int or not 0 <= current['weather_code'] <= 99:
        raise ValueError('invalid current weather code')
    # Canonical decimal strings inside JSON avoid JS/JSONB 0.0 -> 0 hash drift.
    current_values = {name: history.decimal_value(current[api], name) for api, name in FIELD_MAP.items()}
    current_values.update(is_day=current['is_day'], weather_code=current['weather_code'])
    current_conditions = {'valid_at': current_time.isoformat(), 'interval_seconds': interval,
        'model_based': True, 'values': current_values,
        'units': {**{name: UNITS[api] for api, name in FIELD_MAP.items()}, 'is_day': '', 'weather_code': 'wmo code'},
        'time_semantics': {'temperature_humidity_cloud_wind': 'instant_at_valid_at',
                           'radiation': 'mean_over_preceding_interval_seconds',
                           'precipitation': 'sum_over_preceding_interval_seconds'}}
    identity = {'es_sn': 'ES02', 'source_kind': 'forecast', 'provider': 'open_meteo',
        'endpoint': ENDPOINT, 'parameters': parameters(), 'response_sha256': envelope['response_sha256'],
        'fetched_at': fetched.isoformat(), 'mapping_version': VERSION}
    batch_id = history.digest(identity)
    metadata = {**identity, 'requested_model_selection': 'default_best_match', 'resolved_model': None,
        'response_grid': {k: str(response[k]) for k in ('latitude', 'longitude', 'elevation') if k in response},
        'request_started_at': started.isoformat(), 'response_received_at': received.isoformat(),
        'fetch_timestamp_precision': 'ceil_to_millisecond_after_full_response',
        'expected_batch_rows': len(times), 'first_weather_time': times[0].isoformat(), 'last_weather_time': times[-1].isoformat(),
        'current_conditions': current_conditions, 'hourly_units': {name: UNITS[api] for api, name in FIELD_MAP.items()},
        'time_semantics': {'temperature_humidity_cloud_wind': 'instant_at_weather_time',
                           'radiation': 'mean_over_preceding_hour', 'precipitation': 'sum_over_preceding_hour'},
        'quality_policy': 'finite_ranges_lossless_decimal_units_hourly_continuity_v1'}
    rows = []
    for i, at in enumerate(times):
        values = {name: history.decimal_value(hourly[api][i], name) for api, name in FIELD_MAP.items()}
        row = {'es_sn': 'ES02', 'source_kind': 'forecast', 'provider': 'open_meteo', 'source_batch_id': batch_id,
               'weather_time': at.isoformat(), 'interval_minutes': 60, 'latitude': '23.000000', 'longitude': '113.000000',
               'timezone': 'Asia/Shanghai', 'issued_at': None, 'fetched_at': fetched.isoformat(), **values,
               'quality_status': 'valid' if all(v is not None for v in values.values()) else 'incomplete', 'source_metadata': metadata}
        row['content_hash'] = history.digest(row)
        rows.append(row)
    # The next quarter-hour horizon also needs the final hourly endpoint.
    floor = fetched.replace(minute=fetched.minute//15*15, second=0, microsecond=0)
    forecast_start = floor + timedelta(minutes=15)
    forecast_end = forecast_start + timedelta(hours=24)
    left = forecast_start.replace(minute=0, second=0, microsecond=0)
    right = forecast_end.replace(minute=0, second=0, microsecond=0)
    if forecast_end != right:
        right += timedelta(hours=1)
    required = [left+timedelta(hours=i) for i in range(int((right-left).total_seconds()/3600)+1)]
    by_time = dict(zip(times, rows))
    ready = all(t in by_time and by_time[t]['quality_status'] == 'valid' for t in required)
    summary = {'source_batch_id': batch_id, 'response_sha256': envelope['response_sha256'],
        'expected_rows': len(rows), 'source_kind': 'forecast', 'fetched_at': fetched.isoformat(),
        'first_weather_time': times[0].isoformat(), 'last_weather_time': times[-1].isoformat(),
        'current_conditions': current_conditions, 'incomplete_hourly_rows': sum(r['quality_status'] != 'valid' for r in rows),
        'next_24h_weather_complete': ready, 'next_forecast_start': forecast_start.isoformat(),
        'next_forecast_end': forecast_end.isoformat(), 'mapping_version': VERSION}
    history.compare_rows(rows, rows)
    return rows, summary


def fetch(directory):
    if directory.exists():
        raise ValueError('snapshot directory already exists; use --snapshot to replay it')
    started = datetime.now(timezone.utc)
    request = Request(ENDPOINT+'?'+urlencode(parameters()), headers={'Accept': 'application/json'}, method='GET')
    try:
        with build_opener(history.deployment.base.NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES+1)
    except HTTPError as error:
        raise ValueError(f'Open-Meteo HTTP {error.code}; no automatic retry') from None
    except (URLError, TimeoutError):
        raise ValueError('Open-Meteo network request failed; no automatic retry') from None
    received = datetime.now(timezone.utc)
    if len(raw) > MAX_BYTES:
        raise ValueError('Open-Meteo response exceeds 4MB')
    envelope = {'version': 1, 'es_sn': 'ES02', 'endpoint': ENDPOINT, 'parameters': parameters(),
        'request_started_at': started.astimezone(history.TZ).isoformat(),
        'received_at': received.astimezone(history.TZ).isoformat(),
        'fetched_at': database_time(received).astimezone(history.TZ).isoformat(),
        'response_sha256': hashlib.sha256(raw).hexdigest()}
    directory.mkdir(parents=True, exist_ok=False)
    (directory/'response.json').write_bytes(raw)
    history.save_report(directory/'envelope.json', envelope)
    return envelope, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--fetch-dir', type=Path, help='fetch once to a new snapshot directory')
    inputs.add_argument('--snapshot', type=Path, help='replay an existing snapshot without weather network access')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', action='store_true', help='insert missing rows via NocoBase and verify')
    modes.add_argument('--verify-only', action='store_true', help='only read back the stored NocoBase batch')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.verify_only and args.fetch_dir:
        raise ValueError('--verify-only requires an existing --snapshot')
    directory = args.fetch_dir or args.snapshot
    if args.fetch_dir:
        envelope, raw = fetch(directory)
    else:
        envelope = load_json((directory/'envelope.json').read_bytes())
        raw = (directory/'response.json').read_bytes()
    rows, summary = prepare_snapshot(envelope, raw)
    if args.fetch_dir:
        history.save_report(directory/'weather_rows.json', rows)
        history.save_report(directory/'summary.json', summary)
    if args.execute or args.verify_only:
        report = args.report or directory/('verification.json' if args.verify_only else 'import_verification.json')
        history.execute(rows, summary, report, verify_only=args.verify_only)
    else:
        print(json.dumps({'snapshot': str(directory.resolve()), 'database_written': False, **summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, history.deployment.base.DeploymentError) as error:
        message = str(error) if isinstance(error, (ValueError, history.deployment.base.DeploymentError)) else type(error).__name__
        print(json.dumps({'status': 'failed', 'error': message}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
