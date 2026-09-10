#!/usr/bin/env python3
"""Publish/verify a fixed PV bundle through NocoBase; no POST auto-retry."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from m3_worker.services import pv_publication

spec = importlib.util.spec_from_file_location('pv_publish_weather_import', Path(__file__).with_name('import-weather-history.py'))
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
RUNS = 'energy_pv_forecast_runs'
POINTS = 'energy_pv_forecast_points'


class ForecastRepository:
    def __init__(self, key):
        self.key = key
        self.client = history.BusinessApiClient(history.deployment.schema.BASE_URL,
            key, timeout_seconds=30, max_response_bytes=4*1024*1024)

    def request(self, table, action, *, query=None, body=None):
        allowed = {(RUNS, 'list'), (RUNS, 'create'), (RUNS, 'update'), (POINTS, 'list'), (POINTS, 'create')}
        if (table, action) not in allowed:
            raise ValueError('resource/action outside PV publication scope')
        url = history.deployment.schema.BASE_URL+'/api/'+table+':'+action
        if query:
            url += '?'+urlencode(query)
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        if data is not None and len(data) > 2*1024*1024:
            raise ValueError('publication request exceeds 2MB')
        return self.client._request(Request(url, data=data,
            headers={'Authorization': 'Bearer '+self.key, 'Accept': 'application/json',
                     'Content-Type': 'application/json; charset=utf-8'}, method='GET' if action == 'list' else 'POST'))

    def list_rows(self, table, filter_value, fields, limit):
        result = self.request(table, 'list', query={'filter': json.dumps(filter_value), 'fields': ','.join(fields),
            'page': 1, 'pageSize': limit+1, 'sort': 'id'})
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            raise ValueError('invalid PV list response')
        rows = result['data']
        meta = result.get('meta', {})
        if not isinstance(meta, dict) or len(rows) > limit or meta.get('page', 1) != 1:
            raise ValueError('unexpected PV list count/page')
        if ('count' in meta and (type(meta['count']) is not int or meta['count'] != len(rows))
                or meta.get('hasNext', False) is not False or meta.get('totalPage', 1) > 1):
            raise ValueError('partial PV API page is not a complete batch')
        return rows

    def read_run(self, run_id):
        fields = [f['name'] for f in history.deployment.schema.RUN_FIELDS]
        rows = self.list_rows(RUNS, {'run_id': {'$eq': run_id}}, fields, 1)
        return rows[0] if rows else None

    def latest_run(self, at):
        cutoff=pv_publication.pv_operational.timestamp(at).isoformat()
        filters={'es_sn':{'$eq':'ES02'},'run_kind':{'$eq':'operational'},'status':{'$eq':'completed'},
                 'as_of':{'$lte':cutoff},'generated_at':{'$lte':cutoff}}
        fields=[f['name'] for f in history.deployment.schema.RUN_FIELDS]
        result=self.request(RUNS,'list',query={'filter':json.dumps(filters),'fields':','.join(fields),
            'sort':'-as_of,-id','page':1,'pageSize':1})
        rows=result.get('data');meta=result.get('meta',{})
        if not isinstance(rows,list) or len(rows)>1 or not isinstance(meta,dict) or meta.get('page',1)!=1:
            raise ValueError('invalid latest forecast response')
        if 'count' in meta and (type(meta['count']) is not int or (meta['count']>0)!=bool(rows)):
            raise ValueError('latest forecast count mismatch')
        if not rows and meta.get('hasNext',False):
            raise ValueError('latest forecast page is empty but hasNext')
        return rows[0] if rows else None

    def read_points(self, run_pk):
        fields = [f['name'] for f in history.deployment.schema.POINT_FIELDS]
        return self.list_rows(POINTS, {'run_pk': {'$eq': run_pk}}, fields, 96)

    def create_run(self, run):
        self.request(RUNS, 'create', body=run)

    def create_points(self, points):
        self.request(POINTS, 'create', body=points)

    def complete_run(self, run_pk):
        self.request(RUNS, 'update', query={'filterByTk': pv_publication.integer_id(run_pk)}, body={'status': 'completed'})

    def preflight(self, expected_weather):
        for table, definitions in ((RUNS, history.deployment.schema.RUN_FIELDS),
                                   (POINTS, history.deployment.schema.POINT_FIELDS)):
            fields = [f['name'] for f in definitions]
            response = self.request(table, 'list', query={
                'fields': ','.join(fields), 'page': 1, 'pageSize': 1})
            history.verify_data_columns(response, fields, table)
        weather_client = history.WeatherClient(self.key)
        actual_weather = weather_client.list_batch(expected_weather)
        if len(history.compare_rows(actual_weather, expected_weather)) != len(expected_weather):
            raise ValueError('referenced weather batch is not fully stored')
        return len(actual_weather)


def load_bundle(directory):
    run = json.loads((directory/'run.json').read_text())
    points = json.loads((directory/'points.json').read_text())
    pv_publication.validate_bundle(run, points)
    return run, points


def execute(directory, report_path, *, verify_only=False):
    report = {'status': 'preflight', 'started_at': datetime.now(timezone.utc).isoformat(),
              'automatic_post_retry': False, 'mode': 'verify_only' if verify_only else 'publish',
              'preflight_mode': 'business_data_fields', 'schema_management_checked': False}
    history.save_report(report_path, report)
    try:
        run, points = load_bundle(directory)
        repository = ForecastRepository(history.credential())
        weather_count = repository.preflight(run['source_manifest']['forecast_weather_rows'])
        result = pv_publication.publish(repository, run, points, verify_only=verify_only)
        report.update(result, verified_weather_rows=weather_count,
            read_query={'runs': {'run_id': run['run_id']}, 'points': {'run_pk': result['run_pk'], 'sort': 'target_time'}})
        history.save_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as error:
        report.update(status='incomplete', error_type=type(error).__name__,
            safe_error=str(error) if isinstance(error, (ValueError, history.deployment.base.DeploymentError)) else 'publication incomplete',
            stopped_at=datetime.now(timezone.utc).isoformat())
        history.save_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.execute or args.verify_only:
            execute(args.bundle, args.report or args.bundle/'publication_verification.json', verify_only=args.verify_only)
        else:
            run, points = load_bundle(args.bundle)
            print(json.dumps({'status': 'offline_validated', 'run_id': run['run_id'], 'points': len(points), 'database_written': False}))
    except Exception:
        sys.exit(1)
