"""Read station day/month statistics without turning them into settlement claims."""
import calendar
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .roster import STATION_CABINETS
from .upstream import SourceReadError

TZ = ZoneInfo('Asia/Shanghai')
DAY_FIELDS = ('day_charge', 'day_discharge', 'charge_cost', 'discharge_earnings', 'day_earnings')
MONTH_FIELDS = ('month_charge', 'month_discharge', 'month_earnings', 'month_costs', 'month_income', 'profit')
MONTH_PATTERN = r'20[0-9]{2}-(0[1-9]|1[0-2])'


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(MONTH_PATTERN, month):
        raise ValueError('月份格式应为 YYYY-MM（2000–2099）')
    start = datetime.strptime(month, '%Y-%m').replace(tzinfo=TZ)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(TZ)
    except (ValueError, TypeError, AttributeError):
        raise SourceReadError('统计记录的时间字段无效') from None


def _number(value):
    # Missing is not zero; accept the numeric strings used by NocoBase.
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SourceReadError('统计记录包含无效数值')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal('1e15'):
            raise InvalidOperation
        return result
    except InvalidOperation:
        raise SourceReadError('统计记录包含无效数值') from None


def _json_number(value):
    return None if value is None else float(value.quantize(Decimal('.01')))


class BillingService:
    def __init__(self, client):
        self.client = client

    def fetch(self, station_id, month, *, now=None):
        start, end = month_bounds(month)
        station_code = STATION_CABINETS[station_id][0]
        now = (now or datetime.now(TZ)).astimezone(TZ)
        filters = {'$and': [{'es_sn': {'$eq': station_code}}, {
            'createdAt': {'$gte': start.isoformat(), '$lt': end.isoformat()}}]}
        sources = {}
        for name, table, fields in [('monthly', 't_es_count_stat', MONTH_FIELDS),
                                     ('daily', 't_es_stat', DAY_FIELDS)]:
            try:
                rows = self.client.list_rows(table,
                    fields=','.join(('id', 'es_sn', 'createdAt', 'updatedAt', *fields)),
                    filters=filters, sort='createdAt,id', page_size=100, max_pages=2)
                seen = set()
                normalized = []
                for row in rows:
                    created = _time(row.get('createdAt'))
                    updated = _time(row.get('updatedAt'))
                    day = created.date().isoformat()
                    period = month if name == 'monthly' else day
                    if row.get('es_sn') != station_code or not start <= created < end:
                        raise SourceReadError('统计记录与请求电站或月份不匹配')
                    if period in seen:
                        raise SourceReadError('同一统计周期存在重复记录，未重复计入')
                    seen.add(period)
                    values = {field: _number(row.get(field)) for field in fields}
                    if any(values[field] is not None and values[field] < 0 for field in fields if field.endswith(('charge', 'cost', 'costs'))):
                        raise SourceReadError('统计电量或成本不能为负数')
                    normalized.append({'date': day, 'updated_at': updated.isoformat(),
                                       **{field: _json_number(value) for field, value in values.items()}})
                normalized.sort(key=lambda row: row['date'])
                sources[name] = {'table': table, 'status': 'ready' if rows else 'empty',
                                 'rows': normalized, 'error': None}
            except SourceReadError as error:
                sources[name] = {'table': table, 'status': 'error', 'rows': [], 'error': str(error)}
        daily = sources['daily']['rows']
        monthly = next(iter(sources['monthly']['rows']), None)
        days_in_month = calendar.monthrange(start.year, start.month)[1]
        expected_days = days_in_month if now >= end else now.day if now >= start else 0
        recorded = {row['date'] for row in daily}
        missing = [(start + timedelta(days=index)).date().isoformat()
                   for index in range(expected_days)
                   if (start + timedelta(days=index)).date().isoformat() not in recorded]
        totals = {field: _json_number(sum((Decimal(str(row[field])) for row in daily), Decimal(0)))
                  if daily and all(row[field] is not None for row in daily) else None
                  for field in DAY_FIELDS}
        differences = {}
        for day_field, month_field in [('day_charge', 'month_charge'),
                                      ('day_discharge', 'month_discharge'), ('day_earnings', 'month_earnings')]:
            a, b = monthly.get(month_field) if monthly else None, totals[day_field]
            differences[month_field] = _json_number(Decimal(str(a)) - Decimal(str(b))) if a is not None and b is not None else None
        statuses = [source['status'] for source in sources.values()]
        issues = [source['error'] for source in sources.values() if source['error']]
        if missing:
            issues.append(f'每日统计缺少 {len(missing)} 天，日合计仅包含已返回记录')
        if any(row[field] is None for row in daily for field in DAY_FIELDS) or monthly and any(monthly[field] is None for field in MONTH_FIELDS):
            issues.append('部分统计字段缺失，显示为 —，不按零计算')
        if any(value is not None and abs(value) > .01 for value in differences.values()):
            issues.append('月统计与日合计存在差额，请结合各自更新时间核对')
        status = 'error' if statuses == ['error', 'error'] else 'empty' if statuses == ['empty', 'empty'] else 'partial' if 'error' in statuses or 'empty' in statuses or issues else 'ready'
        return {'schema_version': 'm4-billing-v1', 'station_id': station_id, 'station_code': station_code,
                'month': month, 'timezone': 'Asia/Shanghai', 'period_basis': 'createdAt',
                'period_status': 'ongoing' if start <= now < end else 'historical' if now >= end else 'future',
                'fetched_at': now.isoformat(), 'status': status, 'settlement_status': 'unverified',
                'monthly': monthly, 'daily': daily, 'daily_totals': totals,
                'coverage': {'recorded_days': len(daily), 'expected_days': expected_days, 'missing_dates': missing},
                'differences': differences, 'sources': {key: {k: v for k, v in source.items() if k != 'rows'} for key, source in sources.items()},
                'issues': issues}
