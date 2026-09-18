"""Pure input builders for the shared daily tariff and historical PV reference.

PV is a seven-day historical reference, not a weather forecast. A valid quarter
has at least 12 observed minutes; no two consecutive minutes may be missing,
including across quarter/day boundaries. Missing observations are never zeroed.
"""
from shared.project import get_project
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from statistics import median
from zoneinfo import ZoneInfo


class InputDataError(ValueError):
    """Input data cannot be interpreted without inventing values or rules."""


_SHANGHAI = ZoneInfo('Asia/Shanghai')
_CLOCK = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\Z')
_PRICE_FIELDS = {'gu': 'vprice', 'ping': 'fprice', 'feng': 'hprice', 'jian': 'jprice'}
PV_EXPORT_POLICY = 'pv-export-flat-tariff-v1'
_PV_METHOD = 'seven_day_same_slot_median'


def _plan_start(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InputDataError('计划起点必须是含时区的时间')
    local = value.astimezone(_SHANGHAI)
    if local.minute % 15 or local.second or local.microsecond:
        raise InputDataError('计划起点必须对齐上海时间的 15 分钟边界')
    return local


def _rows(value, label):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InputDataError(f'{label}必须是记录列表')
    if any(not isinstance(row, Mapping) for row in value):
        raise InputDataError(f'{label}包含无效记录')
    return value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InputDataError(f'{label}必须是非负有限数值')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise InputDataError(f'{label}必须是非负有限数值') from None
    if not math.isfinite(result) or result < 0:
        raise InputDataError(f'{label}必须是非负有限数值')
    return 0.0 if result == 0 else result


def _version(prefix, value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return f'{prefix}-{hashlib.sha256(canonical.encode()).hexdigest()[:20]}'


def _clock_seconds(value, *, end=False):
    if end and value == '24:00:00':
        return 86400
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        raise InputDataError('电价时段必须使用有效 HH:MM:SS，日末可用 24:00:00')
    hour, minute, second = map(int, value.split(':'))
    return hour * 3600 + minute * 60 + second


def build_tariff_points(period_rows, rate_rows, *, plan_start_at: datetime) -> dict:
    """Return 96 buy prices and period labels from the shared daily tariff."""
    local_start = _plan_start(plan_start_at)
    periods = _rows(period_rows, '电价时段')
    rates = _rows(rate_rows, '电价配置')
    if len(rates) != 1:
        raise InputDataError('共用电价必须且只能有一条明确生效配置')
    if not periods:
        raise InputDataError('共用电价时段为空')
    sell_price = _number(rates[0].get('fprice'), '平段上网电价 fprice')
    segments = []
    normalized = False
    for row in periods:
        period_type = row.get('period_type')
        if not isinstance(period_type, str) or period_type not in _PRICE_FIELDS:
            raise InputDataError('电价时段含尚未确认的 period_type')
        start = _clock_seconds(row.get('start_time'))
        end = _clock_seconds(row.get('end_time'), end=True)
        if end == 86340:  # User confirmed 23:59 means through the end of day.
            end = 86400
            normalized = True
        if end <= start:
            raise InputDataError('电价时段结束必须晚于开始；跨日时段须按自然日拆分')
        field = _PRICE_FIELDS[period_type]
        segments.append((start, end, _number(rates[0].get(field), f'电价 {field}'), period_type))
    segments.sort()
    boundary = 0
    for start, end, _, _ in segments:
        if start != boundary:
            raise InputDataError('共用电价时段存在重叠或缺口')
        boundary = end
    if boundary != 86400:
        raise InputDataError('共用电价未覆盖完整自然日')

    daily_values = []
    daily_period_types = []
    for slot in range(96):
        start, end = slot * 900, (slot + 1) * 900
        prices = {price for left, right, price, _ in segments if left < end and right > start}
        if len(prices) != 1:
            raise InputDataError('电价在 15 分钟区间内发生变化，不能无依据折算单价')
        period_types = {kind for left, right, _, kind in segments if left < end and right > start}
        if len(period_types) != 1:
            raise InputDataError('电价时段标签在 15 分钟区间内发生变化，不能确定唯一时段')
        daily_values.append(prices.pop())
        daily_period_types.append(period_types.pop())
    first_slot = local_start.hour * 4 + local_start.minute // 15
    return {
        'values': daily_values[first_slot:] + daily_values[:first_slot],
        'period_types': daily_period_types[first_slot:] + daily_period_types[:first_slot],
        'sell_price_per_kwh': sell_price,
        'export_price_policy': PV_EXPORT_POLICY,
        'version': _version('shared-tariff', {'values': daily_values, 'period_types': daily_period_types,
            'sell_price_per_kwh': sell_price, 'export_price_policy': PV_EXPORT_POLICY}),
        'normalized_end_of_day': normalized,
        'source': 'shared_tariff',
    }


def tariff_export_price(source):
    """Require the confirmed flat-price source; never infer or default revenue."""
    if source.get('export_price_policy') != PV_EXPORT_POLICY:
        raise InputDataError('上网电价来源已更新，请重新读取电价。')
    value = source.get('sell_price_per_kwh')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputDataError('缺少有效平段上网电价。')
    return _number(value, '平段上网电价')


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(_SHANGHAI)
    except (ValueError, OverflowError):
        raise InputDataError('光伏历史 timestamp 必须是含时区的有效时间') from None


def _mean(values):
    # Offset first to preserve constant readings exactly, then divide before
    # summing so finite nonnegative samples do not overflow their raw sum.
    baseline = min(values)
    try:
        result = baseline + math.fsum((value - baseline) / len(values) for value in values)
    except OverflowError:
        raise InputDataError('光伏历史聚合结果超出有限数值范围') from None
    if not math.isfinite(result):
        raise InputDataError('光伏历史聚合结果必须是有限数值')
    return result


def build_pv_reference(
    station_id, history_rows, *, plan_start_at: datetime,
    history_end_at: datetime | None = None,
) -> dict:
    """Build future rolling slots from the preceding seven complete local days.

    Public station IDs and source codes come from the project inventory. The
    history interval is [history_start, history_end). An explicit history end
    lets a next-day plan use only days complete when fetching began; otherwise
    it defaults to midnight on the plan's local date.
    """
    local_start = _plan_start(plan_start_at)
    if station_id not in tuple(s.id for s in get_project().stations):
        raise InputDataError('未知电站，无法确定光伏拓扑')
    if history_end_at is None:
        history_end = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        if not isinstance(history_end_at, datetime) or history_end_at.utcoffset() is None:
            raise InputDataError('光伏历史结束时间必须包含时区')
        history_end = history_end_at.astimezone(_SHANGHAI)
        if (history_end.hour or history_end.minute or history_end.second
                or history_end.microsecond or history_end > local_start):
            raise InputDataError('光伏历史结束时间必须是上海零点且不晚于计划起点')
    if not get_project().station(station_id).has_pv:
        return {
            'values': [0.0] * 96,
            'version': _version('pv-reference', {'station': station_id, 'method': 'station_without_pv'}),
            'method': 'station_without_pv',
            'history_start': None, 'history_end': None, 'valid_days': [], 'issues': [],
        }
    history_start = history_end - timedelta(days=7)
    timestamps = {}
    for row in _rows(history_rows, '光伏历史'):
        source_station = row.get('es_sn')
        if not isinstance(source_station, str) or not source_station:
            raise InputDataError('光伏历史缺少明确电站归属')
        if source_station != get_project().station(station_id).source_code:
            continue
        at = _timestamp(row.get('timestamp'))
        if not history_start <= at < history_end:
            continue
        power = _number(row.get('ac_solar_power'), '光伏历史 ac_solar_power')
        if at in timestamps and timestamps[at] != power:
            raise InputDataError('光伏历史同一电站时间存在冲突功率')
        timestamps[at] = power

    minutes = [[] for _ in range(7 * 1440)]
    for at, power in sorted(timestamps.items()):
        minute = int((at - history_start).total_seconds() // 60)
        minutes[minute].append(power)
    previous_missing = False
    for values in minutes:
        missing = not values
        if missing and previous_missing:
            raise InputDataError('光伏历史存在连续缺失分钟，七日参考未通过完整性检查')
        previous_missing = missing

    daily_values = []
    for day in range(7):
        day_values = []
        for slot in range(96):
            offset = day * 1440 + slot * 15
            minute_means = [_mean(values) for values in minutes[offset:offset + 15] if values]
            if len(minute_means) < 12:
                raise InputDataError('光伏历史的 15 分钟区间不足 12 个有效分钟')
            day_values.append(_mean(minute_means))
        daily_values.append(day_values)
    reference = [float(median(day[slot] for day in daily_values)) for slot in range(96)]
    first_slot = local_start.hour * 4 + local_start.minute // 15
    valid_days = [(history_start + timedelta(days=day)).date().isoformat() for day in range(7)]
    return {
        'values': reference[first_slot:] + reference[:first_slot],
        'version': _version('pv-reference', {
            'method': _PV_METHOD, 'days': valid_days, 'daily_values': daily_values,
        }),
        'method': _PV_METHOD,
        'history_start': history_start.isoformat(),
        'history_end': history_end.isoformat(),
        'valid_days': valid_days,
        'issues': [],
    }
