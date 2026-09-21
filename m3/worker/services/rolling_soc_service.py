"""Quarter-hour SOC nowcasts, isolated from persisted day-ahead runs."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from math import isfinite
from statistics import median
from threading import Lock

import pandas as pd

from m3.worker.domain.production_schedule import use_schedule
from m3.worker.domain.soc_power import calibrated_power_deltas
from m3.worker.domain.work_schedule import schedule_day, schedule_slot
from m3.worker.errors import M3Error
from m3.worker.contracts import validate_shanghai_timestamp


POLICY = 'soc-rolling-power-v1'
INTERVAL = timedelta(minutes=15)


def _historical_delta(calibrated, origin, interval_start):
    """Predict energy for [start,start+15m), not the following power bucket."""
    kind = lambda t: 0 if t.weekday() < 5 else t.weekday()
    values = []
    for days in range(1, 8):
        source = interval_start - timedelta(days=days)
        if (source not in calibrated or not origin - timedelta(days=7) <= source < origin
                or kind(source) != kind(interval_start)
                or schedule_day(source) != schedule_day(interval_start)
                or schedule_slot(source)[0] != schedule_slot(interval_start)[0]
                or schedule_slot(source - INTERVAL)[0] != schedule_slot(interval_start - INTERVAL)[0]):
            continue
        values.append(calibrated[source])
    if not values:
        raise M3Error('insufficient_history', 'Recent rolling power profile unavailable')
    return median(values)


def rolling_soc(points, capacity, origin):
    """Use completed buckets only; never treat a missing current SOC as zero."""
    real = {p.ds: p for p in points if p.unique_id == 'storage_soc'
            and p.quality == 'valid' and origin - timedelta(days=8) <= p.ds < origin}
    anchor = real.get(origin - INTERVAL)
    if anchor is None:
        raise M3Error('source_stale', 'Latest completed SOC bucket unavailable')
    frame = pd.DataFrame([{'unique_id': 'storage_soc', 'ds': t, 'y': p.y,
                          'storage_power_kw': p.storage_power_kw}
                         for t, p in sorted(real.items())])
    end = origin.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    periods = int((end - origin) / INTERVAL) + 1
    calibrated = calibrated_power_deltas(
        frame, {t: p.y for t, p in real.items()}, interval_seconds=900,
        capacity_kwh=capacity)
    recent_times = [origin - 2 * INTERVAL, origin - INTERVAL]
    recent = [real.get(t) for t in recent_times]
    stable = False
    current_delta = 0.
    if all(p is not None and p.storage_power_kw is not None and t in calibrated
           for t, p in zip(recent_times, recent)):
        powers = [p.storage_power_kw for p in recent]
        idle_band = capacity * 0.002
        idle = all(abs(p) <= idle_band for p in powers)
        stable = idle or (powers[0] * powers[1] > 0
                         and max(powers) - min(powers) <= 0.2 * max(abs(p) for p in powers))
        if stable:
            current_delta = 0. if idle else median(calibrated[t] for t in recent_times)
    values = [anchor.y]
    for index in range(1, periods):
        delta = _historical_delta(calibrated, origin, origin + (index - 1) * INTERVAL)
        # The observed change is evidence for the near term, not a full-day
        # dispatch plan. Fade back to the historical profile over one hour.
        if stable and index <= 4:
            weight = (5 - index) / 4
            delta = weight * current_delta + (1 - weight) * delta
        values.append(min(99., max(2., values[-1] + delta)))
    return {
        'policy': POLICY, 'origin': origin.isoformat(), 'forecast_end': end.isoformat(),
        'interval_seconds': 900, 'energy_capacity_kwh': capacity,
        'anchor_bucket_start': anchor.ds.isoformat(), 'anchor_soc': anchor.y,
        'recent_power_correction': stable, 'correction_horizon_minutes': 60 if stable else 0,
        'points': [{'target_time': (origin + i * INTERVAL).isoformat(), 'forecast_value': value}
                   for i, value in enumerate(values)],
    }


class RollingSocService:
    def __init__(self, source, station_ids, schedule_provider=None):
        self._source = source
        self._stations = frozenset(station_ids)
        self._schedule = schedule_provider
        self._history = {}
        self._ends = {}
        self._snapshots = {}
        self._lock = Lock()

    def update(self, station_id, at):
        if station_id not in self._stations:
            raise M3Error('station_not_configured', 'Station is not configured')
        validate_shanghai_timestamp(at, 'rolling time', quarter_hour=False)
        origin = at.replace(minute=at.minute // 15 * 15, second=0, microsecond=0)
        try:
            start = origin - timedelta(days=8)
            # One process-owned bounded cache per station; revisit the last two
            # hours for late source corrections instead of refetching eight days.
            cursor = min(origin, max(start, self._ends.get(station_id, start) - timedelta(hours=2)))
            cached = {t: p for t, p in self._history.get(station_id, {}).items() if start <= t < cursor}
            while cursor < origin:
                end = min(origin, cursor + timedelta(days=7))
                points = self._source.list_custom_observations(
                    station_id, cursor, end, interval_seconds=900)
                for p in points:
                    if p.unique_id == 'storage_soc':
                        cached[p.ds] = p
                cursor = end
            self._history[station_id] = cached
            self._ends[station_id] = origin
            capacity = self._source.storage_capacity(station_id)
            if type(capacity) not in {int, float} or not isfinite(capacity) or capacity <= 0:
                raise M3Error('source_contract_invalid', 'Invalid storage capacity')
            calendar = self._schedule.load(
                station_id, start.date(), origin.date(), forecast_start=origin.date()
            ) if self._schedule else None
            with use_schedule(calendar) if calendar is not None else nullcontext():
                result = rolling_soc(list(cached.values()), capacity, origin)
            result.update(station_id=station_id, status='ready', generated_at=at.isoformat())
        except Exception as error:
            with self._lock:
                self._snapshots[station_id] = {'station_id': station_id, 'status': 'unavailable',
                    'generated_at': at.isoformat(), 'points': [],
                    'error_code': error.code if isinstance(error, M3Error) else 'internal_error'}
            raise
        with self._lock:
            self._snapshots[station_id] = result

    def snapshot(self, station_id, now):
        if station_id not in self._stations:
            raise M3Error('station_not_configured', 'Station is not configured')
        with self._lock:
            result = deepcopy(self._snapshots.get(station_id, {
                'station_id': station_id, 'status': 'pending', 'points': []}))
        if result['status'] == 'ready':
            from datetime import datetime
            age = now - datetime.fromisoformat(result['origin'])
            if age < timedelta(0) or age > timedelta(minutes=30):
                result.update(status='stale', points=[])
        return result
