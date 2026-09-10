"""Small, fixed weather models with rolling-origin retrospective evaluation.

Power labels obey the fold cutoff. Reanalysis features are known after the
event, so these scores must never be advertised as operational forecast skill.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from m3.worker.domain.pv_training import TZ

FEATURE_NAMES = ('ghi', 'ghi_temperature', 'ghi_cloud', 'ghi_humidity', 'ghi_wind',
                 'direct_horizontal', 'diffuse', 'ghi_clock_sin', 'ghi_clock_cos')
MODEL_COLUMNS = {'SeasonalNaive': 'baseline_kw', 'IrradianceGain': 'gain_kw', 'WeatherRidge': 'ridge_kw'}
RIDGE_LAMBDA = 0.01
EVALUATION_TYPE = 'retrospective_known_weather'


def weather_features(frame: pd.DataFrame) -> np.ndarray:
    g = frame.ghi_wm2.to_numpy(float)/1000
    hour = frame.index.hour + (frame.index.minute+7.5)/60
    angle = np.asarray(hour) * (2*np.pi/24)
    features = np.column_stack((g, g*(frame.temperature_c.to_numpy()-25)/20,
        g*frame.cloud_cover_pct.to_numpy()/100, g*frame.relative_humidity_pct.to_numpy()/100,
        g*frame.wind_speed_kmh.to_numpy()/20, frame.direct_horizontal_wm2.to_numpy()/1000,
        frame.dhi_wm2.to_numpy()/1000, g*np.sin(angle), g*np.cos(angle)))
    if not np.isfinite(features).all():
        raise ValueError('weather features contain missing or infinite values')
    return features


def fit_weather_models(train: pd.DataFrame) -> dict:
    x = weather_features(train)
    y = train.actual_kw.to_numpy(float)
    if not len(y) or not np.isfinite(y).all() or (y < 0).any():
        raise ValueError('invalid training power')
    rms = np.sqrt(np.mean(x*x, axis=0))
    rms = np.where(rms > 1e-12, rms, 1.0)
    scaled = x/rms
    gram = scaled.T @ scaled/len(y) + RIDGE_LAMBDA*np.eye(x.shape[1])
    weights = np.linalg.solve(gram, scaled.T @ y/len(y))
    ghi = x[:, 0]
    denominator = float(ghi @ ghi)
    if denominator <= 1e-12:
        raise ValueError('training has no irradiance signal')
    gain = max(0.0, float(ghi @ y)/denominator)
    return {'feature_names': list(FEATURE_NAMES), 'ridge_lambda': RIDGE_LAMBDA,
            'rms': rms.tolist(), 'weights': weights.tolist(), 'irradiance_gain': gain,
            'fit_intercept': False, 'postprocess': 'clip_to_nonnegative_no_capacity_assumed'}


def predict_raw_ridge(frame: pd.DataFrame, model: dict) -> np.ndarray:
    """Keep the unconstrained prediction for operational audit records."""
    raw = (weather_features(frame)/np.asarray(model['rms'], dtype=float)) @ np.asarray(model['weights'], dtype=float)
    if not np.isfinite(raw).all():
        raise ValueError('nonfinite prediction')
    return raw


def predict_weather(frame: pd.DataFrame, model: dict) -> dict[str, np.ndarray]:
    x = weather_features(frame)
    result = {'ridge_kw': np.maximum(0.0, predict_raw_ridge(frame, model)),
              'gain_kw': np.maximum(0.0, x[:, 0]*model['irradiance_gain'])}
    if not all(np.isfinite(values).all() for values in result.values()):
        raise ValueError('nonfinite prediction')
    return result


def rolling_backtest(aligned: pd.DataFrame, min_complete_days: int = 7) -> tuple[pd.DataFrame, list[dict]]:
    if type(min_complete_days) is not int or min_complete_days < 1:
        raise ValueError('min_complete_days must be a positive integer')
    if not isinstance(aligned.index, pd.DatetimeIndex) or aligned.index.tz is None or aligned.index.has_duplicates:
        raise ValueError('aligned data needs unique timezone-aware timestamps')
    aligned = aligned.copy().sort_index()
    aligned.index = aligned.index.tz_convert(TZ)
    if not (aligned.index == aligned.index.floor('15min')).all():
        raise ValueError('aligned data must be on quarter-hour grid')
    eligible = aligned.loc[aligned.eligible]
    if eligible.empty:
        raise ValueError('no eligible training data')
    days = pd.date_range(eligible.index.min().normalize(), eligible.index.max().normalize(), freq='D')
    outputs, folds = [], []
    for day in days:
        train = eligible.loc[eligible.index + pd.Timedelta(minutes=15) <= day]
        counts = train.groupby(train.index.normalize()).size()
        complete_days = int((counts == 96).sum())
        if complete_days < min_complete_days:
            continue
        grid = pd.date_range(day, periods=96, freq='15min')
        test = aligned.reindex(grid).copy()
        test['eligible'] = test.eligible.eq(True)
        test['as_of'] = day
        test['baseline_kw'] = aligned.actual_kw.reindex(grid-pd.Timedelta(days=1)).to_numpy()
        test['baseline_source_time'] = grid-pd.Timedelta(days=1)
        test['gain_kw'] = np.nan
        test['ridge_kw'] = np.nan
        model = fit_weather_models(train)
        valid_test = test.loc[test.eligible]
        for column, values in predict_weather(valid_test, model).items():
            test.loc[valid_test.index, column] = values
        test['evaluation_type'] = EVALUATION_TYPE
        test['score_shared'] = test.eligible & test[['actual_kw', *MODEL_COLUMNS.values()]].notna().all(axis=1)
        outputs.append(test)
        folds.append({'as_of': day.isoformat(), 'train_rows': len(train),
                      'complete_training_days': complete_days, 'train_start': train.index.min().isoformat(),
                      'max_training_label_end': (train.index.max()+pd.Timedelta(minutes=15)).isoformat(),
                      'target_end': (day+pd.Timedelta(days=1)).isoformat(),
                      'eligible_test_rows': int(test.eligible.sum()),
                      'shared_score_rows': int(test.score_shared.sum()), 'model': model})
    if not outputs:
        raise ValueError('insufficient complete training days for a held-out day')
    out = pd.concat(outputs)
    out.index.name = 'target_time'
    return out, folds


def metric_values(actual: np.ndarray, predicted: np.ndarray) -> dict:
    actual, predicted = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    if actual.shape != predicted.shape or actual.ndim != 1 or not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise ValueError('metric inputs must be matching finite vectors')
    if not len(actual):
        return {'points': 0, 'mae_kw': None, 'rmse_kw': None, 'wape_pct': None}
    errors = predicted-actual
    total = float(np.abs(actual).sum())
    return {'points': len(actual), 'mae_kw': float(np.abs(errors).mean()),
            'rmse_kw': float(np.sqrt(np.mean(errors*errors))),
            'wape_pct': float(np.abs(errors).sum()/total*100) if total else None}


def summarize(points: pd.DataFrame) -> dict:
    columns = ['actual_kw', *MODEL_COLUMNS.values()]
    values = points[columns].to_numpy(float)
    shared = points.eligible & pd.Series(np.isfinite(values).all(axis=1), index=points.index)
    illuminated = shared & (points.ghi_wm2 > 20)
    models = {}
    for name, column in MODEL_COLUMNS.items():
        models[name] = {scope: metric_values(points.loc[mask, 'actual_kw'].to_numpy(), points.loc[mask, column].to_numpy())
                        for scope, mask in [('all', shared), ('irradiated', illuminated)]}
    daily = []
    for day, rows in points.groupby(points.index.normalize()):
        shared_rows = rows.loc[shared.reindex(rows.index)]
        expected = pd.date_range(day, periods=96, freq='15min')
        full = shared_rows.index.equals(expected)
        entry = {'date': day.date().isoformat(), 'expected_points': 96,
                 'eligible_points': int(rows.eligible.sum()), 'shared_points': len(shared_rows),
                 'full_day_comparable': full, 'actual_kwh': None, 'models': {}}
        if full:
            actual = float(shared_rows.actual_kw.sum()*0.25)
            entry['actual_kwh'] = actual
            for name, column in MODEL_COLUMNS.items():
                predicted = float(shared_rows[column].sum()*0.25)
                entry['models'][name] = {'predicted_kwh': predicted, 'error_kwh': predicted-actual,
                                         'absolute_error_pct': abs(predicted-actual)/actual*100 if actual else None}
        daily.append(entry)
    full_days = [d for d in daily if d['full_day_comparable']]
    for name in MODEL_COLUMNS:
        errors = [abs(d['models'][name]['error_kwh']) for d in full_days]
        models[name]['daily_energy_mae_kwh'] = float(np.mean(errors)) if errors else None
    return {'evaluation_type': EVALUATION_TYPE, 'target_grid_points': len(points),
            'eligible_points': int(points.eligible.sum()), 'shared_points': int(shared.sum()),
            'irradiated_points': int(illuminated.sum()), 'irradiated_definition': 'GHI > 20 W/m2',
            'shared_full_days': len(full_days), 'models': models, 'daily': daily}
