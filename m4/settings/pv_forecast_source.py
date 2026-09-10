"""Read one completed PV batch for rolling M4 inputs; never trigger generation."""
import hashlib
import json
import re
from datetime import timedelta

from .forecast_source import _time, _load_power
from .timeseries import InputDataError
from .upstream import SourceReadError

class ForecastRefreshRequired(InputDataError):
    """A valid new batch can recover this missing/stale/coverage condition."""


POLICY = 'm4-pv-operational-v1'
MAX_AGE_SECONDS = 7200
RUNS = 'energy_pv_forecast_runs'
POINTS = 'energy_pv_forecast_points'
RUN_FIELDS = 'id,run_id,es_sn,run_kind,status,as_of,generated_at,forecast_start,forecast_end,interval_minutes,expected_points,model_name,model_version,weather_batch_id,content_hash'


def validate_source(source, *, start, end, now):
    """Also used immediately before creating an optimizer request."""
    try:
        as_of, generated, begin, finish = (_time(source.get(k)) for k in
            ('as_of', 'generated_at', 'forecast_start', 'forecast_end'))
        if source.get('source_kind') != 'operational_forecast' or source.get('es_sn') != 'ES02':
            raise ValueError
        if not as_of <= generated <= now or not generated < begin:
            raise ValueError
        if not 0 <= (now-as_of).total_seconds() < MAX_AGE_SECONDS:
            raise ForecastRefreshRequired('光伏预测已过期（有效期2小时），请手动生成新预测后刷新输入')
        if not begin <= start < end <= finish:
            raise ForecastRefreshRequired('光伏预测未覆盖完整计划窗口，请手动生成新预测后刷新输入')
    except (TypeError, KeyError, ValueError) as error:
        if isinstance(error, InputDataError):
            raise
        raise InputDataError('光伏预测来源或时间信息无效') from None


def load_pv_forecast(client, *, plan_start_at, now):
    """Return 96 aligned slots; uncovered slots stay None for horizon selection."""
    try:
        start = _time(plan_start_at.isoformat())
        at = _time(now.isoformat())
        if start.minute % 15 or start.second or start.microsecond:
            raise ValueError
        rows = client.list_rows(RUNS, fields=RUN_FIELDS,
            filters={'es_sn': {'$eq': 'ES02'}, 'run_kind': {'$eq': 'operational'},
                'status': {'$eq': 'completed'}, 'as_of': {'$lte': at.isoformat()},
                'generated_at': {'$lte': at.isoformat()}},
            sort='-as_of,-id', page_size=1, limit=1)
        if not rows:
            raise ForecastRefreshRequired('尚无已完成的光伏预测，请先手动生成预测')
        if len(rows) != 1:
            raise ValueError
        run = rows[0]
        if (run.get('es_sn') != 'ES02' or run.get('run_kind') != 'operational'
                or run.get('status') != 'completed' or type(run.get('id')) is not int or run['id'] <= 0
                or type(run.get('interval_minutes')) is not int or run['interval_minutes'] != 15
                or type(run.get('expected_points')) is not int or run['expected_points'] != 96):
            raise ValueError
        for key in ('run_id', 'model_name', 'model_version'):
            if not isinstance(run.get(key), str) or not run[key].strip():
                raise ValueError
        for key in ('content_hash', 'weather_batch_id'):
            if not isinstance(run.get(key), str) or not re.fullmatch('[0-9a-f]{64}', run[key]):
                raise ValueError
        begin, finish, as_of = (_time(run.get(k)) for k in ('forecast_start', 'forecast_end', 'as_of'))
        expected_begin = as_of.replace(minute=as_of.minute//15*15, second=0, microsecond=0)+timedelta(minutes=15)
        if begin != expected_begin or finish != begin+timedelta(days=1):
            raise ValueError
        source = {key: run[key] for key in ('run_id', 'es_sn', 'as_of', 'generated_at',
            'forecast_start', 'forecast_end', 'model_name', 'model_version', 'weather_batch_id', 'content_hash')}
        source.update(source_kind='operational_forecast', source='天气驱动光伏功率预测',
                      max_age_seconds=MAX_AGE_SECONDS, run_pk=run['id'])
        # Check freshness now; full requested-window coverage is checked after
        # M3 load has determined whether this round needs 95 or 96 slots.
        validate_source(source, start=begin, end=finish, now=at)
        points = client.list_rows(POINTS, fields='run_pk,target_time,horizon_step,forecast_kw',
            filters={'run_pk': {'$eq': run['id']}}, sort='horizon_step', page_size=100, max_pages=2)
        if len(points) != 96:
            raise InputDataError('已完成光伏批次的预测点不完整，要求连续96点')
        values = {}
        for point in points:
            if (type(point.get('run_pk')) not in (str, int) or str(point['run_pk']) != str(run['id'])
                    or type(point.get('horizon_step')) is not int or not 1 <= point['horizon_step'] <= 96):
                raise ValueError
            ts = _time(point.get('target_time'))
            if ts != begin+timedelta(minutes=15*(point['horizon_step']-1)) or ts in values:
                raise ValueError
            values[ts] = _load_power(point.get('forecast_kw'))
        aligned = [values.get(start+timedelta(minutes=15*i)) for i in range(96)]
        digest = hashlib.sha256(json.dumps({'policy': POLICY, 'source': source,
            'start': start.isoformat(), 'values': aligned}, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
        return {**source, 'values': aligned, 'version': f'pv-forecast-{digest}',
                'coverage_points': sum(v is not None for v in aligned), 'issues': []}
    except (InputDataError, SourceReadError):
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InputDataError('光伏预测批次、时间轴或功率数据无效') from None
