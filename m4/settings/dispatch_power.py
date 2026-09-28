"""Minimum station-level advice power accepted for EMS schedule dispatch."""

from shared.project import get_project

MIN_DISPATCH_POWER_KW = get_project().m4['minimum_dispatch_power_kw']


def daily_dispatch_versions(station_id):
    """Opt in only VIFA station 2 daily solves, not night fallback or history."""
    from m4.optimizer.dispatch_policy import POLICY
    if get_project().id == 'vifa' and station_id == 'station-2':
        return dict(dispatch_power_policy=POLICY,
                    minimum_dispatch_power_kw=format(MIN_DISPATCH_POWER_KW, '.17g'))
    return {}


def dispatch_points(points):
    """Filter already validated advice without changing the original evidence."""
    return [dict(point, mode='idle', target_power_kw=0)
            if point['target_power_kw'] <= MIN_DISPATCH_POWER_KW else dict(point)
            for point in points]
