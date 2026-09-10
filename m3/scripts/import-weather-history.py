#!/usr/bin/env python3
"""Validate/import ES02's historical Open-Meteo CSV through NocoBase only.

Default is offline validation. Execution preflights business data fields, compares every
existing row in this immutable batch, inserts only missing rows, then compares
all stored fields. It never updates/deletes rows or automatically retries POST.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

M3_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = M3_ROOT.parent
TABLE = 'energy_weather_points'
TZ = ZoneInfo('Asia/Shanghai')
MAPPING_VERSION = 'open-meteo-history-csv-v1'
FIELDS = {
    '气温(℃)': 'temperature_c',
    '相对湿度(%)': 'relative_humidity_pct',
    '总云量(%)': 'cloud_cover_pct',
    '低层云量(%)': 'cloud_cover_low_pct',
    '中层云量(%)': 'cloud_cover_mid_pct',
    '高层云量(%)': 'cloud_cover_high_pct',
    '短波辐射(W/㎡)': 'ghi_wm2',
    '直接辐射(W/㎡)': 'direct_horizontal_wm2',
    '散射辐射(W/㎡)': 'dhi_wm2',
    '降水量(mm)': 'precipitation_mm',
    '10米风速(km/h)': 'wind_speed_kmh',
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


deployment = load_module('pv_schema_deployment', M3_ROOT/'scripts/create-pv-forecast-collections.py')
credentials = load_module('pv_shared_credentials', PROJECT_ROOT/'m3/worker/pv_credentials.py')
DECIMAL_FIELDS = {f['name']: tuple(map(int, f['type'][8:-1].split(',')))
                  for f in deployment.schema.WEATHER_FIELDS if f['type'].startswith('numeric(')}


class ImportErrorSafe(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def decimal_value(value, name):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise ImportErrorSafe(f'{name} 不是有效数值')
    try:
        number = Decimal(str(value))
        precision, scale = DECIMAL_FIELDS[name]
        unit = Decimal(1).scaleb(-scale)
        if not number.is_finite() or abs(number) >= Decimal(10) ** (precision-scale):
            raise ValueError
        normalized = number.quantize(unit)
        if normalized != number:
            raise ValueError
        if name == 'latitude' and not -90 <= number <= 90:
            raise ValueError
        if name == 'longitude' and not -180 <= number <= 180:
            raise ValueError
        if name.endswith('_pct') and not 0 <= number <= 100:
            raise ValueError
        if name not in ('temperature_c', 'latitude', 'longitude') and number < 0:
            raise ValueError
        return format(abs(normalized) if normalized == 0 else normalized, 'f')
    except (InvalidOperation, ValueError):
        raise ImportErrorSafe(f'{name} 存在非法数值、越界或无法无损保存的精度') from None


def csv_timestamp(value):
    try:
        at = datetime.fromisoformat(value.strip())
        if at.tzinfo is None:
            at = at.replace(tzinfo=TZ)
        at = at.astimezone(TZ)
        if at.minute or at.second or at.microsecond:
            raise ValueError
        return at
    except (ValueError, TypeError, AttributeError):
        raise ImportErrorSafe('CSV含无效或非整点时间') from None


def prepare(path):
    raw = path.read_bytes()
    if len(raw) > 50*1024*1024:
        raise ImportErrorSafe('CSV超过50MB导入上限')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if reader.fieldnames != ['时间', *FIELDS]:
        raise ImportErrorSafe('CSV字段或顺序不符合已确认的历史天气格式')
    source = list(reader)
    if not 1 <= len(source) <= 100000:
        raise ImportErrorSafe('CSV行数不在支持范围内')
    times = [csv_timestamp(row['时间']) for row in source]
    if any(b-a != timedelta(hours=1) for a, b in zip(times, times[1:])):
        raise ImportErrorSafe('CSV时间重复、乱序或缺少连续小时')
    identity = {
        'csv_sha256': hashlib.sha256(raw).hexdigest(), 'source_file_name': path.name,
        'es_sn': 'ES02', 'source_kind': 'historical_reanalysis', 'provider': 'open_meteo',
        'latitude': '23.000000', 'longitude': '113.000000', 'timezone': 'Asia/Shanghai',
        'mapping_version': MAPPING_VERSION,
    }
    batch_id = digest(identity)
    metadata = {
        **identity, 'source_endpoint': 'https://archive-api.open-meteo.com/v1/archive',
        'requested_model_selection': 'default', 'resolved_model': None, 'resolved_grid': None,
        'expected_batch_rows': len(source), 'first_weather_time': times[0].isoformat(),
        'last_weather_time': times[-1].isoformat(),
        'time_semantics': {
            'temperature_humidity_cloud_wind': 'instant_at_weather_time',
            'radiation': 'mean_over_preceding_hour', 'precipitation': 'sum_over_preceding_hour',
            'original_csv_timezone': 'Asia/Shanghai', 'timezone_assignment': 'user_confirmed_source_configuration',
        },
        'units': {'temperature_c': 'degC', 'relative_humidity_pct': 'percent',
                  'cloud_cover_pct': 'percent', 'cloud_cover_low_pct': 'percent',
                  'cloud_cover_mid_pct': 'percent', 'cloud_cover_high_pct': 'percent',
                  'ghi_wm2': 'W/m2', 'direct_horizontal_wm2': 'W/m2', 'dhi_wm2': 'W/m2',
                  'precipitation_mm': 'mm', 'wind_speed_kmh': 'km/h'},
        'quality_policy': 'hourly_csv_continuity_finite_numeric_precision_and_ranges_v1',
    }
    rows = []
    nulls = 0
    for at, source_row in zip(times, source):
        if None in source_row:
            raise ImportErrorSafe('CSV包含多余字段')
        values = {name: decimal_value(source_row[label], name) for label, name in FIELDS.items()}
        nulls += sum(v is None for v in values.values())
        record = {
            'es_sn': 'ES02', 'source_kind': 'historical_reanalysis', 'provider': 'open_meteo',
            'source_batch_id': batch_id, 'weather_time': at.isoformat(), 'interval_minutes': 60,
            'latitude': identity['latitude'], 'longitude': identity['longitude'], 'timezone': 'Asia/Shanghai',
            'issued_at': None, 'fetched_at': None, **values,
            'quality_status': 'valid' if all(v is not None for v in values.values()) else 'incomplete',
            'source_metadata': metadata,
        }
        record['content_hash'] = digest(record)
        rows.append(record)
    summary = {'source_file': str(path.resolve()), 'csv_sha256': identity['csv_sha256'],
               'source_batch_id': batch_id, 'expected_rows': len(rows), 'null_weather_cells': nulls,
               'first_weather_time': times[0].isoformat(), 'last_weather_time': times[-1].isoformat(),
               'time_semantics': metadata['time_semantics'], 'mapping_version': MAPPING_VERSION}
    return rows, summary


def credential():
    direct = os.environ.get('PV_NOCOBASE_IMPORT_API_KEY', '').strip()
    path = os.environ.get('PV_NOCOBASE_IMPORT_API_KEY_FILE', '').strip()
    if bool(direct) == bool(path):
        raise ImportErrorSafe('请配置且仅配置PV_NOCOBASE_IMPORT_API_KEY或PV_NOCOBASE_IMPORT_API_KEY_FILE')
    value = direct if direct else credentials.read_token_file(path)
    if deployment.base.TOKEN.fullmatch(value) is None:
        raise ImportErrorSafe('导入凭据格式无效')
    return value


class BusinessApiClient(deployment.base.NocoBaseSchemaClient):
    """Keep runtime API errors distinct from schema-management failures."""
    def _request(self, request, **kwargs):
        try:
            return super()._request(request, **kwargs)
        except deployment.base.DeploymentError as error:
            if isinstance(error.__cause__, HTTPError):
                resource = urlsplit(request.full_url).path.rsplit('/', 1)[-1]
                raise deployment.base.DeploymentError(
                    f'NocoBase {resource} HTTP {error.__cause__.code}; no automatic retry'
                ) from error
            raise


def verify_data_columns(response, fields, table):
    rows = response.get('data') if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) > 1:
        raise ImportErrorSafe(f'{table}:list 字段预检响应无效')
    if any(not isinstance(row, dict) or not set(fields).issubset(row) for row in rows):
        raise ImportErrorSafe(f'{table}:list 未返回完整业务字段')
    # Empty tables are valid on first use; create responses and final reads must
    # still pass the existing full-field/value/hash verification.
    return {'table': table, 'fields_requested': len(fields), 'sample_row_checked': bool(rows)}


class WeatherClient:
    def __init__(self, key):
        self.key = key
        self.client = BusinessApiClient(deployment.schema.BASE_URL, key,
                      timeout_seconds=30, max_response_bytes=4*1024*1024)

    def request(self, action, *, query=None, rows=None):
        if action not in ('list', 'create'):
            raise ImportErrorSafe('不支持的操作')
        url = deployment.schema.BASE_URL+'/api/'+TABLE+':'+action
        if query:
            url += '?' + urlencode(query)
        body = None if rows is None else json.dumps(rows, ensure_ascii=False, allow_nan=False).encode()
        request = Request(url, data=body,
            headers={'Authorization': 'Bearer '+self.key, 'Accept': 'application/json',
                     'Content-Type': 'application/json; charset=utf-8'}, method='GET' if body is None else 'POST')
        return self.client._request(request)

    def list_batch(self, expected):
        first = expected[0]
        filter_value = {name: {'$eq': first[name]} for name in ('es_sn', 'source_kind', 'source_batch_id')}
        query = {'filter': json.dumps(filter_value), 'fields': ','.join(['id', *first]),
                 'sort': 'weather_time', 'pageSize': 500}
        rows = []
        for page in range(1, len(expected)//500+4):
            payload = self.request('list', query={**query, 'page': page})
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ImportErrorSafe('天气回读格式无效')
            batch = payload['data']
            meta = payload.get('meta', {})
            if not isinstance(meta, dict) or meta.get('page', page) != page:
                raise ImportErrorSafe('天气回读分页无效')
            rows.extend(batch)
            if len(rows) > len(expected):
                raise ImportErrorSafe('同批次记录数量超出CSV，未覆盖现有数据')
            if 'hasNext' in meta:
                more = meta['hasNext']
                if not isinstance(more, bool):
                    raise ImportErrorSafe('天气回读hasNext无效')
            elif 'count' in meta:
                count = meta['count']
                if type(count) is not int or count < len(rows):
                    raise ImportErrorSafe('天气回读count无效')
                more = len(rows) < count
            elif 'totalPage' in meta:
                total = meta['totalPage']
                if type(total) is not int or total < 0:
                    raise ImportErrorSafe('天气回读totalPage无效')
                more = page < total
            else:
                more = len(batch) >= meta.get('pageSize', 500)
            if not more:
                return rows
            if not batch:
                raise ImportErrorSafe('天气回读分页中断')
        raise ImportErrorSafe('天气回读页数超限')


def normalized_actual(row, expected):
    if not isinstance(row, dict) or not set(expected).issubset(row):
        raise ImportErrorSafe('天气回读缺少字段')
    actual = {key: row[key] for key in expected}
    for key in DECIMAL_FIELDS:
        actual[key] = decimal_value(actual[key], key)
    for key in ('weather_time', 'issued_at', 'fetched_at'):
        if key not in actual or actual[key] is None and key != 'weather_time':
            continue
        try:
            at = datetime.fromisoformat(actual[key].replace('Z', '+00:00'))
            if at.utcoffset() is None:
                raise ValueError
            actual[key] = at.astimezone(TZ).isoformat()
        except (ValueError, AttributeError, TypeError):
            raise ImportErrorSafe('天气回读时间缺少明确时区') from None
    if type(actual['interval_minutes']) is not int:
        raise ImportErrorSafe('天气回读间隔不是整数')
    return actual


def compare_rows(actual_rows, expected):
    by_time = {row['weather_time']: row for row in expected}
    seen = set()
    for row in actual_rows:
        normalized = normalized_actual(row, expected[0])
        at = normalized['weather_time']
        if at in seen or at not in by_time:
            raise ImportErrorSafe('回读存在重复或额外时间，未覆盖现有记录')
        seen.add(at)
        if normalized != by_time[at]:
            different = [key for key in normalized if normalized[key] != by_time[at][key]]
            raise ImportErrorSafe(f'时间 {at} 的回读值冲突：'+','.join(different))
        body = {key: value for key, value in normalized.items() if key != 'content_hash'}
        if digest(body) != normalized['content_hash']:
            raise ImportErrorSafe('回读内容与内容哈希不一致')
    return seen


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def execute(expected, summary, report_path, *, verify_only=False):
    report = {**summary, 'started_at': datetime.now(timezone.utc).isoformat(),
              'collection': TABLE, 'status': 'preflight', 'inserted_rows_this_run': 0,
              'updated_rows': 0, 'deleted_rows': 0, 'automatic_post_retry': False}
    save_report(report_path, report)
    try:
        client = WeatherClient(credential())
        fields = [f['name'] for f in deployment.schema.WEATHER_FIELDS]
        probe = client.request('list', query={'fields': ','.join(fields), 'page': 1, 'pageSize': 1})
        report.update(preflight_mode='business_data_fields', schema_management_checked=False,
                      data_preflight=verify_data_columns(probe, fields, TABLE))
        save_report(report_path, report)
        existing = client.list_batch(expected)
        present = compare_rows(existing, expected)
        missing = [row for row in expected if row['weather_time'] not in present]
        report.update(status='verified_existing', existing_identical_rows=len(present), pending_rows=len(missing))
        save_report(report_path, report)
        if verify_only and missing:
            raise ImportErrorSafe(f'天气批次尚缺{len(missing)}行')
        for offset in range(0, len(missing), 100):
            chunk = missing[offset:offset+100]
            report.update(status='posting', active_chunk_start=chunk[0]['weather_time'], active_chunk_rows=len(chunk))
            save_report(report_path, report)
            result = client.request('create', rows=chunk)
            data = result.get('data') if isinstance(result, dict) else None
            if not isinstance(data, list) or len(data) != len(chunk):
                raise ImportErrorSafe('创建响应数量不确定；请先回读，未自动重试')
            compare_rows(data, chunk)
            report['inserted_rows_this_run'] += len(chunk)
            report['pending_rows'] -= len(chunk)
            save_report(report_path, report)
            print(json.dumps({'confirmed_inserted': report['inserted_rows_this_run'],
                              'remaining': report['pending_rows']}, ensure_ascii=False), flush=True)
        stored = client.list_batch(expected) if missing else existing
        matched = compare_rows(stored, expected)
        if len(matched) != len(expected):
            raise ImportErrorSafe('最终回读未覆盖全部CSV时间')
        numeric_totals = {name: format(sum((Decimal(row[name]) for row in expected if row[name] is not None), Decimal(0)), 'f')
                          for name in FIELDS.values()}
        report.update(status='completed', mode='verify_only' if verify_only else 'import',
                      verified_rows=len(matched), all_stored_fields_match_csv_and_metadata=True,
                      recomputed_row_hashes_match=True, numeric_column_sums=numeric_totals,
                      completed_at=datetime.now(timezone.utc).isoformat(),
                      read_query={'es_sn': expected[0]['es_sn'], 'source_kind': expected[0]['source_kind'],
                                  'source_batch_id': summary['source_batch_id'], 'sort': 'weather_time'})
        report.pop('active_chunk_start', None)
        report.pop('active_chunk_rows', None)
        save_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as error:
        report.update(status='incomplete', error_type=type(error).__name__,
                      safe_error=str(error) if isinstance(error, (ImportErrorSafe, deployment.base.DeploymentError)) else '未完成导入',
                      stopped_at=datetime.now(timezone.utc).isoformat())
        save_report(report_path, report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, default=PROJECT_ROOT/'m3/data/历史天气数据.csv')
    parser.add_argument('--report', type=Path, default=PROJECT_ROOT/'outputs/m3/contracts/20260909/weather_history_import_verification.json')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', action='store_true')
    modes.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    expected, summary = prepare(args.csv)
    if args.execute or args.verify_only:
        execute(expected, summary, args.report, verify_only=args.verify_only)
    else:
        print(json.dumps({'mode': 'offline_validation', **summary,
                          'ready_rows': len(expected), 'database_written': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ImportErrorSafe, deployment.base.DeploymentError) as error:
        print(json.dumps({'error': str(error), 'automatic_post_retry': False}, ensure_ascii=False))
        sys.exit(1)
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'automatic_post_retry': False}))
        sys.exit(1)
