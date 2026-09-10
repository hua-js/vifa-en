from datetime import datetime, timedelta
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from m3.worker.json_contract import validate_exact_json_mapping


SeriesId = Literal["station_total_load", "storage_soc"]
SERIES_IDS: tuple[SeriesId, ...] = ("station_total_load", "storage_soc")
ASIA_SHANGHAI_OFFSET = timedelta(hours=8)


def is_load_series(unique_id: SeriesId) -> bool:
    return unique_id == "station_total_load"


def validate_shanghai_timestamp(
    value: datetime, field_name: str, *, quarter_hour: bool
) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    if value.utcoffset() != ASIA_SHANGHAI_OFFSET:
        raise ValueError(f"{field_name} must use the Asia/Shanghai UTC+08:00 offset")
    if quarter_hour and (value.minute % 15 or value.second or value.microsecond):
        raise ValueError(f"{field_name} must be on a 15-minute boundary")


def _origin_fields(
    value: object,
    model_type: type,
    fields: tuple[str, ...],
    context: str,
) -> dict[str, object]:
    if type(value) is dict:
        try:
            return {field: value[field] for field in fields}
        except KeyError as error:
            raise ValueError(f"{context} is missing a required field") from error
    if type(value) is model_type:
        try:
            return {field: getattr(value, field) for field in fields}
        except AttributeError as error:
            raise ValueError(f"{context} is missing a required field") from error
    raise ValueError(f"{context} must be an exact object or typed model")


def _exact_string(value: object, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact string")


def _exact_timestamp_origin(value: object, field_name: str) -> None:
    if type(value) not in {str, datetime}:
        raise ValueError(f"{field_name} must be an exact string or datetime")


def _exact_finite_number_origin(value: object, field_name: str) -> None:
    if type(value) not in {int, float}:
        raise ValueError(f"{field_name} must be an exact JSON number")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError(f"{field_name} must be finite") from error
    if not finite:
        raise ValueError(f"{field_name} must be finite")


def validate_forecast_point_origins(value: object) -> None:
    """Reject any point value that would require Pydantic runtime coercion."""

    fields = _origin_fields(
        value,
        ForecastPoint,
        (
            "data_time",
            "target_time",
            "horizon_step",
            "raw_forecast",
            "forecast_value",
            "is_clipped",
        ),
        "forecast point",
    )
    _exact_timestamp_origin(fields["data_time"], "data_time")
    _exact_timestamp_origin(fields["target_time"], "target_time")
    if type(fields["horizon_step"]) is not int:
        raise ValueError("horizon_step must be an exact integer")
    _exact_finite_number_origin(fields["raw_forecast"], "raw_forecast")
    _exact_finite_number_origin(fields["forecast_value"], "forecast_value")
    if type(fields["is_clipped"]) is not bool:
        raise ValueError("is_clipped must be an exact boolean")


def validate_forecast_series_origins(value: object) -> None:
    """Validate every publication-relevant raw origin in one forecast series."""

    fields = _origin_fields(
        value,
        ForecastSeries,
        (
            "unique_id",
            "unit",
            "model_name",
            "status",
            "points",
        ),
        "forecast series",
    )
    fallback_reason = (
        value.get("fallback_reason")
        if type(value) is dict
        else value.fallback_reason
    )
    for field_name in ("unique_id", "unit", "model_name", "status"):
        _exact_string(fields[field_name], field_name)
    _exact_string(fallback_reason, "fallback_reason", optional=True)
    points = fields["points"]
    if type(points) is not list:
        raise ValueError("points must be an exact list")
    for point in points:
        if type(point) not in {dict, ForecastPoint}:
            raise ValueError("points must contain exact objects or typed points")
        validate_forecast_point_origins(point)


def validate_latest_snapshot_origins(value: object) -> None:
    """Validate the complete raw origin graph of a formal forecast snapshot."""

    fields = _origin_fields(
        value,
        LatestSnapshot,
        (
            "station_id",
            "as_of",
            "generated_at",
            "source_data_end",
            "status",
            "series",
            "model_manifest",
            "content_hash",
        ),
        "latest snapshot",
    )
    for field_name in ("station_id", "status", "content_hash"):
        _exact_string(fields[field_name], field_name)
    for field_name in ("as_of", "generated_at", "source_data_end"):
        _exact_timestamp_origin(fields[field_name], field_name)
    series = fields["series"]
    if type(series) is not list:
        raise ValueError("series must be an exact list")
    for item in series:
        if type(item) not in {dict, ForecastSeries}:
            raise ValueError("series must contain exact objects or typed series")
        validate_forecast_series_origins(item)
    validate_exact_json_mapping(
        fields["model_manifest"], "model_manifest", allow_empty=True
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservationPoint(StrictModel):
    unique_id: SeriesId
    ds: datetime
    y: float | None
    quality: Literal["valid", "invalid"]
    source_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_value(self) -> "ObservationPoint":
        validate_shanghai_timestamp(self.ds, "ds", quarter_hour=True)
        if self.quality == "invalid" and self.y is not None:
            raise ValueError("invalid points require y=null")
        if self.quality == "valid":
            if self.y is None or not math.isfinite(self.y):
                raise ValueError("valid points require a finite y")
            if is_load_series(self.unique_id) and self.y < 0:
                raise ValueError("load must be non-negative")
            if not is_load_series(self.unique_id) and not 0 <= self.y <= 100:
                raise ValueError("SOC must be between 0 and 100")
        return self


class SourcePage(StrictModel):
    station_id: str
    timezone: Literal["Asia/Shanghai"]
    interval_seconds: Literal[900]
    points: list[ObservationPoint]
    next_cursor: str | None = None


class AcceptanceContext(StrictModel):
    active: bool
    acceptance_run_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None

    @model_validator(mode="after")
    def validate_active_window(self) -> "AcceptanceContext":
        values = (self.acceptance_run_id, self.window_start, self.window_end)
        if self.active:
            if any(value is None for value in values):
                raise ValueError(
                    "active acceptance context requires an ordered window and run ID"
                )
            validate_shanghai_timestamp(
                self.window_start, "window_start", quarter_hour=True
            )
            validate_shanghai_timestamp(self.window_end, "window_end", quarter_hour=True)
            if self.window_end <= self.window_start:
                raise ValueError(
                    "active acceptance context requires an ordered window and run ID"
                )
        if not self.active and any(value is not None for value in values):
            raise ValueError("inactive acceptance context must not expose a run window")
        return self


class ForecastPoint(StrictModel):
    data_time: datetime
    target_time: datetime
    horizon_step: int = Field(ge=1, le=96, strict=True)
    raw_forecast: float
    forecast_value: float
    is_clipped: bool

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        validate_forecast_point_origins(value)
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ForecastPoint":
        validate_shanghai_timestamp(self.data_time, "data_time", quarter_hour=True)
        validate_shanghai_timestamp(self.target_time, "target_time", quarter_hour=True)
        if self.target_time - self.data_time != timedelta(minutes=15):
            raise ValueError("forecast interval must be 15 minutes")
        if not math.isfinite(self.raw_forecast) or not math.isfinite(self.forecast_value):
            raise ValueError("forecast values must be finite")
        if self.is_clipped != (self.raw_forecast != self.forecast_value):
            raise ValueError("is_clipped must reflect raw and published values")
        return self


class ForecastSeries(StrictModel):
    unique_id: SeriesId
    unit: Literal["kW", "%"]
    model_name: str = Field(strict=True)
    status: Literal["ok", "warming_up", "degraded", "insufficient_history", "error"]
    points: list[ForecastPoint]
    fallback_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        validate_forecast_series_origins(value)
        return value

    @model_validator(mode="after")
    def validate_series(self) -> "ForecastSeries":
        expected_unit = "kW" if is_load_series(self.unique_id) else "%"
        if self.unit != expected_unit:
            raise ValueError("series unit does not match unique_id")
        requires_fallback_reason = self.status in {
            "degraded",
            "insufficient_history",
            "error",
        }
        if requires_fallback_reason and self.fallback_reason is None:
            raise ValueError(f"{self.status} series requires a fallback reason")
        if not requires_fallback_reason and self.fallback_reason is not None:
            raise ValueError(f"{self.status} series must not include a fallback reason")
        expected_points = 0 if self.status in {"insufficient_history", "error"} else 96
        if len(self.points) != expected_points:
            raise ValueError(f"{self.status} series requires {expected_points} points")
        for index, point in enumerate(self.points, start=1):
            if point.horizon_step != index:
                raise ValueError("horizon steps must be continuous from one")
            if index > 1 and point.data_time != self.points[index - 2].target_time:
                raise ValueError("forecast points must be contiguous")
            if is_load_series(self.unique_id) and point.forecast_value < 0:
                raise ValueError("published load must be non-negative")
            if not is_load_series(self.unique_id) and not 0 <= point.forecast_value <= 100:
                raise ValueError("published SOC must be in 0..100")
        return self


class LatestSnapshot(StrictModel):
    station_id: str
    as_of: datetime
    generated_at: datetime
    source_data_end: datetime
    status: Literal["ok", "warming_up", "degraded"]
    series: list[ForecastSeries]
    model_manifest: dict[str, object]
    content_hash: str

    @model_validator(mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        validate_latest_snapshot_origins(value)
        return value

    @model_validator(mode="after")
    def validate_terminal_series(self) -> "LatestSnapshot":
        validate_shanghai_timestamp(self.as_of, "as_of", quarter_hour=False)
        validate_shanghai_timestamp(
            self.generated_at, "generated_at", quarter_hour=False
        )
        validate_shanghai_timestamp(
            self.source_data_end, "source_data_end", quarter_hour=True
        )
        ids = [item.unique_id for item in self.series]
        if ids != list(SERIES_IDS):
            raise ValueError("latest snapshot requires load and SOC exactly once")
        return self


class JobState(StrictModel):
    job_id: str
    station_id: str
    task: Literal["forecast", "model_selection"]
    status: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None = None
