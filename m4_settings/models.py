"""Manual station limits; live SOC and availability are deliberately separate."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from m4_optimizer.contracts import StrictModel
from .roster import STATION_CABINETS

StationId = Literal['station-1', 'station-2']
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Percentage = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Efficiency = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]


class StationParameters(StrictModel):
    energy_capacity_kwh: Positive
    max_charge_kw: NonNegative
    max_discharge_kw: NonNegative
    charge_efficiency: Efficiency
    discharge_efficiency: Efficiency
    soc_min_pct: Percentage
    soc_max_pct: Percentage
    preferred_soc_min_pct: Percentage
    preferred_soc_max_pct: Percentage
    terminal_soc_tolerance_pct: Percentage
    grid_import_limit_kw: NonNegative | None
    cycle_cost_per_kwh: NonNegative
    max_input_age_seconds: Annotated[int, Field(gt=0, le=86400)]

    @model_validator(mode='after')
    def validate_limits(self):
        if not (self.soc_min_pct < self.soc_max_pct
                and self.soc_min_pct <= self.preferred_soc_min_pct
                <= self.preferred_soc_max_pct <= self.soc_max_pct):
            raise ValueError('SOC 推荐范围必须位于安全上下限之间，安全下限须小于上限')
        return self


class ResolvedControlLimits(StrictModel):
    """Confirmed upstream rules, supplied separately from editable parameters.

    Source values and confirmed policy are resolved by the live input adapter.
    The inactive re_kw value must never become an export allowance or margin.
    """
    station_id: StationId
    source_version: Annotated[str, Field(min_length=1, pattern=r'\S')]
    demand_limit_kw: NonNegative
    grid_export_enabled: bool
    grid_export_limit_kw: NonNegative

    @model_validator(mode='after')
    def validate_export(self):
        if not self.grid_export_enabled and self.grid_export_limit_kw != 0:
            raise ValueError('禁止反送时，反送功率上限必须为 0')
        return self


class StationConfiguration(StrictModel):
    station_id: StationId
    revision: Annotated[int, Field(ge=0)] = 0
    version: str | None = None
    updated_at: datetime | None = None
    capability_source: Literal['manual_limits'] = 'manual_limits'
    parameters: StationParameters | None = None


class SaveSettings(StrictModel):
    expected_revision: Annotated[int, Field(ge=0)]
    parameters: StationParameters


class LiveStationState(StrictModel):
    station_id: StationId
    participating_cabinet_ids: list[str]
    initial_soc_pct: Percentage
    available: bool
    observed_at: datetime
    source_version: Annotated[str, Field(min_length=1)]

    @model_validator(mode='after')
    def validate_observation(self):
        roster = STATION_CABINETS[self.station_id][1]
        participants = self.participating_cabinet_ids
        if len(set(participants)) != len(participants):
            raise ValueError('参与柜名单不能重复')
        if any(cabinet_id not in roster for cabinet_id in participants):
            raise ValueError('参与柜必须属于本站固定配置名单')
        if self.available and not participants:
            raise ValueError('可用状态必须至少包含一台参与柜')
        self.participating_cabinet_ids = [cabinet_id for cabinet_id in roster if cabinet_id in participants]
        if self.observed_at.utcoffset() is None:
            raise ValueError('实时采样时间必须携带时区')
        if not self.source_version.strip():
            raise ValueError('实时数据必须提供来源版本')
        return self
