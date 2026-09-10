"""ES02 measured-power quality gates and retrospective weather alignment.

Hourly reanalysis is expanded explicitly; this does not create measured
quarter-hour weather or establish historical forecast availability.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TZ = 'Asia/Shanghai'
INSTANT_FIELDS = ('temperature_c', 'relative_humidity_pct', 'cloud_cover_pct',
                  'cloud_cover_low_pct', 'cloud_cover_mid_pct', 'cloud_cover_high_pct', 'wind_speed_kmh')
RADIATION_FIELDS = ('ghi_wm2', 'direct_horizontal_wm2', 'dhi_wm2')
WEATHER_FIELDS = (*INSTANT_FIELDS, *RADIATION_FIELDS, 'precipitation_mm')


def prepare_pv(rows: list[dict]) -> pd.DataFrame:
    """Equal-weight minute means; >=12 valid minutes and four quarters/hour."""
    if not rows:
        raise ValueError('empty PV snapshot')
    times, values = [], []
    for row in rows:
        if row.get('es_sn') != 'ES02':
            raise ValueError('PV snapshot must contain ES02 only')
        at = pd.Timestamp(row['timestamp'])
        if pd.isna(at) or at.tzinfo is None:
            raise ValueError('PV timestamp must include timezone')
        times.append(at.tz_convert(TZ))
        raw = row.get('ac_solar_power')
        if raw is None:
            values.append(np.nan)
            continue
        if isinstance(raw, bool):
            raise ValueError('boolean PV power')
        value = float(raw)
        if not np.isfinite(value) or value < 0:
            raise ValueError('PV power must be finite and nonnegative, or null')
        values.append(value)
    series = pd.Series(values, index=pd.DatetimeIndex(times), dtype=float).sort_index()
    if series.index.has_duplicates:
        raise ValueError('duplicate PV timestamp')
    minute = series.resample('min').mean()
    quarters = minute.resample('15min', closed='left', label='left')
    result = pd.DataFrame({'valid_minutes': quarters.count(), 'actual_kw': quarters.mean()})
    result.index.name = 'target_time'
    result['quarter_valid'] = result.valid_minutes >= 12
    result.loc[~result.quarter_valid, 'actual_kw'] = np.nan
    result['hour_valid'] = result.quarter_valid.groupby(result.index.floor('h')).transform('sum') == 4
    return result


def align_weather_grid(index: pd.DatetimeIndex, weather: pd.DataFrame) -> pd.DataFrame:
    """Expand hourly weather onto quarter intervals without inventing PV labels."""
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None or index.has_duplicates:
        raise ValueError('target index must be unique and timezone-aware')
    index = index.tz_convert(TZ)
    if not (index == index.floor('15min')).all():
        raise ValueError('target index must be on quarter-hour grid')
    if not isinstance(weather.index, pd.DatetimeIndex) or weather.index.tz is None:
        raise ValueError('weather index must include timezone')
    weather = weather.copy()
    weather.index = weather.index.tz_convert(TZ)
    if weather.index.has_duplicates or not (weather.index == weather.index.floor('h')).all():
        raise ValueError('duplicate or non-hourly weather timestamp')
    weather = weather.loc[:, WEATHER_FIELDS].astype(float).sort_index()
    if np.isinf(weather.to_numpy()).any():
        raise ValueError('infinite weather value')
    for field in WEATHER_FIELDS:
        if field != 'temperature_c' and (weather[field] < 0).any():
            raise ValueError('negative weather value')
        if field.endswith('_pct') and (weather[field] > 100).any():
            raise ValueError('percentage outside range')
    out = pd.DataFrame(index=index)
    start = out.index.floor('h')
    end = start + pd.Timedelta(hours=1)
    left, right = weather.reindex(start), weather.reindex(end)
    fraction = (out.index.minute.to_numpy() + 7.5) / 60
    for field in INSTANT_FIELDS:
        out[field] = left[field].to_numpy() * (1-fraction) + right[field].to_numpy() * fraction
    for field in RADIATION_FIELDS:
        out[field] = right[field].to_numpy()
    out['precipitation_mm'] = right.precipitation_mm.to_numpy()/4
    out['weather_hour_start'] = start
    out['weather_radiation_time'] = end
    return out


def align_weather(pv: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Join weather and retain measured-power eligibility gates."""
    out = pv.join(align_weather_grid(pv.index, weather))
    available = out.loc[:, WEATHER_FIELDS].notna().all(axis=1)
    out['eligible'] = out.hour_valid & available
    out['rejection_reason'] = np.select(
        [~out.quarter_valid, ~out.hour_valid, ~available],
        ['pv_insufficient_minutes', 'pv_incomplete_hour', 'weather_missing'], default='')
    return out
