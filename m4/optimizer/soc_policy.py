"""Versioned admission of real day-start SOC above the charging ceiling."""

INITIAL_SOC_POLICY = 'historical-initial-soc-100-v1'


def allows_initial_soc_excess(source_versions):
    return (source_versions.get('initial_soc_policy') == INITIAL_SOC_POLICY
            and source_versions.get('planning_basis') in (
                'whole-station-retrospective-v1', 'remaining-day-v1'))
