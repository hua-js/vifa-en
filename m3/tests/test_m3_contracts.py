from collections import UserDict, UserList
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, IntEnum
import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from m3.worker.config import DashboardSettings, Settings, parse_station_bindings
from m3.worker.contracts import (
    AcceptanceContext,
    ForecastPoint,
    ForecastSeries,
    JobState,
    LatestSnapshot,
    ObservationPoint,
    SourcePage,
)
from m3.worker.errors import M3Error


POINT_TIME = "2026-08-25T00:45:00+08:00"
SERIES = (
    ("station_total_load", "kW", 800.0),
    ("storage_soc", "%", 55.0),
)


def forecast_series(unique_id: str, unit: str, _value: float) -> dict[str, object]:
    return {
        "unique_id": unique_id,
        "unit": unit,
        "model_name": "naive",
        "status": "error",
        "points": [],
        "fallback_reason": "forecast_failed",
    }


TERMINAL_ERROR_SERIES = [forecast_series(*item) for item in SERIES]


def latest_snapshot_values(*, series: list[dict[str, object]]) -> dict[str, object]:
    return {
        "station_id": "station-a",
        "as_of": POINT_TIME,
        "generated_at": POINT_TIME,
        "source_data_end": POINT_TIME,
        "status": "degraded",
        "series": series,
        "model_manifest": {},
        "content_hash": "abc123",
    }


class M3ContractTests(unittest.TestCase):
    def test_dashboard_settings_require_only_read_path_credentials(self):
        environment = {
            "M3_STATIONS_JSON": json.dumps([
                {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
                {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
            ]),
            "M3_RAW_SOURCE_URL": "https://source.example.test/api/t_es_data:list",
            "M3_RAW_SOURCE_API_TOKEN": "raw-source-secret",
            "M3_NOCOBASE_BASE_URL": "https://nocobase.example.test",
            "M3_DASHBOARD_NOCOBASE_API_KEY": "dashboard-read-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = DashboardSettings.from_env()

        self.assertEqual(settings.station_ids, ("plant-alpha-ES01", "plant-beta-ES02"))
        self.assertEqual(str(settings.raw_source_api_token), "**********")
        self.assertEqual(str(settings.nocobase_api_key), "**********")
        self.assertFalse(hasattr(settings, "source_api_token"))
        self.assertFalse(hasattr(settings, "admin_api_token"))

    def test_settings_require_every_secret_and_url(self):
        """Removing a required setting must prevent the worker from starting."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_settings_parse_exact_ordered_station_bindings(self):
        """The configured full identities retain their required public-key order."""
        environment = {
            "M3_STATIONS_JSON": json.dumps([
                {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
                {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
            ]),
            "M3_RAW_SOURCE_URL": "https://source.example.test/api/t_es_data:list",
            "M3_RAW_SOURCE_API_TOKEN": "raw-source-secret",
            "M3_SOURCE_BASE_URL": "https://source.example.test/api",
            "M3_SOURCE_API_TOKEN": "source-secret",
            "M3_NOCOBASE_BASE_URL": "https://nocobase.example.test",
            "M3_NOCOBASE_API_KEY": "nocobase-secret",
            "M3_ADMIN_API_TOKEN": "admin-secret",
            "M3_ACCEPTANCE_ENABLED": "false",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.station_ids, ("plant-alpha-ES01", "plant-beta-ES02"))
        self.assertEqual(
            [binding.station_name for binding in settings.stations], ["1# 电站", "2# 电站"]
        )
        self.assertEqual(settings.timezone, "Asia/Shanghai")
        self.assertFalse(settings.acceptance_enabled)

    def test_settings_keep_secret_values_masked(self):
        """A configuration log representation must not disclose the source token."""
        environment = {
            "M3_STATIONS_JSON": json.dumps([
                {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
                {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
            ]),
            "M3_RAW_SOURCE_URL": "https://source.example.test/api/t_es_data:list",
            "M3_RAW_SOURCE_API_TOKEN": "raw-source-secret",
            "M3_SOURCE_BASE_URL": "https://source.example.test/api",
            "M3_SOURCE_API_TOKEN": "source-secret",
            "M3_NOCOBASE_BASE_URL": "https://nocobase.example.test",
            "M3_NOCOBASE_API_KEY": "nocobase-secret",
            "M3_ADMIN_API_TOKEN": "admin-secret",
            "M3_ACCEPTANCE_ENABLED": "false",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(str(settings.source_api_token), "**********")
        self.assertEqual(str(settings.raw_source_api_token), "**********")

    def test_settings_require_an_explicit_strict_acceptance_switch(self):
        environment = {
            "M3_STATIONS_JSON": json.dumps([
                {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
                {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
            ]),
            "M3_RAW_SOURCE_URL": "https://source.example.test/api/t_es_data:list",
            "M3_RAW_SOURCE_API_TOKEN": "raw-source-secret",
            "M3_SOURCE_BASE_URL": "https://source.example.test/api",
            "M3_SOURCE_API_TOKEN": "source-secret",
            "M3_NOCOBASE_BASE_URL": "https://nocobase.example.test",
            "M3_NOCOBASE_API_KEY": "nocobase-secret",
            "M3_ADMIN_API_TOKEN": "admin-secret",
        }
        for invalid in (None, "", "0", "False", "yes", " true "):
            candidate = dict(environment)
            if invalid is not None:
                candidate["M3_ACCEPTANCE_ENABLED"] = invalid
            with self.subTest(value=invalid), patch.dict(
                os.environ, candidate, clear=True
            ):
                with self.assertRaisesRegex(ValueError, "M3_ACCEPTANCE_ENABLED"):
                    Settings.from_env()

        with patch.dict(
            os.environ,
            {**environment, "M3_ACCEPTANCE_ENABLED": "true"},
            clear=True,
        ):
            self.assertTrue(Settings.from_env().acceptance_enabled)

    def test_station_bindings_reject_any_nonexact_two_station_configuration(self):
        """Only the configured full ES01/ES02 pair and public ordering are accepted."""
        valid = [
            {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
            {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
        ]
        invalid = (
            [],
            [valid[0]],
            [valid[0], {**valid[0], "station_key": "station_2"}],
            [valid[0], {**valid[1], "station_key": "station_1"}],
            list(reversed(valid)),
            [{**valid[0], "station_key": "unknown"}, valid[1]],
            [{**valid[0], "station_name": " 1# 电站"}, valid[1]],
            [{**valid[0], "station_name": "1#\n电站"}, valid[1]],
            [{**valid[0], "station_id": "plant-alpha-ES02"}, valid[1]],
            [valid[0], {**valid[1], "station_id": "plant-beta-ES01"}],
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_station_bindings(json.dumps(value))

    def test_invalid_point_requires_null_value(self):
        """An invalid source point must never retain a measurement."""
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="station_total_load",
                ds=POINT_TIME,
                y=812.35,
                quality="invalid",
                source_revision=1,
            )

    def test_storage_soc_uses_percent_bounds(self):
        """SOC observations above 100 percent must be rejected."""
        with self.assertRaises(ValueError):
            ObservationPoint(
                unique_id="storage_soc",
                ds="2026-08-26T09:45:00+08:00",
                y=100.1,
                quality="valid",
                source_revision=1,
            )

    def test_observation_requires_timezone(self):
        """A source timestamp without an offset is invalid."""
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="station_total_load",
                ds="2026-08-25T00:45:00",
                y=1.0,
                quality="valid",
                source_revision=0,
            )

    def test_observation_requires_asia_shanghai_offset(self):
        """A UTC observation must not enter a Shanghai-only source page."""
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="station_total_load",
                ds="2026-08-25T00:45:00+00:00",
                y=1.0,
                quality="valid",
                source_revision=0,
            )

    def test_observation_requires_quarter_hour_boundary(self):
        """A source timestamp outside a 15-minute boundary is invalid."""
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="station_total_load",
                ds="2026-08-25T00:46:00+08:00",
                y=1.0,
                quality="valid",
                source_revision=0,
            )

    def test_models_reject_unknown_fields(self):
        """Unexpected upstream fields must not silently enter the typed boundary."""
        with self.assertRaises(ValidationError):
            JobState(
                job_id="job-1",
                station_id="station-a",
                task="forecast",
                status="queued",
                retry_after_seconds=15,
            )

    def test_source_page_accepts_only_quarter_hour_observations(self):
        """A page exposes its fixed collection interval and validated observations."""
        page = SourcePage(
            station_id="station-a",
            timezone="Asia/Shanghai",
            interval_seconds=900,
            points=[
                {
                    "unique_id": "station_total_load",
                    "ds": POINT_TIME,
                    "y": 100.0,
                    "quality": "valid",
                    "source_revision": 0,
                }
            ],
        )

        self.assertEqual(page.points[0].y, 100.0)

    def test_acceptance_context_requires_complete_ordered_window_when_active(self):
        """An active acceptance run must identify a non-empty time window."""
        with self.assertRaises(ValidationError):
            AcceptanceContext(
                active=True,
                acceptance_run_id="run-1",
                window_start="2026-08-25T01:00:00+08:00",
                window_end="2026-08-25T01:00:00+08:00",
            )

    def test_acceptance_context_requires_asia_shanghai_offsets(self):
        """An active acceptance window cannot be expressed in a non-local offset."""
        for window_start, window_end in (
            ("2026-08-25T01:00:00+00:00", "2026-08-25T01:15:00+00:00"),
            ("2026-08-25T01:00:00+08:00", "2026-08-25T09:15:00+00:00"),
        ):
            with self.subTest(window_start=window_start), self.assertRaises(ValidationError):
                AcceptanceContext(
                    active=True,
                    acceptance_run_id="run-1",
                    window_start=window_start,
                    window_end=window_end,
                )

    def test_acceptance_context_requires_quarter_hour_boundaries(self):
        """An active acceptance window cannot start between source intervals."""
        for window_start, window_end in (
            ("2026-08-25T01:01:00+08:00", "2026-08-25T01:16:00+08:00"),
            ("2026-08-25T01:00:00+08:00", "2026-08-25T01:16:00+08:00"),
        ):
            with self.subTest(window_start=window_start), self.assertRaises(ValidationError):
                AcceptanceContext(
                    active=True,
                    acceptance_run_id="run-1",
                    window_start=window_start,
                    window_end=window_end,
                )

    def test_forecast_point_requires_a_15_minute_interval(self):
        """A forecast target must be exactly one collection interval later."""
        with self.assertRaises(ValidationError):
            ForecastPoint(
                data_time="2026-08-25T00:00:00+08:00",
                target_time="2026-08-25T00:30:00+08:00",
                horizon_step=1,
                raw_forecast=2.0,
                forecast_value=1.0,
                is_clipped=True,
            )

    def test_forecast_point_requires_an_exact_integer_horizon(self):
        """Python bool must not pass as horizon one at the contract boundary."""
        with self.assertRaises(ValidationError):
            ForecastPoint(
                data_time="2026-08-25T00:00:00+08:00",
                target_time="2026-08-25T00:15:00+08:00",
                horizon_step=True,
                raw_forecast=2.0,
                forecast_value=2.0,
                is_clipped=False,
            )

        point = ForecastPoint(
            data_time="2026-08-25T00:00:00+08:00",
            target_time="2026-08-25T00:15:00+08:00",
            horizon_step=1,
            raw_forecast=2.0,
            forecast_value=2.0,
            is_clipped=False,
        )
        self.assertEqual(point.horizon_step, 1)

    def test_forecast_point_rejects_every_coercible_published_origin(self):
        """No nested point value may change runtime type while becoming formal evidence."""
        class Timestamp(str, Enum):
            DATA = "2026-08-25T00:00:00+08:00"
            TARGET = "2026-08-25T00:15:00+08:00"

        class Number(IntEnum):
            TWO = 2

        base = {
            "data_time": "2026-08-25T00:00:00+08:00",
            "target_time": "2026-08-25T00:15:00+08:00",
            "horizon_step": 1,
            "raw_forecast": 2.0,
            "forecast_value": 2.0,
            "is_clipped": False,
        }
        cases = (
            {**base, "data_time": Timestamp.DATA},
            {**base, "target_time": Timestamp.TARGET},
            {**base, "horizon_step": 1.0},
            {**base, "raw_forecast": Decimal("2")},
            {**base, "forecast_value": Decimal("2")},
            {**base, "raw_forecast": Number.TWO},
            {**base, "forecast_value": Number.TWO},
            {**base, "raw_forecast": True, "forecast_value": 1.0},
            {**base, "raw_forecast": 1.0, "forecast_value": True},
            {**base, "is_clipped": 0},
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ForecastPoint.model_validate(payload)

        ordinary_integer = ForecastPoint(**{**base, "raw_forecast": 2})
        self.assertEqual(ordinary_integer.raw_forecast, 2.0)

    def test_forecast_point_requires_asia_shanghai_offsets(self):
        """Forecast timestamps with UTC offsets must not be accepted as local points."""
        for data_time, target_time in (
            ("2026-08-25T00:00:00+00:00", "2026-08-25T08:15:00+08:00"),
            ("2026-08-25T00:00:00+08:00", "2026-08-24T16:15:00+00:00"),
        ):
            with self.subTest(data_time=data_time), self.assertRaises(ValidationError):
                ForecastPoint(
                    data_time=data_time,
                    target_time=target_time,
                    horizon_step=1,
                    raw_forecast=2.0,
                    forecast_value=2.0,
                    is_clipped=False,
                )

    def test_forecast_point_requires_quarter_hour_boundaries(self):
        """Forecast timestamps cannot preserve a 15-minute interval off-grid."""
        for data_time, target_time in (
            ("2026-08-25T00:01:00+08:00", "2026-08-25T00:16:00+08:00"),
            ("2026-08-25T00:00:00+08:00", "2026-08-25T00:15:01+08:00"),
        ):
            with self.subTest(data_time=data_time), self.assertRaises(ValidationError):
                ForecastPoint(
                    data_time=data_time,
                    target_time=target_time,
                    horizon_step=1,
                    raw_forecast=2.0,
                    forecast_value=2.0,
                    is_clipped=False,
                )

    def test_forecast_point_requires_truthful_clipping(self):
        """A changed published forecast must declare that it was clipped."""
        with self.assertRaises(ValidationError):
            ForecastPoint(
                data_time="2026-08-25T00:00:00+08:00",
                target_time="2026-08-25T00:15:00+08:00",
                horizon_step=1,
                raw_forecast=2.0,
                forecast_value=1.0,
                is_clipped=False,
            )

    def test_forecast_series_requires_expected_unit(self):
        """A load series must be identified as kW rather than percent."""
        with self.assertRaises(ValidationError):
            ForecastSeries(
                unique_id="station_total_load",
                unit="%",
                model_name="naive",
                status="ok",
                points=[],
            )

    def test_forecast_series_rejects_coercible_enum_model_name(self):
        """A string-valued Enum must not be normalized into persisted model identity."""
        class ModelName(str, Enum):
            SEASONAL_NAIVE = "SeasonalNaive"

        with self.assertRaises(ValidationError):
            ForecastSeries(
                unique_id="station_total_load",
                unit="kW",
                model_name=ModelName.SEASONAL_NAIVE,
                status="error",
                points=[],
            )

    def test_forecast_series_rejects_all_coercible_identity_and_point_containers(self):
        """Series identity, optional reason, and point list retain exact JSON origins."""
        class Text(str, Enum):
            UNIQUE_ID = "station_total_load"
            UNIT = "kW"
            STATUS = "error"
            FALLBACK = "fallback"

        base = {
            "unique_id": "station_total_load",
            "unit": "kW",
            "model_name": "SeasonalNaive",
            "status": "error",
            "points": [],
            "fallback_reason": None,
        }
        cases = (
            {**base, "unique_id": Text.UNIQUE_ID},
            {**base, "unit": Text.UNIT},
            {**base, "status": Text.STATUS},
            {**base, "fallback_reason": Text.FALLBACK},
            {**base, "points": ()},
            {**base, "points": UserList()},
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ForecastSeries.model_validate(payload)

    def test_latest_snapshot_requires_exact_series_and_manifest_runtime_types(self):
        """Tuple series and custom/non-exact manifest containers cannot be coerced."""
        base = {
            "station_id": "station-1",
            "as_of": "2026-08-25T01:02:00+08:00",
            "generated_at": "2026-08-25T01:02:05+08:00",
            "source_data_end": "2026-08-25T01:00:00+08:00",
            "status": "ok",
            "series": TERMINAL_ERROR_SERIES,
            "model_manifest": {"version": "2.1.1"},
            "content_hash": "hash",
        }
        cases = (
            {**base, "series": tuple(TERMINAL_ERROR_SERIES)},
            {**base, "model_manifest": UserDict({"version": "2.1.1"})},
            {**base, "model_manifest": {"nested": ("not", "json")}},
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                LatestSnapshot.model_validate(payload)

    def test_latest_snapshot_rejects_coercible_identity_and_timestamp_origins(self):
        """Snapshot identity and publication timestamps cannot be normalized from subclasses."""
        class Text(str, Enum):
            STATION = "station-1"
            AS_OF = "2026-08-25T01:02:00+08:00"
            GENERATED = "2026-08-25T01:02:05+08:00"
            SOURCE_END = "2026-08-25T01:00:00+08:00"
            STATUS = "ok"
            HASH = "hash"

        base = {
            "station_id": "station-1",
            "as_of": "2026-08-25T01:02:00+08:00",
            "generated_at": "2026-08-25T01:02:05+08:00",
            "source_data_end": "2026-08-25T01:00:00+08:00",
            "status": "ok",
            "series": TERMINAL_ERROR_SERIES,
            "model_manifest": {"version": "2.1.1"},
            "content_hash": "hash",
        }
        cases = tuple(
            {**base, field: value}
            for field, value in (
                ("station_id", Text.STATION),
                ("as_of", Text.AS_OF),
                ("generated_at", Text.GENERATED),
                ("source_data_end", Text.SOURCE_END),
                ("status", Text.STATUS),
                ("content_hash", Text.HASH),
            )
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                LatestSnapshot.model_validate(payload)

    def test_forecast_series_requires_96_points_when_ok(self):
        """An otherwise valid active series cannot omit its forecast horizon."""
        with self.assertRaises(ValidationError):
            ForecastSeries(
                unique_id="station_total_load",
                unit="kW",
                model_name="naive",
                status="ok",
                points=[],
            )

    def test_forecast_series_requires_status_appropriate_fallback_reason(self):
        """Error states need a safe reason while normal states must not expose one."""
        start = datetime.fromisoformat("2026-08-25T00:00:00+08:00")
        points = [
            ForecastPoint(
                data_time=start + timedelta(minutes=15 * index),
                target_time=start + timedelta(minutes=15 * (index + 1)),
                horizon_step=index + 1,
                raw_forecast=800.0,
                forecast_value=800.0,
                is_clipped=False,
            )
            for index in range(96)
        ]
        for status, series_points, fallback_reason in (
            ("error", [], None),
            ("insufficient_history", [], None),
            ("degraded", points, None),
            ("warming_up", points, "unexpected"),
            ("ok", points, "unexpected"),
        ):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                ForecastSeries(
                    unique_id="station_total_load",
                    unit="kW",
                    model_name="naive",
                    status=status,
                    points=series_points,
                    fallback_reason=fallback_reason,
                )

    def test_latest_snapshot_requires_each_terminal_series_once(self):
        """A published snapshot must include load and SOC exactly once in order."""
        values = latest_snapshot_values(
            series=[forecast_series(*item) for item in SERIES]
        )
        snapshot = LatestSnapshot(**values)
        self.assertEqual(
            [item.unique_id for item in snapshot.series],
            ["station_total_load", "storage_soc"],
        )
        for invalid in (
            [forecast_series(*SERIES[0])],
            [forecast_series(*SERIES[0]), forecast_series(*SERIES[0])],
            [forecast_series(*SERIES[0]), forecast_series("storage_1_soc", "%", 55.0)],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                LatestSnapshot(**{**values, "series": invalid})

    def test_latest_snapshot_requires_asia_shanghai_offsets(self):
        """Every snapshot business timestamp must carry the local UTC+08:00 offset."""
        for field_name in ("as_of", "generated_at", "source_data_end"):
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                values = self.snapshot_values()
                values[field_name] = "2026-08-25T00:45:00+00:00"
                LatestSnapshot(**values)

    def test_latest_snapshot_requires_source_data_end_on_quarter_hour(self):
        """The latest source point cannot be published from an off-grid timestamp."""
        values = self.snapshot_values()
        values["source_data_end"] = "2026-08-25T00:46:00+08:00"

        with self.assertRaises(ValidationError):
            LatestSnapshot(**values)

    def test_latest_snapshot_allows_scheduled_as_of_and_generated_at_off_grid(self):
        """Scheduled publication timestamps may be off-grid while source data remains aligned."""
        values = self.snapshot_values()
        values["as_of"] = "2026-08-25T00:02:00+08:00"
        values["generated_at"] = "2026-08-25T00:02:30+08:00"

        snapshot = LatestSnapshot(**values)

        self.assertEqual(snapshot.as_of.minute, 2)
        self.assertEqual(snapshot.generated_at.second, 30)

    def test_structured_error_exposes_only_its_public_contract(self):
        """Callers receive an error code, safe message, and explicitly supplied details."""
        error = M3Error("source_unavailable", "Source unavailable", {"station_id": "station-a"})

        self.assertEqual(
            error.public_dict(),
            {
                "code": "source_unavailable",
                "message": "Source unavailable",
                "details": {"station_id": "station-a"},
            },
        )

    @staticmethod
    def snapshot_values() -> dict[str, object]:
        return latest_snapshot_values(series=TERMINAL_ERROR_SERIES)
