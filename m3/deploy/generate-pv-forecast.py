#!/usr/bin/env python3
"""Generate an immutable local ES02 future PV bundle; no API writes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from m3_worker.domain import pv_training, pv_backtest, pv_operational
from m3_worker.services import pv_publication


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load('operational_weather_collector', 'fetch-open-meteo-weather.py')
backtest = load('operational_backtest_inputs', 'backtest-pv-history.py')


def now():
    return pd.Timestamp.now(tz='Asia/Shanghai').ceil('ms')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_file(root, relative):
    path = root/relative
    manifest = json.loads((root/'manifest.json').read_text())
    if sha(path) != manifest['files_sha256'][relative]:
        raise ValueError('input artifact SHA mismatch: '+relative)
    return path


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)+'\n')


def load_inputs(backtest_dir, training_source=None):
    if training_source is not None:
        refresh=load('operational_training_refresh', 'refresh-pv-training.py')
        data=refresh.load_source(training_source)
        return data['training'],data['raw'],data['sources'],{
            'historical_weather_batches':data['weather_batches'],'training_weather_lineage':data['lineage'],
            'training_refresh_provenance':data['manifest']['provenance']}
    pv_path = checked_file(backtest_dir, 'inputs/pv_snapshot.jsonl')
    weather_path = checked_file(backtest_dir, 'inputs/weather.csv')
    verification_path = checked_file(backtest_dir, 'inputs/weather_import_verification.json')
    verification = json.loads(verification_path.read_text())
    if (verification['status'] != 'completed' or verification['csv_sha256'] != sha(weather_path)
            or verification['read_query']['es_sn'] != 'ES02'
            or verification['read_query']['source_kind'] != 'historical_reanalysis'):
        raise ValueError('historical weather source lacks matching import verification')
    raw = [json.loads(line) for line in pv_path.read_text().splitlines() if line.strip()]
    csv = pd.read_csv(weather_path, encoding='utf-8-sig')
    if list(csv.columns) != ['时间', *backtest.CSV_FIELDS]:
        raise ValueError('historical weather columns mismatch')
    historical = csv.rename(columns=backtest.CSV_FIELDS).drop(columns='时间')
    historical.index = pd.DatetimeIndex(pd.to_datetime(csv['时间'])).tz_localize(pv_training.TZ)
    aligned = pv_training.align_weather(pv_training.prepare_pv(raw), historical)
    training = aligned.loc[aligned.eligible]
    sources={'pv':pv_path,'historical_weather':weather_path,'historical_verification':verification_path}
    return training,raw,sources,{'historical_weather_batch_id':verification['source_batch_id']}


def generate(backtest_dir, snapshot, output, *, training_source=None):
    if output.exists():
        raise ValueError('output directory already exists')
    training,raw,sources,training_provenance=load_inputs(backtest_dir,training_source)
    envelope = json.loads((snapshot/'envelope.json').read_text())
    weather_rows, weather_summary = collector.prepare_snapshot(envelope, (snapshot/'response.json').read_bytes())
    observed = now()
    sources.update(forecast_envelope=snapshot/'envelope.json', forecast_response=snapshot/'response.json')
    provenance = {'observed_at': observed.isoformat(),
        **training_provenance,
        'files': {key: {'path': str(path.resolve()), 'sha256': sha(path)} for key, path in sources.items()},
        'runtime': {'python': platform.python_version(), 'numpy': np.__version__, 'pandas': pd.__version__}}
    as_of = now()
    valid_times = [pv_operational.timestamp(row['timestamp']) for row in raw if row['ac_solar_power'] is not None]
    run, points = pv_publication.build_bundle(training, weather_rows, as_of, now, provenance, max(valid_times))
    output.mkdir(parents=True, exist_ok=False)
    (output/'inputs').mkdir()
    for key, path in sources.items():
        shutil.copyfile(path, output/'inputs'/(key+path.suffix))
    (output/'code').mkdir()
    for path in [Path(__file__), Path(__file__).with_name('publish-pv-forecast.py'),
                 Path(pv_operational.__file__), Path(pv_training.__file__), Path(pv_backtest.__file__),
                 Path(pv_publication.__file__), Path(collector.__file__),Path(__file__).with_name('refresh-pv-training.py')]:
        shutil.copyfile(path, output/'code'/path.name)
    write_json(output/'run.json', run)
    write_json(output/'points.json', points)
    pd.DataFrame(points).to_csv(output/'forecast_points.csv', encoding='utf-8-sig', index=False)
    training.to_csv(output/'training.csv', encoding='utf-8-sig', index_label='target_time')
    future = pv_operational.prepare_future(weather_rows, as_of)
    future.to_csv(output/'forecast_weather.csv', encoding='utf-8-sig', index_label='target_time')
    values = [float(p['forecast_kw']) for p in points]
    maximum = max(values)
    summary = {'status': 'generated_locally', 'run_id': run['run_id'], 'es_sn': 'ES02',
        'as_of': run['as_of'], 'forecast_start': run['forecast_start'], 'forecast_end': run['forecast_end'],
        'expected_points': 96, 'model_name': run['model_name'], 'model_version': run['model_version'],
        'training_rows': len(training), 'training_start': run['training_start'], 'training_end': run['training_end'],
        'weather_batch_id': run['weather_batch_id'], 'weather_fetched_at': weather_rows[0]['fetched_at'],
        'forecast_energy_kwh': sum(values)*.25, 'peak_kw': maximum,
        'peak_time': points[values.index(maximum)]['target_time'], 'clipped_points': sum(p['is_clipped'] for p in points),
        'bounds_estimated': False, 'operational_accuracy_evaluated': False, 'database_written': False}
    write_json(output/'summary.json', summary)
    write_json(output/'manifest.json', {'version': 1, 'run_id': run['run_id'], 'content_hash': run['content_hash'],
        'files_sha256': {str(path.relative_to(output)): sha(path) for path in sorted(output.rglob('*')) if path.is_file()}})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backtest-dir', type=Path, default=ROOT/'m3/reports/pv_backtest_es02_20260909')
    parser.add_argument('--weather-snapshot', type=Path, required=True)
    parser.add_argument('--training-source',type=Path,help='verified refreshed PV/history input directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.backtest_dir, args.weather_snapshot, args.output,training_source=args.training_source)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
            'safe_error': str(error) if isinstance(error, ValueError) else 'cannot generate forecast bundle'}))
        sys.exit(1)
