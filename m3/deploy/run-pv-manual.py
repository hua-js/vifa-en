#!/usr/bin/env python3
"""Explicit ES02 weather collection or one forecast using a fixed verified training source."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from m3_worker.services.pv_hourly import atomic_json


def load(filename):
    spec = importlib.util.spec_from_file_location('manual_'+filename.replace('-', '_'), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load('fetch-open-meteo-weather.py')
generator = load('generate-pv-forecast.py')
publisher = load('publish-pv-forecast.py')


def perform(kind, directory, training_source):
    if kind not in ('weather', 'forecast'):
        raise ValueError('unsupported manual operation')
    # Fail before any write if the selected training snapshot cannot be verified.
    if kind == 'forecast':
        generator.load_inputs(None, training_source)
    snapshot = directory/'weather'
    envelope, raw = collector.fetch(snapshot)
    rows, summary = collector.prepare_snapshot(envelope, raw)
    collector.history.save_report(snapshot/'weather_rows.json', rows)
    collector.history.save_report(snapshot/'summary.json', summary)
    collector.history.execute(rows, summary, snapshot/'import_verification.json')
    proof = json.loads((snapshot/'import_verification.json').read_text())
    if proof['status'] != 'completed' or proof['source_batch_id'] != summary['source_batch_id']:
        raise ValueError('weather publication has not been verified')
    result = {'status': 'completed', 'operation': kind, 'es_sn': 'ES02',
              'weather_batch_id': summary['source_batch_id'], 'weather_fetched_at': rows[0]['fetched_at'],
              'verified_weather_rows': len(rows)}
    if kind == 'forecast':
        bundle = directory/'forecast'
        generated = generator.generate(None, snapshot, bundle, training_source=training_source)
        publisher.execute(bundle, bundle/'publication_verification.json')
        verified = json.loads((bundle/'publication_verification.json').read_text())
        if verified['status'] != 'completed' or verified['verified_points'] != 96:
            raise ValueError('forecast publication has not been verified')
        result.update({k: generated[k] for k in ('run_id', 'forecast_start', 'forecast_end', 'training_end',
            'training_rows', 'forecast_energy_kwh', 'peak_kw', 'peak_time', 'model_name', 'model_version')})
        result.update(run_pk=verified['run_pk'], verified_points=96)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=('weather', 'forecast'), required=True)
    parser.add_argument('--job-dir', type=Path, required=True)
    parser.add_argument('--training-source', type=Path, required=True)
    args = parser.parse_args()
    with contextlib.redirect_stdout(io.StringIO()):
        result = perform(args.operation, args.job_dir, args.training_source)
    atomic_json(args.job_dir/'result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'status': 'failed', 'code': 'manual_review_required', 'automatic_post_retry': False}))
        sys.exit(1)
