"""Browser-safe contracts for the nested two-station M3 dashboard."""

from datetime import datetime, timedelta
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from m3_worker.contracts import SERIES_IDS, SeriesId, is_load_series, validate_shanghai_timestamp


INTERVAL = timedelta(minutes=15)


def _finite(value: float | None, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if value is None or not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _safe_text(value: str | None, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty safe string")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardRange(ApiModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    history_start: datetime | None
    history_end: datetime | None
    actual_latest: datetime | None
    forecast_start: datetime | None
    forecast_end: datetime | None
    interval_seconds: Literal[900] = 900
    history_hours: Literal[24] = 24
    forecast_hours: Literal[24] = 24
    display_hours: Literal[48] = 48
    now_separator: datetime

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.history_start,
                self.history_end,
                self.actual_latest,
                self.forecast_start,
                self.forecast_end,
            )
        )

    @model_validator(mode="after")
    def validate_range(self) -> "DashboardRange":
        validate_shanghai_timestamp(
            self.now_separator, "now_separator", quarter_hour=False
        )
        boundaries = (
            self.history_start,
            self.history_end,
            self.forecast_start,
            self.forecast_end,
        )
        if all(value is None for value in boundaries):
            if self.actual_latest is not None:
                raise ValueError("empty range cannot include actual_latest")
            return self
        if any(value is None for value in boundaries):
            raise ValueError("dashboard range boundaries must be complete or empty")
        history_start, history_end, forecast_start, forecast_end = boundaries
        for name, value in (
            ("history_start", history_start),
            ("history_end", history_end),
            ("forecast_start", forecast_start),
            ("forecast_end", forecast_end),
        ):
            validate_shanghai_timestamp(value, name, quarter_hour=True)
        if history_end - history_start != timedelta(hours=24):
            raise ValueError("history range must cover exactly 24 hours")
        if forecast_start != history_end:
            raise ValueError("forecast range must begin at history_end")
        if forecast_end - forecast_start != timedelta(hours=24):
            raise ValueError("forecast range must cover exactly 24 hours")
        if self.actual_latest is not None:
            validate_shanghai_timestamp(
                self.actual_latest, "actual_latest", quarter_hour=True
            )
            if not history_start <= self.actual_latest < history_end:
                raise ValueError("actual_latest must fall inside the history range")
        return self


class DashboardStationSystem(ApiModel):
    state: Literal["ready", "initializing", "degraded", "stale", "error"]
    mode: Literal["normal", "degraded", "initializing", "error"]
    generated_at: datetime | None
    stale: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_system(self) -> "DashboardStationSystem":
        if self.generated_at is not None:
            validate_shanghai_timestamp(
                self.generated_at, "station generated_at", quarter_hour=False
            )
        if self.stale != (self.state == "stale"):
            raise ValueError("station stale flag must match stale state")
        if self.state == "error" and self.mode != "error":
            raise ValueError("error station must use error mode")
        if self.mode == "error" and self.state != "error":
            raise ValueError("error mode is reserved for error stations")
        return self


class DashboardActualPoint(ApiModel):
    data_time: datetime
    value: float | None = Field(strict=True)
    quality: Literal["valid", "invalid"]
    source_revision: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_point(self) -> "DashboardActualPoint":
        validate_shanghai_timestamp(self.data_time, "actual data_time", quarter_hour=True)
        if self.quality == "invalid":
            if self.value is not None:
                raise ValueError("invalid actual point requires value=null")
        else:
            _finite(self.value, "actual value")
        return self


class DashboardForecastPoint(ApiModel):
    data_time: datetime
    target_time: datetime
    value: float = Field(strict=True)
    raw_value: float = Field(strict=True)
    is_clipped: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_point(self) -> "DashboardForecastPoint":
        validate_shanghai_timestamp(
            self.data_time, "forecast data_time", quarter_hour=True
        )
        validate_shanghai_timestamp(
            self.target_time, "forecast target_time", quarter_hour=True
        )
        if self.target_time - self.data_time != INTERVAL:
            raise ValueError("forecast target_time must follow data_time by 15 minutes")
        _finite(self.value, "forecast value")
        _finite(self.raw_value, "raw forecast value")
        if self.is_clipped != (self.value != self.raw_value):
            raise ValueError("is_clipped must truthfully describe the published value")
        return self


class DashboardSeries(ApiModel):
    unique_id: SeriesId
    unit: Literal["kW", "%"]
    model_name: str | None
    status: Literal[
        "ok", "warming_up", "degraded",
        "insufficient_history", "initializing", "error",
    ]
    fallback_reason: str | None
    actual: list[DashboardActualPoint]
    forecast: list[DashboardForecastPoint]

    @model_validator(mode="after")
    def validate_series(self) -> "DashboardSeries":
        expected_unit = "kW" if is_load_series(self.unique_id) else "%"
        if self.unit != expected_unit:
            raise ValueError("dashboard series unit does not match unique_id")
        _safe_text(self.model_name, "model_name", optional=True)
        _safe_text(self.fallback_reason, "fallback_reason", optional=True)
        requires_forecast = self.status in {"ok", "warming_up", "degraded"}
        expected_count = 96 if requires_forecast else 0
        if len(self.forecast) != expected_count:
            raise ValueError(f"{self.status} dashboard series requires {expected_count} forecasts")
        if requires_forecast and self.model_name is None:
            raise ValueError("published forecasts require a model_name")
        if self.status in {"ok", "warming_up", "initializing"}:
            if self.fallback_reason is not None:
                raise ValueError(f"{self.status} must not include a fallback reason")
        elif self.fallback_reason is None:
            raise ValueError(f"{self.status} requires a safe fallback reason")
        if len(self.actual) > 96:
            raise ValueError("dashboard actual history cannot exceed 96 points")
        for previous, current in zip(self.actual, self.actual[1:]):
            if current.data_time - previous.data_time != INTERVAL:
                raise ValueError("actual points must be continuous at 15-minute intervals")
        for previous, current in zip(self.forecast, self.forecast[1:]):
            if current.data_time != previous.target_time:
                raise ValueError("forecast points must be continuous at 15-minute intervals")
        for point in self.actual:
            if point.value is None:
                continue
            if is_load_series(self.unique_id) and point.value < 0:
                raise ValueError("published actual load must be non-negative")
            if not is_load_series(self.unique_id) and not 0 <= point.value <= 100:
                raise ValueError("published actual SOC must be in 0..100")
        for point in self.forecast:
            expected = (
                max(point.raw_value, 0.0)
                if is_load_series(self.unique_id)
                else min(max(point.raw_value, 0.0), 100.0)
            )
            if point.value != expected:
                raise ValueError("published forecast does not truthfully apply physical clipping")
        return self


class DashboardAcceptanceResult(ApiModel):
    unique_id: SeriesId
    expected_count: Literal[672]
    valid_count: int = Field(ge=0, le=672, strict=True)
    zero_actual_count: int = Field(ge=0, le=672, strict=True)
    mape_percent: float | None = Field(strict=True)
    mae: float | None = Field(strict=True)
    smape_percent: float | None = Field(strict=True)
    wape_percent: float | None = Field(strict=True)
    median_ape_percent: float | None = Field(strict=True)
    p90_ape_percent: float | None = Field(strict=True)
    outcome: Literal["passed", "failed", "insufficient_data"]

    @model_validator(mode="after")
    def validate_result(self) -> "DashboardAcceptanceResult":
        if self.valid_count + self.zero_actual_count > self.expected_count:
            raise ValueError("acceptance point counts cannot exceed expected_count")
        metrics = (
            self.mape_percent,
            self.mae,
            self.smape_percent,
            self.wape_percent,
            self.median_ape_percent,
            self.p90_ape_percent,
        )
        if any(value is None for value in metrics) and any(
            value is not None for value in metrics
        ):
            raise ValueError("acceptance metrics must be complete or all null")
        if self.outcome in {"passed", "failed"} and any(
            value is None for value in metrics
        ):
            raise ValueError("passed or failed acceptance requires all metrics")
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in metrics
        ):
            raise ValueError("acceptance metrics must be finite and non-negative")
        return self


class DashboardAcceptance(ApiModel):
    acceptance_run_id: str
    status: Literal["in_progress", "passed", "failed", "insufficient_data"]
    completed_days: int = Field(ge=1, le=7, strict=True)
    expected_days: Literal[7] = 7
    results: list[DashboardAcceptanceResult]

    @model_validator(mode="after")
    def validate_acceptance(self) -> "DashboardAcceptance":
        _safe_text(self.acceptance_run_id, "acceptance_run_id")
        if self.status == "in_progress":
            if self.results:
                raise ValueError("in-progress acceptance results must be empty")
        elif [item.unique_id for item in self.results] != list(SERIES_IDS):
            raise ValueError("final acceptance requires the two ordered series")
        return self


class DashboardReadiness(ApiModel):
    required_days: Literal[28] = 28
    available_days: float = Field(ge=0, le=28, strict=True)
    remaining_days: float = Field(ge=0, le=28, strict=True)

    @model_validator(mode="after")
    def validate_progress(self) -> "DashboardReadiness":
        if round(self.available_days, 1) != self.available_days:
            raise ValueError("available_days must use one decimal place")
        if round(self.remaining_days, 1) != self.remaining_days:
            raise ValueError("remaining_days must use one decimal place")
        expected_remaining = round(
            max(0.0, self.required_days - self.available_days), 1
        )
        if self.remaining_days != expected_remaining:
            raise ValueError("remaining_days must match available history")
        return self


class DashboardStation(ApiModel):
    station_key: Literal["station_1", "station_2"]
    station_name: str
    range: DashboardRange
    system: DashboardStationSystem
    series: tuple[DashboardSeries, DashboardSeries]
    acceptance: DashboardAcceptance | None = None
    readiness: DashboardReadiness | None = None

    @model_validator(mode="after")
    def validate_station(self) -> "DashboardStation":
        _safe_text(self.station_name, "station_name")
        if [item.unique_id for item in self.series] != list(SERIES_IDS):
            raise ValueError("station series must be ordered load then SOC")
        if self.range.is_empty:
            if self.system.state not in {"initializing", "error"}:
                raise ValueError("empty station must be initializing or error")
            if self.system.generated_at is not None:
                raise ValueError("empty station cannot invent generated_at")
            expected_status = self.system.state
            if any(
                item.status != expected_status
                or item.model_name is not None
                or item.actual
                or item.forecast
                for item in self.series
            ):
                raise ValueError("empty station cannot invent model or point data")
            if self.acceptance is not None:
                raise ValueError("empty station cannot invent acceptance data")
            if self.readiness is not None:
                raise ValueError("empty station cannot invent readiness data")
            return self
        if self.system.generated_at is None:
            raise ValueError("non-empty station requires generated_at")
        if self.system.state == "error":
            raise ValueError("error station must use an empty range")
        if (
            self.system.state == "ready"
            and self.readiness is not None
            and self.readiness.remaining_days != 0
        ):
            raise ValueError("ready station cannot have remaining history days")
        history_start = self.range.history_start
        history_end = self.range.history_end
        forecast_start = self.range.forecast_start
        forecast_end = self.range.forecast_end
        actual_times = [point.data_time for item in self.series for point in item.actual]
        if any(not history_start <= value < history_end for value in actual_times):
            raise ValueError("actual point falls outside the station history range")
        latest = max(actual_times) if actual_times else None
        if self.range.actual_latest != latest:
            raise ValueError("actual_latest must match the latest published actual point")
        for item in self.series:
            if item.forecast:
                if item.forecast[0].data_time != forecast_start:
                    raise ValueError("forecast must start at the station forecast_start")
                if item.forecast[-1].target_time != forecast_end:
                    raise ValueError("forecast must end at the station forecast_end")
        return self


class DashboardAggregateSystem(ApiModel):
    state: Literal["ready", "initializing", "degraded", "stale"]
    generated_at: datetime
    healthy_station_count: int = Field(ge=0, le=2, strict=True)
    station_count: Literal[2] = 2

    @model_validator(mode="after")
    def validate_generated_at(self) -> "DashboardAggregateSystem":
        validate_shanghai_timestamp(
            self.generated_at, "aggregate generated_at", quarter_hour=False
        )
        return self


class DashboardData(ApiModel):
    operation: Literal["forecast_dashboard"] = "forecast_dashboard"
    system: DashboardAggregateSystem
    stations: tuple[DashboardStation, DashboardStation]

    @model_validator(mode="after")
    def validate_aggregate(self) -> "DashboardData":
        if [item.station_key for item in self.stations] != ["station_1", "station_2"]:
            raise ValueError("dashboard stations must be ordered station_1, station_2")
        states = [item.system.state for item in self.stations]
        if any(state in {"error", "degraded"} for state in states):
            expected_state = "degraded"
        elif "stale" in states:
            expected_state = "stale"
        elif "initializing" in states:
            expected_state = "initializing"
        else:
            expected_state = "ready"
        healthy = sum(
            item.system.state in {"ready", "initializing"} and not item.system.stale
            for item in self.stations
        )
        if self.system.state != expected_state:
            raise ValueError("aggregate state does not match station severity")
        if self.system.healthy_station_count != healthy:
            raise ValueError("aggregate healthy_station_count is incorrect")
        return self


class DashboardEnvelope(ApiModel):
    status: Literal["ok"] = "ok"
    data: DashboardData
