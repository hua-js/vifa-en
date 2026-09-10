"""Validated public projection of the latest persisted ES02 PV forecast."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from m3.worker.domain.pv_operational import timestamp
from m3.worker.services import pv_publication as publication

RUN_KEYS=tuple('run_id es_sn run_kind as_of forecast_start forecast_end interval_minutes expected_points status model_name model_version model_config training_start training_end pv_history_end weather_batch_id weather_source_kind source_manifest evaluation_metrics bounds_coverage_pct generated_at error_code content_hash'.split())
POINT_KEYS=('target_time','horizon_step',*publication.POWER_FIELDS,'is_clipped')


def latest_forecast(repository, now):
    at=timestamp(now)
    record=repository.latest_run(at)
    if record is None:return {'status':'empty','data':None}
    run={k:record[k] for k in RUN_KEYS}
    if run['status']!='completed' or run['es_sn']!='ES02' or run['run_kind']!='operational':
        raise ValueError('latest forecast station/status mismatch')
    for key in publication.TIME_FIELDS:run[key]=timestamp(run[key]).isoformat()
    if timestamp(run['as_of'])>at or timestamp(run['generated_at'])>at:
        raise ValueError('latest forecast is not available at query cutoff')
    run_pk=publication.integer_id(record['id'])
    actual=repository.read_points(run_pk)
    points=[]
    for row in actual:
        if publication.integer_id(row['run_pk'])!=run_pk:
            raise ValueError('latest forecast association mismatch')
        point={k:row[k] for k in POINT_KEYS}
        point['target_time']=timestamp(point['target_time']).isoformat()
        for key in publication.POWER_FIELDS:point[key]=publication.power(point[key])
        points.append(point)
    points.sort(key=lambda p:p['horizon_step'])
    publication.validate_bundle(run,points)
    # Only the display projection leaves this service; raw provenance remains in NocoBase.
    values=[Decimal(p['forecast_kw']) for p in points]
    peak=max(values);first_peak=values.index(peak)
    age=max(0,(at-timestamp(run['as_of'])).total_seconds())
    freshness='expired' if timestamp(run['forecast_end'])<=at else 'stale' if age>=7200 else 'fresh'
    data={k:run[k] for k in ('run_id','es_sn','as_of','generated_at','forecast_start','forecast_end','interval_minutes',
        'expected_points','model_name','model_version','training_end','weather_batch_id')}
    data.update(run_pk=run_pk,queried_at=at.isoformat(),updated_at=timestamp(record.get('updatedAt') or run['generated_at']).isoformat(),
        training_rows=run['source_manifest']['training_rows'],weather_fetched_at=run['source_manifest']['forecast_weather_rows'][0]['fetched_at'],
        forecast_energy_kwh=float(sum(values)/4),peak_kw=float(peak),peak_time=points[first_peak]['target_time'],
        freshness=freshness,age_seconds=age,remaining_full_points=sum(timestamp(p['target_time'])>=at for p in points),
        points=[{**p,**{k:float(p[k]) if p[k] is not None else None for k in publication.POWER_FIELDS}} for p in points])
    return {'status':'ok','data':data}
