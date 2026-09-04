from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


HORIZON_POINTS = 96
INTERVAL_MINUTES = 15
ProfileId = Literal["balanced", "cost", "pv"]
ObjectiveName = Literal[
    "demand_peak",
    "demand_duration",
    "soc_preferred_deviation",
    "energy_cost",
    "pv_unused",
    "throughput",
]
CandidateStatus = Literal["optimal", "feasible", "infeasible", "timeout", "error"]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ForecastPoint(StrictModel):
    timestamp: datetime
    load_forecast_kw: NonNegativeFloat
    pv_forecast_kw: NonNegativeFloat
    buy_price_per_kwh: NonNegativeFloat
    sell_price_per_kwh: NonNegativeFloat = 0.0


class CapabilitySnapshot(StrictModel):
    available: bool
    initial_soc_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    energy_capacity_kwh: PositiveFloat
    max_charge_kw: NonNegativeFloat
    max_discharge_kw: NonNegativeFloat
    charge_efficiency: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    discharge_efficiency: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    derating_reason: str | None = None


class OptimizationConstraints(StrictModel):
    soc_min_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    soc_max_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    preferred_soc_min_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    preferred_soc_max_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    terminal_soc_tolerance_pct: NonNegativeFloat
    demand_limit_kw: NonNegativeFloat
    grid_import_limit_kw: NonNegativeFloat | None
    grid_export_enabled: bool
    grid_export_limit_kw: NonNegativeFloat
    cycle_cost_per_kwh: NonNegativeFloat


class ObjectiveLayer(StrictModel):
    name: str = Field(min_length=1)
    terms: dict[ObjectiveName, PositiveFloat] = Field(min_length=1)
    absolute_tolerance: NonNegativeFloat
    relative_tolerance: NonNegativeFloat


class ObjectiveProfile(StrictModel):
    profile_id: ProfileId
    profile_version: str = Field(min_length=1)
    objective_order: list[ObjectiveLayer] = Field(min_length=1)


class OptimizationRequest(StrictModel):
    request_id: str = Field(min_length=1)
    station_id: str = Field(min_length=1)
    plan_start_at: datetime
    input_observed_at: datetime
    max_input_age_seconds: int = Field(gt=0, strict=True)
    interval_minutes: Literal[15]
    horizon_points: Literal[96]
    source_versions: dict[str, str]
    points: list[ForecastPoint]
    capability: CapabilitySnapshot
    constraints: OptimizationConstraints
    profiles: list[ObjectiveProfile]
    solver_time_limit_seconds: PositiveFloat
    solver_mip_rel_gap: NonNegativeFloat

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "OptimizationRequest":
        if len(self.points) != HORIZON_POINTS:
            raise ValueError("request must contain exactly 96 points")
        if self.plan_start_at.tzinfo is None or self.input_observed_at.tzinfo is None:
            raise ValueError("request timestamps must include timezone")
        expected = [
            self.plan_start_at + timedelta(minutes=INTERVAL_MINUTES * index)
            for index in range(HORIZON_POINTS)
        ]
        if [point.timestamp for point in self.points] != expected:
            raise ValueError("points must form one contiguous 15-minute timeline")
        if self.input_observed_at > self.plan_start_at:
            raise ValueError("input observation cannot be later than plan start")
        age = (self.plan_start_at - self.input_observed_at).total_seconds()
        if age > self.max_input_age_seconds:
            raise ValueError("EMS capability snapshot is stale")
        profile_ids = [profile.profile_id for profile in self.profiles]
        if len(profile_ids) != 3 or set(profile_ids) != {"balanced", "cost", "pv"}:
            raise ValueError("profiles must contain balanced, cost and pv exactly once")
        required_objectives = {
            "demand_peak",
            "demand_duration",
            "soc_preferred_deviation",
            "energy_cost",
            "pv_unused",
            "throughput",
        }
        for profile in self.profiles:
            term_sets = [set(layer.terms) for layer in profile.objective_order]
            if term_sets[:2] != [{"demand_peak"}, {"demand_duration"}]:
                raise ValueError("demand objectives must be the first two layers")
            flattened = [name for layer in profile.objective_order for name in layer.terms]
            if set(flattened) != required_objectives or len(flattened) != len(
                required_objectives
            ):
                raise ValueError("each required objective must appear exactly once")
        bounds = self.constraints
        if not (
            bounds.soc_min_pct
            <= bounds.preferred_soc_min_pct
            <= bounds.preferred_soc_max_pct
            <= bounds.soc_max_pct
        ):
            raise ValueError("preferred SOC range must be inside absolute SOC bounds")
        if not bounds.soc_min_pct <= self.capability.initial_soc_pct <= bounds.soc_max_pct:
            raise ValueError("initial SOC must be inside absolute SOC bounds")
        if not bounds.grid_export_enabled and bounds.grid_export_limit_kw != 0:
            raise ValueError("disabled grid export requires a zero export limit")
        return self


class PlanPoint(StrictModel):
    timestamp: datetime
    mode: Literal["charge", "discharge", "idle"]
    target_power_kw: NonNegativeFloat
    expected_soc_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    grid_import_kw: NonNegativeFloat
    grid_export_kw: NonNegativeFloat
    pv_unabsorbed_kw: NonNegativeFloat
    demand_exceed_kw: NonNegativeFloat


class CandidateMetrics(StrictModel):
    energy_cost: FiniteFloat
    import_cost: NonNegativeFloat
    export_revenue: NonNegativeFloat
    max_grid_import_kw: NonNegativeFloat
    peak_demand_exceed_kw: NonNegativeFloat
    demand_exceed_energy_kwh: NonNegativeFloat
    pv_self_use_kwh: NonNegativeFloat
    pv_self_use_rate: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    grid_export_energy_kwh: NonNegativeFloat
    pv_unabsorbed_energy_kwh: NonNegativeFloat
    charge_energy_kwh: NonNegativeFloat
    discharge_energy_kwh: NonNegativeFloat
    throughput_energy_kwh: NonNegativeFloat
    cycle_cost: NonNegativeFloat
    min_soc_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    max_soc_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    terminal_soc_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    preferred_soc_deviation: NonNegativeFloat


class LayerResult(StrictModel):
    name: str
    best_value: FiniteFloat
    lock_tolerance: NonNegativeFloat


class CandidateResult(StrictModel):
    profile_id: ProfileId
    profile_version: str
    status: CandidateStatus
    solver_message: str
    solve_seconds: NonNegativeFloat
    plan: list[PlanPoint]
    metrics: CandidateMetrics | None
    layers: list[LayerResult]
    risk_codes: list[str]


class OptimizationResult(StrictModel):
    request_id: str
    station_id: str
    plan_start_at: datetime
    input_observed_at: datetime
    started_at: datetime
    finished_at: datetime
    model_version: str
    solver_name: Literal["scipy-highs"]
    solver_version: str
    source_versions: dict[str, str]
    candidates: list[CandidateResult]
