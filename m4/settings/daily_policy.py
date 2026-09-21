"""Versioned daily planning rules; never inferred when replaying old requests."""
from shared.project import get_project
from m4.optimizer.contracts import GRID_CHARGING_POLICY
import math

PEAK_RESERVE_DAILY_POLICY = 'm4-daily-operating-floor-v1'
PEAK_RESERVE_SELECTOR = 'daily-operating-floor-cost-gate-v1'


def physical_grid_policy(station_id):
    # Preserve legacy labels for archived VIFA rules; other limits are explicit.
    limit = get_project().station(station_id).grid_import_limit_kw
    return 'station-1-550-v1' if limit == 550.0 else 'configured-grid-import-v1'


def daily_policy_version(station_id):
    return PEAK_RESERVE_DAILY_POLICY if get_project().station(station_id).policy == 'peak_reserve' else None


def matches_current_daily_policy(station_id, request):
    """Reject stale daily plans before either memory or disk results are adopted."""
    if not isinstance(request, dict):
        return False
    from .timeseries import PV_EXPORT_POLICY
    points = request.get('points')
    versions = request.get('source_versions')
    if not isinstance(versions, dict) or versions.get('pv_export_policy') != PV_EXPORT_POLICY:
        return False
    if versions.get('grid_charging_policy') != GRID_CHARGING_POLICY:
        return False
    if (request.get('pv_midday_economic') is not True
            or not isinstance(points, list) or len(points) != 96
            or any(not isinstance(point, dict)
                   or point.get('tariff_period') not in ('gu', 'ping', 'feng', 'jian')
                   or type(point.get('sell_price_per_kwh')) not in (int, float)
                   or not math.isfinite(point['sell_price_per_kwh'])
                   or point['sell_price_per_kwh'] < 0
                   or format(point['sell_price_per_kwh'], '.17g') != versions.get('pv_export_price')
                   for point in points)):
        return False
    expected = daily_policy_version(station_id)
    if (versions.get('daily_policy') != expected
            or versions.get('project_configuration') != get_project().fingerprint):
        return False
    # Bounded debug plans remain readable as history, but are no longer current.
    if request.get('ems_schedule_modes') is not None or 'ems_schedule_policy' in versions:
        return False
    from .objectives import get_daily_profiles
    policy = request.get('peak_reserve_policy')
    if expected is None:
        return (policy is None and versions.get('physical_grid_policy') == physical_grid_policy(station_id)
                and request.get('constraints', {}).get('grid_import_limit_kw') == get_project().station(station_id).grid_import_limit_kw
                and versions.get('economic_policy') == 'station-1-cost-first-v1'
                and request.get('profiles') == [p.model_dump(mode='json') for p in get_daily_profiles(station_id)])
    from .terminal_policy import POLICY as TERMINAL_POLICY
    if versions.get('terminal_policy') != TERMINAL_POLICY:
        return False
    valley = [p.get('buy_price_per_kwh') for p in points if p['tariff_period'] == 'gu']
    if not valley or any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in valley):
        return False
    if versions.get('terminal_inventory_price') != format(max(valley), '.17g'):
        return False
    if not isinstance(policy, dict) or policy.get('version') != 'peak-reserve-v4':
        return False
    bounds = request.get('constraints', {})
    lower, preferred = bounds.get('soc_min_pct'), bounds.get('preferred_soc_min_pct')
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in (lower, preferred)):
        return False
    if policy.get('terminal_soc_min_pct') != max(lower, preferred):
        return False
    return request.get('profiles') == [
        profile.model_dump(mode='json') for profile in get_daily_profiles(station_id)
    ]
