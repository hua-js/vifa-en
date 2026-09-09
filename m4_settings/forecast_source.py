"""Read existing M3 load forecasts; never request new runs or repeat missing days."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

STATIONS={'station-1':'ES01','station-2':'ES02'}
SHANGHAI=ZoneInfo('Asia/Shanghai')


def _time(value):
    if not isinstance(value,str):raise ValueError('预测时间无效')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.utcoffset() is None:raise ValueError('预测时间缺少时区')
    return parsed.astimezone(SHANGHAI)


def _load_power(value):
    # NocoBase numeric columns may arrive as JSON strings. Parse the decimal
    # before converting to float so negative underflow cannot become valid zero.
    if type(value) not in (str, int, float):
        raise ValueError('M3负荷预测值无效')
    if isinstance(value, str) and (not value or value != value.strip()):
        raise ValueError('M3负荷预测值无效')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        result = float(number)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError('M3负荷预测值无效') from None
    if not math.isfinite(result):
        raise ValueError('M3负荷预测值无效')
    return 0.0 if result == 0 else result


def _load_manual_forecast(client, station_id, *, plan_start_at, now, required_indices):
    if station_id not in STATIONS:raise ValueError('未知电站')
    if plan_start_at.utcoffset() is None or now.utcoffset() is None:raise ValueError('计划时间缺少时区')
    start=plan_start_at.astimezone(SHANGHAI)
    if start.minute%15 or start.second or start.microsecond:raise ValueError('计划起点须对齐15分钟')
    end=start+timedelta(days=1)
    raw=client.list_rows('energy_forecast_manual_runs', fields='id,run_id,station_id,status,forecast_start,forecast_end,interval_seconds,expected_points_per_series,completed_at,content_hash',
        filters={'$and':[{'station_id':{'$eq':STATIONS[station_id]}},{'status':{'$in':['succeeded','evaluated']}},
            {'interval_seconds':{'$eq':900}},{'forecast_start':{'$lt':end.isoformat()}},{'forecast_end':{'$gt':start.isoformat()}}]},
        sort='-completed_at',page_size=100)
    eligible=[]
    for row in raw:
        if row.get('station_id')!=STATIONS[station_id] or row.get('status') not in ('succeeded','evaluated') or row.get('interval_seconds')!=900:continue
        completed=_time(row.get('completed_at'))
        if completed>now:continue
        begin,finish=_time(row.get('forecast_start')),_time(row.get('forecast_end'))
        if not (begin<end and finish>start):continue
        count=row.get('expected_points_per_series')
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=672 or finish-begin!=timedelta(minutes=15*count):raise ValueError('M3预测任务范围与点数不一致')
        if begin.minute%15 or begin.second or begin.microsecond:raise ValueError('M3预测起点未对齐15分钟')
        if not isinstance(row.get('id'),int) or isinstance(row.get('id'),bool) or not isinstance(row.get('run_id'),str) or not row['run_id'].strip():raise ValueError('M3预测任务标识无效')
        eligible.append((completed,row['run_id'],begin,finish,row))
    eligible.sort(key=lambda r:(r[0],r[1]),reverse=True)
    timeline=[start+timedelta(minutes=15*i) for i in range(96)]
    owners=[next((item for item in eligible if item[2]<=ts<item[3]),None)
            if index in required_indices else None for index,ts in enumerate(timeline)]
    selected={item[4]['run_id']:item for item in owners if item is not None}
    maps={}
    for identifier,item in selected.items():
        _,_,begin,finish,row=item
        rows=client.list_rows('energy_forecast_manual_points',fields='run_pk,unique_id,target_time,horizon_step,forecast_value',
            filters={'$and':[{'run_pk':{'$eq':row['id']}},{'unique_id':{'$eq':'station_total_load'}}]},sort='target_time',page_size=1000)
        values={}
        for point in rows:
            if str(point.get('run_pk'))!=str(row['id']) or point.get('unique_id')!='station_total_load':raise ValueError('M3预测点来源不匹配')
            ts=_time(point.get('target_time'));value=_load_power(point.get('forecast_value'))
            if ts in values:raise ValueError('M3预测包含重复时间点')
            step=(ts-begin).total_seconds()/900
            if step!=int(step) or not 0<=step<row['expected_points_per_series'] or point.get('horizon_step')!=int(step)+1:raise ValueError('M3预测时间点或步数不连续')
            values[ts]=float(value)
        expected={begin+timedelta(minutes=15*i) for i in range(row['expected_points_per_series'])}
        if set(values)!=expected:raise ValueError('M3成功任务的负荷预测点不完整')
        maps[identifier]=values
    values=[maps[item[4]['run_id']][ts] if item is not None else None for ts,item in zip(timeline,owners)]
    runs=[{'run_id':item[4]['run_id'],'forecast_start':item[2].isoformat(),'forecast_end':item[3].isoformat(),
        'completed_at':item[0].isoformat(),'content_hash':item[4].get('content_hash')} for item in sorted(selected.values(),key=lambda r:(r[2],r[1]))]
    missing=[ts for ts,value in zip(timeline,values) if value is None]
    digest=hashlib.sha256(json.dumps({'station_id':station_id,'start':start.isoformat(),'runs':runs,'values':values},sort_keys=True).encode()).hexdigest()[:16]
    return {'values':values,'coverage_points':96-len(missing),'runs':runs,'version':f'm3-load-{digest}',
        'point_sources':[{'kind':'manual','run_id':item[4]['run_id']} if item else None for item in owners],
        'issues':[] if not missing else [f'M3负荷预测缺少{len(missing)}点，首个缺点为{missing[0].strftime("%m-%d %H:%M")}（北京时间）'],
        'source':'M3 station_total_load','first_missing_at':missing[0].isoformat() if missing else None}


def _load_rolling_forecast(client, station_id, *, now):
    """Adapt M3's interval START (data_time), never its interval END target_time."""
    rows = client.list_rows('energy_forecast_latest',
        fields='station_id,as_of,generated_at,source_data_end,status,series_payload,content_hash',
        filters={'station_id': {'$eq': STATIONS[station_id]}}, page_size=10)
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('M3滚动预测快照不唯一')
    if not rows:
        return {}, None
    row = rows[0]
    states = ('ok', 'warming_up', 'degraded')
    if (not isinstance(row, dict) or row.get('station_id') != STATIONS[station_id]
            or row.get('status') not in states):
        raise ValueError('M3滚动预测站点或状态无效')
    as_of, generated, begin = (_time(row.get(key)) for key in ('as_of','generated_at','source_data_end'))
    if (not begin <= as_of <= generated <= now
            or begin != as_of.replace(minute=as_of.minute//15*15, second=0, microsecond=0)):
        raise ValueError('M3滚动预测时间顺序或起点无效')
    content_hash = row.get('content_hash')
    if (not isinstance(content_hash, str) or len(content_hash) != 64
            or any(char not in '0123456789abcdef' for char in content_hash)):
        raise ValueError('M3滚动预测版本标识无效')
    series = row.get('series_payload')
    if not isinstance(series, list) or any(not isinstance(item, dict) for item in series):
        raise ValueError('M3滚动预测序列无效')
    loads = [item for item in series if item.get('unique_id') == 'station_total_load']
    if len(loads) != 1:
        raise ValueError('M3滚动负荷序列缺失或重复')
    load = loads[0]
    if (load.get('unit') != 'kW' or load.get('status') not in states
            or not isinstance(load.get('model_name'), str) or not load['model_name'].strip()
            or load['status'] == 'degraded' and not load.get('fallback_reason')):
        raise ValueError('M3滚动负荷单位、模型或状态无效')
    points = load.get('points')
    if not isinstance(points, list) or len(points) != 96:
        raise ValueError('M3滚动负荷预测必须包含连续96点')
    values = {}
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError('M3滚动预测点无效')
        ts = _time(point.get('data_time'))
        if (ts != begin+timedelta(minutes=15*index)
                or _time(point.get('target_time')) != ts+timedelta(minutes=15)
                or type(point.get('horizon_step')) is not int or point['horizon_step'] != index+1):
            raise ValueError('M3滚动预测时间点或步数不连续')
        values[ts] = _load_power(point.get('forecast_value'))
    return values, {'run_id': f'rolling-{STATIONS[station_id]}-{as_of.isoformat()}',
        'source_table': 'energy_forecast_latest', 'as_of': as_of.isoformat(),
        'forecast_start': begin.isoformat(), 'forecast_end': (begin+timedelta(days=1)).isoformat(),
        'completed_at': generated.isoformat(), 'content_hash': content_hash,
        'status': load['status'], 'snapshot_status': row['status'], 'model_name': load['model_name']}


def load_forecast(client, station_id, *, plan_start_at, now, require_full_day=False):
    """Prefer rolling load; use existing manual forecasts only for uncovered slots."""
    if station_id not in STATIONS:
        raise ValueError('未知电站')
    if plan_start_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError('计划时间缺少时区')
    start = plan_start_at.astimezone(SHANGHAI)
    if require_full_day and (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0):
        raise ValueError('自然日预测必须从北京时间零点开始')
    if start.minute % 15 or start.second or start.microsecond:
        raise ValueError('计划起点须对齐15分钟')
    timeline = [start+timedelta(minutes=15*i) for i in range(96)]
    rolling, batch = _load_rolling_forecast(client, station_id, now=now)
    values = [rolling.get(ts) for ts in timeline]
    point_sources = [{'kind':'rolling','run_id':batch['run_id']} if value is not None else None
                     for value in values]
    required = {index for index,value in enumerate(values) if value is None}
    rolling_count = 96-len(required)
    runs = [batch] if rolling_count else []
    # A complete 95-slot prefix is sufficient: no manual read just to add a tail.
    if required and (require_full_day or not (rolling_count == 95 and required == {95})):
        manual = _load_manual_forecast(client, station_id, plan_start_at=start, now=now,
                                      required_indices=required)
        for index in required:
            values[index] = manual['values'][index]
            point_sources[index] = manual['point_sources'][index]
        runs.extend(manual['runs'])
    manual_count = sum(item is not None and item['kind'] == 'manual' for item in point_sources)
    kind = 'rolling_with_manual' if rolling_count and manual_count else 'rolling' if rolling_count else 'manual' if manual_count else 'unavailable'
    horizon = 95 if not require_full_day and values[-1] is None and all(value is not None for value in values[:95]) else 96
    values, point_sources = values[:horizon], point_sources[:horizon]
    missing = [ts for ts,value in zip(timeline, values) if value is None]
    warnings = []
    if horizon == 95:
        warnings.append('本轮采用连续95点，计划时长23小时45分钟；末尾未覆盖时段不参与求解')
    if not rolling_count:
        warnings.append('M3滚动预测尚未覆盖本轮时间窗，当前仅检查已有手动预测；未触发M3任务')
    if manual_count:
        warnings.append(f'负荷来源：滚动预测{rolling_count}点，已有手动预测补充{manual_count}点')
    if rolling_count and (batch['status'] != 'ok' or batch['snapshot_status'] != 'ok'):
        warnings.append(f'M3滚动负荷状态为{batch["status"]}（快照{batch["snapshot_status"]}），模型{batch["model_name"]}')
    digest = hashlib.sha256(json.dumps({'policy':'natural-day-96-v1' if require_full_day else 'rolling-min95-v2',
        'station_id':station_id, 'start':start.isoformat(), 'runs':runs,
        'point_sources':point_sources, 'values':values}, sort_keys=True).encode()).hexdigest()[:16]
    return {'values':values, 'coverage_points':horizon-len(missing), 'runs':runs,
        'horizon_points':horizon, 'requested_horizon_points':96,
        'version':f'm3-load-{digest}', 'point_sources':point_sources, 'source_kind':kind,
        'rolling_points':rolling_count, 'manual_points':manual_count,
        'rolling_status':batch['status'] if batch else None,
        'rolling_generated_at':batch['completed_at'] if batch else None,
        'rolling_forecast_start':batch['forecast_start'] if batch else None,
        'rolling_forecast_end':batch['forecast_end'] if batch else None,
        'issues':[] if not missing else [f'M3负荷预测缺少{len(missing)}点，首个缺点为{missing[0].strftime("%m-%d %H:%M")}（北京时间）'],
        'warnings':warnings, 'source':'M3 station_total_load',
        'first_missing_at':missing[0].isoformat() if missing else None}
