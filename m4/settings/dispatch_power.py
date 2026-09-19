"""Minimum station-level advice power accepted for EMS schedule dispatch."""

MIN_DISPATCH_POWER_KW = 10.0


def dispatch_points(points):
    """Filter already validated advice without changing the original evidence."""
    return [dict(point, mode='idle', target_power_kw=0)
            if point['target_power_kw'] <= MIN_DISPATCH_POWER_KW else dict(point)
            for point in points]
