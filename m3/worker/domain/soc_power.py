"""Calibrate AC storage power against real SOC changes (positive = discharge)."""

from datetime import timedelta
from math import isfinite
from statistics import median

from m3.worker.errors import M3Error


def calibrated_power_deltas(frame, source_values, *, interval_seconds, capacity_kwh):
    """Use only already-filtered real history, including at each CV origin.

    SOC is the last reading of each bucket. Power is the average in that same
    bucket, so power[t] explains SOC[t] - SOC[t-interval]. No filling power gaps.
    Current nominal capacity is a snapshot, not historical capacity evidence.
    """
    if (type(capacity_kwh) not in {int, float} or not isfinite(capacity_kwh)
            or capacity_kwh <= 0 or 'storage_power_kw' not in frame):
        raise M3Error('insufficient_history', 'SOC power inputs unavailable')
    power = {}
    for row in frame.itertuples(index=False):
        if row.ds not in source_values:
            continue
        value = row.storage_power_kw
        if isinstance(value, (int, float)) and isfinite(value):
            power[row.ds] = value
    efficiencies = {'charge': [], 'discharge': []}
    interval = timedelta(seconds=interval_seconds)
    for timestamp, kw in power.items():
        previous = source_values.get(timestamp - interval)
        current = source_values[timestamp]
        if previous is None or not (2 < previous < 99 and 2 < current < 99):
            continue  # SOC clipping/plateaus cannot establish conversion efficiency.
        delta = current - previous
        energy = abs(kw) * interval_seconds / 3600
        soc_energy = abs(delta) * capacity_kwh / 100
        if energy <= 0 or abs(delta) < 0.1 or kw * delta >= 0:
            continue
        direction = 'charge' if kw < 0 else 'discharge'
        efficiency = soc_energy / energy if kw < 0 else energy / soc_energy
        # Reject mismatched units/signs/capacity instead of guessing efficiency.
        if 0.5 <= efficiency <= 1.05:
            efficiencies[direction].append(min(efficiency, 1.0))
    factors = {direction: median(values) for direction, values in efficiencies.items()
               if len(values) >= 8}
    deltas = {}
    for timestamp, kw in power.items():
        if kw == 0:
            deltas[timestamp] = 0.0
            continue
        direction = 'charge' if kw < 0 else 'discharge'
        if direction not in factors:
            continue
        eta = factors[direction]
        battery_kw = -kw * eta if kw < 0 else -kw / eta
        deltas[timestamp] = battery_kw * interval_seconds / 3600 / capacity_kwh * 100
    if not factors:
        raise M3Error('insufficient_history', 'SOC power calibration unavailable')
    return deltas
