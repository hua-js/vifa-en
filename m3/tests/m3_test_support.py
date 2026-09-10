from datetime import datetime, timedelta
import math
from types import SimpleNamespace

from m3.worker.contracts import ObservationPoint


class StationKeyedObservationSource:
    """In-memory observation source whose histories are isolated by station ID."""

    def __init__(self, points_by_station: dict[str, list[ObservationPoint]]) -> None:
        self.points_by_station = {
            station_id: list(points)
            for station_id, points in points_by_station.items()
        }
        self.calls: list[SimpleNamespace] = []

    def list_observations(
        self, station_id: str, start: datetime, end: datetime
    ) -> list[ObservationPoint]:
        self.calls.append(
            SimpleNamespace(station_id=station_id, start=start, end=end)
        )
        return [
            point
            for point in self.points_by_station[station_id]
            if start <= point.ds < end
        ]

    def replace_station_load(self, station_id: str, value: float) -> None:
        self.points_by_station[station_id] = [
            point.model_copy(
                update={
                    "y": value,
                    "source_revision": point.source_revision + 1,
                }
            )
            if point.unique_id == "station_total_load"
            else point
            for point in self.points_by_station[station_id]
        ]


class RecordingForecastSink:
    """Record only completed latest-snapshot publications."""

    def __init__(self) -> None:
        self.latest_calls = []

    def publish_latest(self, snapshot):
        self.latest_calls.append(snapshot)
        return {"id": len(self.latest_calls)}


def make_quarter_hour_points(
    days: int, unique_id: str = "station_total_load"
) -> list[ObservationPoint]:
    start = datetime.fromisoformat("2026-05-27T00:00:00+08:00")
    points = []
    for index in range(days * 96):
        value = 800 + 60 * math.sin(2 * math.pi * (index % 96) / 96)
        if unique_id != "station_total_load":
            value = 50 + 10 * math.sin(2 * math.pi * (index % 96) / 96)
        points.append(
            ObservationPoint(
                unique_id=unique_id,
                ds=start + timedelta(minutes=15 * index),
                y=value,
                quality="valid",
                source_revision=1,
            )
        )
    return points


def make_cv_result():
    import pandas as pd

    ds = pd.date_range("2026-08-18T00:00:00+08:00", periods=4, freq="15min")
    return pd.DataFrame(
        {
            "unique_id": ["station_total_load"] * 4,
            "ds": ds,
            "cutoff": [pd.Timestamp("2026-08-17T23:45:00+08:00")] * 4,
            "y": [100.0, 100.0, 100.0, 100.0],
            "SeasonalNaive": [99.0, 101.0, 99.0, 101.0],
            "AutoETS": [95.0, 105.0, 95.0, 105.0],
            "AutoARIMA": [90.0, 110.0, 90.0, 110.0],
            "MSTL": [80.0, 120.0, 80.0, 120.0],
        }
    )


def make_forecast_result(model_name: str, start: datetime, values=None):
    import pandas as pd

    if values is None:
        values = [800.0] * 96
    return pd.DataFrame(
        {
            "unique_id": ["station_total_load"] * len(values),
            "ds": pd.date_range(start, periods=len(values), freq="15min"),
            model_name: values,
        }
    )


def make_m3_dashboard_series() -> list[dict]:
    """Build the exact public Task 12 two-series dashboard shape."""
    history_start = datetime.fromisoformat("2026-08-24T01:00:00+08:00")
    forecast_start = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
    definitions = (
        ("station_total_load", "kW", 800.0, "AutoETS", "ok", None),
        ("storage_soc", "%", 55.0, "AutoARIMA", "ok", None),
    )
    series = []
    for unique_id, unit, base, model_name, status, fallback_reason in definitions:
        actual = []
        for index in range(96):
            invalid = unique_id == "station_total_load" and index == 20
            actual.append(
                {
                    "data_time": (
                        history_start + timedelta(minutes=15 * index)
                    ).isoformat(),
                    "value": None if invalid else base + index / 10,
                    "quality": "invalid" if invalid else "valid",
                    "source_revision": index + 1,
                }
            )
        forecast = []
        for index in range(96):
            raw_value = (
                103.0
                if unique_id == "storage_soc" and index == 10
                else base + index / 20
            )
            value = 99.0 if unit == "%" and raw_value > 99 else raw_value
            forecast.append(
                {
                    "data_time": (
                        forecast_start + timedelta(minutes=15 * index)
                    ).isoformat(),
                    "target_time": (
                        forecast_start + timedelta(minutes=15 * (index + 1))
                    ).isoformat(),
                    "value": value,
                    "raw_value": raw_value,
                    "is_clipped": raw_value != value,
                }
            )
        series.append(
            {
                "unique_id": unique_id,
                "unit": unit,
                "model_name": model_name,
                "status": status,
                "fallback_reason": fallback_reason,
                "actual": actual,
                "forecast": forecast,
            }
        )
    return series


def build_m3_dashboard_response() -> dict:
    return {
        "status": "ok",
        "data": {
            "operation": "forecast_dashboard",
            "range": {
                "timezone": "Asia/Shanghai",
                "history_start": "2026-08-24T01:00:00+08:00",
                "history_end": "2026-08-25T01:00:00+08:00",
                "actual_latest": "2026-08-25T00:45:00+08:00",
                "forecast_start": "2026-08-25T01:00:00+08:00",
                "forecast_end": "2026-08-26T01:00:00+08:00",
                "interval_seconds": 900,
                "history_hours": 24,
                "forecast_hours": 24,
                "display_hours": 48,
                "now_separator": "2026-08-25T01:10:00+08:00",
            },
            "system": {
                "state": "ready",
                "mode": "normal",
                "generated_at": "2026-08-25T01:02:08+08:00",
                "stale": False,
            },
            "series": make_m3_dashboard_series(),
            "acceptance": {
                "acceptance_run_id": "run-20260825",
                "status": "in_progress",
                "completed_days": 1,
                "expected_days": 7,
                "results": [],
            },
        },
    }


def build_m3_dashboard_final_response() -> dict:
    response = build_m3_dashboard_response()
    response["data"]["acceptance"] = {
        "acceptance_run_id": "run-20260825",
        "status": "passed",
        "completed_days": 7,
        "expected_days": 7,
        "results": [
            {
                "unique_id": "station_total_load",
                "expected_count": 672,
                "valid_count": 650,
                "zero_actual_count": 10,
                "mape_percent": 12.5,
                "mae": 3.2,
                "smape_percent": 11.1,
                "outcome": "passed",
            },
            {
                "unique_id": "storage_soc",
                "expected_count": 672,
                "valid_count": 651,
                "zero_actual_count": 9,
                "mape_percent": 9.5,
                "mae": 1.3,
                "smape_percent": 9.1,
                "outcome": "passed",
            },
        ],
    }
    return response


def build_m3_dashboard_degraded_response() -> dict:
    """Build a Task 12 degraded dashboard with one real fallback series."""
    response = build_m3_dashboard_response()
    response["data"]["system"].update({"state": "degraded", "mode": "degraded"})
    affected = response["data"]["series"][1]
    affected.update({"status": "degraded", "fallback_reason": "champion_failed"})
    return response


def build_m3_dashboard_stale_response() -> dict:
    """Build the Task 12 stale form of a previously normal full dashboard."""
    response = build_m3_dashboard_response()
    response["data"]["range"]["now_separator"] = "2026-08-25T01:32:09+08:00"
    response["data"]["system"].update({"state": "stale", "stale": True})
    return response


def build_m3_dashboard_stale_degraded_response() -> dict:
    """Build a stale dashboard whose underlying latest snapshot was degraded."""
    response = build_m3_dashboard_degraded_response()
    response["data"]["range"]["now_separator"] = "2026-08-25T01:32:09+08:00"
    response["data"]["system"].update({"state": "stale", "stale": True})
    return response


def build_m3_dashboard_sparse_response() -> dict:
    """Build a valid Task 12 dashboard with independently sparse actual series."""
    response = build_m3_dashboard_response()
    series = response["data"]["series"]
    series[0]["actual"] = [series[0]["actual"][0], series[0]["actual"][4]]
    series[1]["actual"] = [series[1]["actual"][1]]
    response["data"]["range"]["actual_latest"] = "2026-08-24T02:00:00+08:00"
    return response


def _build_m3_empty_dashboard(state: str) -> dict:
    if state not in {"initializing", "error"}:
        raise ValueError("unsupported empty dashboard state")
    return {
        "status": "ok",
        "data": {
            "operation": "forecast_dashboard",
            "range": {
                "timezone": "Asia/Shanghai",
                "history_start": None,
                "history_end": None,
                "actual_latest": None,
                "forecast_start": None,
                "forecast_end": None,
                "interval_seconds": 900,
                "history_hours": 24,
                "forecast_hours": 24,
                "display_hours": 48,
                "now_separator": "2026-08-25T01:15:00+08:00",
            },
            "system": {
                "state": state,
                "mode": state,
                "generated_at": None,
                "stale": False,
            },
            "series": [
                {
                    "unique_id": unique_id,
                    "unit": "kW" if unique_id == "station_total_load" else "%",
                    "model_name": None,
                    "status": state,
                    "fallback_reason": None,
                    "actual": [],
                    "forecast": [],
                }
                for unique_id in (
                    "station_total_load",
                    "storage_soc",
                )
            ],
            "acceptance": None,
        },
    }


def build_m3_dashboard_initializing_response() -> dict:
    return _build_m3_empty_dashboard("initializing")


def build_m3_dashboard_error_response() -> dict:
    return _build_m3_empty_dashboard("error")
