#!/usr/bin/env python3
"""ES02 weather collection or forecast with incremental training refresh."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from m3.worker.services.pv_hourly import atomic_json


def load(filename):
    spec = importlib.util.spec_from_file_location('manual_'+filename.replace('-', '_'), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load('fetch-open-meteo-weather.py')
generator = load('generate-pv-forecast.py')
publisher = load('publish-pv-forecast.py')
refresh = load('refresh-pv-training.py')


def refresh_training(directory, seed, state_path):
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict) or not isinstance(state.get('training_source'), str) or not state['training_source']:
            raise ValueError('invalid persisted training state')
        previous = Path(state['training_source'])
    else:
        # Missing state is only a first-boot condition when no publication
        # artifacts exist. An interrupted publish/state update needs review.
        for jobs in (state_path.parent/'jobs', state_path.parent):
            for marker in jobs.glob('*/training-refresh.json'):
                if (marker.parent/'forecast/publication_verification.json').exists():
                    raise ValueError('training state missing after forecast publication; review required')
        previous = seed
    data = refresh.load_source(previous)
    if data['training'].empty:
        raise ValueError('previous training is empty')
    # Start from eligible training, not raw telemetry: weather can lag telemetry.
    start = max(refresh.pd.Timestamp('2026-09-01', tz='Asia/Shanghai'),
                data['training'].index.max()-refresh.pd.Timedelta(hours=48))
    end = refresh.pd.Timestamp.now(tz='Asia/Shanghai').normalize()-refresh.pd.Timedelta(days=1)
    archive_path = directory/'training-weather'
    refresh.archive.fetch(archive_path, start.date().isoformat(), end.date().isoformat())
    training = directory/'training'
    atomic_json(directory/'training-refresh.json', {'policy':'continuous-training-v1',
        'previous_source':str(previous.resolve())})
    refresh.refresh(archive_path, training, previous, require_import=False)
    generator.load_inputs(None, training)
    return training


def perform(kind, directory, training_source, training_state=None):
    if kind not in ('weather', 'forecast'):
        raise ValueError('unsupported manual operation')
    # Fail before any write if the selected training snapshot cannot be verified.
    if kind == 'forecast':
        training_state = training_state or directory.parent/'training-state.json'
        training_source = refresh_training(directory, training_source, training_state)
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
        atomic_json(training_state, {'training_source': str(training_source.resolve()),
            'training_end': generated['training_end'], 'run_id': generated['run_id']})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=('weather', 'forecast'), required=True)
    parser.add_argument('--job-dir', type=Path, required=True)
    parser.add_argument('--training-source', type=Path, required=True)
    parser.add_argument('--training-state', type=Path)
    args = parser.parse_args()
    state = args.training_state or args.job_dir.parent/'training-state.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('a+') as lock, contextlib.redirect_stdout(io.StringIO()):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = perform(args.operation, args.job_dir, args.training_source, state)
    atomic_json(args.job_dir/'result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'status': 'failed', 'code': 'manual_review_required', 'automatic_post_retry': False}))
        sys.exit(1)
