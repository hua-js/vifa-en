"""Versioned admission of real day-start SOC above the charging ceiling."""

INITIAL_SOC_POLICY = 'historical-initial-soc-100-v1'
INITIAL_IDLE_PREFERENCE_POLICY = 'inherited-upper-soc-idle-v1'


def allows_initial_soc_excess(source_versions):
    return (source_versions.get('initial_soc_policy') == INITIAL_SOC_POLICY
            and source_versions.get('planning_basis') in (
                'whole-station-retrospective-v1', 'remaining-day-v1'))


def preferred_soc_upper(request):
    """Avoid discharging inherited excess solely to reach the charging ceiling.

    A distinct, lower preferred SOC target keeps its original meaning. Charging
    constraints still use soc_max_pct; this is only an optimization preference.
    Unversioned historical requests retain their original metric and objective.
    """
    bounds = request.constraints
    if (request.source_versions.get('initial_idle_preference') == INITIAL_IDLE_PREFERENCE_POLICY
            and allows_initial_soc_excess(request.source_versions)
            and bounds.preferred_soc_max_pct == bounds.soc_max_pct
            and request.capability.initial_soc_pct > bounds.soc_max_pct):
        return request.capability.initial_soc_pct
    return bounds.preferred_soc_max_pct
