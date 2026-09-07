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


def load_forecast(client, station_id, *, plan_start_at, now):
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
    owners=[next((item for item in eligible if item[2]<=ts<item[3]),None) for ts in timeline]
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
        'issues':[] if not missing else [f'M3负荷预测缺少{len(missing)}点，首个缺点为{missing[0].strftime("%m-%d %H:%M")}（北京时间）'],
        'source':'M3 station_total_load','first_missing_at':missing[0].isoformat() if missing else None}
