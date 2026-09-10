"""ES02 operational WeatherRidge: one available forecast batch, 96 quarters."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from m3_worker.domain import pv_backtest, pv_training

VERSION = 'weather-ridge-operational-v1'


def timestamp(value):
    at = pd.Timestamp(value)
    if pd.isna(at) or at.tzinfo is None:
        raise ValueError('timestamp requires an explicit timezone')
    return at.tz_convert(pv_training.TZ)


def next_grid(as_of):
    start = timestamp(as_of).floor('15min') + pd.Timedelta(minutes=15)
    return pd.date_range(start, periods=96, freq='15min', name='target_time')


def prepare_future(rows, as_of):
    at = timestamp(as_of)
    if len(rows) != 50:
        raise ValueError('forecast batch must contain its 50 hourly anchors')
    first = rows[0]
    if not re.fullmatch('[0-9a-f]{64}', first['source_batch_id']):
        raise ValueError('invalid weather batch identity')
    for row in rows:
        expected = {'es_sn': 'ES02', 'source_kind': 'forecast', 'provider': 'open_meteo',
                    'source_batch_id': first['source_batch_id'], 'interval_minutes': 60,
                    'timezone': pv_training.TZ}
        if any(row.get(k) != v for k, v in expected.items()):
            raise ValueError('weather station, source or batch mismatch')
        if float(row['latitude']) != 23 or float(row['longitude']) != 113:
            raise ValueError('weather coordinates mismatch')
        fetched = timestamp(row['fetched_at'])
        if fetched > at or fetched != timestamp(first['fetched_at']):
            raise ValueError('weather was not available at as_of or has mixed fetch times')
        if row['issued_at'] is not None and timestamp(row['issued_at']) > fetched:
            raise ValueError('weather issue time is later than receipt')
    weather = pd.DataFrame(rows)
    weather.index = pd.DatetimeIndex([timestamp(r['weather_time']) for r in rows])
    weather = weather.sort_index()
    if not weather.index.equals(pd.date_range(weather.index.min(), periods=50, freq='h')):
        raise ValueError('weather batch is not continuous')
    grid = next_grid(at)
    required = pd.date_range(grid.min().floor('h'), (grid.max()+pd.Timedelta(minutes=15)).ceil('h'), freq='h')
    if not required.isin(weather.index).all() or not weather.loc[required, 'quality_status'].eq('valid').all():
        raise ValueError('weather does not cover the full forecast window')
    frame = pv_training.align_weather_grid(grid, weather)
    if not np.isfinite(frame.loc[:, pv_training.WEATHER_FIELDS].to_numpy(float)).all():
        raise ValueError('missing weather in forecast window')
    return frame


def generate(training, rows, as_of):
    at = timestamp(as_of)
    index = training.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None or index.has_duplicates:
        raise ValueError('training needs unique timezone-aware timestamps')
    if not (index == index.floor('15min')).all():
        raise ValueError('training is not on quarter-hour grid')
    train = training.loc[index+pd.Timedelta(minutes=15) <= at].copy().sort_index()
    train.index = train.index.tz_convert(pv_training.TZ)
    if (train.groupby(train.index.normalize()).size() == 96).sum() < 7:
        raise ValueError('at least seven complete training days required')
    future = prepare_future(rows, at)
    model = pv_backtest.fit_weather_models(train)
    raw = pv_backtest.predict_raw_ridge(future, model)
    points = pd.DataFrame({'horizon_step': np.arange(1, 97), 'raw_forecast_kw': raw,
                          'forecast_kw': np.maximum(0.0, raw), 'is_clipped': raw < 0}, index=future.index)
    return {'points': points, 'model': model, 'training': train, 'weather': future}
