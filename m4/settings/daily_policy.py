"""Versioned daily planning rules; never inferred when replaying old requests."""
from shared.project import get_project

PEAK_RESERVE_DAILY_POLICY = 'm4-daily-peak-reserve-v3'
PEAK_RESERVE_SELECTOR = 'daily-peak-reserve-cost-gate-v3'


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
    expected = daily_policy_version(station_id)
    versions = request.get('source_versions')
    if (not isinstance(versions, dict) or versions.get('daily_policy') != expected
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
    if not isinstance(policy, dict) or policy.get('version') != 'peak-reserve-v3':
        return False
    return request.get('profiles') == [
        profile.model_dump(mode='json') for profile in get_daily_profiles(station_id)
    ]
