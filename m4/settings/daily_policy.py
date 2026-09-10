"""Versioned daily planning rules; never inferred when replaying old requests."""

PEAK_RESERVE_DAILY_POLICY = 'm4-daily-peak-reserve-v2'
PEAK_RESERVE_SELECTOR = 'daily-peak-reserve-cost-gate-v2'


def daily_policy_version(station_id):
    return PEAK_RESERVE_DAILY_POLICY if station_id == 'station-2' else None


def matches_current_daily_policy(station_id, request):
    """Reject stale daily plans before either memory or disk results are adopted."""
    if not isinstance(request, dict):
        return False
    expected = daily_policy_version(station_id)
    versions = request.get('source_versions')
    if not isinstance(versions, dict) or versions.get('daily_policy') != expected:
        return False
    policy = request.get('peak_reserve_policy')
    if expected is None:
        return policy is None
    if not isinstance(policy, dict) or policy.get('version') != 'peak-reserve-v2':
        return False
    from .objectives import get_daily_profiles
    return request.get('profiles') == [
        profile.model_dump(mode='json') for profile in get_daily_profiles(station_id)
    ]
