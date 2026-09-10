"""Profile ordering for the optimizer's public candidate display."""

from m4.optimizer.contracts import ObjectiveProfile, OptimizationRequest


PROFILE_ORDER = ("balanced", "cost", "pv")


def ordered_profiles(request: OptimizationRequest) -> tuple[ObjectiveProfile, ...]:
    """Return request-provided profiles in the stable public display order."""
    by_id = {profile.profile_id: profile for profile in request.profiles}
    return tuple(by_id[profile_id] for profile_id in PROFILE_ORDER)
