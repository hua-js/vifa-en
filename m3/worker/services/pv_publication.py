"""Auditable PV forecast bundles and read-before-write publication gates."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid

import numpy as np
import pandas as pd

from m3.worker.domain import pv_backtest, pv_operational, pv_training

TIME_FIELDS = ('as_of', 'forecast_start', 'forecast_end', 'training_start', 'training_end',
               'pv_history_end', 'generated_at')
POWER_FIELDS = ('forecast_kw', 'raw_forecast_kw', 'baseline_kw', 'lower_kw', 'upper_kw')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def stable_numbers(value):
    """Persist floating-point inputs/coefficients losslessly across JS/JSONB."""
    if isinstance(value, dict):
        return {k: stable_numbers(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [stable_numbers(v) for v in value]
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value):
            raise ValueError('nonfinite snapshot number')
        return format(float(value), '.17g')
    return value


def power(value, *, quantize=False):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('boolean power')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) >= 100000000:
            raise ValueError('power exceeds numeric(14,6)')
        rounded = number.quantize(Decimal('0.000001'))
        if abs(rounded) >= 100000000 or not quantize and rounded != number:
            raise ValueError('power precision exceeds numeric(14,6)')
        return format(abs(rounded) if rounded == 0 else rounded, 'f')
    except InvalidOperation:
        raise ValueError('invalid decimal power') from None


def point_records(frame):
    return [{'target_time': at.isoformat(), 'horizon_step': int(row.horizon_step),
             'forecast_kw': power(row.forecast_kw, quantize=True),
             'raw_forecast_kw': power(row.raw_forecast_kw, quantize=True),
             'is_clipped': bool(row.is_clipped), 'baseline_kw': None, 'lower_kw': None, 'upper_kw': None}
            for at, row in frame.iterrows()]


def content_hash(run, points):
    return digest({'run': {k: v for k, v in run.items() if k not in ('status', 'content_hash')}, 'points': points})


def build_bundle(training, weather_rows, as_of, generated_at, provenance, pv_history_end):
    at = pv_operational.timestamp(as_of)
    result = pv_operational.generate(training, weather_rows, at)
    train = result['training'].loc[:, ['actual_kw', *pv_training.WEATHER_FIELDS]]
    samples = [{'target_time': idx.isoformat(), **stable_numbers(row.to_dict())} for idx, row in train.iterrows()]
    generated = pv_operational.timestamp(generated_at() if callable(generated_at) else generated_at)
    points = point_records(result['points'])
    run = {'run_id': str(uuid.uuid4()), 'es_sn': 'ES02', 'run_kind': 'operational',
        'as_of': at.isoformat(), 'forecast_start': points[0]['target_time'],
        'forecast_end': (result['points'].index[-1]+pd.Timedelta(minutes=15)).isoformat(),
        'interval_minutes': 15, 'expected_points': 96, 'status': 'completed',
        'model_name': 'WeatherRidge', 'model_version': pv_operational.VERSION,
        'model_config': stable_numbers(result['model']), 'training_start': train.index.min().isoformat(),
        'training_end': (train.index.max()+pd.Timedelta(minutes=15)).isoformat(),
        'pv_history_end': pv_operational.timestamp(pv_history_end).isoformat(),
        'weather_batch_id': weather_rows[0]['source_batch_id'], 'weather_source_kind': 'forecast',
        'source_manifest': {'version': 1, 'provenance': provenance,
            'training_samples': samples, 'forecast_weather_rows': weather_rows,
            'training_rows': len(train), 'training_label_staleness_hours': format((at-train.index.max()-pd.Timedelta(minutes=15)).total_seconds()/3600, '.6f'),
            'aggregation': 'minute_equal_weight_then_15min_mean_min12minutes_all4quarters_per_hour',
            'weather_alignment': 'end_tagged_hourly_radiation_midpoint_interpolated_instants_precipitation_div4',
            'baseline_policy': 'not_generated', 'prediction_interval_policy': 'not_estimated',
            'capacity_limit_kw': None, 'evaluation_status': 'awaiting_future_actuals'},
        'evaluation_metrics': None, 'bounds_coverage_pct': None, 'generated_at': generated.isoformat(),
        'error_code': None}
    run['content_hash'] = content_hash(run, points)
    validate_bundle(run, points)
    return run, points


def validate_bundle(run, points):
    if run['content_hash'] != content_hash(run, points):
        raise ValueError('forecast bundle hash mismatch')
    fixed = {'es_sn': 'ES02', 'run_kind': 'operational', 'interval_minutes': 15,
             'expected_points': 96, 'status': 'completed', 'model_name': 'WeatherRidge',
             'model_version': pv_operational.VERSION, 'weather_source_kind': 'forecast',
             'evaluation_metrics': None, 'bounds_coverage_pct': None, 'error_code': None}
    if any(run.get(k) != v for k, v in fixed.items()) or str(uuid.UUID(run['run_id'])) != run['run_id']:
        raise ValueError('unsupported forecast run contract')
    dates = {k: pv_operational.timestamp(run[k]) for k in TIME_FIELDS}
    as_of = dates['as_of']
    grid = pv_operational.next_grid(as_of)
    if (dates['forecast_start'] != grid[0] or dates['forecast_end'] != grid[-1]+pd.Timedelta(minutes=15)
            or not as_of <= dates['generated_at'] < dates['forecast_start']
            or not dates['training_start'] < dates['training_end'] <= as_of
            or dates['pv_history_end'] > as_of):
        raise ValueError('invalid forecast time boundaries')
    source = run['source_manifest']
    if pv_operational.timestamp(source['provenance']['observed_at']) > as_of:
        raise ValueError('input observed after as_of')
    if source['forecast_weather_rows'][0]['source_batch_id'] != run['weather_batch_id']:
        raise ValueError('forecast weather reference mismatch')
    train = pd.DataFrame(source['training_samples'])
    train.index = pd.DatetimeIndex([pv_operational.timestamp(t) for t in train.pop('target_time')])
    train = train.astype(float)
    if len(train) != source['training_rows'] or not len(train):
        raise ValueError('training snapshot count mismatch')
    if train.index.min() != dates['training_start'] or train.index.max()+pd.Timedelta(minutes=15) != dates['training_end']:
        raise ValueError('training snapshot boundary mismatch')
    result = pv_operational.generate(train, source['forecast_weather_rows'], as_of)
    # The persisted model is authoritative for replay; refitting also detects input/config mismatches.
    config = run['model_config']
    for key in ('feature_names', 'ridge_lambda', 'fit_intercept', 'postprocess'):
        if config[key] != stable_numbers(result['model'])[key]:
            raise ValueError('model policy mismatch')
    for key in ('rms', 'weights', 'irradiance_gain'):
        if not np.allclose(np.asarray(config[key], float), np.asarray(result['model'][key], float), rtol=1e-10, atol=1e-10):
            raise ValueError('model does not match stored training inputs')
    raw = pv_backtest.predict_raw_ridge(result['weather'], config)
    reproduced = pd.DataFrame({'horizon_step': np.arange(1, 97), 'raw_forecast_kw': raw,
        'forecast_kw': np.maximum(0, raw), 'is_clipped': raw < 0}, index=grid)
    if points != point_records(reproduced):
        raise ValueError('96 forecast points do not reproduce from stored inputs/model')


def integer_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError('invalid database id')
    return int(value)


def compare_run(actual, expected):
    if not isinstance(actual, dict) or not set(expected).issubset(actual):
        raise ValueError('run readback missing fields')
    normalized = {k: actual[k] for k in expected}
    for key in TIME_FIELDS:
        normalized[key] = pv_operational.timestamp(normalized[key]).isoformat()
    if normalized['status'] not in ('running', 'completed'):
        raise ValueError('existing run has incompatible state')
    normalized['status'] = expected['status']
    if normalized != expected:
        raise ValueError('existing run content conflicts with bundle')
    return integer_id(actual['id'])


def compare_points(actual, expected, run_pk):
    by_step = {p['horizon_step']: p for p in expected}
    seen = set()
    for row in actual:
        if integer_id(row['run_pk']) != run_pk or not set(expected[0]).issubset(row):
            raise ValueError('point fields or run association mismatch')
        step = row['horizon_step']
        if type(step) is not int or step in seen or step not in by_step:
            raise ValueError('duplicate or unexpected forecast step')
        normalized = {k: row[k] for k in expected[0]}
        normalized['target_time'] = pv_operational.timestamp(normalized['target_time']).isoformat()
        for key in POWER_FIELDS:
            normalized[key] = power(normalized[key])
        if type(normalized['is_clipped']) is not bool or normalized != by_step[step]:
            raise ValueError('stored forecast point conflicts with bundle')
        seen.add(step)
    return seen


def publish(repository, run, points, *, verify_only=False, now=lambda: datetime.now(timezone.utc)):
    """No automatic POST retry. Interrupted batches remain non-completed."""
    validate_bundle(run, points)
    actual_run = repository.read_run(run['run_id'])
    report = {'run_id': run['run_id'], 'inserted_runs': 0, 'inserted_points': 0,
              'updated_points': 0, 'deleted_rows': 0, 'automatic_post_retry': False}

    def before_window():
        if pv_operational.timestamp(now()) >= pv_operational.timestamp(run['forecast_start']):
            raise ValueError('unpublished forecast window has started; generate a new run')

    if actual_run is None:
        if verify_only:
            raise ValueError('run has not been published')
        before_window()
        repository.create_run({**run, 'status': 'running'})
        report['inserted_runs'] = 1
        actual_run = repository.read_run(run['run_id'])
    run_pk = compare_run(actual_run, run)
    stored = repository.read_points(run_pk)
    present = compare_points(stored, points, run_pk)
    missing = [p for p in points if p['horizon_step'] not in present]
    if actual_run['status'] == 'completed' and missing:
        raise ValueError('completed batch has missing points')
    if verify_only and (missing or actual_run['status'] != 'completed'):
        raise ValueError('batch is not fully published')
    if actual_run['status'] != 'completed':
        before_window()
        if missing:
            repository.create_points([{**p, 'run_pk': run_pk} for p in missing])
            report['inserted_points'] = len(missing)
        stored = repository.read_points(run_pk)
        if len(compare_points(stored, points, run_pk)) != 96:
            raise ValueError('cannot complete a batch without 96 verified points')
        before_window()
        repository.complete_run(run_pk)
    final_run = repository.read_run(run['run_id'])
    if compare_run(final_run, run) != run_pk or final_run['status'] != 'completed':
        raise ValueError('run completion readback failed')
    if len(compare_points(repository.read_points(run_pk), points, run_pk)) != 96:
        raise ValueError('final forecast point readback incomplete')
    report.update(status='completed', run_pk=run_pk, verified_points=96,
                  forecast_start=run['forecast_start'], forecast_end=run['forecast_end'],
                  content_hash=run['content_hash'], all_fields_match=True,
                  completed_at=pd.Timestamp(now()).isoformat())
    return report
