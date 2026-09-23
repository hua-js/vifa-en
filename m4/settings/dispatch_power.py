"""Minimum station-level advice power accepted for EMS schedule dispatch."""

from shared.project import get_project

MIN_DISPATCH_POWER_KW = get_project().m4['minimum_dispatch_power_kw']


def dispatch_points(points):
    """Filter already validated advice without changing the original evidence."""
    return [dict(point, mode='idle', target_power_kw=0)
            if point['target_power_kw'] <= MIN_DISPATCH_POWER_KW else dict(point)
            for point in points]
