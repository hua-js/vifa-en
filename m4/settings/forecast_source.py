"""Read existing M3 load forecasts; never request new runs or repeat missing days."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .load_accuracy import read_gate

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


LOAD_POLICY = 'm3-current-task-load-v1'


def load_forecast(client, station_id, *, plan_start_at, now, require_full_day=False):
    """Use the same M3 task for forecast and score; refresh actuals separately."""
    if station_id not in STATIONS:
        raise ValueError('未知电站')
    if plan_start_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError('计划时间缺少时区')
    start = plan_start_at.astimezone(SHANGHAI)
    if start.minute % 15 or start.second or start.microsecond:
        raise ValueError('计划起点须对齐15分钟')
    if require_full_day and (start.hour, start.minute) != (0, 0):
        raise ValueError('自然日预测必须从北京时间零点开始')
    gate = read_gate(client, station_id, now)
    evidence = gate.get('evidence') or {}
    run = evidence.get('run') or {}
    points = evidence.get('points')
    if (run.get('station_id') != STATIONS[station_id]
            or run.get('status') not in ('succeeded', 'evaluated')
            or run.get('interval_seconds') != 900
            or run.get('forecast_days') != 1
            or run.get('expected_points_per_series') != 96
            or not isinstance(run.get('run_id'), str) or not run['run_id']
            or not isinstance(points, list) or len(points) != 96):
        raise ValueError('M3当前负荷预测任务不可用')
    begin, end = _time(run.get('forecast_start')), _time(run.get('forecast_end'))
    if (end != begin + timedelta(days=1) or begin.minute % 15
            or begin.second or begin.microsecond or _time(run.get('completed_at')) > now):
        raise ValueError('M3当前负荷预测任务时间无效')
    forecasts, actuals = {}, {}
    for point in points:
        at = _time(point.get('target_time'))
        step = point.get('horizon_step')
        if (point.get('run_id') != run['run_id']
                or point.get('unique_id') != 'station_total_load'
                or type(step) is not int or not 1 <= step <= 96
                or at != begin + timedelta(minutes=15*(step-1)) or at in forecasts):
            raise ValueError('M3当前负荷预测点来源或时间不一致')
        forecasts[at] = _load_power(point.get('forecast_value'))
        actuals[at] = None
        if point.get('actual_quality') == 'valid' and at + timedelta(minutes=15) <= now:
            try:
                actuals[at] = _load_power(point.get('actual_value'))
            except ValueError:
                pass
    timeline = [start + timedelta(minutes=15*i) for i in range(96)]
    values = [forecasts.get(at) for at in timeline]
    actual_values = [actuals.get(at) for at in timeline]
    missing = [at for at, value in zip(timeline, values) if value is None]
    # Actual overlays and MAPE refreshes must not change forecast identity.
    digest = hashlib.sha256(json.dumps({'policy': LOAD_POLICY, 'station': station_id,
        'run_id': run['run_id'], 'start': start.isoformat(), 'values': values},
        sort_keys=True).encode()).hexdigest()[:16]
    return {'accuracy_gate': gate, 'values': values, 'actual_values': actual_values,
        'actual_read_at': now.isoformat(), 'run_id': run['run_id'],
        'coverage_points': 96-len(missing), 'horizon_points': 96,
        'requested_horizon_points': 96, 'version': 'm3-load-current-'+digest,
        'policy': LOAD_POLICY, 'source_kind': 'current_task',
        'point_sources': [{'kind': 'current_task', 'run_id': run['run_id']}
                          if value is not None else None for value in values],
        'runs': [{'run_id': run['run_id'], 'forecast_start': begin.isoformat(),
                  'forecast_end': end.isoformat(), 'completed_at': run['completed_at']}],
        'rolling_points': 0, 'manual_points': 96-len(missing),
        'rolling_status': None, 'rolling_generated_at': None,
        'rolling_forecast_start': None, 'rolling_forecast_end': None,
        'issues': gate['issues'] + ([] if not missing else ['M3当前任务未覆盖完整计划时段']),
        'warnings': [], 'source': 'M3 station_total_load',
        'first_missing_at': missing[0].isoformat() if missing else None}


def validate_forecast_values(source, points):
    """Bind solver values to the very task whose MAPE was checked."""
    evidence = source.get('accuracy_gate', {}).get('evidence') or {}
    run_id = (evidence.get('run') or {}).get('run_id')
    if source.get('policy') != LOAD_POLICY or not run_id or source.get('run_id') != run_id:
        raise ValueError('负荷预测来源已变化，请重新读取')
    forecasts = {_time(p['target_time']): _load_power(p['forecast_value'])
                 for p in evidence['points']}
    for point in points:
        if forecasts.get(point.timestamp) != point.load_forecast_kw:
            raise ValueError('负荷预测与M3当前任务不一致')
