"""Read station controls under the confirmed policy; never issue device commands."""
import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .schedule_power import SCHEDULE_POWER_POLICY
from .roster import STATION_CABINETS


class ControlSourceError(ValueError):
    """A source is unavailable or cannot be interpreted safely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


_STATIONS = {'station-1': 'ES01', 'station-2': 'ES02'}
_TABLES = {
    't_es': ('电站容量', 'id,sn,es_power_storage,updatedAt'),
    't_need': ('需量控制', 'id,f_es_sn,need_kw,reserved_kw,rated_capacity,load_rate,updatedAt'),
    're_flow': ('防逆流', 'id,fk_es_sn,re_kw,updatedAt'),
    't_model': ('每日充放电计划', 'id,es_sn,start_time,end_time,type,kw,repeat,updatedAt'),
}
_TIME = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\Z')
_PAGE_SIZE = 100
_MAX_PAGES = 20
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CONTROL_POLICY_VERSION = SCHEDULE_POWER_POLICY


def _number(row, field, label):
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ControlSourceError(f'{label}的 {field} 必须是非负有限数值')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ControlSourceError(f'{label}的 {field} 必须是非负有限数值') from None
    if not math.isfinite(number) or number < 0:
        raise ControlSourceError(f'{label}的 {field} 必须是非负有限数值')
    return number


def _updated_at(row, label):
    value = row.get('updatedAt')
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlSourceError(f'{label}的更新时间格式无效')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ControlSourceError(f'{label}的更新时间须包含时区') from None
    return parsed.astimezone(timezone.utc).isoformat()


def _station_rows(rows, field, source_station, label, *, array=False):
    selected = []
    for row in rows:
        value = row.get(field)
        if array:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ControlSourceError(f'{label}的电站归属必须为数组')
            matches = source_station in value
        else:
            if not isinstance(value, str):
                raise ControlSourceError(f'{label}的电站归属格式无效')
            matches = value == source_station
        if matches:
            selected.append(row)
    return selected


def _single(rows, label):
    if not rows:
        raise ControlSourceError(f'本站尚无{label}记录')
    if len(rows) != 1:
        raise ControlSourceError(f'本站存在重复的{label}记录，无法确定生效项')
    return rows[0]


def _schedule(rows):
    result = []
    identifiers = set()
    for row in rows:
        identifier = row.get('id')
        if (isinstance(identifier, bool) or not isinstance(identifier, (str, int))
                or isinstance(identifier, str) and not identifier.strip()):
            raise ControlSourceError('每日充放电计划的记录标识无效')
        if str(identifier) in identifiers:
            raise ControlSourceError('本站存在重复的每日充放电计划记录')
        identifiers.add(str(identifier))
        start, end = row.get('start_time'), row.get('end_time')
        if not (isinstance(start, str) and _TIME.fullmatch(start)
                and isinstance(end, str) and _TIME.fullmatch(end)):
            raise ControlSourceError('每日充放电计划时间须为有效的 HH:MM:SS')
        mode = row.get('type')
        if mode not in ('charge', 'discharge'):
            raise ControlSourceError('每日充放电计划含未知充放电模式')
        if row.get('repeat') != '每天重复':
            raise ControlSourceError('仅支持已确认的“每天重复”充放电计划')
        result.append({
            'id': identifier, 'start_time': start, 'end_time': end,
            'mode': mode, 'power_kw': _number(row, 'kw', '每日充放电计划'),
            'repeat': 'daily', 'updated_at': _updated_at(row, '每日充放电计划'),
        })
    return sorted(result, key=lambda row: (row['start_time'], row['end_time'], str(row['id'])))


def _business_values(value):
    if isinstance(value, dict):
        return {key: _business_values(item) for key, item in value.items()
                if key not in ('updated_at', 'fetched_at', 'issues', 'warnings')}
    if isinstance(value, list):
        return [_business_values(item) for item in value]
    return value


class ControlSourceReader:
    def __init__(self, token: str, base_url='https://vifa.hlszh.com/api/'):
        if (not isinstance(token, str) or not token.strip()
                or '\r' in token or '\n' in token):
            raise ControlSourceError('尚未配置有效的数据源访问凭据')
        try:
            parts = urlsplit(base_url)
            if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.query or parts.fragment):
                raise ValueError
            self._origin = (parts.scheme, parts.hostname, parts.port or 443)
        except (TypeError, ValueError, AttributeError):
            raise ControlSourceError('数据源地址必须是无凭据、查询参数或片段的 HTTPS 地址') from None
        self._base_url = base_url.rstrip('/') + '/'
        self._token = token.strip()
        self._opener = build_opener(_NoRedirect())

    def _read_page(self, table, page):
        label, fields = _TABLES[table]
        url = self._base_url + table + ':list?' + urlencode({
            'fields': fields, 'pageSize': _PAGE_SIZE, 'page': page, 'sort': 'id',
        })
        try:
            parts = urlsplit(url)
            if (parts.scheme, parts.hostname, parts.port or 443) != self._origin:
                raise ValueError
            request = Request(url, headers={
                'Authorization': f'Bearer {self._token}', 'Accept': 'application/json',
            }, method='GET')
            with self._opener.open(request, timeout=8) as response:
                final = urlsplit(response.geturl())
                if ((final.scheme, final.hostname, final.port or 443) != self._origin
                        or getattr(response, 'status', 200) != 200):
                    raise ValueError
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError
                payload = json.loads(body)
        except Exception:
            # Never include upstream URLs, response bodies or credential-bearing exceptions.
            raise ControlSourceError(f'{label}接口读取失败，请检查连接与访问权限') from None
        if (not isinstance(payload, dict) or not isinstance(payload.get('data'), list)
                or any(not isinstance(row, dict) for row in payload['data'])):
            raise ControlSourceError(f'{label}接口返回格式无效')
        return payload

    def _read_table(self, table):
        rows = []
        label = _TABLES[table][0]
        for page in range(1, _MAX_PAGES + 1):
            payload = self._read_page(table, page)
            batch = payload['data']
            rows.extend(batch)
            meta = payload.get('meta', {})
            if not isinstance(meta, dict):
                raise ControlSourceError(f'{label}接口分页信息无效')
            has_next = meta.get('hasNext')
            total_pages = meta.get('totalPage')
            if 'page' in meta and (isinstance(meta['page'], bool) or meta['page'] != page):
                raise ControlSourceError(f'{label}接口分页未按请求推进')
            if 'hasNext' in meta and not isinstance(has_next, bool):
                raise ControlSourceError(f'{label}接口分页信息无效')
            if total_pages is not None and (isinstance(total_pages, bool)
                    or not isinstance(total_pages, int) or total_pages < 0):
                raise ControlSourceError(f'{label}接口分页信息无效')
            if total_pages == 0 and batch:
                raise ControlSourceError(f'{label}接口分页信息与记录不一致')
            if has_next is not None and total_pages is not None and has_next != (page < total_pages):
                raise ControlSourceError(f'{label}接口分页信息相互矛盾')
            if has_next is not None:
                more = has_next
            elif total_pages is not None:
                more = page < total_pages
            else:
                more = len(batch) == _PAGE_SIZE
            if not more:
                return rows
            if not batch:
                raise ControlSourceError(f'{label}接口分页中断，未取得完整数据')
        raise ControlSourceError(f'{label}接口记录超出读取上限，未取得完整数据')

    def fetch(self, station_id):
        if not isinstance(station_id, str) or station_id not in _STATIONS:
            raise ControlSourceError('未知电站')
        source_station = _STATIONS[station_id]
        warnings = []
        health = {'demand': 'ready', 'reverse_flow': 'ready', 'schedule': 'ready'}

        def reverse_config(rows):
            row = _single(_station_rows(rows, 'fk_es_sn', source_station, '防逆流'), '防逆流')
            return {'re_kw': _number(row, 're_kw', '防逆流'),
                    'enabled': False, 'effective_limit_kw': None,
                    'updated_at': _updated_at(row, '防逆流')}

        def daily_schedule(rows):
            return _schedule(_station_rows(rows, 'es_sn', source_station, '每日充放电计划', array=True))

        def optional_source(future, key, label, normalize, empty):
            try:
                return normalize(future.result())
            except ControlSourceError as error:
                message = str(error)
            except Exception:
                # Optional data cannot block valid demand input or expose an exception body.
                message = f'{label}数据无法读取或校验'
            health[key] = 'unavailable'
            warnings.append(f'{label}参考源不可用：{message}；本轮不使用该参考数据')
            return empty

        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {table: pool.submit(self._read_table, table) for table in _TABLES}
            demand = _single(_station_rows(pending['t_need'].result(), 'f_es_sn', source_station, '需量控制'), '需量控制')
            normalized_demand = {
                **{field: _number(demand, field, '需量控制')
                   for field in ('need_kw', 'reserved_kw', 'rated_capacity', 'load_rate')},
                'updated_at': _updated_at(demand, '需量控制'),
            }
            reverse = optional_source(pending['re_flow'], 'reverse_flow', '防逆流', reverse_config, None)
            schedule = optional_source(pending['t_model'], 'schedule', '每日充放电计划', daily_schedule, [])
            capacity_row = _single(_station_rows(pending['t_es'].result(), 'sn', source_station, '电站容量'), '电站容量')
            capacity_kwh = _number(capacity_row, 'es_power_storage', '电站容量')
            if capacity_kwh <= 0:
                raise ControlSourceError('电站容量 es_power_storage 必须大于0，不能用旧手填值替代')
            capacity = {'scope': 'station', 'source_station_id': source_station,
                        'source_table': 't_es', 'source_field': 'es_power_storage',
                        'energy_capacity_kwh': capacity_kwh, 'updated_at': _updated_at(capacity_row, '电站容量')}
        issues = ['防逆流控制当前未启用，限制功率仅供展示，不参与优化约束']
        if health['schedule'] == 'ready' and not schedule:
            issues.append('本站未配置每日充放电计划，不补充停机或其他动作')
        result = {
            'station_id': station_id, 'source_station_id': source_station,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'status': 'ready', 'power_scope': 'cabinet', 'storage_capacity': capacity,
            'configured_cabinet_count': len(STATION_CABINETS[station_id][1]),
            'control_policy_version': CONTROL_POLICY_VERSION,
            'demand': normalized_demand, 'reverse_flow': reverse,
            'schedule': schedule, 'source_health': health,
            'issues': issues, 'warnings': warnings,
        }
        digest = hashlib.sha256(json.dumps(_business_values(result), sort_keys=True,
                                          ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:16]
        result['version'] = f'{station_id}-controls-{digest}'
        return result
