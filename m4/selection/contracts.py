"""Strict contracts for local, post-solver selection previews."""
from typing import Literal

from pydantic import Field, model_validator

from m4.optimizer.contracts import (
    FiniteFloat, NonNegativeFloat, ProfileId, StrictModel,
)

PreferenceMetric = Literal[
    'energy_cost', 'preferred_soc_deviation', 'pv_unabsorbed_energy_kwh',
    'profile_priority',
]
SelectionMetric = Literal[
    'peak_demand_exceed_kw', 'demand_exceed_energy_kwh',
    'energy_cost', 'preferred_soc_deviation', 'pv_unabsorbed_energy_kwh',
    'profile_priority',
]


class SelectionPolicy(StrictModel):
    station_id: str
    policy_id: str
    version: str
    metric: PreferenceMetric
    demand_peak_tolerance_kw: NonNegativeFloat
    demand_energy_tolerance_kwh: NonNegativeFloat
    metric_tolerance: NonNegativeFloat
    tie_order: list[ProfileId]

    @model_validator(mode='after')
    def validate_policy(self) -> 'SelectionPolicy':
        if any(not value.strip() for value in (self.station_id, self.policy_id, self.version)):
            raise ValueError('policy identifiers must be non-blank')
        if len(self.tie_order) != 3 or set(self.tie_order) != {'balanced', 'cost', 'pv'}:
            raise ValueError('tie_order must contain balanced, cost and pv exactly once')
        if self.metric == 'profile_priority' and self.metric_tolerance != 0:
            raise ValueError('profile_priority tolerance must be zero')
        return self


class SelectedPlan(StrictModel):
    profile_id: ProfileId
    profile_version: str
    plan_version: str


class ExcludedCandidate(StrictModel):
    profile_id: ProfileId
    code: Literal['solver_unusable', 'validation_failed']
    detail: str


class ComparisonStep(StrictModel):
    metric: SelectionMetric
    tolerance: NonNegativeFloat
    values: dict[ProfileId, FiniteFloat]
    minimum: FiniteFloat
    remaining_ids: list[ProfileId]


class SelectionResult(StrictModel):
    schema_version: Literal['m4-selection-v1'] = 'm4-selection-v1'
    selector_version: Literal['demand-then-preference-v1', 'demand-then-profile-v1', 'pyomo-highs-selection-v2'] = 'demand-then-preference-v1'
    usage: Literal['preview_only'] = 'preview_only'
    dispatch_status: Literal['not_dispatched'] = 'not_dispatched'
    request_id: str
    station_id: str
    input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    policy: SelectionPolicy | None
    status: Literal['selected', 'pending_policy', 'device_unavailable', 'no_usable_candidate']
    selected: SelectedPlan | None = None
    reason: str = Field(min_length=1, max_length=50)
    excluded: list[ExcludedCandidate] = Field(default_factory=list)
    steps: list[ComparisonStep] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_selection(self) -> 'SelectionResult':
        if self.status == 'selected':
            if self.selected is None or self.policy is None or len(self.steps) != 3:
                raise ValueError('selected preview requires plan, policy and three comparison steps')
            if self.selected.profile_id not in self.steps[-1].remaining_ids:
                raise ValueError('selected plan must belong to the final comparison group')
        elif self.selected is not None or self.steps:
            raise ValueError('blocked preview cannot carry a selection or comparison steps')
        return self
