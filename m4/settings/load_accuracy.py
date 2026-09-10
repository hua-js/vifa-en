"""Use the same current-task load MAPE formula as the M3 page."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import hashlib
import json

STATIONS = {'station-1': 'ES01', 'station-2': 'ES02'}
POLICY = 'm4-current-task-load-mape-v2'
MAX_MAPE = Decimal('30')
SHANGHAI = ZoneInfo('Asia/Shanghai')
# Match the M3 page's default output: 15 minutes, one day, current load policy.
INTERVAL_SECONDS = 900
FORECAST_DAYS = 1
SELECTION_POLICY = 'weekly_load_v2'


def _time(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.utcoffset() is None:
        raise ValueError
    return at


def _number(value):
    if type(value) not in (str, int, float):
        raise ValueError
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError
    return number


def assess(evidence, station_id, now):
    result = {'policy': POLICY, 'threshold_percent': str(MAX_MAPE), 'status': 'unavailable',
              'mape_percent': None, 'issues': [], 'evidence': evidence}
    message = 'M3当前负荷预测MAPE无法评估，未允许求解'
    try:
        run, points = evidence['run'], evidence['points']
        today = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = _time(run['forecast_start']), _time(run['forecast_end'])
        if (run['station_id'] != STATIONS[station_id] or run['status'] not in ('succeeded', 'evaluated')
                or not start <= today < end or _time(run['completed_at']) > now
                or run['interval_seconds'] != INTERVAL_SECONDS or run['forecast_days'] != FORECAST_DAYS
                or end-start != timedelta(days=FORECAST_DAYS)
                or run['model_manifest']['selection_policy'] != SELECTION_POLICY
                or not isinstance(run['run_id'], str) or not run['run_id']):
            raise ValueError
        count = run['expected_points_per_series']
        if type(count) is not int or count != 86400*FORECAST_DAYS//INTERVAL_SECONDS or len(points) != count:
            raise ValueError
        steps, times, errors = set(), set(), []
        actual_count = zero_count = 0
        for point in points:
            step = point['horizon_step']
            at = _time(point['target_time'])
            if (point['run_id'] != run['run_id'] or point['unique_id'] != 'station_total_load'
                    or type(step) is not int or not 1 <= step <= count or step in steps or at in times
                    or at != start+timedelta(seconds=INTERVAL_SECONDS*(step-1))):
                raise ValueError
            steps.add(step);times.add(at)
            # Current M3 MAPE excludes invalid/nonfinite pairs and zero actuals.
            if point.get('actual_quality') != 'valid':
                continue
            try:
                actual, forecast = _number(point.get('actual_value')), _number(point.get('forecast_value'))
            except (ValueError, InvalidOperation):
                continue
            if at > now:
                raise ValueError
            actual_count += 1
            if actual == 0:
                zero_count += 1
            else:
                errors.append(abs(forecast-actual)/abs(actual)*100)
        result.update(run_id=run['run_id'], window_start=start.isoformat(), window_end=end.isoformat(),
            valid_count=len(errors), actual_count=actual_count, zero_actual_count=zero_count, expected_count=count,
            provisional=actual_count<count)
        if not errors:
            message = 'M3当前负荷预测尚无有效非零实测对比点，无法计算MAPE，未允许求解'
            raise ValueError
        number = sum(errors)/len(errors)
        result.update(mape_percent=str(number), status='ready' if number <= MAX_MAPE else 'blocked')
        if number > MAX_MAPE:
            result['issues'] = [f'M3当前负荷预测MAPE {number:.4f}% 大于{MAX_MAPE}%，未允许求解']
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
        result['issues'] = [message]
    result['version'] = 'load-accuracy-'+hashlib.sha256(json.dumps({
        'policy': POLICY, 'threshold': str(MAX_MAPE), 'evidence': evidence,
        'status': result['status']}, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return result


def read_gate(client, station_id, now):
    try:
        return assess(client.current_load_result(STATIONS[station_id]), station_id, now)
    except Exception:
        result = assess(None, station_id, now)
        result['issues'] = ['M3当前负荷预测MAPE读取失败，未允许求解，请检查M3结果接口、Socket和专用凭据']
        return result


def require_gate(gate, station_id, now):
    checked = assess(gate.get('evidence') if isinstance(gate, dict) else None, station_id, now)
    if checked['status'] != 'ready':
        raise ValueError('；'.join(checked['issues']))
    if gate.get('version') != checked['version']:
        raise ValueError('负荷MAPE证据或门槛版本已变化，请刷新输入')
    return checked
