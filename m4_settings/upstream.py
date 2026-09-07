"""Bounded, read-only NocoBase access to M4's known upstream data tables."""
import json
import re
from time import monotonic
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .control_sources import _NoRedirect


class SourceReadError(ValueError):
    """An upstream source is unavailable, incomplete or invalid."""


_BASE_URL = 'https://vifa.hlszh.com/api/'
_ORIGIN = ('https', 'vifa.hlszh.com', 443)
_TABLES = {
    't_emu': '储能设备实时数据',
    't_es_data': '电站历史与实时数据',
    't_peak_diy': '峰谷时段',
    't_rate': '电价',
    'energy_forecast_manual_runs': '预测任务',
    'energy_forecast_manual_points': '预测时序',
}
_FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z')
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOTAL_READ_SECONDS = 30.0


def _positive_integer(value, maximum=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value > 0
            and (maximum is None or value <= maximum))


def _field_list(value, *, sort=False):
    if not isinstance(value, str) or not value.strip():
        return False
    for item in value.split(','):
        item = item.strip()
        if sort and item.startswith('-'):
            item = item[1:]
        if not _FIELD.fullmatch(item):
            return False
    return True


def _has_more(meta, *, page, batch_size, loaded, requested_size, label):
    if not isinstance(meta, dict):
        raise SourceReadError(f'{label}接口分页信息无效')
    if 'page' in meta and (not _positive_integer(meta['page']) or meta['page'] != page):
        raise SourceReadError(f'{label}接口分页未按请求推进')
    effective_size = meta.get('pageSize', requested_size)
    if not _positive_integer(effective_size):
        raise SourceReadError(f'{label}接口分页大小无效')
    signals = []
    if 'hasNext' in meta:
        if not isinstance(meta['hasNext'], bool):
            raise SourceReadError(f'{label}接口分页信息无效')
        signals.append(meta['hasNext'])
    total_pages = meta.get('totalPage')
    count = meta.get('count')
    for number in (total_pages, count):
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 0):
            raise SourceReadError(f'{label}接口分页信息无效')
    if total_pages is not None:
        if total_pages == 0 and loaded or total_pages > 0 and page > total_pages:
            raise SourceReadError(f'{label}接口分页信息与记录不一致')
        signals.append(page < total_pages)
    if count is not None:
        if loaded > count:
            raise SourceReadError(f'{label}接口记录数与分页信息不一致')
        signals.append(loaded < count)
    if signals and any(signal != signals[0] for signal in signals):
        raise SourceReadError(f'{label}接口分页信息相互矛盾，未取得完整数据')
    more = signals[0] if signals else batch_size >= effective_size
    if more and batch_size == 0:
        raise SourceReadError(f'{label}接口分页中断，未取得完整数据')
    return more


class NocoBaseClient:
    def __init__(self, token: str):
        # Missing credentials must not prevent the local parameter service starting.
        self._token = token
        self._opener = build_opener(_NoRedirect())

    def _page(self, table, query, *, timeout):
        label = _TABLES[table]
        url = _BASE_URL + table + ':list?' + urlencode(query)
        try:
            parts = urlsplit(url)
            if (parts.scheme, parts.hostname, parts.port or 443) != _ORIGIN:
                raise ValueError
            request = Request(url, headers={
                'Authorization': f'Bearer {self._token.strip()}',
                'Accept': 'application/json',
            }, method='GET')
            with self._opener.open(request, timeout=timeout) as response:
                final = urlsplit(response.geturl())
                if ((final.scheme, final.hostname, final.port or 443) != _ORIGIN
                        or getattr(response, 'status', 200) != 200):
                    raise ValueError
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError
                payload = json.loads(body)
        except Exception:
            # Never propagate URLs, response bodies, header values or exception details.
            raise SourceReadError(f'{label}接口读取失败，请检查连接与访问权限') from None
        if (not isinstance(payload, dict) or not isinstance(payload.get('data'), list)
                or any(not isinstance(row, dict) for row in payload['data'])):
            raise SourceReadError(f'{label}接口返回格式无效')
        return payload

    def list_rows(self, table, *, fields: str, filters: dict | None = None,
                  sort: str | None = None, page_size: int = 100,
                  max_pages: int = 100, limit: int | None = None) -> list[dict]:
        if not isinstance(table, str) or table not in _TABLES:
            raise SourceReadError('不支持的数据表')
        label = _TABLES[table]
        if not _field_list(fields) or sort is not None and not _field_list(sort, sort=True):
            raise SourceReadError(f'{label}的字段或排序配置无效')
        if (not _positive_integer(page_size, 2000) or not _positive_integer(max_pages, 100)
                or limit is not None and not _positive_integer(limit)):
            raise SourceReadError(f'{label}的分页或读取数量配置无效')
        if filters is not None and not isinstance(filters, dict):
            raise SourceReadError(f'{label}的筛选配置无效')
        if (not isinstance(self._token, str) or not self._token.strip()
                or '\r' in self._token or '\n' in self._token):
            raise SourceReadError('尚未配置有效的数据源访问凭据')
        size = min(page_size, limit) if limit is not None else page_size
        query = {'fields': fields, 'pageSize': size}
        if sort is not None:
            query['sort'] = sort
        if filters is not None:
            try:
                query['filter'] = json.dumps(filters, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            except (TypeError, ValueError, OverflowError):
                raise SourceReadError(f'{label}的筛选配置无效') from None
        deadline = monotonic() + _TOTAL_READ_SECONDS
        rows = []
        for page in range(1, max_pages + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SourceReadError(f'{label}读取超过总时限，未取得完整数据')
            payload = self._page(table, {**query, 'page': page}, timeout=min(8, remaining))
            if monotonic() > deadline:
                raise SourceReadError(f'{label}读取超过总时限，未取得完整数据')
            batch = payload['data']
            rows.extend(batch)
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if not _has_more(payload.get('meta', {}), page=page, batch_size=len(batch),
                             loaded=len(rows), requested_size=size, label=label):
                return rows
        raise SourceReadError(f'{label}记录超出读取页数上限，未取得完整数据')
