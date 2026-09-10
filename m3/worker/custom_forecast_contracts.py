"""Strict interval-independent contracts for user-submitted M3 forecasts."""

from datetime import datetime, timedelta
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from m3.worker.contracts import ASIA_SHANGHAI_OFFSET, SeriesId, is_load_series


ALLOWED_INTERVAL_SECONDS: tuple[int, ...] = (30, 60, 300, 900, 1800, 3600)
MAX_POINTS_PER_SERIES = 7 * (86_400 // min(ALLOWED_INTERVAL_SECONDS))
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ModelPolicy = Literal["seasonal_naive_only", "full_selection"]
RunStatus = Literal["queued", "running", "succeeded", "evaluated", "failed"]
SeriesStatus = Literal[
    "ok", "warming_up", "degraded", "insufficient_history", "error"
]


def _exact_mapping(value: object, fields: tuple[str, ...], context: str) -> dict:
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f"{context} must contain exactly the approved fields")
    return value


def _exact_datetime_origin(value: object, field_name: str) -> None:
    if type(value) not in {str, datetime}:
        raise ValueError(f"{field_name} must be an exact timestamp string or datetime")


def _validate_shanghai(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != ASIA_SHANGHAI_OFFSET:
        raise ValueError(f"{field_name} must use the Asia/Shanghai UTC+08:00 offset")


def _validate_midnight(value: datetime, field_name: str) -> None:
    _validate_shanghai(value, field_name)
    if value.hour or value.minute or value.second or value.microsecond:
        raise ValueError(f"{field_name} must be an Asia/Shanghai midnight")


def _finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomForecastRequest(StrictModel):
    history_start: datetime
    history_end: datetime
    forecast_days: int = Field(strict=True, ge=1, le=7)
    interval_seconds: int = Field(strict=True)
    idempotency_key: str = Field(strict=True)

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        fields = _exact_mapping(
            value,
            (
                "history_start",
                "history_end",
                "forecast_days",
                "interval_seconds",
                "idempotency_key",
            ),
            "custom forecast request",
        )
        _exact_datetime_origin(fields["history_start"], "history_start")
        _exact_datetime_origin(fields["history_end"], "history_end")
        if type(fields["forecast_days"]) is not int:
            raise ValueError("forecast_days must be an exact integer")
        if type(fields["interval_seconds"]) is not int:
            raise ValueError("interval_seconds must be an exact integer")
        if type(fields["idempotency_key"]) is not str:
            raise ValueError("idempotency_key must be an exact string")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> "CustomForecastRequest":
        _validate_midnight(self.history_start, "history_start")
        _validate_midnight(self.history_end, "history_end")
        history_days = (self.history_end - self.history_start).days
        if self.history_end - self.history_start != timedelta(days=history_days):
            raise ValueError("history window must contain complete days")
        if not 7 <= history_days <= 90:
            raise ValueError("history window must be between 7 and 90 days")
        if self.interval_seconds not in ALLOWED_INTERVAL_SECONDS:
            raise ValueError("interval_seconds is not supported")
        if IDEMPOTENCY_KEY.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency_key is invalid")
        return self


class CustomForecastConfig(StrictModel):
    history_start: datetime
    history_end: datetime
    history_days: int = Field(strict=True, ge=7, le=90)
    forecast_start: datetime
    forecast_end: datetime
    forecast_days: int = Field(strict=True, ge=1, le=7)
    interval_seconds: int = Field(strict=True)
    points_per_day: int = Field(strict=True, ge=24, le=2880)
    expected_points_per_series: int = Field(
        strict=True, ge=24, le=MAX_POINTS_PER_SERIES
    )
    model_policy: ModelPolicy

    @model_validator(mode="after")
    def validate_derived_fields(self) -> "CustomForecastConfig":
        for field_name in (
            "history_start",
            "history_end",
            "forecast_start",
            "forecast_end",
        ):
            _validate_midnight(getattr(self, field_name), field_name)
        if self.interval_seconds not in ALLOWED_INTERVAL_SECONDS:
            raise ValueError("interval_seconds is not supported")
        if self.history_end != self.forecast_start:
            raise ValueError("forecast_start must equal history_end")
        if self.history_end - self.history_start != timedelta(days=self.history_days):
            raise ValueError("history_days does not match the history window")
        if self.forecast_end - self.forecast_start != timedelta(
            days=self.forecast_days
        ):
            raise ValueError("forecast_days does not match the forecast window")
        expected_daily = 86_400 // self.interval_seconds
        if self.points_per_day != expected_daily:
            raise ValueError("points_per_day does not match interval_seconds")
        if self.expected_points_per_series != expected_daily * self.forecast_days:
            raise ValueError("expected point count does not match the forecast window")
        expected_policy = (
            "seasonal_naive_only" if self.history_days < 28 else "full_selection"
        )
        if self.model_policy != expected_policy:
            raise ValueError("model_policy does not match history_days")
        return self

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self.interval_seconds)

    @property
    def pandas_frequency(self) -> str:
        return f"{self.interval_seconds}s"

    @property
    def daily_season_length(self) -> int:
        return self.points_per_day

    @property
    def weekly_season_length(self) -> int:
        return self.points_per_day * 7


def build_custom_forecast_config(
    request: CustomForecastRequest,
) -> CustomForecastConfig:
    request = CustomForecastRequest.model_validate(request)
    history_days = (request.history_end - request.history_start).days
    points_per_day = 86_400 // request.interval_seconds
    return CustomForecastConfig(
        history_start=request.history_start,
        history_end=request.history_end,
        history_days=history_days,
        forecast_start=request.history_end,
        forecast_end=request.history_end + timedelta(days=request.forecast_days),
        forecast_days=request.forecast_days,
        interval_seconds=request.interval_seconds,
        points_per_day=points_per_day,
        expected_points_per_series=points_per_day * request.forecast_days,
        model_policy=(
            "seasonal_naive_only" if history_days < 28 else "full_selection"
        ),
    )


class CustomObservationPoint(StrictModel):
    unique_id: SeriesId
    ds: datetime
    y: float | None
    quality: Literal["valid", "invalid"]
    source_state: Literal[
        "valid",
        "no_rows",
        "no_numeric",
        "negative",
        "out_of_range",
        "coverage",
    ]
    source_revision: int = Field(strict=True, ge=0)

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        fields = _exact_mapping(
            value,
            (
                "unique_id",
                "ds",
                "y",
                "quality",
                "source_state",
                "source_revision",
            ),
            "custom observation point",
        )
        _exact_datetime_origin(fields["ds"], "ds")
        if type(fields["source_revision"]) is not int:
            raise ValueError("source_revision must be an exact integer")
        if fields["y"] is not None and type(fields["y"]) not in {int, float}:
            raise ValueError("y must be an exact number or null")
        return value

    @model_validator(mode="after")
    def validate_point(self) -> "CustomObservationPoint":
        _validate_shanghai(self.ds, "ds")
        if (self.quality == "valid") != (self.source_state == "valid"):
            raise ValueError("quality and source_state must agree")
        if self.source_state == "negative" and not is_load_series(self.unique_id):
            raise ValueError("negative source state is only valid for load")
        if self.quality == "invalid" and self.y is not None:
            raise ValueError("invalid observations require y=null")
        if self.quality == "valid":
            if self.y is None:
                raise ValueError("valid observations require y")
            _finite(self.y, "y")
            if is_load_series(self.unique_id) and self.y < 0:
                raise ValueError("load must be non-negative")
            if not is_load_series(self.unique_id) and not 0 <= self.y <= 100:
                raise ValueError("SOC must be between 0 and 100")
        return self


class CustomForecastPoint(StrictModel):
    target_time: datetime
    horizon_step: int = Field(strict=True, ge=1, le=MAX_POINTS_PER_SERIES)
    raw_forecast: float
    forecast_value: float
    is_clipped: bool

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        fields = _exact_mapping(
            value,
            (
                "target_time",
                "horizon_step",
                "raw_forecast",
                "forecast_value",
                "is_clipped",
            ),
            "custom forecast point",
        )
        _exact_datetime_origin(fields["target_time"], "target_time")
        if type(fields["horizon_step"]) is not int:
            raise ValueError("horizon_step must be an exact integer")
        for field_name in ("raw_forecast", "forecast_value"):
            if type(fields[field_name]) not in {int, float}:
                raise ValueError(f"{field_name} must be an exact number")
        if type(fields["is_clipped"]) is not bool:
            raise ValueError("is_clipped must be an exact boolean")
        return value

    @model_validator(mode="after")
    def validate_point(self) -> "CustomForecastPoint":
        _validate_shanghai(self.target_time, "target_time")
        _finite(self.raw_forecast, "raw_forecast")
        _finite(self.forecast_value, "forecast_value")
        if self.is_clipped != (self.raw_forecast != self.forecast_value):
            raise ValueError("is_clipped must reflect raw and published values")
        return self


class CustomForecastSeries(StrictModel):
    unique_id: SeriesId
    unit: Literal["kW", "%"]
    model_name: str = Field(strict=True)
    status: SeriesStatus
    points: list[CustomForecastPoint]
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def validate_series(self) -> "CustomForecastSeries":
        expected_unit = "kW" if is_load_series(self.unique_id) else "%"
        if self.unit != expected_unit:
            raise ValueError("series unit does not match unique_id")
        needs_reason = self.status in {
            "degraded",
            "insufficient_history",
            "error",
        }
        if needs_reason != (self.fallback_reason is not None):
            raise ValueError("series fallback reason does not match status")
        for index, point in enumerate(self.points, start=1):
            if point.horizon_step != index:
                raise ValueError("horizon steps must be continuous from one")
            if is_load_series(self.unique_id) and point.forecast_value < 0:
                raise ValueError("published load must be non-negative")
            if not is_load_series(self.unique_id) and not 0 <= point.forecast_value <= 100:
                raise ValueError("published SOC must be in 0..100")
        return self


def validate_custom_series_for_config(
    series: CustomForecastSeries, config: CustomForecastConfig
) -> None:
    if series.status in {"insufficient_history", "error"}:
        if series.points:
            raise ValueError("unavailable series must not contain forecast points")
        return
    if len(series.points) != config.expected_points_per_series:
        raise ValueError("forecast series point count does not match configuration")
    for index, point in enumerate(series.points):
        expected_time = config.forecast_start + index * config.interval
        if point.target_time != expected_time:
            raise ValueError("forecast target times are not contiguous")


class CustomRunRecord(StrictModel):
    run_id: str = Field(strict=True)
    station_id: str = Field(strict=True)
    idempotency_key: str = Field(strict=True)
    config: CustomForecastConfig
    status: RunStatus
    requested_by: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "CustomRunRecord":
        if RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("run_id is invalid")
        if not self.station_id or self.station_id != self.station_id.strip():
            raise ValueError("station_id is invalid")
        if IDEMPOTENCY_KEY.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency_key is invalid")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed runs require an error_code")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("non-failed runs must not expose an error_code")
        return self
