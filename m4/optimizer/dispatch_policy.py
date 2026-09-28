"""Request-bound minimum action power; archived/unmarked solves stay unchanged."""
import math

POLICY = 'minimum-action-v1'
# Strictly above the table's exclusion boundary, clear of solver feasibility noise.
MINIMUM_POWER_MARGIN_KW = 0.001


def minimum_action_power(request):
    versions = request.source_versions
    policy = versions.get('dispatch_power_policy')
    if policy is None and 'minimum_dispatch_power_kw' not in versions:
        return 0.0
    try:
        value = float(versions['minimum_dispatch_power_kw'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid minimum action power policy') from None
    if policy != POLICY or not math.isfinite(value) or value <= 0:
        raise ValueError('invalid minimum action power policy')
    return value
