"""Deterministic selection of independently validated, already-solved plans."""
from fractions import Fraction
from hashlib import sha256
import json

from m4_optimizer.contracts import OptimizationRequest, OptimizationResult
from m4_optimizer.metrics import calculate_metrics
from m4_optimizer.validation import validate_candidate
from m4_selection.contracts import (
    ComparisonStep, ExcludedCandidate, SelectedPlan, SelectionPolicy, SelectionResult,
)

PROFILES = ('balanced', 'cost', 'pv')
LABELS = {
    'energy_cost': '净电费',
    'preferred_soc_deviation': '推荐SOC累计偏离',
    'pv_unabsorbed_energy_kwh': '未吸收光伏',
}


def select_candidate(
    request: OptimizationRequest,
    result: OptimizationResult,
    policy: SelectionPolicy | None = None,
) -> SelectionResult:
    """Return a preview only; freshness against the live clock is the caller's job.

    Invalid structures/bindings raise ValueError. Semantically invalid individual
    plans are excluded with diagnostics. No optimizer, AI or dispatch is invoked.
    """
    # model_validate(instance) alone can trust mutated Pydantic objects. Dump to
    # Python data first to revalidate strict types without JSON coercion/NaN loss.
    try:
        request = OptimizationRequest.model_validate(request.model_dump())
    except OverflowError as exc:
        raise ValueError('request horizon exceeds the supported date range') from exc
    result = OptimizationResult.model_validate(result.model_dump())
    if policy is not None:
        policy = SelectionPolicy.model_validate(policy.model_dump())
        if policy.station_id != request.station_id:
            raise ValueError('policy station_id does not match request')
    _validate_binding(request, result)
    encoded = json.dumps(
        {'request': request.model_dump(mode='json'), 'result': result.model_dump(mode='json')},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
    ).encode('utf-8')
    common = dict(request_id=request.request_id, station_id=request.station_id,
                  input_sha256=sha256(encoded).hexdigest(), policy=policy)
    if policy is not None and policy.metric == 'profile_priority':
        common['selector_version'] = 'demand-then-profile-v1'
    candidates = sorted(result.candidates, key=lambda item: item.profile_id)
    usable = {}
    excluded = []
    for candidate in candidates:
        if candidate.status not in {'optimal', 'feasible'}:
            excluded.append(ExcludedCandidate(profile_id=candidate.profile_id,
                code='solver_unusable', detail=f'solver status: {candidate.status}'))
            continue
        try:
            validate_candidate(request, candidate)
            # Rank fresh metrics even if supplied metrics differ within the
            # independent validator's physical/numerical acceptance tolerance.
            metrics = calculate_metrics(request, candidate.plan)
        except ValueError as exc:
            excluded.append(ExcludedCandidate(profile_id=candidate.profile_id,
                code='validation_failed', detail=str(exc)))
        else:
            usable[candidate.profile_id] = (candidate, metrics)
    common['excluded'] = excluded
    if not request.capability.available:
        return SelectionResult(**common, status='device_unavailable', reason='设备不可用，未选择计划。')
    if not usable:
        return SelectionResult(**common, status='no_usable_candidate', reason='没有通过复验的可用候选，未选择计划。')
    if policy is None:
        return SelectionResult(**common, status='pending_policy', reason='运营偏好尚未配置，待配置后选择。')

    remaining = list(usable)
    steps = []
    for metric, tolerance in (
        ('peak_demand_exceed_kw', policy.demand_peak_tolerance_kw),
        ('demand_exceed_energy_kwh', policy.demand_energy_tolerance_kwh),
        (policy.metric, policy.metric_tolerance),
    ):
        values = {pid: (float(policy.tie_order.index(pid)) if metric == 'profile_priority'
                        else getattr(usable[pid][1], metric)) for pid in remaining}
        # Exact decimal-string ratios avoid both binary addition errors and
        # rounding from a caller-modified thread-local Decimal context.
        ratios = {pid: Fraction(str(value)) for pid, value in values.items()}
        minimum = min(ratios.values())
        remaining = [pid for pid in remaining
                     if ratios[pid] - minimum <= Fraction(str(tolerance))]
        steps.append(ComparisonStep(metric=metric, tolerance=tolerance, values=values,
                                    minimum=float(minimum), remaining_ids=remaining.copy()))
    chosen_id = next(pid for pid in policy.tie_order if pid in remaining)
    chosen = usable[chosen_id][0]
    if policy.metric == 'profile_priority':
        reason = '需量逐层筛选后，按配置的候选顺序选择。'
    else:
        label = LABELS[policy.metric]
        reason = (f'需量逐层筛选后，{label}容差内并列，按配置顺序选择。'
                  if len(remaining) > 1 else f'需量逐层筛选后，按{label}及容差选中候选。')
    return SelectionResult(**common, status='selected', reason=reason, steps=steps,
                           selected=SelectedPlan(profile_id=chosen.profile_id,
                               profile_version=chosen.profile_version, plan_version=chosen.plan_version))


def _validate_binding(request: OptimizationRequest, result: OptimizationResult) -> None:
    for field in ('request_id', 'station_id', 'plan_start_at', 'input_observed_at', 'source_versions'):
        if getattr(request, field) != getattr(result, field):
            raise ValueError(f'result {field} does not match request')
    if not result.model_version.strip():
        raise ValueError('result model_version must be non-blank')
    ids = [item.profile_id for item in result.candidates]
    if len(ids) != 3 or set(ids) != set(PROFILES):
        raise ValueError('result must contain balanced, cost and pv exactly once')
    profiles = {item.profile_id: item for item in request.profiles}
    for item in result.candidates:
        expected = profiles[item.profile_id]
        if item.profile_version != expected.profile_version:
            raise ValueError(f'{item.profile_id} profile version does not match request')
        version = f'{request.request_id}/{result.model_version}/{item.profile_id}/{item.profile_version}'
        if item.plan_version != version:
            raise ValueError(f'{item.profile_id} plan version does not match result')
