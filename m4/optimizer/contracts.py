from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


HORIZON_POINTS = 96
INTERVAL_MINUTES = 15
GRID_CHARGING_POLICY = "peak-grid-charging-v1"
ProfileId = Literal["balanced", "cost", "pv"]
ObjectiveName = Literal[
    "demand_peak",
    "demand_duration",
    "soc_preferred_deviation",
    "energy_cost",
    "pv_unused",
    "throughput",
    "valley_charge_delay",
    "power_variation",
    "discharge_starts",
    "peak_reserve_shortfall",
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
    tariff_period: Literal["gu", "ping", "feng", "jian"] | None = None


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


class PeakReservePolicy(StrictModel):
    version: Literal["peak-reserve-v1", "peak-reserve-v2", "peak-reserve-v3", "peak-reserve-v4"] = "peak-reserve-v1"
    terminal_soc_min_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class OptimizationRequest(StrictModel):
    ems_schedule_modes: list[Literal["charge", "discharge", "idle"]] | None = None
    pv_dispatch_policy: Literal["legacy", "load_first_economic", "load_first_export_priority", "load_first_storage_priority"] = "legacy"
    pv_midday_economic: bool = False

    peak_reserve_policy: PeakReservePolicy | None = None
    request_id: str = Field(min_length=1)
    station_id: str = Field(min_length=1)
    plan_start_at: datetime
    input_observed_at: datetime
    max_input_age_seconds: int = Field(gt=0, strict=True)
    interval_minutes: Literal[15]
    horizon_points: Annotated[int, Field(ge=1, le=96, strict=True)]
    source_versions: dict[str, str]
    points: list[ForecastPoint]
    capability: CapabilitySnapshot
    constraints: OptimizationConstraints
    profiles: list[ObjectiveProfile]
    solver_time_limit_seconds: PositiveFloat
    solver_mip_rel_gap: NonNegativeFloat

    @model_serializer(mode="wrap")
    def serialize_optional_policy(self, handler):
        output = handler(self)
        if self.ems_schedule_modes is None:
            output.pop("ems_schedule_modes", None)
        if not self.pv_midday_economic:
            output.pop("pv_midday_economic", None)
        if self.peak_reserve_policy is None:
            output.pop("peak_reserve_policy", None)
        return output

    def pv_policy_at(self, timestamp: datetime) -> str:
        if self.pv_midday_economic and 12 <= timestamp.astimezone(ZoneInfo("Asia/Shanghai")).hour < 14:
            return "load_first_economic"
        return self.pv_dispatch_policy

    @property
    def is_remaining_day(self) -> bool:
        return self.source_versions.get('planning_basis') in (
            'remaining-day-v1', 'remaining-day-pv-correction-v2',
            'remaining-day-pv-correction-v3', 'remaining-day-fixed-baseline-v4',
            'night-valley-ems-startup-v1')

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "OptimizationRequest":
        if self.is_remaining_day:
            start = self.plan_start_at
            end = start + timedelta(minutes=15*self.horizon_points)
            startup = self.source_versions.get('planning_basis') == 'night-valley-ems-startup-v1'
            if startup:
                valid_end = (self.station_id == 'station-2' and end.date() == start.date()
                    and end <= start.replace(hour=12, minute=0, second=0, microsecond=0)
                    and self.source_versions.get('startup_window_end') == end.isoformat()
                    and self.source_versions.get('startup_policy') == 'night-valley-ems-startup-v1'
                    and all(p.tariff_period == 'gu' for p in self.points))
            else:
                valid_end = ((end.hour, end.minute, end.second, end.microsecond) == (0, 0, 0, 0)
                    and end.date() == start.date() + timedelta(days=1))
            if (start.utcoffset() != timedelta(hours=8) or start.minute % 15
                    or start.second or start.microsecond or not valid_end):
                raise ValueError('rolling window must match its authorized planning horizon')
        elif self.horizon_points not in (95, 96):
            raise ValueError('non-rolling requests require 95 or 96 points')
        if self.ems_schedule_modes is not None and len(self.ems_schedule_modes) != self.horizon_points:
            raise ValueError("EMS schedule must cover every request point")
        if len(self.points) != self.horizon_points:
            raise ValueError(f"request must contain exactly {self.horizon_points} points")
        timestamp_values = [
            ("plan_start_at", self.plan_start_at),
            ("input_observed_at", self.input_observed_at),
            *(
                (f"points[{index}].timestamp", point.timestamp)
                for index, point in enumerate(self.points)
            ),
        ]
        offsets = []
        for label, value in timestamp_values:
            offset = value.utcoffset() if value.tzinfo is not None else None
            if offset is None:
                raise ValueError(f"{label} must have a valid UTC offset")
            offsets.append(offset)
        if any(offset != offsets[0] for offset in offsets[1:]):
            raise ValueError("request timestamps must use the same UTC offset")
        expected = [
            self.plan_start_at + timedelta(minutes=INTERVAL_MINUTES * index)
            for index in range(self.horizon_points)
        ]
        if [point.timestamp for point in self.points] != expected:
            raise ValueError("points must form one contiguous 15-minute timeline")
        if self.input_observed_at > self.plan_start_at:
            raise ValueError("input observation cannot be later than plan start")
        age = (self.plan_start_at - self.input_observed_at).total_seconds()
        if age > self.max_input_age_seconds:
            raise ValueError("EMS capability snapshot is stale")
        if not self.source_versions or any(
            not key.strip() or not value.strip()
            for key, value in self.source_versions.items()
        ):
            raise ValueError(
                "source_versions must be non-empty with non-blank keys and values"
            )
        profile_ids = [profile.profile_id for profile in self.profiles]
        if len(profile_ids) != 3 or set(profile_ids) != {"balanced", "cost", "pv"}:
            raise ValueError("profiles must contain balanced, cost and pv exactly once")
        if self.peak_reserve_policy is not None:
            if any(point.tariff_period is None for point in self.points):
                raise ValueError("peak reserve policy requires known tariff periods")
            if not any(point.tariff_period in ("feng", "jian") for point in self.points) and not self.is_remaining_day:
                raise ValueError("peak reserve policy requires at least one peak period")
        required_objectives = {
            "demand_peak",
            "demand_duration",
            "soc_preferred_deviation",
            "energy_cost",
            "pv_unused",
            "throughput",
        }
        from shared.project import get_project
        station = next((s for s in get_project().stations if s.id == self.station_id), None)
        cost_first = self.source_versions.get('economic_policy') == 'station-1-cost-first-v1'
        if cost_first and (station is None or station.policy != 'cost_first'
                or self.constraints.grid_import_limit_kw is None
                or self.source_versions.get('physical_grid_policy') not in ('station-1-550-v1', 'configured-grid-import-v1')
                or (self.source_versions.get('physical_grid_policy') == 'station-1-550-v1'
                    and self.constraints.grid_import_limit_kw != 550.0)
                or self.peak_reserve_policy is not None):
            raise ValueError('cost-first policy requires a configured physical ceiling')
        for profile in self.profiles:
            term_sets = [set(layer.terms) for layer in profile.objective_order]
            if cost_first:
                if term_sets[:3] != [{"energy_cost"}, {"demand_peak"}, {"demand_duration"}]:
                    raise ValueError('cost-first policy requires cost before demand tie-breaks')
            elif term_sets[:2] != [{"demand_peak"}, {"demand_duration"}]:
                raise ValueError("demand objectives must be the first two layers")
            flattened = [name for layer in profile.objective_order for name in layer.terms]
            if self.peak_reserve_policy is not None:
                if self.peak_reserve_policy.version in ("peak-reserve-v3", "peak-reserve-v4"):
                    if (flattened.count("peak_reserve_shortfall") != 1
                            or {"peak_reserve_shortfall"} not in term_sets):
                        raise ValueError("economic reserve requires one separate reserve layer")
                    reserve_index = term_sets.index({"peak_reserve_shortfall"})
                    cost_index = next((i for i, terms in enumerate(term_sets) if "energy_cost" in terms), len(term_sets))
                    if reserve_index <= cost_index:
                        raise ValueError("economic cost must precede reserve preparation")
                elif (term_sets[2:3] != [{"peak_reserve_shortfall"}]
                        or flattened.count("peak_reserve_shortfall") != 1):
                    raise ValueError("peak reserve objective must be a separate third layer")
                flattened.remove("peak_reserve_shortfall")
            elif "peak_reserve_shortfall" in flattened:
                raise ValueError("peak reserve objective requires an explicit policy")
            early_valley_before_smoothing = (
                station is not None and station.policy == 'peak_reserve'
                and self.peak_reserve_policy is not None
                and self.peak_reserve_policy.version in ('peak-reserve-v3', 'peak-reserve-v4')
                and profile.profile_version.endswith('/station-2-continuity-v3'))
            if "discharge_starts" in flattened:
                starts_tail = ([{'discharge_starts'}, {'valley_charge_delay'}, {'power_variation'}]
                               if early_valley_before_smoothing else
                               [{'discharge_starts'}, {'power_variation'}, {'valley_charge_delay'}])
                if (station is None or station.policy != 'peak_reserve' or self.peak_reserve_policy is None
                        or self.peak_reserve_policy.version not in ('peak-reserve-v3', 'peak-reserve-v4')
                        or flattened.count('discharge_starts') != 1
                        or term_sets[-3:] != starts_tail
                        or 'throughput' not in flattened[:flattened.index('discharge_starts')]):
                    raise ValueError('discharge starts requires a late separate station-2 daily layer')
                flattened.remove('discharge_starts')
            if "power_variation" in flattened:
                continuity_scope = cost_first or (
                    station is not None and station.policy == 'peak_reserve' and self.peak_reserve_policy is not None
                    and self.peak_reserve_policy.version in ('peak-reserve-v3', 'peak-reserve-v4'))
                continuity_before_soc = (cost_first and
                    profile.profile_version.endswith('/station-1-continuity-v2'))
                expected_tail = ([{'power_variation'}, {'soc_preferred_deviation'},
                                  {'valley_charge_delay'}] if continuity_before_soc else
                                 [{'power_variation'}, {'valley_charge_delay'}])
                if early_valley_before_smoothing:
                    expected_tail = [{'discharge_starts'}, {'valley_charge_delay'}, {'power_variation'}]
                if (not continuity_scope
                        or flattened.count('power_variation') != 1
                        or term_sets[-len(expected_tail):] != expected_tail
                        or 'throughput' not in flattened[:flattened.index('power_variation')]):
                    raise ValueError('continuity requires a supported daily policy and a late separate layer')
                flattened.remove('power_variation')
            if "valley_charge_delay" in flattened:
                if (
                    term_sets[-2 if early_valley_before_smoothing else -1] != {"valley_charge_delay"}
                    or flattened.count("valley_charge_delay") != 1
                ):
                    raise ValueError("valley charge preference must be a separate final layer or precede station-2 v3 smoothing")
                flattened = flattened[:-1]
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
        if self.peak_reserve_policy is not None and not (
            bounds.preferred_soc_min_pct
            <= self.peak_reserve_policy.terminal_soc_min_pct
            <= bounds.soc_max_pct
        ):
            raise ValueError("peak reserve terminal SOC floor must respect preferred minimum and safety maximum")
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
    plan_version: str
    status: CandidateStatus
    solver_message: str
    solve_seconds: NonNegativeFloat
    plan: list[PlanPoint]
    metrics: CandidateMetrics | None
    layers: list[LayerResult]
    risk_codes: list[str]
    risk_messages: list[str]

    @model_validator(mode="after")
    def validate_audit_fields(self) -> "CandidateResult":
        if not self.plan_version.strip():
            raise ValueError("plan_version must be non-blank")
        if len(self.risk_codes) != len(self.risk_messages):
            raise ValueError("risk_codes and risk_messages must have the same length")
        if any(not code.strip() for code in self.risk_codes):
            raise ValueError("risk_codes must not contain blank values")
        if any(not message.strip() for message in self.risk_messages):
            raise ValueError("risk_messages must not contain blank values")
        return self


class OptimizationResult(StrictModel):
    request_id: str
    station_id: str
    plan_start_at: datetime
    input_observed_at: datetime
    started_at: datetime
    finished_at: datetime
    model_version: str
    solver_name: Literal["scipy-highs", "pyomo-highs"]
    solver_version: str
    source_versions: dict[str, str]
    candidates: list[CandidateResult]
