"""Read-only metered energy and economic contributions from t_es_data.

Integrate adjacent observations, never forecasts or device commands.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import math
from statistics import median
from threading import Lock, Thread
from time import monotonic
from zoneinfo import ZoneInfo

from shared.project import get_project
from .billing import _time
from .timeseries import build_tariff_points
from .upstream import SourceReadError

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = 'timestamp,es_sn,grid_power,load_power,ac_solar_power,emus_power'
RESULT_FRESH_SECONDS = 5
RESULT_MAX_AGE_SECONDS = 120


def _number(value):
    if value is None or isinstance(value, bool) or isinstance(value, str) and not value.strip():
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and abs(result) < 1e9 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _amount(value):
    return round(value, 2) if value is not None and math.isfinite(value) else None


def _cost(power, buy, sell):
    return max(power, 0) * buy - max(-power, 0) * sell


def _exclude(intervals, start, end, reasons, cards):
    """Keep affected money windows for diagnosis without changing source data."""
    entry = {'start_at': start.isoformat(), 'end_at': end.isoformat(),
             'reasons': reasons, 'affected_cards': cards}
    if (intervals and intervals[-1]['end_at'] == entry['start_at']
            and intervals[-1]['reasons'] == reasons
            and intervals[-1]['affected_cards'] == cards):
        intervals[-1]['end_at'] = entry['end_at']
    else:
        intervals.append(entry)


class TodayRevenueService:
    def __init__(self, client):
        self.client = client
        self._locks = {station.id: Lock() for station in get_project().stations}
        self._samples = {}
        self._tariffs = {}
        self._result_locks = {station.id: Lock() for station in get_project().stations}
        self._result_guard = Lock()
        self._results = {}
        self._attempts = {}

    def _cached_result(self, station_id, now):
        with self._result_guard:
            cached = self._results.get(station_id)
            if not cached:
                return None
            fetched = datetime.fromisoformat(cached['data']['fetched_at'])
            age = max(monotonic() - cached['at'], (now - fetched).total_seconds())
            if fetched > now or fetched.date() != now.date() or age > RESULT_MAX_AGE_SECONDS:
                return None
            result = deepcopy(cached['data'])
            attempt = self._attempts.get(station_id)
            result['cache_age_seconds'] = round(age, 3)
            result['refresh_status'] = ('refreshing' if self._result_locks[station_id].locked()
                else 'error' if attempt and not attempt['ok'] else 'ready')
            return result

    def _refresh_result(self, station_id, now):
        try:
            result = self._calculate(station_id, now=now)
            ok = (result['sources']['measurements']['status'] in ('ready', 'empty')
                  and result['sources']['tariff']['status'] == 'ready')
        except Exception:
            with self._result_guard:
                self._attempts[station_id] = {'at': monotonic(), 'ok': False}
            raise
        with self._result_guard:
            self._attempts[station_id] = {'at': monotonic(), 'ok': ok,
                                         'data': deepcopy(result) if not ok else None}
            if ok:
                self._results[station_id] = {'at': monotonic(), 'data': deepcopy(result)}
        return {**result, 'refresh_status': 'ready' if ok else 'error', 'cache_age_seconds': 0}

    def _recent_failure(self, station_id, now):
        # Share failed cold reads too, without replacing the successful cache.
        with self._result_guard:
            attempt = self._attempts.get(station_id)
            if not attempt or attempt['ok'] or not attempt.get('data'):
                return None
            age = monotonic() - attempt['at']
            fetched = datetime.fromisoformat(attempt['data']['fetched_at'])
            if age >= RESULT_FRESH_SECONDS or fetched > now or fetched.date() != now.date():
                return None
            return {**deepcopy(attempt['data']), 'refresh_status': 'error',
                    'cache_age_seconds': round(age, 3)}

    def _refresh_in_background(self, station_id, now):
        try:
            self._refresh_result(station_id, now)
        except Exception:
            # The last successful result retains its original timestamps.
            pass
        finally:
            self._result_locks[station_id].release()

    def fetch(self, station_id, *, now=None):
        get_project().station(station_id)
        explicit_now = now is not None
        now = (now or datetime.now(TZ)).astimezone(TZ)
        cached = self._cached_result(station_id, now)
        lock = self._result_locks[station_id]
        if cached:
            with self._result_guard:
                attempt = self._attempts.get(station_id)
                retry_due = not attempt or monotonic() - attempt['at'] >= RESULT_FRESH_SECONDS
            if cached['cache_age_seconds'] >= RESULT_FRESH_SECONDS and retry_due and lock.acquire(blocking=False):
                try:
                    Thread(target=self._refresh_in_background, args=(station_id, now), daemon=True).start()
                except Exception:
                    lock.release()
                    raise
            return self._cached_result(station_id, now) or cached
        failed = self._recent_failure(station_id, now)
        if failed:
            return failed
        # A cold start has no usable result. Concurrent requests share its read.
        with lock:
            if not explicit_now:
                now = datetime.now(TZ)
            cached = self._cached_result(station_id, now)
            if cached:
                with self._result_guard:
                    cached['refresh_status'] = 'error' if not self._attempts[station_id]['ok'] else 'ready'
                return cached
            failed = self._recent_failure(station_id, now)
            if failed:
                return failed
            return self._refresh_result(station_id, now)

    def _read(self, station, now):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._locks[station.id]:
            cached = self._samples.get(station.id)
            full = (not cached or cached['date'] != day.date().isoformat()
                or cached['latest'] > now
                or monotonic() - cached['full_at'] >= 300)
            start = day if full else max(day, cached['latest'] - timedelta(minutes=2))
            rows = self.client.list_rows('t_es_data', fields=FIELDS,
                filters={'$and': [{'es_sn': {'$eq': station.source_code}},
                    {'timestamp': {'$gte': start.isoformat(), '$lte': now.isoformat()}}]},
                sort='timestamp', page_size=2000, max_pages=20)
            points = {} if full else {stamp: value for stamp, value in cached['points'].items() if stamp < start}
            seen = set()
            for row in rows:
                stamp = _time(row.get('timestamp'))
                if (row.get('es_sn') != station.source_code or not start <= stamp <= now
                        or stamp in seen):
                    raise SourceReadError('实测数据的电站、时间或唯一性不匹配')
                seen.add(stamp)
                points[stamp] = {'at': stamp, 'grid': _number(row.get('grid_power')),
                    'load': _number(row.get('load_power')),
                    'pv': _number(row.get('ac_solar_power')) if station.has_pv else 0.0,
                    'storage': _number(row.get('emus_power'))}
            if len(points) > 10000:
                raise SourceReadError('当日实测记录超出读取上限')
            ordered = sorted(points.values(), key=lambda row: row['at'])
            self._samples[station.id] = {'date': day.date().isoformat(), 'points': points,
                'latest': ordered[-1]['at'] if ordered else day,
                'full_at': monotonic() if full else cached['full_at']}
            return ordered

    def _tariff(self, station_id, day):
        cached = self._tariffs.get(station_id)
        if cached and cached['date'] == day.date().isoformat() and monotonic() - cached['at'] < 300:
            return cached['value']
        with ThreadPoolExecutor(max_workers=2) as pool:
            periods = pool.submit(self.client.list_rows, 't_peak_diy',
                fields='id,start_time,end_time,period_type,updatedAt', sort='start_time', page_size=100)
            rates = pool.submit(self.client.list_rows, 't_rate',
                fields='id,hprice,fprice,vprice,jprice,updatedAt', sort='id', page_size=100)
            tariff = build_tariff_points(periods.result(), rates.result(), plan_start_at=day)
        self._tariffs[station_id] = {'date': day.date().isoformat(), 'at': monotonic(), 'value': tariff}
        return tariff

    @staticmethod
    def _intervals(points):
        durations = [(b['at'] - a['at']).total_seconds() for a, b in zip(points, points[1:])]
        # Observed cadence: 30 s. Sparse records must not conceal long outages.
        cadence = min(median(durations), 60) if durations else 30
        for a, b in zip(points, points[1:]):
            seconds = (b['at'] - a['at']).total_seconds()
            yield a, b, seconds, 0 < seconds <= cadence

    def _calculate(self, station_id, *, now=None):
        station = get_project().station(station_id)
        now = (now or datetime.now(TZ)).astimezone(TZ)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        points, tariff = [], None
        source_status = tariff_status = 'ready'
        with ThreadPoolExecutor(max_workers=2) as pool:
            reads = {'points': pool.submit(self._read, station, now),
                     'tariff': pool.submit(self._tariff, station_id, day)}
            try:
                points = reads['points'].result()
                if not points:
                    source_status = 'empty'
            except Exception:
                source_status = 'error'
            try:
                tariff = reads['tariff'].result()
            except Exception:
                tariff_status = 'error'
        totals = dict.fromkeys(('grid_cost', 'storage_revenue', 'pv_revenue',
            'import', 'export', 'charge', 'discharge', 'generation', 'self_use'), 0.0)
        valid = dict.fromkeys(totals, len(points) >= 2)
        complete = bool(points) and points[0]['at'] == day
        covered = 0.0
        amount_seconds = dict.fromkeys(('grid', 'storage', 'pv'), 0.0)
        excluded = []
        if points and points[0]['at'] > day:
            _exclude(excluded, day, points[0]['at'], ['missing_day_start'],
                     ['grid', 'storage', 'pv', 'total'])
        for a, b, seconds, adjacent in self._intervals(points):
            if not adjacent:
                complete = False
                _exclude(excluded, a['at'], b['at'], ['sampling_gap'],
                         ['grid', 'storage', 'pv', 'total'])
                continue
            covered += seconds
            # A trapezoid requires two valid endpoints. Never retain half of an
            # invalid interval, or interpolate money across an invalid sample.
            grid_reasons = []
            if not tariff:
                grid_reasons.append('tariff_unavailable')
            if any(row['grid'] is None for row in (a, b)):
                grid_reasons.append('missing_grid')
            contribution_reasons = list(grid_reasons)
            for key in ('load', 'pv', 'storage'):
                if any(row[key] is None for row in (a, b)):
                    contribution_reasons.append('missing_' + key)
            if any(row['load'] is not None and row['load'] < 0 for row in (a, b)):
                contribution_reasons.append('negative_load')
            if any(row['pv'] is not None and row['pv'] < 0 for row in (a, b)):
                contribution_reasons.append('negative_pv')
            if grid_reasons:
                _exclude(excluded, a['at'], b['at'], grid_reasons, ['grid'])
            else:
                amount_seconds['grid'] += seconds
            if contribution_reasons:
                _exclude(excluded, a['at'], b['at'], contribution_reasons, ['storage', 'pv', 'total'])
            else:
                amount_seconds['storage'] += seconds
                amount_seconds['pv'] += seconds
            cuts = {0.0, 1.0}
            boundary = a['at'].replace(minute=a['at'].minute // 15 * 15, second=0, microsecond=0) + timedelta(minutes=15)
            while boundary < b['at']:
                cuts.add((boundary - a['at']).total_seconds() / seconds)
                boundary += timedelta(minutes=15)
            channels = [(a[key], b[key]) for key in ('grid', 'load', 'storage')]
            if all(row[key] is not None for row in (a, b) for key in ('load', 'pv')):
                channels.append((a['load'] - a['pv'], b['load'] - b['pv']))
            if all(row[key] is not None for row in (a, b) for key in ('pv', 'grid')):
                channels.append((a['pv'] + a['grid'], b['pv'] + b['grid']))
            for left, right in channels:
                if left is not None and right is not None and left * right < 0:
                    cuts.add(-left / (right - left))
            cuts = sorted(cuts)
            for left, right in zip(cuts, cuts[1:]):
                hours = seconds * (right - left) / 3600
                at = a['at'] + timedelta(seconds=seconds * (left + right) / 2)
                buy = tariff['values'][at.hour * 4 + at.minute // 15] if tariff else None
                sell = tariff['sell_price_per_kwh'] if tariff else None
                values = []
                for fraction in (left, right):
                    values.append({key: a[key] + (b[key] - a[key]) * fraction
                        if a[key] is not None and b[key] is not None else None
                        for key in ('grid', 'load', 'pv', 'storage')})
                for value in values:
                    weight = hours / 2
                    grid, load, pv, storage = (value[key] for key in ('grid', 'load', 'pv', 'storage'))
                    if grid is not None:
                        totals['import'] += max(grid, 0) * weight
                        totals['export'] += max(-grid, 0) * weight
                        if not grid_reasons:
                            totals['grid_cost'] += _cost(grid, buy, sell) * weight
                    else:
                        valid['grid_cost'] = valid['import'] = valid['export'] = False
                    if storage is not None:
                        totals['charge'] += max(-storage, 0) * weight
                        totals['discharge'] += max(storage, 0) * weight
                    else:
                        valid['charge'] = valid['discharge'] = False
                    if pv is not None and pv >= 0:
                        totals['generation'] += pv * weight
                        if grid is not None:
                            totals['self_use'] += (pv - min(pv, max(-grid, 0))) * weight
                        else:
                            valid['self_use'] = False
                    else:
                        valid['generation'] = valid['self_use'] = False
                    if not contribution_reasons:
                        without_assets = _cost(load, buy, sell)
                        without_storage = _cost(load - pv, buy, sell)
                        actual = _cost(grid, buy, sell)
                        totals['pv_revenue'] += (without_assets - without_storage) * weight
                        totals['storage_revenue'] += (without_storage - actual) * weight
        updated = points[-1]['at'].isoformat() if points else None
        if not covered:
            valid = dict.fromkeys(valid, False)
        cards = {
            'grid': {'amount_yuan': totals['grid_cost'], 'import_kwh': totals['import'], 'export_kwh': totals['export']},
            'storage': {'amount_yuan': totals['storage_revenue'], 'charge_kwh': totals['charge'], 'discharge_kwh': totals['discharge']},
            'pv': {'amount_yuan': totals['pv_revenue'], 'generation_kwh': totals['generation'], 'self_use_kwh': totals['self_use']},
        }
        metrics = {
            'grid': ('grid_cost', 'import', 'export'),
            'storage': ('storage_revenue', 'charge', 'discharge'),
            'pv': ('pv_revenue', 'generation', 'self_use'),
        }
        observed_seconds = (points[-1]['at'] - day).total_seconds() if points else 0.0
        for name, card in cards.items():
            for (key, value), metric in zip(list(card.items()), metrics[name]):
                usable = amount_seconds[name] > 0 if key == 'amount_yuan' else valid[metric]
                card[key] = _amount(value) if usable else None
            card['amount_status'] = ('unavailable' if card['amount_yuan'] is None else
                'ready' if complete and amount_seconds[name] == observed_seconds else 'estimated')
            card['amount_coverage'] = {'included_seconds': amount_seconds[name],
                'excluded_seconds': max(0.0, observed_seconds - amount_seconds[name])}
            card['status'] = ('ready' if card['amount_status'] == 'ready'
                and all(valid[metric] for metric in metrics[name][1:]) else 'partial')
            card['updated_at'] = updated
        storage, pv = cards['storage']['amount_yuan'], cards['pv']['amount_yuan']
        total_status = ('unavailable' if storage is None or pv is None else
            'ready' if cards['storage']['amount_status'] == cards['pv']['amount_status'] == 'ready' else 'estimated')
        cards['total'] = {'amount_yuan': _amount(storage + pv) if storage is not None and pv is not None else None,
            'amount_status': total_status, 'amount_coverage': dict(cards['storage']['amount_coverage']),
            'status': 'ready' if cards['storage']['status'] == cards['pv']['status'] == 'ready' else 'partial',
            'updated_at': updated}
        return {'schema_version': 'm4-today-revenue-v1', 'station_id': station_id,
            'date': day.date().isoformat(), 'timezone': 'Asia/Shanghai',
            'fetched_at': now.isoformat(), 'settlement_status': 'unverified', 'cards': cards,
            'coverage': {'complete': complete, 'covered_seconds': covered, 'samples': len(points),
                'excluded_intervals': excluded},
            'sources': {'measurements': {'table': 't_es_data', 'status': source_status, 'updated_at': updated},
                'tariff': {'status': tariff_status}}}

    def storage_log(self, station_id, *, now=None):
        station = get_project().station(station_id)
        now = (now or datetime.now(TZ)).astimezone(TZ)
        points = self._read(station, now)
        segments, active = [], None
        for a, b, seconds, adjacent in self._intervals(points):
            left, right = a['storage'], b['storage']
            if not adjacent or left is None or right is None:
                active = None
                continue
            cuts = [0.0, -left / (right - left), 1.0] if left * right < 0 else [0.0, 1.0]
            for start, end in zip(cuts, cuts[1:]):
                mean = left + (right - left) * (start + end) / 2
                if mean == 0:
                    active = None
                    continue
                mode = 'discharge' if mean > 0 else 'charge'
                begin = a['at'] + timedelta(seconds=seconds * start)
                finish = a['at'] + timedelta(seconds=seconds * end)
                duration = seconds * (end - start)
                if active and active['mode'] == mode and active['end'] == begin:
                    active['end'] = finish
                    active['weighted_power'] += abs(mean) * duration
                    active['seconds'] += duration
                else:
                    active = {'mode': mode, 'start': begin, 'end': finish,
                        'weighted_power': abs(mean) * duration, 'seconds': duration}
                    segments.append(active)
        return {'schema_version': 'm4-storage-log-v1', 'station_id': station_id,
            'date': now.date().isoformat(), 'timezone': 'Asia/Shanghai', 'fetched_at': now.isoformat(),
            'observed_until': points[-1]['at'].isoformat() if points else None,
            'status': 'ready' if points else 'empty',
            'items': [{'start_at': row['start'].isoformat(), 'end_at': row['end'].isoformat(),
                'mode': row['mode'], 'power_kw': round(row['weighted_power'] / row['seconds'], 3)}
                for row in reversed(segments)]}
