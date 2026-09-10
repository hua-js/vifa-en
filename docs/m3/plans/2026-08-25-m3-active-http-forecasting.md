# M3 Active HTTP Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M3 service that actively pulls normalized station load/SOC observations over HTTP, forecasts all three series with StatsForecast, writes the latest snapshot and seven-day acceptance evidence to NocoBase over HTTP, and renders the result in a standalone HTML dashboard.

**Architecture:** A single-instance Python Worker owns scheduling and orchestration. Its HTTP clients pull the Node-RED Source API and write NocoBase Resource API actions, while pure domain modules prepare time series, run StatsForecast, enforce physical bounds, and evaluate MAPE. Node-RED remains the manufacturer-field adapter and same-origin browser gateway; the browser never calls the Worker or NocoBase directly.

**Tech Stack:** Python 3.12, StatsForecast 2.1.1, Pandas 2.3.3, NumPy 2.4.6, FastAPI 0.141.1, Pydantic 2.13.4, HTTPX 0.28.1, Uvicorn 0.52.3, `unittest`, Node.js 22, Node-RED flow JSON, vanilla HTML/CSS/SVG, Playwright.

**Spec:** `m3/M3 场站未来能耗预测设计.md`

## Global Constraints

- Forecast exactly `station_total_load`, `storage_1_soc`, and `storage_2_soc`; do not add PV, branch-load, aggregate-SOC, MLForecast, NeuralForecast, or exogenous features.
- Use 15-minute `ds` bucket starts, `h=96`, `freq="15min"`, and timezone `Asia/Shanghai`.
- Use only `SeasonalNaive(96)`, `AutoETS(96)`, `AutoARIMA(96)`, and `MSTL([96, 672], trend_forecaster=AutoARIMA())` for full model selection.
- Use temporal cross-validation with `h=96`, `step_size=96`, and `n_windows=7`; never random-split time series.
- Use standard MAPE; exclude zero actuals from MAPE and count them separately. Require at least 605 of 672 valid non-zero points for an acceptance result.
- Keep original actuals and acceptance data unchanged. Only the training copy may linearly interpolate gaps of one or two buckets.
- Clip published load to `>=0` and SOC to `0..100`; retain raw forecast and `is_clipped`.
- Pull data from Node-RED Source API and write all results through NocoBase HTTP. The Worker must not directly connect to PostgreSQL or interpret manufacturer fields.
- Store only one latest rolling snapshot per station; do not accumulate rolling forecast history. Store the fixed `01:02` acceptance batches for seven days.
- Automatic work is driven by the Worker at `00:30`, `01:02`, and minute `02/17/32/47`; Node-RED must not trigger Worker jobs.
- Run one scheduler instance. Production uses one Uvicorn worker until a cross-instance lease is implemented.
- Use HTTPS across hosts; plain HTTP is allowed only on loopback or a controlled container network.
- FastAPI uses typed Pydantic models, router-level dependencies, `Annotated`, explicit return types, and no `RootModel` or ellipsis defaults.
- Secrets are loaded from environment variables, represented as `SecretStr`, and never logged or returned.
- The workspace is not a Git repository, so each task ends with focused tests and inspection checkpoints instead of commit commands.
- Production device IDs, manufacturer field mappings, URLs, and credentials remain deployment inputs. Missing values must fail closed; code must not guess them.

## File Structure

- Create `pyproject.toml` and generated `uv.lock`: isolated, reproducible M3 runtime and test environment.
- Create `m3/worker/config.py`: environment-only deployment settings.
- Create `m3/worker/errors.py`: safe structured domain/application errors.
- Create `m3/worker/contracts.py`: Pydantic HTTP and persistence contracts.
- Create `m3/worker/domain/training_data.py`: long-table validation, limited interpolation, continuous-segment selection, and cold-start classification.
- Create `m3/worker/domain/evaluation.py`: MAPE/MAE/SMAPE and acceptance outcomes.
- Create `m3/worker/domain/forecasting.py`: StatsForecast candidate construction, CV selection, forecasting, fallback, and clipping.
- Create `m3/worker/clients/http.py`: bounded retry policy and safe HTTP execution.
- Create `m3/worker/clients/source_api.py`: paginated Source API reads.
- Create `m3/worker/clients/nocobase_api.py`: one function per NocoBase Resource API operation.
- Create `m3/worker/services/station_cache.py`: per-station 90-day in-memory state and revision-aware merges.
- Create `m3/worker/services/forecast_service.py`: active pull, model selection, rolling forecast, latest publication, and state transitions.
- Create `m3/worker/services/acceptance_service.py`: baseline publication, actual backfill, evaluation, and interrupted-write reconciliation.
- Create `m3/worker/services/job_service.py`: bounded in-memory manual-run state and execution.
- Create `m3/worker/sinks/forecast_sink.py`: latest snapshot upsert and recoverable `writing -> complete` protocol.
- Create `m3/worker/scheduler/runner.py`: deterministic due-slot calculation and single-process background loop.
- Create `m3/worker/api/models.py`, `dependencies.py`, and `routes.py`: health, state, and authorized manual reruns only.
- Create `m3/worker/main.py`: app lifespan, resource assembly, and scheduler startup/shutdown.
- Create `m3/contracts/nocobase_collections.json`: exact logical collection contract.
- Create `m3/node_red/forecast_contract.js` and `m3/node_red/energy_forecast_flow.json`: normalized Source API and same-origin dashboard gateway.
- Create `docs/m3/部署说明.md`: environment variables, NocoBase ACL, Node-RED import/wiring, process command, and acceptance gates.
- Create `场站未来能耗预测.html`: independent M3 dashboard.
- Create focused `tests/test_m3_*.py`, `m3/tests/m3_test_support.py`, `tests/test_m3_node_red_contract.js`, and `m3/tests/m3_dashboard_e2e.js`.
- Leave `m2/station_energy_backend.py`, `m2/station_efficiency_history.py`, and `m2/web/场站三条能效链路能流图.html` behavior unchanged.

---

### Task 1: Reproducible M3 Runtime

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock` by running `uv lock`
- Create: `m3/worker/__init__.py`
- Create: package marker files under `m3/worker/api`, `clients`, `domain`, `scheduler`, `services`, and `sinks`
- Test: `m3/tests/test_m3_environment.py`

**Interfaces:**
- Consumes: Python 3.12 and the existing `uv` executable.
- Produces: an isolated `.venv`, locked dependency graph, and `m3.worker.__version__ == "0.1.0"`.

- [ ] **Step 1: Write the failing environment test**

```python
import importlib.metadata
import unittest

import m3.worker


class M3EnvironmentTests(unittest.TestCase):
    def test_locked_runtime_versions(self):
        self.assertEqual(m3.worker.__version__, "0.1.0")
        self.assertEqual(importlib.metadata.version("statsforecast"), "2.1.1")
        self.assertEqual(importlib.metadata.version("fastapi"), "0.141.1")
        self.assertEqual(importlib.metadata.version("httpx"), "0.28.1")
```

- [ ] **Step 2: Run the test and verify the red state**

Run: `python3 -m unittest m3.tests.test_m3_environment -v`

Expected: import error because `m3.worker` does not exist.

- [ ] **Step 3: Add the package metadata and pinned dependencies**

```toml
[project]
name = "vifa-m3-worker"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "fastapi==0.141.1",
  "httpx==0.28.1",
  "numpy==2.4.6",
  "pandas==2.3.3",
  "pydantic==2.13.4",
  "statsforecast==2.1.1",
  "uvicorn==0.52.3",
]

[tool.fastapi]
entrypoint = "m3.worker.main:app"

[tool.unittest]
start-directory = "tests"
```

Add to `m3/worker/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Resolve the lock file and run the test in the isolated environment**

Run:

```bash
uv lock
uv sync
uv run python -m unittest m3.tests.test_m3_environment -v
```

Expected: one passing test and `uv.lock` records StatsForecast 2.1.1.

- [ ] **Step 5: Run existing Python regressions inside the new environment**

Run: `uv run python -m unittest m2.tests.test_station_energy_backend m2.tests.test_station_efficiency_history -v`

Expected: all existing M1/M2 tests pass unchanged.

---

### Task 2: Configuration, Errors, and Typed Contracts

**Files:**
- Create: `m3/worker/config.py`
- Create: `m3/worker/errors.py`
- Create: `m3/worker/contracts.py`
- Create: `m3/tests/test_m3_contracts.py`

**Interfaces:**
- Consumes: environment variables `M3_STATION_IDS`, `M3_SOURCE_BASE_URL`, `M3_SOURCE_API_TOKEN`, `M3_NOCOBASE_BASE_URL`, `M3_NOCOBASE_API_KEY`, `M3_ADMIN_API_TOKEN`, and optional `M3_TIMEZONE`.
- Produces: `Settings.from_env() -> Settings`, `M3Error`, `ObservationPoint`, `SourcePage`, `AcceptanceContext`, `ForecastPoint`, `ForecastSeries`, `LatestSnapshot`, and `JobState`.

- [ ] **Step 1: Write failing tests for fail-closed configuration and observation validation**

```python
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from m3.worker.config import Settings
from m3.worker.contracts import ObservationPoint


class M3ContractTests(unittest.TestCase):
    def test_settings_require_every_secret_and_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_invalid_point_requires_null_value(self):
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="station_total_load",
                ds="2026-08-25T00:45:00+08:00",
                y=812.35,
                quality="invalid",
                source_revision=1,
            )

    def test_soc_rejects_out_of_range_value(self):
        with self.assertRaises(ValidationError):
            ObservationPoint(
                unique_id="storage_1_soc",
                ds="2026-08-25T00:45:00+08:00",
                y=101,
                quality="valid",
                source_revision=1,
            )
```

- [ ] **Step 2: Run the tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_contracts -v`

Expected: import errors for the three new modules.

- [ ] **Step 3: Implement safe settings and structured errors**

```python
# m3/worker/config.py
from dataclasses import dataclass
import os
from pydantic import HttpUrl, SecretStr, TypeAdapter


@dataclass(frozen=True)
class Settings:
    station_ids: tuple[str, ...]
    source_base_url: HttpUrl
    source_api_token: SecretStr
    nocobase_base_url: HttpUrl
    nocobase_api_key: SecretStr
    admin_api_token: SecretStr
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls) -> "Settings":
        required = (
            "M3_STATION_IDS", "M3_SOURCE_BASE_URL", "M3_SOURCE_API_TOKEN",
            "M3_NOCOBASE_BASE_URL", "M3_NOCOBASE_API_KEY", "M3_ADMIN_API_TOKEN",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(f"missing required M3 settings: {','.join(missing)}")
        ids = tuple(value.strip() for value in os.environ["M3_STATION_IDS"].split(",") if value.strip())
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("M3_STATION_IDS must contain unique non-empty IDs")
        timezone = os.environ.get("M3_TIMEZONE", "Asia/Shanghai")
        if timezone != "Asia/Shanghai":
            raise ValueError("M3_TIMEZONE must be Asia/Shanghai")
        url_adapter = TypeAdapter(HttpUrl)
        return cls(
            station_ids=ids,
            source_base_url=url_adapter.validate_python(os.environ["M3_SOURCE_BASE_URL"]),
            source_api_token=SecretStr(os.environ["M3_SOURCE_API_TOKEN"]),
            nocobase_base_url=url_adapter.validate_python(os.environ["M3_NOCOBASE_BASE_URL"]),
            nocobase_api_key=SecretStr(os.environ["M3_NOCOBASE_API_KEY"]),
            admin_api_token=SecretStr(os.environ["M3_ADMIN_API_TOKEN"]),
            timezone=timezone,
        )
```

```python
# m3/worker/errors.py
class M3Error(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}
```

- [ ] **Step 4: Implement strict Pydantic contracts**

Use normal required annotations, not ellipsis defaults:

```python
# m3/worker/contracts.py
from datetime import datetime, timedelta
from typing import Literal
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator

SeriesId = Literal["station_total_load", "storage_1_soc", "storage_2_soc"]


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
        if self.ds.tzinfo is None or self.ds.utcoffset() is None:
            raise ValueError("ds must include a timezone")
        if self.ds.minute % 15 or self.ds.second or self.ds.microsecond:
            raise ValueError("ds must be on a 15-minute boundary")
        if self.quality == "invalid" and self.y is not None:
            raise ValueError("invalid points require y=null")
        if self.quality == "valid":
            if self.y is None or not math.isfinite(self.y):
                raise ValueError("valid points require a finite y")
            if self.unique_id == "station_total_load" and self.y < 0:
                raise ValueError("load must be non-negative")
            if self.unique_id != "station_total_load" and not 0 <= self.y <= 100:
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
        if self.active and (any(value is None for value in values) or self.window_end <= self.window_start):
            raise ValueError("active acceptance context requires an ordered window and run ID")
        if not self.active and any(value is not None for value in values):
            raise ValueError("inactive acceptance context must not expose a run window")
        return self


class ForecastPoint(StrictModel):
    data_time: datetime
    target_time: datetime
    horizon_step: int = Field(ge=1, le=96)
    raw_forecast: float
    forecast_value: float
    is_clipped: bool

    @model_validator(mode="after")
    def validate_interval(self) -> "ForecastPoint":
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.data_time, self.target_time)):
            raise ValueError("forecast times require timezone offsets")
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
    model_name: str
    status: Literal["ok", "warming_up", "degraded", "insufficient_history", "error"]
    points: list[ForecastPoint]
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def validate_series(self) -> "ForecastSeries":
        expected_unit = "kW" if self.unique_id == "station_total_load" else "%"
        if self.unit != expected_unit:
            raise ValueError("series unit does not match unique_id")
        expected_points = 0 if self.status in {"insufficient_history", "error"} else 96
        if len(self.points) != expected_points:
            raise ValueError(f"{self.status} series requires {expected_points} points")
        for index, point in enumerate(self.points, start=1):
            if point.horizon_step != index:
                raise ValueError("horizon steps must be continuous from one")
            if index > 1 and point.data_time != self.points[index - 2].target_time:
                raise ValueError("forecast points must be contiguous")
            if self.unique_id == "station_total_load" and point.forecast_value < 0:
                raise ValueError("published load must be non-negative")
            if self.unique_id != "station_total_load" and not 0 <= point.forecast_value <= 100:
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

    @model_validator(mode="after")
    def validate_terminal_series(self) -> "LatestSnapshot":
        ids = [item.unique_id for item in self.series]
        if len(ids) != 3 or set(ids) != {"station_total_load", "storage_1_soc", "storage_2_soc"}:
            raise ValueError("latest snapshot requires exactly three unique series")
        return self


class JobState(StrictModel):
    job_id: str
    station_id: str
    task: Literal["forecast", "model_selection"]
    status: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None = None
```

- [ ] **Step 5: Run contract tests and inspect secret handling**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_contracts -v
rg -n 'print\(|logger\..*token|SecretStr.*get_secret_value' m3.worker
```

Expected: tests pass; secret extraction appears only at HTTP Authorization-header construction added in later tasks.

---

### Task 3: Training-Series Preparation and Cold Start

**Files:**
- Create: `m3/worker/domain/training_data.py`
- Create: `m3/tests/m3_test_support.py`
- Create: `m3/tests/test_m3_training_data.py`

**Interfaces:**
- Consumes: `list[ObservationPoint]`, explicit `as_of`, and a configured `SeriesId`.
- Produces: `TrainingDataset(frame: pandas.DataFrame, imputed_keys: frozenset[tuple[str, datetime]], start: datetime, end: datetime, mode: str)` and `build_training_dataset(...)`.

- [ ] **Step 1: Add deterministic fixtures and failing boundary tests**

```python
# m3/tests/m3_test_support.py
from datetime import datetime, timedelta
import math
from m3.worker.contracts import ObservationPoint


def make_quarter_hour_points(days: int, unique_id: str = "station_total_load") -> list[ObservationPoint]:
    start = datetime.fromisoformat("2026-05-27T00:00:00+08:00")
    points = []
    for index in range(days * 96):
        value = 800 + 60 * math.sin(2 * math.pi * (index % 96) / 96)
        if unique_id != "station_total_load":
            value = 50 + 10 * math.sin(2 * math.pi * (index % 96) / 96)
        points.append(ObservationPoint(
            unique_id=unique_id,
            ds=start + timedelta(minutes=15 * index),
            y=value,
            quality="valid",
            source_revision=1,
        ))
    return points
```

```python
# m3/tests/test_m3_training_data.py
import unittest
from m3.worker.domain.training_data import build_training_dataset
from m3.tests.m3_test_support import make_quarter_hour_points


class TrainingDataTests(unittest.TestCase):
    def test_two_bucket_gap_is_imputed_only_in_training_copy(self):
        points = make_quarter_hour_points(28)
        original = [point.model_copy(deep=True) for point in points]
        points[100] = points[100].model_copy(update={"quality": "invalid", "y": None})
        points[101] = points[101].model_copy(update={"quality": "invalid", "y": None})
        dataset = build_training_dataset(points, "station_total_load")
        self.assertEqual(len(dataset.imputed_keys), 2)
        self.assertEqual(points[100].quality, "invalid")
        self.assertIsNone(points[100].y)
        self.assertIsNotNone(original[100].y)
        self.assertEqual(dataset.mode, "full")

    def test_three_bucket_gap_selects_continuous_tail(self):
        points = make_quarter_hour_points(35)
        for index in (600, 601, 602):
            points[index] = points[index].model_copy(update={"quality": "invalid", "y": None})
        dataset = build_training_dataset(points, "station_total_load")
        self.assertGreater(dataset.start, points[602].ds)

    def test_history_modes_are_exact(self):
        self.assertEqual(build_training_dataset(make_quarter_hour_points(6), "station_total_load").mode, "insufficient")
        self.assertEqual(build_training_dataset(make_quarter_hour_points(7), "station_total_load").mode, "warming_up")
        self.assertEqual(build_training_dataset(make_quarter_hour_points(28), "station_total_load").mode, "full")
```

- [ ] **Step 2: Run the tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_training_data -v`

Expected: import error for `training_data.py`.

- [ ] **Step 3: Implement immutable preparation and limited interpolation**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
import pandas as pd
from m3.worker.contracts import ObservationPoint, SeriesId


@dataclass(frozen=True)
class TrainingDataset:
    frame: pd.DataFrame
    imputed_keys: frozenset[tuple[str, datetime]]
    start: datetime
    end: datetime
    mode: str


def _history_mode(point_count: int) -> str:
    days = point_count / 96
    if days < 7:
        return "insufficient"
    if days < 28:
        return "warming_up"
    return "full"


def build_training_dataset(points: list[ObservationPoint], unique_id: SeriesId) -> TrainingDataset:
    selected = sorted((point for point in points if point.unique_id == unique_id), key=lambda point: point.ds)
    if not selected:
        raise ValueError(f"no observations for {unique_id}")
    if len({point.ds for point in selected}) != len(selected):
        raise ValueError("duplicate observation times")
    rows = [{"unique_id": unique_id, "ds": point.ds, "y": point.y if point.quality == "valid" else None} for point in selected]
    frame = pd.DataFrame(rows).set_index("ds").asfreq("15min")
    missing = frame["y"].isna()
    groups = (missing != missing.shift()).cumsum()
    long_gap_ends = [group.index[-1] for _, group in frame[missing].groupby(groups[missing]) if len(group) > 2]
    if long_gap_ends:
        frame = frame.loc[max(long_gap_ends) + timedelta(minutes=15):]
    before = frame["y"].isna()
    frame["y"] = frame["y"].interpolate(method="time", limit=2, limit_area="inside")
    after = frame["y"].isna()
    frame = frame.loc[~after].reset_index()
    frame["unique_id"] = unique_id
    imputed_times = set(frame.loc[frame["ds"].isin(before[before].index), "ds"])
    return TrainingDataset(
        frame=frame[["unique_id", "ds", "y"]],
        imputed_keys=frozenset((unique_id, value.to_pydatetime()) for value in imputed_times),
        start=frame["ds"].iloc[0].to_pydatetime(),
        end=frame["ds"].iloc[-1].to_pydatetime(),
        mode=_history_mode(len(frame)),
    )
```

- [ ] **Step 4: Run focused tests and validate no source mutation path exists**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_training_data -v
rg -n 'point\.y\s*=|quality\s*=' m3/worker/domain/training_data.py
```

Expected: all tests pass; the inspection finds no mutation assignment to source model fields.

---

### Task 4: Evaluation Metrics and Acceptance Outcome

**Files:**
- Create: `m3/worker/domain/evaluation.py`
- Create: `m3/tests/test_m3_evaluation.py`

**Interfaces:**
- Consumes: aligned `actual`, `forecast`, and `quality` sequences.
- Produces: `MetricResult`, `evaluate_series(...)`, and `overall_outcome(results)`.

- [ ] **Step 1: Write failing tests for zero actuals, the 605 threshold, and three-series outcome**

```python
import unittest
from m3.worker.domain.evaluation import evaluate_series, overall_outcome


class M3EvaluationTests(unittest.TestCase):
    def test_zero_actual_is_excluded_and_counted(self):
        result = evaluate_series([0.0, 100.0], [50.0, 90.0], ["valid", "valid"], minimum_valid=1)
        self.assertEqual(result.zero_actual_count, 1)
        self.assertEqual(result.valid_count, 1)
        self.assertAlmostEqual(result.mape_percent, 10.0)

    def test_604_is_insufficient_and_605_can_pass(self):
        insufficient = evaluate_series([100.0] * 604, [90.0] * 604, ["valid"] * 604)
        passing = evaluate_series([100.0] * 605, [90.0] * 605, ["valid"] * 605)
        self.assertEqual(insufficient.outcome, "insufficient_data")
        self.assertEqual(passing.outcome, "passed")

    def test_one_failed_series_fails_overall(self):
        passed = evaluate_series([100.0] * 605, [90.0] * 605, ["valid"] * 605)
        failed = evaluate_series([100.0] * 605, [60.0] * 605, ["valid"] * 605)
        self.assertEqual(overall_outcome([passed, passed, failed]), "failed")
```

- [ ] **Step 2: Run the test and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_evaluation -v`

Expected: import error for `evaluation.py`.

- [ ] **Step 3: Implement the exact metric formulas**

```python
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MetricResult:
    expected_count: int
    valid_count: int
    zero_actual_count: int
    mape_percent: float | None
    mae: float | None
    smape_percent: float | None
    outcome: str


def evaluate_series(actual, forecast, quality, minimum_valid: int = 605) -> MetricResult:
    if not (len(actual) == len(forecast) == len(quality)):
        raise ValueError("metric inputs must have equal length")
    valid_pairs = [
        (float(a), float(f)) for a, f, q in zip(actual, forecast, quality, strict=True)
        if q == "valid" and a is not None and f is not None and math.isfinite(a) and math.isfinite(f)
    ]
    zero_count = sum(a == 0 for a, _ in valid_pairs)
    scored = [(a, f) for a, f in valid_pairs if a != 0]
    if not scored:
        return MetricResult(len(actual), 0, zero_count, None, None, None, "insufficient_data")
    ape = [abs(a - f) / abs(a) for a, f in scored]
    absolute = [abs(a - f) for a, f in scored]
    smape = [200 * abs(a - f) / (abs(a) + abs(f)) for a, f in scored if abs(a) + abs(f) > 0]
    mape = 100 * sum(ape) / len(ape)
    outcome = "insufficient_data" if len(scored) < minimum_valid else ("passed" if mape <= 30 else "failed")
    return MetricResult(len(actual), len(scored), zero_count, mape, sum(absolute) / len(absolute), sum(smape) / len(smape), outcome)


def overall_outcome(results: list[MetricResult]) -> str:
    if any(result.outcome == "insufficient_data" for result in results):
        return "insufficient_data"
    return "failed" if any(result.outcome == "failed" for result in results) else "passed"
```

- [ ] **Step 4: Run the tests and an explicit formula inspection**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_evaluation -v
rg -n 'epsilon|max\(actual|maximum\(actual' m3/worker/domain/evaluation.py
```

Expected: tests pass; the inspection returns no denominator modification.

---

### Task 5: StatsForecast Candidate Selection and Prediction

**Files:**
- Create: `m3/worker/domain/forecasting.py`
- Create: `m3/tests/test_m3_forecasting.py`

**Interfaces:**
- Consumes: `TrainingDataset`, selected model name, and explicit forecast start.
- Produces: `candidate_models()`, `select_champion(dataset) -> Champion`, and `forecast_dataset(dataset, champion) -> ForecastSeries`.

- [ ] **Step 1: Write failing tests for the exact pool, CV arguments, independent winner, fallback, and clipping**

```python
import unittest
from unittest.mock import patch
from m3.worker.domain.forecasting import candidate_models, clip_value, forecast_one, select_champion
from m3.worker.domain.training_data import build_training_dataset
from m3.tests.m3_test_support import make_cv_result, make_quarter_hour_points


class M3ForecastingTests(unittest.TestCase):
    def test_candidate_pool_is_exact(self):
        self.assertEqual([model.alias for model in candidate_models()], [
            "SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL",
        ])

    def test_physical_clipping_preserves_raw_value(self):
        self.assertEqual(clip_value("station_total_load", -3), (-3.0, 0.0, True))
        self.assertEqual(clip_value("storage_1_soc", 103), (103.0, 100.0, True))

    def test_less_than_seven_days_emits_no_forecast_points(self):
        dataset = build_training_dataset(make_quarter_hour_points(6), "station_total_load")
        series = forecast_one(dataset, None, dataset.end)
        self.assertEqual(series.status, "insufficient_history")
        self.assertEqual(series.points, [])

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_selection_uses_exact_cv_arguments(self, statsforecast_type):
        dataset = build_training_dataset(make_quarter_hour_points(28), "station_total_load")
        statsforecast_type.return_value.cross_validation.return_value = make_cv_result()
        champion = select_champion(dataset)
        self.assertEqual(statsforecast_type.call_count, 4)
        self.assertEqual(statsforecast_type.return_value.cross_validation.call_count, 4)
        for call in statsforecast_type.return_value.cross_validation.call_args_list:
            self.assertIs(call.kwargs["df"], dataset.frame)
            self.assertEqual(call.kwargs["h"], 96)
            self.assertEqual(call.kwargs["step_size"], 96)
            self.assertEqual(call.kwargs["n_windows"], 7)
        self.assertEqual(champion.model_name, "SeasonalNaive")
```

Add `make_cv_result()` to `m3/tests/m3_test_support.py`; it returns a Pandas frame with `unique_id`, `ds`, `cutoff`, `y`, and all four model columns, where `SeasonalNaive` has the lowest standard MAPE.

```python
def make_cv_result():
    import pandas as pd
    ds = pd.date_range("2026-08-18T00:00:00+08:00", periods=4, freq="15min")
    return pd.DataFrame({
        "unique_id": ["station_total_load"] * 4,
        "ds": ds,
        "cutoff": [pd.Timestamp("2026-08-17T23:45:00+08:00")] * 4,
        "y": [100.0, 100.0, 100.0, 100.0],
        "SeasonalNaive": [99.0, 101.0, 99.0, 101.0],
        "AutoETS": [95.0, 105.0, 95.0, 105.0],
        "AutoARIMA": [90.0, 110.0, 90.0, 110.0],
        "MSTL": [80.0, 120.0, 80.0, 120.0],
    })
```

- [ ] **Step 2: Run the test and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_forecasting -v`

Expected: import error for `forecasting.py`.

- [ ] **Step 3: Implement the exact StatsForecast model constructors and CV scorer**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import version
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoARIMA, AutoETS, MSTL, SeasonalNaive
from m3.worker.contracts import ForecastPoint, ForecastSeries
from m3.worker.domain.training_data import TrainingDataset
from m3.worker.errors import M3Error


def candidate_models():
    return [
        SeasonalNaive(season_length=96, alias="SeasonalNaive"),
        AutoETS(season_length=96, alias="AutoETS"),
        AutoARIMA(season_length=96, alias="AutoARIMA"),
        MSTL(season_length=[96, 672], trend_forecaster=AutoARIMA(), alias="MSTL"),
    ]


@dataclass(frozen=True)
class Champion:
    model_name: str
    cv_mape_percent: float | None
    selected_at: pd.Timestamp
    training_start: pd.Timestamp
    training_end: pd.Timestamp
    statsforecast_version: str
    selection_reason: str | None = None


def seasonal_naive_champion(
    dataset: TrainingDataset,
    selection_reason: str | None = None,
) -> Champion:
    return Champion(
        "SeasonalNaive", None, pd.Timestamp.now(tz="Asia/Shanghai"),
        pd.Timestamp(dataset.start), pd.Timestamp(dataset.end),
        version("statsforecast"), selection_reason,
    )


def _model_mapes(cv: pd.DataFrame, imputed_keys) -> dict[str, float]:
    scorable = cv[(cv["y"] != 0) & cv["y"].notna()].copy()
    scorable = scorable[~scorable.apply(lambda row: (row["unique_id"], row["ds"].to_pydatetime()) in imputed_keys, axis=1)]
    return {
        name: float((abs(scorable["y"] - scorable[name]) / abs(scorable["y"])).mean() * 100)
        for name in ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL")
        if name in scorable and scorable[name].notna().any()
    }


def select_champion(dataset: TrainingDataset) -> Champion:
    if dataset.mode == "insufficient":
        raise M3Error("insufficient_history", "fewer than seven complete training days")
    if dataset.mode == "warming_up":
        return seasonal_naive_champion(dataset)
    scores = {}
    for model in candidate_models():
        engine = StatsForecast(models=[model], freq="15min", n_jobs=1)
        try:
            cv = engine.cross_validation(
                df=dataset.frame, h=96, step_size=96, n_windows=7,
            )
        except Exception:
            continue
        score = _model_mapes(cv, dataset.imputed_keys).get(model.alias)
        if score is not None:
            scores[model.alias] = score
    if not scores:
        raise M3Error("model_selection_failed", "all StatsForecast candidates failed")
    winner = min(scores, key=lambda name: (scores[name], name))
    return Champion(winner, scores[winner], pd.Timestamp.now(tz="Asia/Shanghai"), pd.Timestamp(dataset.start), pd.Timestamp(dataset.end), version("statsforecast"))
```

- [ ] **Step 4: Implement single-champion forecasting, StatsForecast-only fallback, and clipping**

```python
def clip_value(unique_id: str, raw: float) -> tuple[float, float, bool]:
    value = float(raw)
    published = max(value, 0.0) if unique_id == "station_total_load" else min(max(value, 0.0), 100.0)
    return value, published, value != published


def model_by_name(name: str):
    models = {model.alias: model for model in candidate_models()}
    if name not in models:
        raise ValueError(f"unsupported StatsForecast model: {name}")
    return models[name]


def forecast_frame(dataset: TrainingDataset, model_name: str) -> tuple[pd.DataFrame, str, str | None]:
    try:
        engine = StatsForecast(models=[model_by_name(model_name)], freq="15min", n_jobs=1)
        return engine.forecast(df=dataset.frame, h=96), model_name, None
    except Exception as champion_error:
        if model_name == "SeasonalNaive":
            raise
        fallback = StatsForecast(models=[model_by_name("SeasonalNaive")], freq="15min", n_jobs=1)
        return fallback.forecast(df=dataset.frame, h=96), "SeasonalNaive", type(champion_error).__name__


def forecast_one(
    dataset: TrainingDataset,
    champion: Champion | None,
    as_of: datetime,
) -> ForecastSeries:
    unique_id = dataset.frame["unique_id"].iloc[0]
    unit = "kW" if unique_id == "station_total_load" else "%"
    if dataset.mode == "insufficient":
        return ForecastSeries(
            unique_id=unique_id,
            unit=unit,
            model_name="none",
            status="insufficient_history",
            points=[],
            fallback_reason="history_under_7_days",
        )
    if champion is None:
        raise M3Error("model_unavailable", f"no champion for {unique_id}")
    frame, used_model, fallback_reason = forecast_frame(dataset, champion.model_name)
    expected_first_data_time = as_of.replace(
        minute=(as_of.minute // 15) * 15, second=0, microsecond=0,
    )
    if len(frame) != 96 or pd.Timestamp(frame["ds"].iloc[0]).to_pydatetime() != expected_first_data_time:
        raise M3Error("forecast_alignment_invalid", "forecast horizon is not aligned to the completed bucket")
    points = []
    for horizon_step, row in enumerate(frame.itertuples(index=False), start=1):
        data_time = pd.Timestamp(row.ds).to_pydatetime()
        raw, published, clipped = clip_value(unique_id, getattr(row, used_model))
        points.append(ForecastPoint(
            data_time=data_time,
            target_time=data_time + timedelta(minutes=15),
            horizon_step=horizon_step,
            raw_forecast=raw,
            forecast_value=published,
            is_clipped=clipped,
        ))
    status = "degraded" if fallback_reason or champion.selection_reason else ("warming_up" if dataset.mode == "warming_up" else "ok")
    return ForecastSeries(
        unique_id=unique_id,
        unit=unit,
        model_name=used_model,
        status=status,
        points=points,
        fallback_reason=fallback_reason or champion.selection_reason,
    )
```

- [ ] **Step 5: Run mock-based tests, then one real-library smoke test**

Add the smoke test before running it:

```python
class M3ForecastingSmokeTests(unittest.TestCase):
    def test_all_four_models_forecast_fixed_fixture(self):
        from statsforecast import StatsForecast
        dataset = build_training_dataset(make_quarter_hour_points(28), "station_total_load")
        result = StatsForecast(models=candidate_models(), freq="15min", n_jobs=1).forecast(
            df=dataset.frame,
            h=96,
        )
        self.assertEqual(len(result), 96)
        for column in ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"):
            self.assertTrue(result[column].notna().all(), column)
```

Run:

```bash
uv run python -m unittest m3.tests.test_m3_forecasting -v
uv run python -m unittest m3.tests.test_m3_forecasting.M3ForecastingSmokeTests.test_all_four_models_forecast_fixed_fixture -v
```

Expected: mock tests pass quickly; the smoke test runs all four StatsForecast models on one deterministic 28-day series and returns 96 finite points per model.

---

### Task 6: Bounded HTTP Retry and Active Source API Client

**Files:**
- Create: `m3/worker/clients/http.py`
- Create: `m3/worker/clients/source_api.py`
- Create: `m3/tests/test_m3_source_client.py`

**Interfaces:**
- Consumes: configured base URL/token and Source API contracts.
- Produces: `RetryPolicy`, `send_with_retry(...)`, `SourceApiClient.list_observations(...)`, and `SourceApiClient.get_acceptance_context(...)`.

- [ ] **Step 1: Write failing HTTPX MockTransport tests**

```python
import unittest
from datetime import datetime, timedelta
import httpx
from m3.worker.clients.http import RetryPolicy
from m3.worker.clients.source_api import SourceApiClient


def source_page(next_cursor):
    return {
        "station_id": "station-1",
        "timezone": "Asia/Shanghai",
        "interval_seconds": 900,
        "points": [{
            "unique_id": "station_total_load",
            "ds": "2026-08-24T23:45:00+08:00",
            "y": 800.0,
            "quality": "valid",
            "source_revision": 1,
        }],
        "next_cursor": next_cursor,
    }


class StatusSequenceResult:
    def __init__(self, request_count):
        self.request_count = request_count


def run_status_sequence(statuses):
    request_count = 0
    def handler(request):
        nonlocal request_count
        status = statuses[request_count]
        request_count += 1
        body = {"status": "ok", "data": source_page(None)} if status == 200 else {"status": "error"}
        return httpx.Response(status, json=body)
    api = SourceApiClient(
        "http://source.internal", "secret",
        httpx.Client(transport=httpx.MockTransport(handler)),
        RetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    try:
        api.list_observations(
            "station-1",
            datetime.fromisoformat("2026-08-24T00:00:00+08:00"),
            datetime.fromisoformat("2026-08-25T00:00:00+08:00"),
        )
    except Exception:
        pass
    return StatusSequenceResult(request_count)


class SourceClientTests(unittest.TestCase):
    def test_paginates_without_leaking_token_into_url(self):
        requests = []
        def handler(request):
            requests.append(request)
            cursor = request.url.params.get("cursor")
            body = source_page(next_cursor="page-2" if cursor is None else None)
            return httpx.Response(200, json={"status": "ok", "data": body})
        client = SourceApiClient(
            "http://source.internal", "secret",
            httpx.Client(transport=httpx.MockTransport(handler)),
            RetryPolicy(max_attempts=3, base_delay_seconds=0),
        )
        points = client.list_observations(
            "station-1",
            datetime.fromisoformat("2026-08-18T00:00:00+08:00"),
            datetime.fromisoformat("2026-08-25T00:00:00+08:00"),
        )
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.headers["Authorization"] == "Bearer secret" for request in requests))
        self.assertTrue(all("secret" not in str(request.url) for request in requests))

    def test_retries_429_and_does_not_retry_403(self):
        self.assertEqual(run_status_sequence([429, 200]).request_count, 2)
        self.assertEqual(run_status_sequence([403]).request_count, 1)
```

- [ ] **Step 2: Run the tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_source_client -v`

Expected: import errors for both HTTP client modules.

- [ ] **Step 3: Implement one bounded retry primitive**

```python
from dataclasses import dataclass
import random
import time
import httpx
from m3.worker.errors import M3Error


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25


def send_with_retry(client: httpx.Client, request: httpx.Request, policy: RetryPolicy) -> httpx.Response:
    retryable = {408, 429, 500, 502, 503, 504}
    for attempt in range(1, policy.max_attempts + 1):
        retry_after = 0.0
        try:
            response = client.send(request)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as error:
            if attempt == policy.max_attempts:
                raise M3Error("http_retry_exhausted", "HTTP request retry exhausted") from error
        else:
            if response.status_code not in retryable or attempt == policy.max_attempts:
                return response
            if response.status_code == 429:
                try:
                    retry_after = max(0.0, float(response.headers.get("Retry-After", "0")))
                except ValueError:
                    retry_after = 0.0
        delay = max(retry_after, policy.base_delay_seconds * (2 ** (attempt - 1)))
        if delay:
            time.sleep(delay + random.uniform(0, delay / 4))
    raise AssertionError("retry loop exhausted without returning")
```

- [ ] **Step 4: Implement Source API operations with strict page parsing**

```python
from datetime import datetime, timedelta
import httpx
from m3.worker.clients.http import RetryPolicy, send_with_retry
from m3.worker.contracts import AcceptanceContext, ObservationPoint, SourcePage
from m3.worker.errors import M3Error


class SourceApiClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client, retry: RetryPolicy):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client
        self._retry = retry

    def _get_json(self, path: str, params: dict[str, str]) -> object:
        request = self._client.build_request("GET", f"{self._base_url}{path}", params=params, headers=self._headers)
        try:
            response = send_with_retry(self._client, request, self._retry)
        except M3Error as error:
            if error.code == "http_retry_exhausted":
                raise M3Error("source_http_failed", "Source API retry exhausted") from error
            raise
        if response.status_code in (401, 403):
            raise M3Error("source_unauthorized", "Source API authorization failed")
        if response.status_code >= 400:
            raise M3Error("source_http_failed", "Source API request failed", {"status_code": response.status_code})
        try:
            return response.json()["data"]
        except (ValueError, KeyError, TypeError) as error:
            raise M3Error("source_contract_invalid", "Source API response is invalid") from error

    def list_observations(self, station_id: str, start: datetime, end: datetime) -> list[ObservationPoint]:
        if end <= start or end - start > timedelta(days=7):
            raise M3Error("source_contract_invalid", "Source API request window must be in (0, 7 days]")
        cursor = None
        points = []
        seen_cursors = set()
        while True:
            params = {"start": start.isoformat(), "end": end.isoformat()}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get_json(f"/internal/energy-forecast/v1/stations/{station_id}/observations", params)
            page = SourcePage.model_validate(data)
            if page.station_id != station_id:
                raise M3Error("source_contract_invalid", "station_id mismatch")
            points.extend(page.points)
            cursor = page.next_cursor
            if cursor is None:
                return points
            if cursor in seen_cursors or len(seen_cursors) >= 100:
                raise M3Error("source_contract_invalid", "Source API pagination did not terminate")
            seen_cursors.add(cursor)

    def get_acceptance_context(self, station_id: str) -> AcceptanceContext:
        data = self._get_json(f"/internal/energy-forecast/v1/stations/{station_id}/acceptance-context", {})
        return AcceptanceContext.model_validate(data)
```

- [ ] **Step 5: Run the client tests and inspect for dynamic URL injection**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_source_client -v
rg -n 'params.*url|request.*base_url|token.*params' m3/worker/clients
```

Expected: tests pass; paths are constructed only from configured base URL and already-configured station IDs.

---

### Task 7: NocoBase Resource Client and Recoverable Forecast Sink

**Files:**
- Create: `m3/worker/clients/nocobase_api.py`
- Create: `m3/worker/sinks/forecast_sink.py`
- Create: `m3/tests/test_m3_nocobase_sink.py`

**Interfaces:**
- Consumes: NocoBase base URL/API Key, `LatestSnapshot`, deterministic acceptance batch/point records, and the retry primitive from Task 6.
- Produces: `NocoBaseApiClient.list_records/create_record/update_record/update_or_create/first_or_create`, `canonical_hash`, `acceptance_content_hash`, and `ForecastSink` publication/reconciliation methods.

- [ ] **Step 1: Write failing tests for exact actions, latest upsert, and interrupted acceptance recovery**

```python
import unittest
from datetime import datetime, timedelta
import httpx
from m3.worker.clients.http import RetryPolicy
from m3.worker.clients.nocobase_api import NocoBaseApiClient
from m3.worker.contracts import ForecastPoint, ForecastSeries, LatestSnapshot
from m3.worker.sinks.forecast_sink import ForecastSink, acceptance_content_hash, canonical_hash


def make_latest_snapshot():
    start = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
    series = []
    for unique_id, unit, base in (
        ("station_total_load", "kW", 800.0),
        ("storage_1_soc", "%", 50.0),
        ("storage_2_soc", "%", 60.0),
    ):
        points = [ForecastPoint(
            data_time=start + timedelta(minutes=15 * index),
            target_time=start + timedelta(minutes=15 * (index + 1)),
            horizon_step=index + 1,
            raw_forecast=base,
            forecast_value=base,
            is_clipped=False,
        ) for index in range(96)]
        series.append(ForecastSeries(
            unique_id=unique_id, unit=unit, model_name="SeasonalNaive",
            status="ok", points=points,
        ))
    return LatestSnapshot(
        station_id="station-1",
        as_of=datetime.fromisoformat("2026-08-25T01:02:00+08:00"),
        generated_at=datetime.fromisoformat("2026-08-25T01:02:05+08:00"),
        source_data_end=start,
        status="ok",
        series=series,
        model_manifest={"statsforecast_version": "2.1.1"},
        content_hash="snapshot-hash",
    )


def make_acceptance_records():
    snapshot = make_latest_snapshot()
    points = []
    for item in snapshot.series:
        for point in item.points:
            points.append({
                "unique_id": item.unique_id,
                "data_time": point.data_time.isoformat(),
                "target_time": point.target_time.isoformat(),
                "horizon_step": point.horizon_step,
                "model_name": item.model_name,
                "raw_forecast": point.raw_forecast,
                "forecast_value": point.forecast_value,
                "is_clipped": point.is_clipped,
            })
    batch = {
        "station_id": "station-1",
        "acceptance_run_id": "run-20260825",
        "issued_at": "2026-08-25T01:02:00+08:00",
        "forecast_start_time": "2026-08-25T01:00:00+08:00",
        "forecast_end_time": "2026-08-26T01:00:00+08:00",
        "status": "ok",
        "model_manifest": snapshot.model_manifest,
    }
    batch["content_hash"] = acceptance_content_hash(batch, points)
    batch["point_templates"] = points
    return batch, points


class FakeNocoBase:
    def __init__(self, fail_after_point=None):
        self.fail_after_point = fail_after_point
        self.actions = []
        self.batch = None
        self.points = {}
        self.latest = None

    def update_or_create(self, collection, filter, values):
        self.actions.append((f"{collection}:updateOrCreate", {"filter": filter, "values": values}))
        if collection == "energy_forecast_latest":
            self.latest = dict(values)
        return dict(values)

    def first_or_create(self, collection, filter, values):
        self.actions.append((f"{collection}:firstOrCreate", {"filter": filter, "values": values}))
        if collection == "energy_forecast_batches":
            if self.batch is None:
                self.batch = {"id": 1, **values}
            return dict(self.batch)
        if self.fail_after_point is not None and len(self.points) >= self.fail_after_point:
            raise RuntimeError("injected write failure")
        key = (values["batch_id"], values["unique_id"], values["data_time"])
        self.points.setdefault(key, dict(values))
        return dict(self.points[key])

    def list_records(self, collection, *, filter, fields, sort=None):
        if collection == "energy_forecast_latest":
            return [] if self.latest is None else [
                {field: self.latest[field] for field in fields}
            ]
        return [{field: value[field] for field in fields} for _, value in sorted(self.points.items())]

    def update_record(self, collection, record_id, values):
        self.batch.update(values)
        return dict(self.batch)


def make_sink(fake):
    return ForecastSink(fake)


class NocoBaseSinkTests(unittest.TestCase):
    def test_client_uses_resource_action_and_bearer_header(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": {"id": 1}})
        api = NocoBaseApiClient(
            "http://nocobase.internal", "sink-secret",
            httpx.Client(transport=httpx.MockTransport(handler)),
            RetryPolicy(max_attempts=1, base_delay_seconds=0),
        )
        api.update_or_create("energy_forecast_latest", {"station_id": "station-1"}, {"status": "ok"})
        self.assertEqual(requests[0].url.path, "/api/energy_forecast_latest:updateOrCreate")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer sink-secret")

    def test_latest_snapshot_uses_update_or_create(self):
        fake = FakeNocoBase()
        sink = make_sink(fake)
        sink.publish_latest(make_latest_snapshot())
        self.assertEqual(fake.actions[-1][0], "energy_forecast_latest:updateOrCreate")
        self.assertEqual(fake.actions[-1][1]["filter"], {"station_id": "station-1"})

    def test_same_latest_as_of_with_different_hash_conflicts(self):
        fake = FakeNocoBase()
        sink = make_sink(fake)
        snapshot = make_latest_snapshot()
        sink.publish_latest(snapshot)
        with self.assertRaisesRegex(Exception, "latest snapshot hash mismatch"):
            sink.publish_latest(snapshot.model_copy(update={"content_hash": "different"}))

    def test_acceptance_is_invisible_until_all_288_points_exist(self):
        fake = FakeNocoBase(fail_after_point=100)
        sink = make_sink(fake)
        batch, points = make_acceptance_records()
        with self.assertRaises(Exception):
            sink.publish_acceptance(batch, points)
        self.assertEqual(fake.batch["write_state"], "writing")
        fake.fail_after_point = None
        sink.reconcile_acceptance(fake.batch, fake.batch["point_templates"])
        self.assertEqual(fake.batch["write_state"], "complete")
        self.assertEqual(len(fake.points), 288)

    def test_same_business_key_with_different_hash_conflicts(self):
        self.assertNotEqual(canonical_hash({"value": 1}), canonical_hash({"value": 2}))
```

- [ ] **Step 2: Run the tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_nocobase_sink -v`

Expected: import errors for the client and sink.

- [ ] **Step 3: Implement one method per NocoBase HTTP action**

```python
import json
import httpx
from m3.worker.clients.http import RetryPolicy, send_with_retry
from m3.worker.errors import M3Error


class NocoBaseApiClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client, retry: RetryPolicy):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = client
        self._retry = retry

    def _request(self, method: str, action: str, *, params=None, json=None):
        request = self._client.build_request(method, f"{self._base_url}/api/{action}", params=params, json=json, headers=self._headers)
        try:
            response = send_with_retry(self._client, request, self._retry)
        except M3Error as error:
            if error.code == "http_retry_exhausted":
                raise M3Error("sink_http_failed", "NocoBase retry exhausted", {"action": action}) from error
            raise
        if response.status_code in (401, 403):
            raise M3Error("sink_unauthorized", "NocoBase authorization failed")
        if response.status_code >= 400:
            raise M3Error("sink_http_failed", "NocoBase action failed", {"action": action, "status_code": response.status_code})
        try:
            return response.json().get("data")
        except (ValueError, TypeError, AttributeError) as error:
            raise M3Error("sink_contract_invalid", "NocoBase response is invalid", {"action": action}) from error

    def list_records(self, collection: str, *, filter: dict, fields: list[str], sort: list[str] | None = None):
        params = {
            "filter": json.dumps(filter, separators=(",", ":"), sort_keys=True),
            "fields": ",".join(fields),
        }
        if sort:
            params["sort"] = ",".join(sort)
        return self._request("GET", f"{collection}:list", params=params)

    def create_record(self, collection: str, values: dict):
        return self._request("POST", f"{collection}:create", json=values)

    def update_record(self, collection: str, record_id: int, values: dict):
        return self._request("POST", f"{collection}:update", params={"filterByTk": str(record_id)}, json=values)

    def update_or_create(self, collection: str, filter: dict, values: dict):
        return self._request("POST", f"{collection}:updateOrCreate", json={"filter": filter, "values": values})

    def first_or_create(self, collection: str, filter: dict, values: dict):
        return self._request("POST", f"{collection}:firstOrCreate", json={"filter": filter, "values": values})
```

Before live deployment, Task 13 verifies the exact `filter/values` body against the installed NocoBase API Documentation plugin. If the installed release expects query parameters, change only this adapter and its contract test.

- [ ] **Step 4: Implement deterministic hashes and the recoverable publish protocol**

```python
import hashlib
import json
from pydantic_core import to_jsonable_python
from m3.worker.errors import M3Error


def canonical_hash(value: object) -> str:
    normalized = to_jsonable_python(value)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BATCH_HASH_FIELDS = (
    "station_id", "acceptance_run_id", "issued_at", "forecast_start_time",
    "forecast_end_time", "status", "model_manifest",
)
POINT_HASH_FIELDS = (
    "unique_id", "data_time", "target_time", "horizon_step", "model_name",
    "raw_forecast", "forecast_value", "is_clipped",
)


def acceptance_content_hash(batch: dict, points: list[dict]) -> str:
    immutable_points = [
        {key: point[key] for key in POINT_HASH_FIELDS}
        for point in points
    ]
    return canonical_hash({
        "batch": {key: batch[key] for key in BATCH_HASH_FIELDS},
        "points": sorted(immutable_points, key=lambda point: (point["unique_id"], point["data_time"])),
    })


class ForecastSink:
    def __init__(self, api: object):
        self._api = api

    def publish_latest(self, snapshot) -> dict:
        values = snapshot.model_dump(mode="json")
        values["series_payload"] = values.pop("series")
        existing = self._api.list_records(
            "energy_forecast_latest",
            filter={"station_id": snapshot.station_id},
            fields=["as_of", "content_hash"],
        )
        if (
            existing
            and existing[0]["as_of"] == values["as_of"]
            and existing[0]["content_hash"] != values["content_hash"]
        ):
            raise M3Error("idempotency_conflict", "latest snapshot hash mismatch for the same as_of")
        return self._api.update_or_create(
            "energy_forecast_latest",
            {"station_id": snapshot.station_id},
            values,
        )

    def publish_acceptance(self, batch: dict, points: list[dict]) -> dict:
        if len(points) != 288:
            raise M3Error("acceptance_write_incomplete", "acceptance batch requires 288 points")
        batch_values = {
            key: batch[key]
            for key in (*BATCH_HASH_FIELDS, "content_hash", "point_templates")
        }
        persisted = self._api.first_or_create(
            "energy_forecast_batches",
            {key: batch[key] for key in ("station_id", "acceptance_run_id", "issued_at")},
            {**batch_values, "write_state": "writing"},
        )
        if persisted["content_hash"] != batch["content_hash"]:
            raise M3Error("idempotency_conflict", "acceptance batch hash mismatch")
        for template in points:
            point = {**template, "batch_id": persisted["id"]}
            self._api.first_or_create(
                "energy_forecast_points",
                {key: point[key] for key in ("batch_id", "unique_id", "data_time")},
                point,
            )
        stored = self._api.list_records(
            "energy_forecast_points",
            filter={"batch_id": persisted["id"]},
            fields=list(POINT_HASH_FIELDS),
            sort=["unique_id", "data_time"],
        )
        if len(stored) != 288 or acceptance_content_hash(persisted, stored) != batch["content_hash"]:
            raise M3Error("acceptance_write_incomplete", "acceptance point verification failed")
        return self._api.update_record("energy_forecast_batches", persisted["id"], {"write_state": "complete"})

    def reconcile_acceptance(self, batch: dict, points: list[dict]) -> dict:
        return self.publish_acceptance(batch, points)
```

The hash covers the fixed batch context plus the canonical sorted immutable point fields. `point_templates` remains on the batch row until normal retention expiry so a restarted Worker can resume the exact original write without rerunning a model.

- [ ] **Step 5: Run sink tests and inspect the Worker for direct SQL**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_nocobase_sink -v
rg -n 'psycopg|asyncpg|sqlalchemy|postgresql://|SELECT |INSERT |UPDATE ' m3.worker
```

Expected: sink tests pass; no direct database client or SQL statement exists.

---

### Task 8: Revision-Aware Station Cache and Forecast Orchestration

**Files:**
- Create: `m3/worker/services/station_cache.py`
- Create: `m3/worker/services/forecast_service.py`
- Create: `m3/tests/test_m3_forecast_service.py`

**Interfaces:**
- Consumes: Source client, `ForecastSink`, training/forecast functions, station IDs, and explicit clocks.
- Produces: `StationState`, `StationCache.replace/merge/window`, `ForecastService.bootstrap`, `select_models`, and `run_forecast`.

- [ ] **Step 1: Write failing tests for atomic replace, overlapping revisions, active pull, and publish failure**

```python
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from m3.worker.contracts import ForecastPoint, ForecastSeries
from m3.worker.domain.forecasting import Champion
from m3.worker.errors import M3Error
from m3.worker.services.forecast_service import ForecastService
from m3.worker.services.station_cache import StationCache
from m3.tests.m3_test_support import make_quarter_hour_points


def make_three_series_points(days):
    points = []
    for unique_id in ("station_total_load", "storage_1_soc", "storage_2_soc"):
        points.extend(make_quarter_hour_points(days, unique_id))
    return points


class FakeSource:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def list_observations(self, station_id, start, end):
        self.calls.append(SimpleNamespace(kind="observations", station_id=station_id, start=start, end=end))
        return [point for point in self.points if start <= point.ds < end]


class FakeSink:
    def __init__(self):
        self.latest = []

    def publish_latest(self, snapshot):
        self.latest.append(snapshot)
        return {"id": 1}


class FailingSink(FakeSink):
    def publish_latest(self, snapshot):
        raise M3Error("sink_http_failed", "injected sink failure")


def fake_champion(dataset):
    return Champion(
        model_name="SeasonalNaive",
        cv_mape_percent=10.0,
        selected_at=dataset.end,
        training_start=dataset.start,
        training_end=dataset.end,
        statsforecast_version="2.1.1",
    )


def fake_forecast_one(dataset, champion, as_of):
    start = as_of.replace(minute=(as_of.minute // 15) * 15, second=0, microsecond=0)
    unit = "kW" if dataset.frame["unique_id"].iloc[0] == "station_total_load" else "%"
    points = [ForecastPoint(
        data_time=start + timedelta(minutes=15 * index),
        target_time=start + timedelta(minutes=15 * (index + 1)),
        horizon_step=index + 1,
        raw_forecast=50.0,
        forecast_value=50.0,
        is_clipped=False,
    ) for index in range(96)]
    return ForecastSeries(
        unique_id=dataset.frame["unique_id"].iloc[0], unit=unit,
        model_name=champion.model_name, status="ok", points=points,
    )


def make_forecast_service(source, sink):
    caches = {"station-1": StationCache("station-1")}
    return ForecastService(
        source, sink, caches, fake_forecast_one,
        now=lambda: datetime.fromisoformat("2026-08-25T01:17:05+08:00"),
    )


def make_ready_service(sink):
    service = make_forecast_service(FakeSource(make_three_series_points(28)), sink)
    service.bootstrap("station-1", datetime.fromisoformat("2026-08-25T00:30:00+08:00"))
    service.select_models("station-1")
    return service


class ForecastServiceTests(unittest.TestCase):
    def setUp(self):
        self.champion_patch = patch("m3.worker.services.forecast_service.select_champion", side_effect=fake_champion)
        self.champion_patch.start()

    def tearDown(self):
        self.champion_patch.stop()

    def test_higher_revision_replaces_and_same_revision_conflict_fails(self):
        cache = StationCache("station-1")
        point = make_quarter_hour_points(7)[0]
        cache.merge([point])
        cache.merge([point.model_copy(update={"y": point.y + 1, "source_revision": 2})])
        self.assertEqual(cache.window(point.unique_id)[0].source_revision, 2)
        with self.assertRaises(M3Error):
            cache.merge([point.model_copy(update={"y": point.y + 2, "source_revision": 2})])

    def test_run_forecast_pulls_before_model_execution_and_publishes_once(self):
        source = FakeSource(make_three_series_points(28))
        sink = FakeSink()
        service = make_forecast_service(source, sink)
        service.bootstrap("station-1", datetime.fromisoformat("2026-08-25T00:30:00+08:00"))
        service.select_models("station-1")
        service.run_forecast("station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00"))
        self.assertEqual(source.calls[-1].kind, "observations")
        self.assertEqual(len(sink.latest), 1)

    def test_sink_failure_does_not_advance_last_published_at(self):
        service = make_ready_service(sink=FailingSink())
        before = service.state("station-1").last_published_at
        with self.assertRaises(M3Error):
            service.run_forecast("station-1", datetime.fromisoformat("2026-08-25T01:17:00+08:00"))
        self.assertEqual(service.state("station-1").last_published_at, before)
```

- [ ] **Step 2: Run the tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_forecast_service -v`

Expected: import errors for both service modules.

- [ ] **Step 3: Implement the cache with atomic replacement and a 90-day trim**

```python
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from m3.worker.contracts import ObservationPoint, SeriesId
from m3.worker.errors import M3Error


@dataclass
class StationState:
    state: str = "initializing"
    champions: dict[str, object] = field(default_factory=dict)
    last_source_at: datetime | None = None
    last_published_at: datetime | None = None
    last_error_code: str | None = None


class StationCache:
    def __init__(self, station_id: str):
        self.station_id = station_id
        self.state = StationState()
        self._points: dict[tuple[str, datetime], ObservationPoint] = {}
        self._lock = RLock()

    def replace(self, points: list[ObservationPoint]) -> None:
        candidate = {(point.unique_id, point.ds): point for point in points}
        if len(candidate) != len(points):
            raise M3Error("source_contract_invalid", "duplicate points in full pull")
        with self._lock:
            self._points = candidate

    def merge(self, points: list[ObservationPoint]) -> None:
        with self._lock:
            candidate = dict(self._points)
            for point in points:
                key = (point.unique_id, point.ds)
                current = candidate.get(key)
                if current and point.source_revision == current.source_revision and point != current:
                    raise M3Error("source_contract_invalid", "same revision contains conflicting values")
                if current is None or point.source_revision > current.source_revision:
                    candidate[key] = point
            if candidate:
                cutoff = max(point.ds for point in candidate.values()) - timedelta(days=90)
                candidate = {key: point for key, point in candidate.items() if point.ds >= cutoff}
            self._points = candidate

    def window(self, unique_id: SeriesId) -> list[ObservationPoint]:
        with self._lock:
            return sorted((point for (series, _), point in self._points.items() if series == unique_id), key=lambda point: point.ds)
```

- [ ] **Step 4: Implement active-pull service methods and state transitions**

```python
from dataclasses import replace
from datetime import datetime, timedelta
from importlib.metadata import version
from m3.worker.contracts import LatestSnapshot
from m3.worker.domain.forecasting import seasonal_naive_champion, select_champion
from m3.worker.domain.training_data import build_training_dataset
from m3.worker.errors import M3Error
from m3.worker.sinks.forecast_sink import canonical_hash


SERIES = ("station_total_load", "storage_1_soc", "storage_2_soc")


def build_latest_snapshot(
    station_id: str,
    as_of: datetime,
    generated_at: datetime,
    source_data_end: datetime,
    series: list,
    champions: dict,
) -> LatestSnapshot:
    by_id = {item.unique_id: item for item in series}
    if len(series) != 3 or set(by_id) != set(SERIES):
        raise M3Error("forecast_incomplete", "latest snapshot requires exactly three terminal series")
    statuses = {item.status for item in series}
    if statuses & {"degraded", "insufficient_history", "error"}:
        status = "degraded"
    elif "warming_up" in statuses:
        status = "warming_up"
    elif statuses == {"ok"}:
        status = "ok"
    else:
        raise M3Error("forecast_status_invalid", f"unsupported terminal status set: {sorted(statuses)}")
    versions = {
        champion.statsforecast_version
        for champion in champions.values()
        if champion is not None
    }
    if len(versions) > 1:
        raise M3Error("model_manifest_invalid", "champions must use one StatsForecast version")
    manifest = {
        "statsforecast_version": versions.pop() if versions else version("statsforecast"),
        "series": {
            unique_id: (
                {
                    "model_name": champions[unique_id].model_name,
                    "cv_mape_percent": champions[unique_id].cv_mape_percent,
                    "selected_at": champions[unique_id].selected_at.isoformat(),
                    "training_start": champions[unique_id].training_start.isoformat(),
                    "training_end": champions[unique_id].training_end.isoformat(),
                    "selection_reason": champions[unique_id].selection_reason,
                }
                if champions[unique_id] is not None
                else {"model_name": None, "reason": "insufficient_history"}
            )
            for unique_id in SERIES
        },
    }
    body = {
        "station_id": station_id,
        "as_of": as_of,
        "generated_at": generated_at,
        "source_data_end": source_data_end,
        "status": status,
        "series": [by_id[unique_id] for unique_id in SERIES],
        "model_manifest": manifest,
    }
    return LatestSnapshot(**body, content_hash=canonical_hash(body))


class ForecastService:
    def __init__(self, source, sink, caches, forecast_one, now):
        self._source = source
        self._sink = sink
        self._caches = caches
        self._forecast_one = forecast_one
        self._now = now

    def state(self, station_id: str):
        return self._caches[station_id].state

    def bootstrap(self, station_id: str, as_of: datetime) -> None:
        start = as_of - timedelta(days=90)
        points = []
        cursor = start
        while cursor < as_of:
            end = min(cursor + timedelta(days=7), as_of)
            points.extend(self._source.list_observations(station_id, cursor, end))
            cursor = end
        observed_series = {point.unique_id for point in points}
        if observed_series != set(SERIES):
            raise M3Error(
                "source_contract_invalid",
                f"full pull series mismatch: {sorted(observed_series)}",
            )
        self._caches[station_id].replace(points)
        self._caches[station_id].state.last_source_at = as_of

    def select_models(self, station_id: str) -> None:
        cache = self._caches[station_id]
        champions = {}
        degraded = False
        for unique_id in SERIES:
            dataset = build_training_dataset(cache.window(unique_id), unique_id)
            try:
                champions[unique_id] = select_champion(dataset)
            except M3Error as error:
                if error.code == "insufficient_history":
                    champions[unique_id] = None
                elif error.code == "model_selection_failed":
                    previous = cache.state.champions.get(unique_id)
                    champions[unique_id] = (
                        replace(previous, selection_reason="model_selection_failed")
                        if previous is not None
                        else seasonal_naive_champion(dataset, "model_selection_failed")
                    )
                    degraded = True
                else:
                    raise
        cache.state.champions = champions
        cache.state.state = "degraded" if degraded else "ready"

    def run_forecast(self, station_id: str, as_of: datetime) -> LatestSnapshot:
        cache = self._caches[station_id]
        completed_end = as_of.replace(minute=(as_of.minute // 15) * 15, second=0, microsecond=0)
        overlap_start = completed_end - timedelta(minutes=30)
        cache.merge(self._source.list_observations(station_id, overlap_start, completed_end))
        series = [self._forecast_one(build_training_dataset(cache.window(unique_id), unique_id), cache.state.champions[unique_id], as_of) for unique_id in SERIES]
        snapshot = build_latest_snapshot(
            station_id, as_of, self._now(), completed_end, series, cache.state.champions,
        )
        self._sink.publish_latest(snapshot)
        cache.state.last_published_at = snapshot.generated_at
        return snapshot
```

- [ ] **Step 5: Run service tests and the full pure-Python M3 suite**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_forecast_service -v
uv run python -m unittest m3.tests.test_m3_contracts m3.tests.test_m3_training_data m3.tests.test_m3_evaluation m3.tests.test_m3_forecasting -v
```

Expected: all focused domain and service tests pass.

---

### Task 9: Seven-Day Acceptance Service

**Files:**
- Create: `m3/worker/services/acceptance_service.py`
- Create: `m3/tests/test_m3_acceptance_service.py`

**Interfaces:**
- Consumes: active `AcceptanceContext`, a completed three-series forecast, Source API actuals, `ForecastSink`, and evaluation functions.
- Produces: `run_baseline`, `backfill_actuals`, `recalculate`, and `reconcile_writing_batches`.

- [ ] **Step 1: Write failing tests for the fixed baseline window, immutable predictions, actual-zero handling, and overall result**

```python
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from m3.worker.contracts import AcceptanceContext, ObservationPoint
from m3.worker.domain.evaluation import MetricResult
from m3.worker.services.acceptance_service import AcceptanceService
from m3.tests.test_m3_nocobase_sink import make_latest_snapshot


def passing_metric():
    return MetricResult(672, 605, 0, 10.0, 5.0, 9.0, "passed")


def failing_metric():
    return MetricResult(672, 605, 0, 40.0, 20.0, 35.0, "failed")


class AcceptanceSource:
    def get_acceptance_context(self, station_id):
        return AcceptanceContext(
            active=True,
            acceptance_run_id="run-20260825",
            window_start=datetime.fromisoformat("2026-08-25T01:00:00+08:00"),
            window_end=datetime.fromisoformat("2026-09-01T01:00:00+08:00"),
        )

    def list_observations(self, station_id, start, end):
        return [ObservationPoint(
            unique_id="station_total_load", ds=start, y=100.0,
            quality="valid", source_revision=1,
        )]


class RecordingApi:
    def __init__(self):
        self.update_calls = []
        self.evaluation_rows = []

    def list_records(self, collection, *, filter, fields, sort=None):
        if collection == "energy_forecast_points" and "id" in fields:
            return [{
                "id": 1, "unique_id": "station_total_load",
                "data_time": "2026-08-25T01:00:00+08:00", "forecast_value": 90.0,
                "actual_source_revision": None,
            }]
        return [{
            "unique_id": unique_id, "actual_value": 100.0,
            "forecast_value": 90.0, "actual_quality": "valid",
            "data_time": "2026-08-25T01:00:00+08:00",
        } for unique_id in ("station_total_load", "storage_1_soc", "storage_2_soc")]

    def update_record(self, collection, record_id, values):
        self.update_calls.append(SimpleNamespace(collection=collection, record_id=record_id, values=values))
        return values

    def update_or_create(self, collection, filter, values):
        self.evaluation_rows.append(values)
        return values


class RecordingAcceptanceSink:
    def __init__(self):
        self.batch = None
        self.points = []

    def publish_acceptance(self, batch, points):
        self.batch = batch
        self.points = points
        return {"id": 1, **batch}


class ForecastServiceStub:
    def run_forecast(self, station_id, as_of):
        return make_latest_snapshot().model_copy(update={"as_of": as_of})


def make_acceptance_service(api=None, metric_results=None):
    api = api or RecordingApi()
    sink = RecordingAcceptanceSink()
    evaluator = Mock(side_effect=metric_results) if metric_results else None
    service = AcceptanceService(
        AcceptanceSource(), api, sink, ForecastServiceStub(),
        now=lambda: datetime.fromisoformat("2026-08-25T02:02:05+08:00"),
        evaluator=evaluator,
    )
    return service, sink, api


class AcceptanceServiceTests(unittest.TestCase):
    def test_0102_baseline_uses_0100_data_time_and_0115_target_time(self):
        service, sink, _ = make_acceptance_service()
        service.run_baseline("station-1", datetime.fromisoformat("2026-08-25T01:02:00+08:00"))
        first = sink.points[0]
        last = sink.points[95]
        self.assertEqual(first["data_time"], "2026-08-25T01:00:00+08:00")
        self.assertEqual(first["target_time"], "2026-08-25T01:15:00+08:00")
        self.assertEqual(last["target_time"], "2026-08-26T01:00:00+08:00")
        self.assertEqual(len(sink.points), 288)

    def test_backfill_updates_only_actual_columns(self):
        api = RecordingApi()
        service, _, _ = make_acceptance_service(api=api)
        service.backfill_actuals("station-1", datetime.fromisoformat("2026-08-25T02:02:00+08:00"))
        allowed = {"actual_value", "actual_quality", "actual_source_revision", "actual_recorded_at", "evaluated_at", "absolute_percentage_error"}
        self.assertTrue(all(set(call.values) <= allowed for call in api.update_calls))

    def test_three_series_must_each_pass(self):
        service, _, _ = make_acceptance_service(metric_results=[passing_metric(), passing_metric(), failing_metric()])
        result = service.recalculate("station-1", "run-20260825")
        self.assertEqual(result["overall"]["outcome"], "failed")
```

- [ ] **Step 2: Run tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_acceptance_service -v`

Expected: import error for `acceptance_service.py`.

- [ ] **Step 3: Implement deterministic baseline construction and publication**

```python
from datetime import datetime, timedelta
from types import SimpleNamespace
from m3.worker.domain.evaluation import evaluate_series, overall_outcome
from m3.worker.errors import M3Error
from m3.worker.sinks.forecast_sink import acceptance_content_hash


def build_acceptance_points(snapshot):
    templates = []
    for series in snapshot.series:
        for point in series.points:
            templates.append({
                "unique_id": series.unique_id,
                "data_time": point.data_time.isoformat(),
                "target_time": point.target_time.isoformat(),
                "horizon_step": point.horizon_step,
                "model_name": series.model_name,
                "raw_forecast": point.raw_forecast,
                "forecast_value": point.forecast_value,
                "is_clipped": point.is_clipped,
            })
    return sorted(templates, key=lambda item: (item["unique_id"], item["data_time"]))


class AcceptanceService:
    def __init__(self, source, api, sink, forecast_service, now, evaluator=None):
        self._source = source
        self._api = api
        self._sink = sink
        self._forecast_service = forecast_service
        self._now = now
        self._evaluator = evaluator or evaluate_series

    def run_baseline(self, station_id: str, as_of: datetime):
        self.backfill_actuals(station_id, as_of)
        context = self._source.get_acceptance_context(station_id)
        if not context.active or context.acceptance_run_id is None:
            raise M3Error("acceptance_inactive", "no active acceptance run")
        baseline_start = as_of.replace(minute=0, second=0, microsecond=0)
        if (
            context.window_start is None
            or context.window_end is None
            or baseline_start < context.window_start
            or baseline_start + timedelta(days=1) > context.window_end
        ):
            raise M3Error("acceptance_window_invalid", "baseline is outside the active seven-day window")
        snapshot = self._forecast_service.run_forecast(station_id, as_of)
        if any(len(series.points) != 96 for series in snapshot.series):
            raise M3Error("acceptance_write_incomplete", "all three series require 96 points")
        points = build_acceptance_points(snapshot)
        issued_at = as_of.replace(second=0, microsecond=0)
        batch = {
            "station_id": station_id,
            "acceptance_run_id": context.acceptance_run_id,
            "issued_at": issued_at.isoformat(),
            "forecast_start_time": baseline_start.isoformat(),
            "forecast_end_time": (baseline_start + timedelta(days=1)).isoformat(),
            "status": snapshot.status,
            "model_manifest": snapshot.model_manifest,
        }
        batch["content_hash"] = acceptance_content_hash(batch, points)
        batch["point_templates"] = points
        return self._sink.publish_acceptance(batch, points)
```

`build_acceptance_points()` deliberately omits `batch_id`; `ForecastSink.publish_acceptance()` injects the persisted batch ID returned by `firstOrCreate`.

- [ ] **Step 4: Implement active actual pull, allowed-column updates, and evaluation upserts**

```python
    def backfill_actuals(self, station_id: str, as_of: datetime) -> int:
        open_points = self._api.list_records(
            "energy_forecast_points",
            filter={
                "batch.write_state": "complete",
                "batch.station_id": station_id,
                "data_time": {"$lt": as_of.isoformat()},
                "$or": [
                    {"actual_recorded_at": None},
                    {"data_time": {"$gte": (as_of - timedelta(minutes=30)).isoformat()}},
                ],
            },
            fields=["id", "unique_id", "data_time", "forecast_value", "actual_source_revision"],
            sort=["data_time"],
        )
        if not open_points:
            return 0
        start = min(datetime.fromisoformat(point["data_time"]) for point in open_points)
        actuals = self._source.list_observations(station_id, start, as_of)
        by_key = {(point.unique_id, point.ds): point for point in actuals}
        updated = 0
        for stored in open_points:
            key = (stored["unique_id"], datetime.fromisoformat(stored["data_time"]))
            actual = by_key.get(key)
            if actual is None:
                continue
            if stored["actual_source_revision"] is not None and stored["actual_source_revision"] >= actual.source_revision:
                continue
            values = {
                "actual_value": actual.y,
                "actual_quality": actual.quality,
                "actual_source_revision": actual.source_revision,
                "actual_recorded_at": self._now().isoformat(),
                "evaluated_at": self._now().isoformat(),
                "absolute_percentage_error": None if actual.y in (None, 0) or actual.quality != "valid" else 100 * abs(actual.y - stored["forecast_value"]) / abs(actual.y),
            }
            self._api.update_record("energy_forecast_points", stored["id"], values)
            updated += 1
        context = self._source.get_acceptance_context(station_id)
        if updated and context.active and context.acceptance_run_id is not None:
            self.recalculate(station_id, context.acceptance_run_id)
        return updated

    def recalculate(self, station_id: str, acceptance_run_id: str) -> dict[str, dict]:
        rows = self._api.list_records(
            "energy_forecast_points",
            filter={"batch.station_id": station_id, "batch.acceptance_run_id": acceptance_run_id, "batch.write_state": "complete"},
            fields=["unique_id", "data_time", "actual_value", "forecast_value", "actual_quality"],
            sort=["unique_id", "data_time"],
        )
        results = {}
        for unique_id in ("station_total_load", "storage_1_soc", "storage_2_soc"):
            selected = [row for row in rows if row["unique_id"] == unique_id]
            if not selected:
                raise M3Error(
                    "acceptance_points_incomplete",
                    f"no complete acceptance points for {unique_id}",
                )
            metric = self._evaluator(
                [row["actual_value"] for row in selected],
                [row["forecast_value"] for row in selected],
                [row["actual_quality"] for row in selected],
            )
            values = {
                "station_id": station_id,
                "acceptance_run_id": acceptance_run_id,
                "evaluation_key": unique_id,
                **metric.__dict__,
                "expected_count": 672,
                "window_start": min(row["data_time"] for row in selected),
                "window_end": (max(datetime.fromisoformat(row["data_time"]) for row in selected) + timedelta(minutes=15)).isoformat(),
                "calculated_at": self._now().isoformat(),
            }
            self._api.update_or_create(
                "energy_forecast_evaluations",
                {"station_id": station_id, "acceptance_run_id": acceptance_run_id, "evaluation_key": unique_id},
                values,
            )
            results[unique_id] = values
        overall = {
            "station_id": station_id,
            "acceptance_run_id": acceptance_run_id,
            "evaluation_key": "overall",
            "expected_count": 2016,
            "valid_count": sum(results[key]["valid_count"] for key in ("station_total_load", "storage_1_soc", "storage_2_soc")),
            "zero_actual_count": sum(results[key]["zero_actual_count"] for key in ("station_total_load", "storage_1_soc", "storage_2_soc")),
            "window_start": min(results[key]["window_start"] for key in ("station_total_load", "storage_1_soc", "storage_2_soc")),
            "window_end": max(results[key]["window_end"] for key in ("station_total_load", "storage_1_soc", "storage_2_soc")),
            "outcome": overall_outcome([SimpleNamespace(**results[key]) for key in ("station_total_load", "storage_1_soc", "storage_2_soc")]),
            "calculated_at": self._now().isoformat(),
        }
        self._api.update_or_create(
            "energy_forecast_evaluations",
            {"station_id": station_id, "acceptance_run_id": acceptance_run_id, "evaluation_key": "overall"},
            overall,
        )
        results["overall"] = overall
        return results

    def reconcile_writing_batches(self) -> int:
        batches = self._api.list_records(
            "energy_forecast_batches",
            filter={"write_state": "writing"},
            fields=[
                "id", "station_id", "acceptance_run_id", "issued_at",
                "forecast_start_time", "forecast_end_time", "status",
                "model_manifest", "content_hash", "point_templates",
            ],
            sort=["issued_at"],
        )
        recovered = 0
        for batch in batches:
            points = batch["point_templates"]
            if not isinstance(points, list) or len(points) != 288:
                raise M3Error(
                    "acceptance_write_incomplete",
                    f"batch {batch['id']} has no valid recovery payload",
                )
            if acceptance_content_hash(batch, points) != batch["content_hash"]:
                raise M3Error(
                    "idempotency_conflict",
                    f"batch {batch['id']} recovery payload hash mismatch",
                )
            self._sink.reconcile_acceptance(batch, points)
            recovered += 1
        return recovered
```

`reconcile_writing_batches()` resumes from the persisted immutable templates and never reruns a model or creates a replacement batch.

- [ ] **Step 5: Run acceptance tests and inspect immutable-field writes**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_acceptance_service -v
rg -n 'update_record.*(forecast_value|raw_forecast|model_name|data_time|target_time)' m3.worker
```

Expected: tests pass; no update path writes immutable prediction columns.

---

### Task 10: Deterministic Scheduler and FastAPI Operations Surface

**Files:**
- Create: `m3/worker/scheduler/runner.py`
- Create: `m3/worker/api/models.py`
- Create: `m3/worker/api/dependencies.py`
- Create: `m3/worker/api/routes.py`
- Create: `m3/worker/services/job_service.py`
- Create: `m3/worker/main.py`
- Create: `m3/tests/test_m3_scheduler.py`
- Create: `m3/tests/test_m3_worker_api.py`

**Interfaces:**
- Consumes: station services, configured station IDs, explicit clock, and admin token.
- Produces: `due_slots(now)`, `SchedulerRunner.tick`, `create_app(settings, resources) -> FastAPI`, and the five approved operations endpoints.

- [ ] **Step 1: Write failing scheduler tests for all exact slots and no duplicates**

```python
import unittest
from datetime import datetime
from m3.worker.scheduler.runner import SchedulerRunner, due_slots
from m3.worker.services.job_service import JobService


class RecordingService:
    def __init__(self):
        self.forecast_calls = []
        self.selection_calls = []

    def run_forecast(self, station_id, now):
        self.forecast_calls.append((station_id, now))

    def bootstrap(self, station_id, now):
        self.selection_calls.append(("bootstrap", station_id, now))

    def select_models(self, station_id):
        self.selection_calls.append(("select", station_id))


class RecordingAcceptance:
    def __init__(self):
        self.baseline_calls = []

    def backfill_actuals(self, station_id, now):
        return 0

    def run_baseline(self, station_id, now):
        self.baseline_calls.append((station_id, now))


class SchedulerTests(unittest.TestCase):
    def test_due_slots_are_exact(self):
        self.assertEqual(due_slots(datetime.fromisoformat("2026-08-25T00:30:00+08:00")), ("model_selection",))
        self.assertEqual(due_slots(datetime.fromisoformat("2026-08-25T01:02:00+08:00")), ("acceptance_baseline",))
        self.assertEqual(due_slots(datetime.fromisoformat("2026-08-25T01:17:00+08:00")), ("forecast",))

    def test_tick_runs_each_station_slot_once(self):
        service = RecordingService()
        runner = SchedulerRunner(("station-1",), service, RecordingAcceptance())
        now = datetime.fromisoformat("2026-08-25T01:17:00+08:00")
        runner.tick(now)
        runner.tick(now)
        self.assertEqual(service.forecast_calls, [("station-1", now)])

    def test_manual_job_reuses_bounded_runner_and_reaches_terminal_state(self):
        calls = []
        now = datetime.fromisoformat("2026-08-25T03:00:00+08:00")
        jobs = JobService(
            run_task=lambda station_id, task, at: calls.append((station_id, task, at)),
            now=lambda: now,
            max_workers=1,
        )
        submitted = jobs.submit("station-1", "forecast")
        jobs.close()
        self.assertEqual(jobs.get(submitted.job_id).status, "succeeded")
        self.assertEqual(calls, [("station-1", "forecast", now)])
```

- [ ] **Step 2: Write failing API tests for authorization and typed responses**

```python
import unittest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from pydantic import HttpUrl, SecretStr, TypeAdapter
from m3.worker.config import Settings
from m3.worker.contracts import JobState
from m3.worker.main import create_app


def test_settings():
    url = TypeAdapter(HttpUrl)
    return Settings(
        station_ids=("station-1",),
        source_base_url=url.validate_python("http://source.internal"),
        source_api_token=SecretStr("source-secret"),
        nocobase_base_url=url.validate_python("http://nocobase.internal"),
        nocobase_api_key=SecretStr("sink-secret"),
        admin_api_token=SecretStr("admin-secret"),
    )


class FakeJobs:
    def submit(self, station_id, task):
        return JobState(
            job_id=f"{station_id}:{task}:1", station_id=station_id,
            task=task, status="queued",
        )


def fake_resources():
    forecast_state = SimpleNamespace(
        state="ready", champions={}, last_source_at=None,
        last_published_at=None, last_error_code=None,
    )
    return SimpleNamespace(
        forecast_service=SimpleNamespace(state=lambda station_id: forecast_state),
        jobs=FakeJobs(),
        scheduler=SimpleNamespace(start=lambda: None, stop=lambda: None),
        close=lambda: None,
    )


class WorkerApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(test_settings(), fake_resources(), start_scheduler=False))

    def test_state_requires_bearer_token(self):
        self.assertEqual(self.client.get("/v1/stations/station-1/state").status_code, 401)
        response = self.client.get("/v1/stations/station-1/state", headers={"Authorization": "Bearer admin-secret"})
        self.assertEqual(response.status_code, 200)

    def test_unknown_station_is_not_routable(self):
        response = self.client.get(
            "/v1/stations/unknown/state",
            headers={"Authorization": "Bearer admin-secret"},
        )
        self.assertEqual(response.status_code, 404)

    def test_manual_forecast_accepts_no_observation_or_url_body(self):
        response = self.client.post(
            "/v1/stations/station-1/runs/forecast",
            headers={"Authorization": "Bearer admin-secret"},
            json={"source_url": "https://attacker.invalid"},
        )
        self.assertEqual(response.status_code, 422)
```

- [ ] **Step 3: Run both tests and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_scheduler m3.tests.test_m3_worker_api -v`

Expected: import errors for scheduler/API modules.

- [ ] **Step 4: Implement pure due-slot calculation and single-process runner**

```python
from datetime import datetime, timedelta
from threading import Event, Lock, Thread
from zoneinfo import ZoneInfo
from m3.worker.errors import M3Error


def due_slots(now: datetime) -> tuple[str, ...]:
    slots = []
    if (now.hour, now.minute) == (0, 30):
        slots.append("model_selection")
    if (now.hour, now.minute) == (1, 2):
        slots.append("acceptance_baseline")
    elif now.minute in (2, 17, 32, 47):
        slots.append("forecast")
    return tuple(slots)


class SchedulerRunner:
    def __init__(self, station_ids, forecast_service, acceptance_service, alert_sink=None, clock=None):
        self._station_ids = tuple(station_ids)
        self._forecast = forecast_service
        self._acceptance = acceptance_service
        self._alert_sink = alert_sink or (lambda station_id, slot, code, now: None)
        self._clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self._seen = set()
        self._station_locks = {station_id: Lock() for station_id in station_ids}
        self._stop = Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("scheduler already started")
        self._stop.clear()
        self._thread = Thread(target=self.run, name="m3-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def run(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            self.tick(now)
            cutoff = now.replace(second=0, microsecond=0) - timedelta(days=2)
            self._seen = {key for key in self._seen if key[2] >= cutoff}
            self._stop.wait(15)

    def tick(self, now: datetime) -> None:
        minute_key = now.replace(second=0, microsecond=0)
        for station_id in self._station_ids:
            for slot in due_slots(now):
                key = (station_id, slot, minute_key)
                if key in self._seen:
                    continue
                with self._station_locks[station_id]:
                    try:
                        if slot == "model_selection":
                            self._forecast.bootstrap(station_id, now)
                            self._forecast.select_models(station_id)
                        elif slot == "forecast":
                            self._forecast.run_forecast(station_id, now)
                            self._acceptance.backfill_actuals(station_id, now)
                        else:
                            self._acceptance.run_baseline(station_id, now)
                    except M3Error as error:
                        self._alert_sink(station_id, slot, error.code, now)
                    finally:
                        self._seen.add(key)
```

`01:02` emits only `acceptance_baseline`: `AcceptanceService.run_baseline()` already calls the normal rolling forecast path and publishes the latest snapshot, so adding a separate `forecast` slot would run the model twice for the same `as_of`.

The alert sink receives only station, slot, safe error code, and time; it never receives a token, response body, or dynamic URL. Extract the locked operation body into `SchedulerRunner.run_manual(station_id, task, now)` so scheduled and manual work share the same per-station lock.

Add the bounded job service in `m3/worker/services/job_service.py`:

```python
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import uuid4
from m3.worker.contracts import JobState
from m3.worker.errors import M3Error


class JobService:
    def __init__(self, run_task, now, max_workers: int):
        self._run_task = run_task
        self._now = now
        self._states: dict[str, JobState] = {}
        self._lock = Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="m3-manual")

    def submit(self, station_id: str, task: str) -> JobState:
        job = JobState(
            job_id=str(uuid4()), station_id=station_id, task=task,
            status="queued", error_code=None,
        )
        with self._lock:
            self._states[job.job_id] = job
        self._pool.submit(self._execute, job.job_id)
        return job

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self._states[job_id]
            self._states[job_id] = job.model_copy(update={"status": "running"})
        try:
            self._run_task(job.station_id, job.task, self._now())
        except M3Error as error:
            update = {"status": "failed", "error_code": error.code}
        except Exception:
            update = {"status": "failed", "error_code": "internal_error"}
        else:
            update = {"status": "succeeded", "error_code": None}
        with self._lock:
            self._states[job_id] = self._states[job_id].model_copy(update=update)

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._states.get(job_id)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
```

- [ ] **Step 5: Implement typed FastAPI routes with router-level authentication**

```python
# m3/worker/api/dependencies.py
from typing import Annotated
import secrets
from fastapi import Depends, Header, HTTPException, Path, Request


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


AdminDep = Annotated[None, Depends(require_admin)]


def configured_station(
    request: Request,
    station_id: Annotated[str, Path(min_length=1)],
) -> str:
    if station_id not in request.app.state.settings.station_ids:
        raise HTTPException(status_code=404, detail="station not configured")
    return station_id


StationDep = Annotated[str, Depends(configured_station)]
```

```python
# m3/worker/api/routes.py
from fastapi import APIRouter, Depends, HTTPException, Request
from m3.worker.api.dependencies import StationDep, require_admin
from m3.worker.api.models import ManualRunRequest
from m3.worker.contracts import JobState

health_router = APIRouter(tags=["health"])
router = APIRouter(prefix="/v1", tags=["m3-operations"], dependencies=[Depends(require_admin)])


@health_router.get("/health")
def health(request: Request) -> dict[str, object]:
    states = {
        station_id: request.app.state.resources.forecast_service.state(station_id).state
        for station_id in request.app.state.settings.station_ids
    }
    ready = bool(states) and all(state in {"ready", "degraded"} for state in states.values())
    return {"status": "ok" if ready else "initializing", "stations": states}


@router.get("/stations/{station_id}/state")
def station_state(request: Request, station_id: StationDep) -> dict[str, object]:
    return request.app.state.resources.forecast_service.state(station_id).__dict__


@router.post("/stations/{station_id}/runs/forecast", status_code=202)
def run_forecast(request: Request, station_id: StationDep, payload: ManualRunRequest) -> JobState:
    return request.app.state.resources.jobs.submit(station_id, "forecast")


@router.post("/stations/{station_id}/runs/model-selection", status_code=202)
def run_model_selection(request: Request, station_id: StationDep, payload: ManualRunRequest) -> JobState:
    return request.app.state.resources.jobs.submit(station_id, "model_selection")


@router.get("/jobs/{job_id}")
def job_state(request: Request, job_id: str) -> JobState:
    job = request.app.state.resources.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
```

Create the body model in `m3/worker/api/models.py`; manual POST operations accept only an empty `{}` object, so submitted observations, URLs, or credentials fail with `422`:

```python
from pydantic import BaseModel, ConfigDict


class ManualRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

- [ ] **Step 6: Assemble app resources in lifespan without duplicate schedulers**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from m3.worker.api.routes import health_router, router


def create_app(settings, resources, start_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.resources = resources
        if start_scheduler:
            resources.recover()
            resources.scheduler.start()
        try:
            yield
        finally:
            if start_scheduler:
                resources.scheduler.stop()
            resources.close()

    app = FastAPI(title="VIFA M3 Forecast Worker", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(router)
    return app
```

`build_resources()` creates separate Source and NocoBase clients with the same explicit bounds:

```python
timeout = httpx.Timeout(connect=2.0, read=15.0, write=15.0, pool=2.0)
limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
source_http = httpx.Client(timeout=timeout, limits=limits, follow_redirects=False)
nocobase_http = httpx.Client(timeout=timeout, limits=limits, follow_redirects=False)
```

The resources object owns both clients plus `JobService`; `resources.close()` closes the job pool before both HTTP clients. The module-level `app` calls `Settings.from_env()` through `build_resources()`. Production command is exactly `uv run uvicorn m3.worker.main:app --host 127.0.0.1 --port 8013 --workers 1`.

`resources.recover()` loops over configured stations, actively performs the 90-day pull and model selection, then calls `acceptance_service.reconcile_writing_batches()` before the health state can become `ready`; a first-start failure leaves the station `initializing` and emits only the safe alert code.

- [ ] **Step 7: Run scheduler/API tests and OpenAPI inspection**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_scheduler m3.tests.test_m3_worker_api -v
uv run python -c 'from m3.worker.main import create_app; from m3.tests.m3_test_support import test_settings, fake_resources; app=create_app(test_settings(), fake_resources(), False); print(sorted(app.openapi()["paths"]))'
```

Expected: tests pass; OpenAPI contains only `/health`, state, two manual-run routes, and job lookup—no observation/bootstrap/forecast payload endpoints.

---

### Task 11: NocoBase Collection Contract and ACL Deployment Gate

**Files:**
- Create: `m3/contracts/nocobase_collections.json`
- Create: `m3/tests/fixtures/contracts/latest-smoke-record.json`
- Create: `m3/tests/test_m3_collection_contract.py`
- Create: `docs/m3/部署说明.md`

**Interfaces:**
- Consumes: the four table definitions in the approved design.
- Produces: a machine-checkable logical schema and exact operator checklist for creating/validating collections and API-key permissions.

- [ ] **Step 1: Write a failing schema-contract test**

```python
import json
from pathlib import Path
import unittest


class CollectionContractTests(unittest.TestCase):
    def test_four_collections_and_business_keys_are_exact(self):
        contract = json.loads(Path("m3/contracts/nocobase_collections.json").read_text())
        collections = {item["name"]: item for item in contract["collections"]}
        self.assertEqual(set(collections), {
            "energy_forecast_latest", "energy_forecast_batches",
            "energy_forecast_points", "energy_forecast_evaluations",
        })
        self.assertEqual(collections["energy_forecast_latest"]["unique"], [["station_id"]])
        self.assertEqual(collections["energy_forecast_points"]["unique"], [["batch_id", "unique_id", "data_time"]])
        self.assertIn("delete", contract["worker_role"]["denied_actions"])
        self.assertEqual(contract["dashboard_role"]["allowed_actions"], ["list", "get"])
        self.assertIn("point_templates", contract["dashboard_role"]["hidden_fields"]["energy_forecast_batches"])

    def test_latest_smoke_record_is_valid_json(self):
        record = json.loads(Path("m3/tests/fixtures/contracts/latest-smoke-record.json").read_text())
        self.assertEqual(record["filter"], {"station_id": "m3-contract-smoke"})
```

- [ ] **Step 2: Run the test and verify the red state**

Run: `uv run python -m unittest m3.tests.test_m3_collection_contract -v`

Expected: file-not-found failure.

- [ ] **Step 3: Add the complete logical collection contract**

The JSON root must contain:

```json
{
  "version": 1,
  "collections": [
    {
      "name": "energy_forecast_latest",
      "unique": [["station_id"]],
      "fields": {
        "station_id": "string|required",
        "as_of": "datetime_tz|required",
        "generated_at": "datetime_tz|required",
        "source_data_end": "datetime_tz|required",
        "status": "string|required",
        "series_payload": "json|required",
        "model_manifest": "json|required",
        "content_hash": "string|required",
        "updated_at": "datetime_tz|required"
      }
    },
    {
      "name": "energy_forecast_batches",
      "unique": [["station_id", "acceptance_run_id", "issued_at"]],
      "fields": {
        "station_id": "string|required",
        "acceptance_run_id": "string|required",
        "issued_at": "datetime_tz|required",
        "forecast_start_time": "datetime_tz|required",
        "forecast_end_time": "datetime_tz|required",
        "status": "string|required",
        "write_state": "string|required",
        "model_manifest": "json|required",
        "point_templates": "json|required",
        "content_hash": "string|required"
      }
    },
    {
      "name": "energy_forecast_points",
      "unique": [["batch_id", "unique_id", "data_time"]],
      "fields": {
        "batch_id": "bigint|required|index",
        "unique_id": "string|required",
        "data_time": "datetime_tz|required",
        "target_time": "datetime_tz|required",
        "horizon_step": "integer|required",
        "model_name": "string|required",
        "raw_forecast": "decimal|required",
        "forecast_value": "decimal|required",
        "is_clipped": "boolean|required",
        "actual_value": "decimal|nullable",
        "actual_quality": "string|nullable",
        "actual_source_revision": "bigint|nullable",
        "actual_recorded_at": "datetime_tz|nullable",
        "evaluated_at": "datetime_tz|nullable",
        "absolute_percentage_error": "decimal|nullable"
      }
    },
    {
      "name": "energy_forecast_evaluations",
      "unique": [["station_id", "acceptance_run_id", "evaluation_key"]],
      "fields": {
        "station_id": "string|required",
        "acceptance_run_id": "string|required",
        "evaluation_key": "string|required",
        "window_start": "datetime_tz|required",
        "window_end": "datetime_tz|required",
        "expected_count": "integer|required",
        "valid_count": "integer|required",
        "zero_actual_count": "integer|required",
        "mape_percent": "decimal|nullable",
        "mae": "decimal|nullable",
        "smape_percent": "decimal|nullable",
        "outcome": "string|required",
        "calculated_at": "datetime_tz|required"
      }
    }
  ],
  "worker_role": {
    "allowed_actions": ["list", "get", "create", "update", "updateOrCreate", "firstOrCreate"],
    "denied_actions": ["destroy", "delete", "export", "import"]
  },
  "dashboard_role": {
    "allowed_actions": ["list", "get"],
    "denied_actions": ["create", "update", "destroy", "delete", "export", "import"],
    "hidden_fields": {
      "energy_forecast_batches": ["point_templates"]
    }
  }
}
```

Create `latest-smoke-record.json` with one valid, clearly non-production record:

```json
{
  "filter": {"station_id": "m3-contract-smoke"},
  "values": {
    "station_id": "m3-contract-smoke",
    "as_of": "2026-08-25T01:17:00+08:00",
    "generated_at": "2026-08-25T01:17:05+08:00",
    "source_data_end": "2026-08-25T01:00:00+08:00",
    "status": "degraded",
    "series_payload": [
      {"unique_id": "station_total_load", "unit": "kW", "model_name": "none", "status": "error", "points": [], "fallback_reason": "contract_smoke"},
      {"unique_id": "storage_1_soc", "unit": "%", "model_name": "none", "status": "error", "points": [], "fallback_reason": "contract_smoke"},
      {"unique_id": "storage_2_soc", "unit": "%", "model_name": "none", "status": "error", "points": [], "fallback_reason": "contract_smoke"}
    ],
    "model_manifest": {"statsforecast_version": "2.1.1"},
    "content_hash": "7e4b1d5ccf6cbdd4118c00a8298aa4f0ad1c385df902edc7511f5fbb8ab79b8b",
    "updated_at": "2026-08-25T01:17:06+08:00"
  }
}
```

- [ ] **Step 4: Add exact NocoBase setup and verification commands to the deployment guide**

Document these operations without putting secrets in the file:

```bash
curl -fsS -H "Authorization: Bearer ${M3_NOCOBASE_API_KEY}" \
  "${M3_NOCOBASE_BASE_URL}/api/energy_forecast_latest:list?page=1&pageSize=1"
curl -fsS -X POST -H "Authorization: Bearer ${M3_NOCOBASE_API_KEY}" \
  -H "Content-Type: application/json" \
  "${M3_NOCOBASE_BASE_URL}/api/energy_forecast_latest:updateOrCreate" \
  --data-binary @m3/tests/fixtures/contracts/latest-smoke-record.json
```

The guide requires an administrator to create four collections from the exact contract, add every unique/index constraint, activate the API Key plugin, create the minimal Worker role, inspect each action in the installed API Documentation plugin, and remove the smoke row with an administrator credential—not the Worker key.

- [ ] **Step 5: Run the contract test and JSON parser**

Run:

```bash
uv run python -m unittest m3.tests.test_m3_collection_contract -v
uv run python -m json.tool m3/contracts/nocobase_collections.json >/dev/null
```

Expected: contract test passes and the JSON parses.

---

### Task 12: Node-RED Source API and Same-Origin Dashboard Gateway

**Files:**
- Create: `m3/node_red/forecast_contract.js`
- Create: `m3/node_red/energy_forecast_flow.json`
- Create: `tests/test_m3_node_red_contract.js`
- Modify: `docs/m3/部署说明.md`

**Interfaces:**
- Consumes: existing EMS records, environment-supplied manufacturer field mapping, NocoBase M3 result APIs, and fixed HTTP paths.
- Produces: `GET /internal/energy-forecast/v1/stations/:station_id/observations`, `GET /internal/energy-forecast/v1/stations/:station_id/acceptance-context`, and public `GET /energy-forecast-api`.

- [ ] **Step 1: Write failing Node contract tests**

```javascript
const assert = require("assert");
const fs = require("fs");
const contract = require("../m3/node_red/forecast_contract.js");

function makeThirtySecondRecords() {
  const start = Date.parse("2026-08-25T00:00:00+08:00");
  return Array.from({ length: 30 }, (_, index) => ({
    time: new Date(start + index * 30000).toISOString(),
    load_kw: 800 + index,
    soc_1: 50 + index / 100,
    soc_2: 60 + index / 100,
    updated_at: new Date(start + index * 30000 + 1000).toISOString(),
  }));
}

const points = contract.aggregateObservations({
  stationId: "station-1",
  records: makeThirtySecondRecords(),
  mapping: {
    timestamp: "time",
    stationTotalLoad: "load_kw",
    storage1Soc: "soc_1",
    storage2Soc: "soc_2",
  },
  start: "2026-08-25T00:00:00+08:00",
  end: "2026-08-25T00:15:00+08:00",
});
assert.strictEqual(points.length, 3);
assert.strictEqual(points[0].ds, "2026-08-25T00:00:00+08:00");
assert.strictEqual(points[0].quality, "valid");

const flow = JSON.parse(fs.readFileSync("m3/node_red/energy_forecast_flow.json"));
const urls = flow.filter((node) => node.type === "http in").map((node) => node.url);
assert.ok(urls.includes("/internal/energy-forecast/v1/stations/:station_id/observations"));
assert.ok(urls.includes("/energy-forecast-api"));
assert.ok(!JSON.stringify(flow).includes("/runs/forecast"));
assert.ok(!JSON.stringify(flow).includes("/bootstrap"));
```

- [ ] **Step 2: Run the test and verify the red state**

Run: `node tests/test_m3_node_red_contract.js`

Expected: module/file-not-found failure.

- [ ] **Step 3: Implement pure mapping and 15-minute quality functions**

Export these exact functions from `forecast_contract.js`:

```javascript
"use strict";

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function bucketStart(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error("invalid timestamp");
  date.setUTCMinutes(Math.floor(date.getUTCMinutes() / 15) * 15, 0, 0);
  return date;
}

function timeWeightedMean(samples, bucketEnd) {
  if (samples.length < 2) return null;
  let weighted = 0;
  let coveredMs = 0;
  for (let index = 0; index < samples.length - 1; index += 1) {
    const span = samples[index + 1].time - samples[index].time;
    if (span > 120000) return null;
    weighted += samples[index].value * span;
    coveredMs += span;
  }
  const tail = bucketEnd - samples.at(-1).time;
  if (tail <= 120000) {
    weighted += samples.at(-1).value * tail;
    coveredMs += tail;
  }
  return coveredMs >= 720000 ? weighted / coveredMs : null;
}

function lastFreshSoc(samples, bucketEnd) {
  const last = samples.at(-1);
  return last && bucketEnd - last.time <= 90000 ? last.value : null;
}

function isoShanghai(milliseconds) {
  return new Date(milliseconds + 8 * 60 * 60 * 1000)
    .toISOString().replace("Z", "+08:00");
}

function aggregateObservations({ stationId, records, mapping, start, end }) {
  const required = ["timestamp", "stationTotalLoad", "storage1Soc", "storage2Soc"];
  if (!stationId || !Array.isArray(records) || required.some((key) => !mapping?.[key])) {
    throw new Error("source_not_configured");
  }
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs || endMs - startMs > 7 * 86400000) {
    throw new Error("invalid_source_range");
  }
  const buckets = new Map();
  for (let cursor = startMs; cursor < endMs; cursor += 900000) {
    buckets.set(cursor, { load: [], soc1: [], soc2: [], revision: 1 });
  }
  for (const record of records) {
    const time = Date.parse(record[mapping.timestamp]);
    if (!Number.isFinite(time) || time < startMs || time >= endMs) continue;
    const bucket = Math.floor(time / 900000) * 900000;
    const target = buckets.get(bucket);
    if (!target) continue;
    const load = record[mapping.stationTotalLoad];
    const soc1 = record[mapping.storage1Soc];
    const soc2 = record[mapping.storage2Soc];
    if (finiteNumber(load) && load >= 0) target.load.push({ time, value: load });
    if (finiteNumber(soc1) && soc1 >= 0 && soc1 <= 100) target.soc1.push({ time, value: soc1 });
    if (finiteNumber(soc2) && soc2 >= 0 && soc2 <= 100) target.soc2.push({ time, value: soc2 });
    if (mapping.updatedAt) {
      const revision = Date.parse(record[mapping.updatedAt]);
      if (Number.isFinite(revision)) target.revision = Math.max(target.revision, revision);
    }
  }
  const points = [];
  for (const [bucket, values] of buckets) {
    const bucketEnd = bucket + 900000;
    values.load.sort((a, b) => a.time - b.time);
    values.soc1.sort((a, b) => a.time - b.time);
    values.soc2.sort((a, b) => a.time - b.time);
    const output = [
      ["station_total_load", timeWeightedMean(values.load, bucketEnd)],
      ["storage_1_soc", lastFreshSoc(values.soc1, bucketEnd)],
      ["storage_2_soc", lastFreshSoc(values.soc2, bucketEnd)],
    ];
    for (const [uniqueId, value] of output) {
      points.push({
        unique_id: uniqueId,
        ds: isoShanghai(bucket),
        y: value,
        quality: value === null ? "invalid" : "valid",
        source_revision: values.revision,
      });
    }
  }
  return points;
}

function buildDashboard({ latest, actuals, acceptance, nowMs = Date.now() }) {
  if (!latest || !Array.isArray(latest.series_payload)) throw new Error("latest_snapshot_missing");
  const sortedActuals = [...(actuals || [])].sort((left, right) => Date.parse(left.ds) - Date.parse(right.ds));
  const actualBySeries = {};
  for (const point of sortedActuals) {
    (actualBySeries[point.unique_id] ||= []).push(point);
  }
  const series = latest.series_payload.map((item) => ({
    unique_id: item.unique_id,
    unit: item.unit,
    model_name: item.model_name,
    status: item.status,
    actual: (actualBySeries[item.unique_id] || []).map((point) => ({
      time: point.ds, value: point.y, quality: point.quality,
    })),
    forecast: item.points.map((point) => ({
      time: point.data_time, target_time: point.target_time,
      value: point.forecast_value, raw_value: point.raw_forecast,
      is_clipped: point.is_clipped,
    })),
  }));
  const forecastPoints = series
    .flatMap((item) => item.forecast)
    .sort((left, right) => Date.parse(left.time) - Date.parse(right.time));
  return {
    operation: "forecast_dashboard",
    range: {
      timezone: "Asia/Shanghai",
      history_start: sortedActuals[0]?.ds || null,
      actual_latest: sortedActuals.at(-1)?.ds || null,
      forecast_start: forecastPoints[0]?.time || null,
      forecast_end: forecastPoints.at(-1)?.target_time || null,
      interval_seconds: 900,
    },
    system: {
      state: latest.status === "ok" ? "ready" : latest.status,
      mode: latest.status === "degraded" ? "degraded" : "normal",
      generated_at: latest.generated_at,
      stale: nowMs - Date.parse(latest.generated_at) > 30 * 60 * 1000,
    },
    series,
    acceptance,
  };
}

module.exports = {
  aggregateObservations, buildDashboard, bucketStart,
  lastFreshSoc, timeWeightedMean,
};
```

The function filters `[start,end)`, emits three points for every bucket, preserves invalid points as `y:null`, and assigns a deterministic revision from the maximum configured `updatedAt` value or `1` for immutable source records.

- [ ] **Step 4: Create the importable Node-RED flow without Worker triggers**

The exported flow must contain three HTTP-in chains:

```text
observations http-in
  -> validate configured station/start/end/cursor
  -> query existing EMS source through configured HTTP request node
  -> global.get("m3ForecastContract").aggregateObservations(...)
  -> {status:"ok",data:{station_id,timezone:"Asia/Shanghai",interval_seconds:900,points,next_cursor}}
  -> http-response

acceptance-context http-in
  -> query existing EMS acceptance context
  -> {active,acceptance_run_id,window_start,window_end}
  -> http-response

energy-forecast-api http-in
  -> validate existing iframe token and authorized station
  -> query latest snapshot + complete batches/points/evaluations + prior-24-hour actuals
  -> global.get("m3ForecastContract").buildDashboard(...)
  -> http-response
```

Function nodes read `M3_SOURCE_RECORDS_URL`, `M3_SOURCE_READ_TOKEN`, `M3_FIELD_MAPPING_JSON`, `M3_NOCOBASE_BASE_URL`, and a read-only dashboard API key from Node-RED credential/environment storage. Missing mapping returns `503 source_not_configured`; query parameters can never override these URLs.

- [ ] **Step 5: Add exact Node-RED installation wiring**

Add to the deployment guide:

```javascript
functionGlobalContext: {
  m3ForecastContract: require("/data/m3_node_red/forecast_contract.js")
}
```

Then copy the module to that path, set environment/credential values, import `energy_forecast_flow.json`, wire the existing EMS record provider, deploy, and verify that Node-RED exposes the three GET routes while showing no scheduled Inject node targeting the Worker.

- [ ] **Step 6: Run Node contract tests and scan the flow**

Run:

```bash
node tests/test_m3_node_red_contract.js
rg -n 'runs/forecast|runs/model-selection|bootstrap|exec' m3/node_red
```

Expected: Node tests pass; scan finds no Worker trigger or `exec` node.

---

### Task 13: Standalone Forecast Dashboard and End-to-End Verification

**Files:**
- Create: `场站未来能耗预测.html`
- Extend: `m3/tests/m3_test_support.py`
- Create: `m3/tests/m3_dashboard_e2e.js`
- Modify: `docs/m3/部署说明.md`

**Interfaces:**
- Consumes: same-origin `GET /energy-forecast-api` response containing `range`, `system`, `series`, and `acceptance`.
- Produces: two accessible 48-hour SVG charts, status/model cards, acceptance progress, and fixed same-origin fetch behavior.

- [ ] **Step 1: Add a deterministic public dashboard fixture**

```python
from datetime import datetime, timedelta


def make_dashboard_series() -> list[dict]:
    history_start = datetime.fromisoformat("2026-08-24T01:00:00+08:00")
    forecast_start = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
    series = []
    for unique_id, unit, base in (
        ("station_total_load", "kW", 800.0),
        ("storage_1_soc", "%", 50.0),
        ("storage_2_soc", "%", 60.0),
    ):
        actual = [{
            "time": (history_start + timedelta(minutes=15 * index)).isoformat(),
            "value": None if index == 20 else base + index / 10,
            "quality": "invalid" if index == 20 else "valid",
        } for index in range(96)]
        forecast = []
        for index in range(96):
            raw = 103.0 if unique_id == "storage_1_soc" and index == 10 else base + index / 20
            published = 100.0 if raw > 100 and unit == "%" else raw
            forecast.append({
                "time": (forecast_start + timedelta(minutes=15 * index)).isoformat(),
                "target_time": (forecast_start + timedelta(minutes=15 * (index + 1))).isoformat(),
                "value": published,
                "raw_value": raw,
                "is_clipped": raw != published,
            })
        series.append({
            "unique_id": unique_id,
            "unit": unit,
            "model_name": "SeasonalNaive",
            "status": "ok",
            "actual": actual,
            "forecast": forecast,
        })
    return series


def build_m3_dashboard_response() -> dict:
    return {
        "status": "ok",
        "data": {
            "operation": "forecast_dashboard",
            "range": {
                "timezone": "Asia/Shanghai",
                "history_start": "2026-08-24T01:00:00+08:00",
                "actual_latest": "2026-08-25T00:45:00+08:00",
                "forecast_start": "2026-08-25T01:00:00+08:00",
                "forecast_end": "2026-08-26T01:00:00+08:00",
                "interval_seconds": 900,
            },
            "system": {"state": "ready", "mode": "normal", "generated_at": "2026-08-25T01:02:08+08:00", "stale": False},
            "series": make_dashboard_series(),
            "acceptance": {
                "acceptance_run_id": "run-20260825", "status": "in_progress",
                "completed_days": 1, "expected_days": 7,
                "results": [{"unique_id": "station_total_load", "valid_count": 96, "mape_percent": 8.42}],
            },
        },
    }
```

The fixture returns three series with 96 actual points for the previous 24 hours and 96 forecast points for the next 24 hours; it includes one `null` gap and one clipped SOC forecast.

- [ ] **Step 2: Write the failing Playwright test**

```javascript
const assert = require("assert");
const fs = require("fs");
const http = require("http");
const { spawnSync } = require("child_process");
const { once } = require("events");
const { chromium } = require("playwright");

(async () => {
  const html = fs.readFileSync("场站未来能耗预测.html");
  const server = http.createServer((request, response) => {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(html);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const fixture = spawnSync("uv", ["run", "python", "-c",
    "import json; from m3.tests.m3_test_support import build_m3_dashboard_response; print(json.dumps(build_m3_dashboard_response()))",
  ], { encoding: "utf8" });
  assert.strictEqual(fixture.status, 0, fixture.stderr);
  const payload = JSON.parse(fixture.stdout);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const apiRequests = [];
    const externalRequests = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.hostname !== "127.0.0.1") externalRequests.push(request.url());
    });
    await page.route("**/energy-forecast-api*", async (route) => {
      apiRequests.push(route.request().url());
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
    });
    const port = server.address().port;
    await page.goto(
      `http://127.0.0.1:${port}/场站未来能耗预测.html?token=example-token&api=${encodeURIComponent("https://example.invalid/leak")}`,
      { waitUntil: "networkidle" },
    );
    await page.locator("#forecast-dashboard[data-state='ready']").waitFor();
    assert.strictEqual(await page.locator("#load-chart path[data-kind='actual']").count(), 1);
    assert.strictEqual(await page.locator("#load-chart path[data-kind='forecast']").count(), 1);
    assert.strictEqual(await page.locator("#soc-chart path[data-series='storage_1_soc'][data-kind='forecast']").count(), 1);
    assert.strictEqual(
      await page.locator("#load-chart path[data-kind='actual']").evaluate((node) => getComputedStyle(node).strokeDasharray),
      "none",
    );
    assert.notStrictEqual(
      await page.locator("#load-chart path[data-kind='forecast']").evaluate((node) => getComputedStyle(node).strokeDasharray),
      "none",
    );
    assert.ok((await page.locator("#load-chart path[data-kind='actual']").getAttribute("d")).match(/\bM\b/g).length >= 2);
    assert.strictEqual(await page.locator("[data-kind='current-divider']").count(), 2);
    assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-window-hours"), "48");
    assert.strictEqual(await page.locator("#acceptance-progress").innerText(), "1 / 7 天");
    assert.strictEqual(await page.locator("#system-state").innerText(), "正常");
    assert.ok(apiRequests[0].includes("token=example-token"));
    assert.deepStrictEqual(externalRequests, []);
    const beforeRefresh = apiRequests.length;
    await page.evaluate(async () => Promise.all([loadDashboard(), loadDashboard()]));
    assert.strictEqual(apiRequests.length, beforeRefresh + 1);
    for (const [state, expected] of [
      ["initializing", "初始化中"], ["warming_up", "预热中"],
      ["degraded", "降级"], ["error", "错误"],
    ]) {
      await page.evaluate(({ data, state }) => {
        renderDashboard({ ...data, system: { ...data.system, state, stale: false } });
      }, { data: payload.data, state });
      assert.strictEqual(await page.locator("#system-state").innerText(), expected);
    }
    await page.evaluate((data) => {
      renderDashboard({ ...data, system: { ...data.system, state: "ready", stale: true } });
    }, payload.data);
    assert.strictEqual(await page.locator("#system-state").innerText(), "陈旧");
    await page.evaluate(() => renderError("预测数据加载失败"));
    assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "error");
    await page.evaluate((data) => renderDashboard({ ...data, series: [] }), payload.data);
    assert.strictEqual(await page.locator("#empty-state").innerText(), "暂无预测数据");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
```

The HTML implementation must expose the named `loadDashboard`, `renderDashboard`, and `renderError` functions used by this same-origin black-box test; it must not add a test-only remote control surface.

- [ ] **Step 3: Run the browser test and verify the red state**

Run: `node m3/tests/m3_dashboard_e2e.js`

Expected: file-not-found failure for `场站未来能耗预测.html`.

- [ ] **Step 4: Implement the fixed same-origin loader and state model**

```javascript
const pageParams = new URLSearchParams(window.location.search);
const token = pageParams.get("token");
const apiUrl = "/energy-forecast-api";
let fetchInFlight = false;

async function loadDashboard() {
  if (fetchInFlight) return;
  fetchInFlight = true;
  const endpoint = new URL(apiUrl, window.location.origin);
  if (token) endpoint.searchParams.set("token", token);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(endpoint, { credentials: "same-origin", signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    renderDashboard(payload.data);
  } catch (error) {
    renderError(error.name === "AbortError" ? "请求超时" : "预测数据加载失败");
  } finally {
    window.clearTimeout(timeout);
    fetchInFlight = false;
  }
}
```

Ignore the `api` query parameter completely. Never embed demo curves or replace `null` with zero.

- [ ] **Step 5: Implement the two 48-hour SVG renderers**

Use one reusable pure renderer:

```javascript
function buildPath(points, valueKey, xScale, yScale) {
  let path = "";
  let previousTime = null;
  for (const point of points) {
    const time = Date.parse(point.time);
    const value = point[valueKey];
    const gap = previousTime !== null && time - previousTime > 15 * 60 * 1000;
    if (value === null || value === undefined || !Number.isFinite(time) || !Number.isFinite(Number(value))) {
      previousTime = null;
      continue;
    }
    path += `${previousTime === null || gap ? " M " : " L "}${xScale(time)} ${yScale(Number(value))}`;
    previousTime = time;
  }
  return path;
}
```

Render `#load-chart` with kW scale and one actual/forecast pair. Render `#soc-chart` with percent scale `0..100` and actual/forecast pairs for both storage systems. Add a vertical current-time divider, legends, accessible labels, tooltips with target time/model/generated time, status cards, and the acceptance table.

- [ ] **Step 6: Run browser and existing-dashboard regressions**

Run:

```bash
node m3/tests/m3_dashboard_e2e.js
node m2/tests/energy_dashboard_e2e.js
```

Expected: both M3 and existing M1/M2 dashboards pass; no external request or console error is recorded.

- [ ] **Step 7: Run the complete implementation verification gate**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_m3_*.py' -v
uv run python -m unittest m2.tests.test_station_energy_backend m2.tests.test_station_efficiency_history -v
node tests/test_m3_node_red_contract.js
node m3/tests/m3_dashboard_e2e.js
node m2/tests/energy_dashboard_e2e.js
uv run python -m json.tool m3/contracts/nocobase_collections.json >/dev/null
rg -n 'MLForecast|NeuralForecast|Prophet|psycopg|asyncpg|postgresql://' m3.worker
rg -n 'runs/forecast|runs/model-selection|bootstrap|exec' m3/node_red
```

Expected: every test passes; both scans return no disallowed runtime/library or Node-RED trigger.

- [ ] **Step 8: Complete live deployment gates in order**

Follow `docs/m3/部署说明.md` exactly:

1. Populate actual station IDs and the manufacturer field mapping in protected Node-RED configuration.
2. Create/verify the four NocoBase collections, unique keys, indexes, and Worker/dashboard roles.
3. Test Source API 7-day pagination, `[start,end)`, three-series completeness, invalid quality, and revision behavior with production-like data.
4. Test every NocoBase action body against the installed API Documentation plugin and update only `nocobase_api.py` if the installed format differs.
5. Start exactly one Worker with `--workers 1`; verify startup 90-day pull, `00:30` model selection, and one `02/17/32/47` rolling snapshot.
6. Verify `energy_forecast_latest` contains one row per station and no rolling history rows.
7. Activate an EMS acceptance run, verify the `01:02` batch reaches `complete` with exactly 288 points, and interrupt one test write to prove reconciliation.
8. Embed `场站未来能耗预测.html` through the same-origin iframe and verify tenant/station isolation.
9. Start the seven-day run only after all prior gates pass; do not backfill or replace a missed baseline.

Expected: production readiness is not claimed until all nine gates are recorded with timestamps, station ID, action status, and sanitized error codes.
