# M4 Mathematical Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个与数据库、AI 和 EMS 解耦的纯 Python MILP 优化核心，为单个电站生成均衡、成本优先和光伏优先三套可验证的 96 点调度候选。

**Architecture:** 使用 Pydantic 定义严格输入输出契约，以 SciPy `milp`/HiGHS 构建站级储能模型，通过重复求解和目标锁定实现分层优化。优化核心只接收完整请求并返回候选；独立验证器复算功率平衡、SOC 和指标，不包含 M3、AI、EMS、数据库或页面集成。

**Tech Stack:** Python 3.12、Pydantic 2.13.4、NumPy 2.4.6、SciPy 1.18.1/HiGHS、`unittest`

**Spec:** `m4/docs/superpowers/specs/2026-09-04-m4-mathematical-optimizer-design.md`

## Global Constraints

- 一次请求只优化一个电站；两个电站由编排层分别调用，状态和失败完全隔离。
- 时间范围固定为未来 24 小时、15 分钟粒度、96 个计划点和 97 个 SOC 状态点。
- `initial_soc_pct` 只能来自 EMS 当前能力快照；M3 未来 SOC 不能进入状态方程或硬约束。
- 负荷输入必须是不含储能充放电功率的站内原生负荷。
- 安全 SOC、功率、设备可用性和物理进线边界是硬约束，不得进入可降级目标序列。
- 需量是最高优先级软目标，不能通过削减负荷或突破储能安全边界满足。
- 优化器内部不填补缺失预测，不访问数据库、M3、AI 或 EMS。
- 输出功率始终为非负值，使用 `mode=charge|discharge|idle` 表示方向。
- `expected_soc_pct` 只表示预计轨迹，不是 EMS 的直接 SOC 指令。
- 首版不实现人工调整、EMU 分配、真实 AI、真实 EMS 或静态页面改造。
- SciPy 必须作为 `pyproject.toml` 的显式固定依赖，不能依赖传递安装。
- 实现过程使用 TDD；每个任务先看到目标测试失败，再写最小实现。

---

## 文件结构

本计划创建以下文件：

```text
m4_optimizer/
├── __init__.py          # 对外导出稳定入口和契约
├── contracts.py         # 严格请求、配置、候选与结果模型
├── solver.py            # 通用 SciPy MILP 调用和状态转换
├── model.py             # 变量索引、稀疏约束矩阵和目标向量
├── lexicographic.py     # 分层目标求解与容差锁定
├── metrics.py           # 计划解码与汇总指标复算
├── validation.py        # 请求域校验和候选独立复验
├── profiles.py          # 三类配置的完整性与固定输出顺序
├── service.py           # 一次请求生成三套候选
└── README.md            # 模块边界、调用示例和验证命令

tests/
├── m4_optimizer_test_support.py
├── test_m4_optimizer_contracts.py
├── test_m4_optimizer_solver.py
├── test_m4_optimizer_model.py
├── test_m4_optimizer_lexicographic.py
├── test_m4_optimizer_validation.py
└── test_m4_optimizer_scenarios.py
```

现有 `m4/` 静态 HTML、M3 代码、EMS 配置和数据库代码不在本计划修改范围内。

---

### Task 1: 严格契约与测试数据工厂

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `m4_optimizer/__init__.py`
- Create: `m4_optimizer/contracts.py`
- Create: `tests/m4_optimizer_test_support.py`
- Create: `tests/test_m4_optimizer_contracts.py`

**Interfaces:**
- Consumes: 无。
- Produces: `OptimizationRequest`、`ObjectiveProfile`、`ObjectiveLayer`、`PlanPoint`、`CandidateMetrics`、`CandidateResult`、`OptimizationResult`、`CandidateStatus`、`ProfileId`、`ObjectiveName`，以及测试工厂 `make_request()` 和 `make_profiles()`。

- [ ] **Step 1: 编写契约失败测试**

在 `tests/test_m4_optimizer_contracts.py` 中覆盖以下行为：

```python
import unittest
from datetime import timedelta

from pydantic import ValidationError

from tests.m4_optimizer_test_support import make_request


class M4OptimizerContractTests(unittest.TestCase):
    def test_request_requires_exactly_96_contiguous_points(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"] = payload["points"][:-1]
        with self.assertRaisesRegex(ValidationError, "96 points"):
            type(request).model_validate(payload)

    def test_request_rejects_a_shifted_timestamp(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"][12]["timestamp"] += timedelta(minutes=1)
        with self.assertRaisesRegex(ValidationError, "15-minute timeline"):
            type(request).model_validate(payload)

    def test_request_rejects_non_finite_forecast(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"][2]["pv_forecast_kw"] = float("nan")
        with self.assertRaises(ValidationError):
            type(request).model_validate(payload)

    def test_request_requires_balanced_cost_and_pv_profiles_once_each(self):
        request = make_request()
        payload = request.model_dump()
        payload["profiles"] = payload["profiles"][:2]
        with self.assertRaisesRegex(ValidationError, "balanced, cost and pv"):
            type(request).model_validate(payload)

    def test_every_profile_keeps_demand_as_the_first_soft_goal(self):
        request = make_request()
        payload = request.model_dump()
        payload["profiles"][0]["objective_order"][0]["terms"] = {"energy_cost": 1.0}
        with self.assertRaisesRegex(ValidationError, "demand objectives"):
            type(request).model_validate(payload)

    def test_initial_soc_must_be_inside_absolute_bounds(self):
        request = make_request()
        payload = request.model_dump()
        payload["capability"]["initial_soc_pct"] = 5.0
        payload["constraints"]["soc_min_pct"] = 10.0
        with self.assertRaisesRegex(ValidationError, "initial SOC"):
            type(request).model_validate(payload)

    def test_stale_ems_snapshot_is_rejected(self):
        request = make_request()
        payload = request.model_dump()
        payload["input_observed_at"] = request.plan_start_at - timedelta(minutes=31)
        payload["max_input_age_seconds"] = 1800
        with self.assertRaisesRegex(ValidationError, "stale"):
            type(request).model_validate(payload)
```

- [ ] **Step 2: 运行契约测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_contracts.py -v
```

Expected: FAIL，原因是 `m4_optimizer.contracts` 或测试工厂尚不存在。

- [ ] **Step 3: 增加 SciPy 显式依赖并创建契约**

执行：

```bash
uv add scipy==1.18.1
```

在 `m4_optimizer/contracts.py` 中定义以下稳定字段和字面量；所有模型使用 `ConfigDict(extra="forbid", strict=True)`，所有数值字段禁止 `NaN` 和无穷值：

```python
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
            if set(flattened) != required_objectives or len(flattened) != len(required_objectives):
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
```

同时定义输出模型：

```python
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
```

在 `tests/m4_optimizer_test_support.py` 中用固定 `+08:00` 时间、平坦负荷/光伏和三份完整目标配置构建 `make_request(**overrides)`。工厂必须创建新的列表和模型，不能在测试之间共享可变对象。测试目标配置由以下函数创建：

```python
def layer(name: str, terms: dict[str, float]) -> ObjectiveLayer:
    return ObjectiveLayer(
        name=name,
        terms=terms,
        absolute_tolerance=1e-6,
        relative_tolerance=0.0,
    )


def make_profiles() -> list[ObjectiveProfile]:
    orders = {
        "balanced": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("balanced-value", {"energy_cost": 0.01, "pv_unused": 0.001}),
            layer("throughput", {"throughput": 1.0}),
        ],
        "cost": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("energy-cost", {"energy_cost": 1.0}),
            layer("pv-unused", {"pv_unused": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("throughput", {"throughput": 1.0}),
        ],
        "pv": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("pv-unused", {"pv_unused": 1.0}),
            layer("energy-cost", {"energy_cost": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("throughput", {"throughput": 1.0}),
        ],
    }
    return [
        ObjectiveProfile(
            profile_id=profile_id,
            profile_version="test-v1",
            objective_order=orders[profile_id],
        )
        for profile_id in ("balanced", "cost", "pv")
    ]
```

在 `m4_optimizer/__init__.py` 中只导出契约类型；求解入口留到 Task 6 再加入。

- [ ] **Step 4: 运行契约测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_contracts.py -v
```

Expected: 7 tests PASS。

- [ ] **Step 5: 提交契约与依赖**

```bash
git add pyproject.toml uv.lock m4_optimizer/__init__.py m4_optimizer/contracts.py tests/m4_optimizer_test_support.py tests/test_m4_optimizer_contracts.py
git commit -m "feat(m4): define optimizer contracts"
```

---

### Task 2: SciPy MILP 求解器适配层

**Files:**
- Create: `m4_optimizer/solver.py`
- Create: `tests/test_m4_optimizer_solver.py`

**Interfaces:**
- Consumes: `CandidateStatus` from `m4_optimizer.contracts`。
- Produces: `MilpProblem`、`ObjectiveLock`、`RawSolveResult`、`solve_milp(problem, objective, locks, time_limit_seconds, mip_rel_gap)`。

- [ ] **Step 1: 编写通用求解器失败测试**

```python
import unittest
from unittest.mock import patch

import numpy as np
from scipy.optimize import OptimizeResult
from scipy.sparse import csr_matrix

from m4_optimizer.solver import MilpProblem, solve_milp


class M4OptimizerSolverTests(unittest.TestCase):
    def test_solver_finds_the_cheapest_binary_choice(self):
        problem = MilpProblem(
            integrality=np.array([1, 1], dtype=np.uint8),
            lower_bounds=np.array([0.0, 0.0]),
            upper_bounds=np.array([1.0, 1.0]),
            matrix=csr_matrix([[1.0, 1.0]]),
            constraint_lower=np.array([1.0]),
            constraint_upper=np.array([np.inf]),
        )
        result = solve_milp(
            problem,
            objective=np.array([1.0, 2.0]),
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertEqual(result.status, "optimal")
        np.testing.assert_allclose(result.x, [1.0, 0.0], atol=1e-7)

    def test_solver_maps_an_infeasible_problem(self):
        problem = MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix([[1.0]]),
            constraint_lower=np.array([2.0]),
            constraint_upper=np.array([np.inf]),
        )
        result = solve_milp(
            problem,
            objective=np.array([1.0]),
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.x)

    def test_time_limited_finite_incumbent_is_feasible(self):
        problem = self.make_one_variable_problem()
        fake = OptimizeResult(
            status=1,
            x=np.array([0.5]),
            message="time limit",
            mip_gap=0.2,
        )
        with patch("m4_optimizer.solver.milp", return_value=fake):
            result = solve_milp(problem, np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "feasible")
        self.assertAlmostEqual(result.objective_value, 0.5)

    def test_time_limit_without_an_incumbent_is_timeout(self):
        problem = self.make_one_variable_problem()
        fake = OptimizeResult(status=1, x=None, message="time limit")
        with patch("m4_optimizer.solver.milp", return_value=fake):
            result = solve_milp(problem, np.array([1.0]), (), 2.0, 0.0)
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.x)

    def make_one_variable_problem(self):
        return MilpProblem(
            integrality=np.array([0], dtype=np.uint8),
            lower_bounds=np.array([0.0]),
            upper_bounds=np.array([1.0]),
            matrix=csr_matrix((0, 1)),
            constraint_lower=np.array([], dtype=float),
            constraint_upper=np.array([], dtype=float),
        )
```

该测试文件同时导入 `OptimizeResult` 和 `patch`。

- [ ] **Step 2: 运行求解器测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_solver.py -v
```

Expected: FAIL，原因是 `m4_optimizer.solver` 尚不存在。

- [ ] **Step 3: 实现求解器适配层**

在 `m4_optimizer/solver.py` 中使用以下数据结构：

```python
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix, vstack

from m4_optimizer.contracts import CandidateStatus


FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class MilpProblem:
    integrality: NDArray[np.uint8]
    lower_bounds: FloatArray
    upper_bounds: FloatArray
    matrix: csr_matrix
    constraint_lower: FloatArray
    constraint_upper: FloatArray


@dataclass(frozen=True)
class ObjectiveLock:
    vector: FloatArray
    upper_bound: float


@dataclass(frozen=True)
class RawSolveResult:
    status: CandidateStatus
    x: FloatArray | None
    objective_value: float | None
    message: str
    mip_gap: float | None
```

`solve_milp()` 必须检查目标向量和锁定向量长度，使用 `vstack` 把每个锁定目标追加为 `-inf <= c_lock @ x <= upper_bound`，然后调用：

```python
result = milp(
    c=objective,
    integrality=problem.integrality,
    bounds=Bounds(problem.lower_bounds, problem.upper_bounds),
    constraints=LinearConstraint(matrix, lower, upper),
    options={
        "time_limit": time_limit_seconds,
        "mip_rel_gap": mip_rel_gap,
        "presolve": True,
    },
)
```

按 SciPy 状态和是否存在有限 `x` 转换：

```python
if result.status == 0:
    status = "optimal"
elif result.status == 1 and result.x is not None and np.isfinite(result.x).all():
    status = "feasible"
elif result.status == 1:
    status = "timeout"
elif result.status == 2:
    status = "infeasible"
else:
    status = "error"
```

仅当 `x` 全部有限时保留解；`objective_value` 必须用传入目标向量重新计算，不能盲目信任求解器字段。

- [ ] **Step 4: 运行求解器测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_solver.py -v
```

Expected: 4 tests PASS。

- [ ] **Step 5: 提交求解器适配层**

```bash
git add m4_optimizer/solver.py tests/test_m4_optimizer_solver.py
git commit -m "feat(m4): add scipy milp solver adapter"
```

---

### Task 3: 基础储能 MILP 模型

**Files:**
- Create: `m4_optimizer/model.py`
- Create: `tests/test_m4_optimizer_model.py`

**Interfaces:**
- Consumes: `OptimizationRequest` and `PlanPoint` from `contracts.py`; `MilpProblem` from `solver.py`。
- Produces: `VariableIndex`、`BuiltModel`、`build_model(request)`，其中 `BuiltModel.objectives` 包含六个 `ObjectiveName` 对应的线性向量。

- [ ] **Step 1: 编写物理模型失败测试**

```python
import unittest

import numpy as np

from m4_optimizer.model import build_model
from m4_optimizer.solver import solve_milp
from tests.m4_optimizer_test_support import make_request


class M4OptimizerModelTests(unittest.TestCase):
    def test_unavailable_storage_has_zero_charge_and_discharge(self):
        request = make_request(available=False)
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        self.assertEqual(result.status, "optimal")
        np.testing.assert_allclose(result.x[built.index.charge], 0.0, atol=1e-7)
        np.testing.assert_allclose(result.x[built.index.discharge], 0.0, atol=1e-7)

    def test_energy_state_uses_charge_and_discharge_efficiency(self):
        request = make_request()
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        x = result.x
        for t in range(96):
            expected = (
                x[built.index.energy.start + t]
                + request.capability.charge_efficiency
                * x[built.index.charge.start + t]
                * 0.25
                - x[built.index.discharge.start + t]
                * 0.25
                / request.capability.discharge_efficiency
            )
            self.assertAlmostEqual(x[built.index.energy.start + t + 1], expected, places=6)

    def test_model_exposes_every_required_objective(self):
        built = build_model(make_request())
        self.assertEqual(
            set(built.objectives),
            {
                "demand_peak",
                "demand_duration",
                "soc_preferred_deviation",
                "energy_cost",
                "pv_unused",
                "throughput",
            },
        )
```

加入逐点功率平衡和互斥测试：

```python
    def test_solution_satisfies_power_balance_at_every_point(self):
        request = make_request()
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        x = result.x
        for t, point in enumerate(request.points):
            balance = (
                point.pv_forecast_kw
                + x[built.index.grid_import.start + t]
                + x[built.index.discharge.start + t]
                - point.load_forecast_kw
                - x[built.index.charge.start + t]
                - x[built.index.grid_export.start + t]
                - x[built.index.pv_unabsorbed.start + t]
            )
            self.assertAlmostEqual(balance, 0.0, places=6)

    def test_solution_never_charges_and_discharges_or_imports_and_exports(self):
        request = make_request()
        built = build_model(request)
        result = solve_milp(
            built.problem,
            built.objectives["energy_cost"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        x = result.x
        for t in range(96):
            charge = x[built.index.charge.start + t]
            discharge = x[built.index.discharge.start + t]
            grid_import = x[built.index.grid_import.start + t]
            grid_export = x[built.index.grid_export.start + t]
            self.assertFalse(charge > 1e-7 and discharge > 1e-7)
            self.assertFalse(grid_import > 1e-7 and grid_export > 1e-7)
```

- [ ] **Step 2: 运行模型测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_model.py -v
```

Expected: FAIL，原因是 `m4_optimizer.model` 尚不存在。

- [ ] **Step 3: 实现变量索引和稀疏行构建器**

在 `m4_optimizer/model.py` 定义：

```python
from dataclasses import dataclass
from typing import Mapping

import numpy as np
from numpy.typing import NDArray
from scipy.sparse import csr_matrix

from m4_optimizer.contracts import ObjectiveName, OptimizationRequest
from m4_optimizer.solver import MilpProblem


@dataclass(frozen=True)
class VariableIndex:
    charge: slice
    discharge: slice
    grid_import: slice
    grid_export: slice
    pv_unabsorbed: slice
    energy: slice
    demand_exceed: slice
    peak_demand_exceed: int
    charge_on: slice
    discharge_on: slice
    grid_import_on: slice
    soc_low_deviation: slice
    soc_high_deviation: slice
    size: int


@dataclass(frozen=True)
class BuiltModel:
    problem: MilpProblem
    index: VariableIndex
    objectives: dict[ObjectiveName, NDArray[np.float64]]


class RowBuilder:
    def __init__(self, variable_count: int) -> None:
        self.variable_count = variable_count
        self.rows: list[dict[int, float]] = []
        self.lower: list[float] = []
        self.upper: list[float] = []

    def add(
        self,
        coefficients: Mapping[int, float],
        *,
        lower: float = -np.inf,
        upper: float = np.inf,
    ) -> None:
        self.rows.append(dict(coefficients))
        self.lower.append(lower)
        self.upper.append(upper)

    def build(self) -> tuple[csr_matrix, NDArray[np.float64], NDArray[np.float64]]:
        row_ids: list[int] = []
        column_ids: list[int] = []
        values: list[float] = []
        for row_id, row in enumerate(self.rows):
            for column_id, value in row.items():
                row_ids.append(row_id)
                column_ids.append(column_id)
                values.append(value)
        matrix = csr_matrix(
            (values, (row_ids, column_ids)),
            shape=(len(self.rows), self.variable_count),
            dtype=float,
        )
        return matrix, np.asarray(self.lower), np.asarray(self.upper)
```

使用顺序切片分配 96 个充电、放电、购电、反送、光伏余量和需量变量，97 个能量变量，三组 96 个二进制状态变量，以及两组 96 个 SOC 推荐区间偏离变量。所有二进制变量上下界为 0/1，`integrality` 对应位置为 1。

- [ ] **Step 4: 实现硬约束和六个目标向量**

对每个 `t` 使用 `RowBuilder.add()` 加入以下精确线性形式：

```python
# Power balance: import + discharge - charge - export - unused = load - pv.
rows.add(
    {
        grid_import_t: 1.0,
        discharge_t: 1.0,
        charge_t: -1.0,
        grid_export_t: -1.0,
        pv_unabsorbed_t: -1.0,
    },
    lower=point.load_forecast_kw - point.pv_forecast_kw,
    upper=point.load_forecast_kw - point.pv_forecast_kw,
)

# SOC state equation.
rows.add(
    {
        energy_next: 1.0,
        energy_now: -1.0,
        charge_t: -request.capability.charge_efficiency * 0.25,
        discharge_t: 0.25 / request.capability.discharge_efficiency,
    },
    lower=0.0,
    upper=0.0,
)

# Demand exceed and maximum exceed.
rows.add(
    {demand_exceed_t: 1.0, grid_import_t: -1.0},
    lower=-request.constraints.demand_limit_kw,
)
rows.add(
    {peak_demand_exceed: 1.0, demand_exceed_t: -1.0},
    lower=0.0,
)

# Charge/discharge exclusivity.
rows.add(
    {charge_t: 1.0, charge_on_t: -effective_max_charge_kw},
    upper=0.0,
)
rows.add(
    {discharge_t: 1.0, discharge_on_t: -effective_max_discharge_kw},
    upper=0.0,
)
rows.add(
    {charge_on_t: 1.0, discharge_on_t: 1.0},
    upper=1.0 if request.capability.available else 0.0,
)

# Grid import/export exclusivity.
rows.add(
    {grid_import_t: 1.0, grid_import_on_t: -grid_import_big_m},
    upper=0.0,
)
rows.add(
    {grid_export_t: 1.0, grid_import_on_t: grid_export_big_m},
    upper=grid_export_big_m,
)

# Preferred SOC deviation.
rows.add(
    {
        soc_low_deviation_t: 1.0,
        energy_next: 100.0 / request.capability.energy_capacity_kwh,
    },
    lower=request.constraints.preferred_soc_min_pct,
)
rows.add(
    {
        soc_high_deviation_t: 1.0,
        energy_next: -100.0 / request.capability.energy_capacity_kwh,
    },
    lower=-request.constraints.preferred_soc_max_pct,
)
```

变量边界必须同时完成：

- `energy[0]` 上下界都等于 EMS 初始能量；
- 所有 `energy` 状态受绝对 SOC 上下限限制；
- `energy[96]` 再与初始 SOC ±终端容差取交集；
- `pv_unabsorbed[t] <= pv_forecast_kw[t]`；
- 禁止反送时 `grid_export` 上界为 0；允许反送时上界为 `grid_export_limit_kw`；
- 有物理进线限额时使用该值作为 `grid_import` 硬上界；否则使用 `max(load_forecast_kw) + effective_max_charge_kw` 作为有限 Big-M；
- `grid_export_big_m` 使用允许的反送上限，禁止反送时取 0。

六个目标向量按以下系数创建：

```python
objectives["demand_peak"][peak_demand_exceed] = 1.0
objectives["demand_duration"][index.demand_exceed] = 0.25
objectives["soc_preferred_deviation"][index.soc_low_deviation] = 0.25
objectives["soc_preferred_deviation"][index.soc_high_deviation] = 0.25
objectives["throughput"][index.charge] = 0.25
objectives["throughput"][index.discharge] = 0.25
```

逐点给 `energy_cost` 的购电变量设置 `buy_price * 0.25`、反送变量设置 `-sell_price * 0.25`；给 `pv_unused` 的反送和不可吸收变量都设置 `0.25`。

- [ ] **Step 5: 运行模型测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_model.py -v
```

Expected: 所有模型测试 PASS。

- [ ] **Step 6: 提交基础模型**

```bash
git add m4_optimizer/model.py tests/test_m4_optimizer_model.py
git commit -m "feat(m4): build station battery milp model"
```

---

### Task 4: 分层目标求解

**Files:**
- Create: `m4_optimizer/lexicographic.py`
- Create: `tests/test_m4_optimizer_lexicographic.py`

**Interfaces:**
- Consumes: `ObjectiveProfile`、`LayerResult`、`BuiltModel`、`ObjectiveLock`、`solve_milp()`。
- Produces: `ProfileSolveResult` and `solve_profile(built, profile, time_limit_seconds, mip_rel_gap)`。

- [ ] **Step 1: 编写优先级锁定失败测试**

使用两变量连续模型 `x + y >= 10`，第一层最小化 `x`，第二层最小化 `y`：

```python
import unittest

import numpy as np
from scipy.sparse import csr_matrix

from m4_optimizer.contracts import ObjectiveLayer, ObjectiveProfile
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.model import BuiltModel, VariableIndex
from m4_optimizer.solver import MilpProblem


class M4OptimizerLexicographicTests(unittest.TestCase):
    def test_zero_tolerance_preserves_primary_optimum(self):
        built = self.make_two_variable_model()
        profile = ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )
        result = solve_profile(built, profile, 2.0, 0.0)
        np.testing.assert_allclose(result.x, [0.0, 10.0], atol=1e-7)

    def test_absolute_tolerance_caps_primary_tradeoff(self):
        built = self.make_two_variable_model()
        profile = ObjectiveProfile(
            profile_id="balanced",
            profile_version="test-v1",
            objective_order=[
                ObjectiveLayer(
                    name="primary",
                    terms={"demand_peak": 1.0},
                    absolute_tolerance=1.0,
                    relative_tolerance=0.0,
                ),
                ObjectiveLayer(
                    name="secondary",
                    terms={"energy_cost": 1.0},
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ],
        )
        result = solve_profile(built, profile, 2.0, 0.0)
        self.assertLessEqual(result.x[0], 1.0 + 1e-7)
        self.assertAlmostEqual(result.x[1], 9.0, places=6)
```

`make_two_variable_model()` 必须返回目标 `demand_peak=[1,0]`、`energy_cost=[0,1]`，其他四个目标为零向量；其 `VariableIndex` 使用合法空切片占位，但 `size=2`。

- [ ] **Step 2: 运行分层测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_lexicographic.py -v
```

Expected: FAIL，原因是 `m4_optimizer.lexicographic` 尚不存在。

- [ ] **Step 3: 实现分层求解**

在 `m4_optimizer/lexicographic.py` 定义：

```python
from dataclasses import dataclass
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from m4_optimizer.contracts import CandidateStatus, LayerResult, ObjectiveLayer, ObjectiveProfile
from m4_optimizer.model import BuiltModel
from m4_optimizer.solver import ObjectiveLock, solve_milp


@dataclass(frozen=True)
class ProfileSolveResult:
    status: CandidateStatus
    x: NDArray[np.float64] | None
    layers: tuple[LayerResult, ...]
    message: str
    solve_seconds: float


def build_layer_objective(built: BuiltModel, layer: ObjectiveLayer) -> NDArray[np.float64]:
    objective = np.zeros(built.index.size, dtype=float)
    for name, weight in layer.terms.items():
        objective += weight * built.objectives[name]
    return objective
```

`solve_profile()` 按顺序调用 `solve_milp()`。每层成功后计算：

```python
best_value = float(objective @ raw.x)
lock_tolerance = max(
    layer.absolute_tolerance,
    layer.relative_tolerance * abs(best_value),
)
locks.append(
    ObjectiveLock(
        vector=objective.copy(),
        upper_bound=best_value + lock_tolerance,
    )
)
```

每次调用求解器都使用总时限扣除 `monotonic()` 已消耗时间后的剩余秒数。任一层没有可行 `x` 时立即返回该状态；任一层只达到 `feasible` 时继续后序求解，但最终状态最多为 `feasible`，不能标记为 `optimal`。

- [ ] **Step 4: 运行分层测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_lexicographic.py -v
```

Expected: 2 tests PASS。

- [ ] **Step 5: 提交分层求解器**

```bash
git add m4_optimizer/lexicographic.py tests/test_m4_optimizer_lexicographic.py
git commit -m "feat(m4): add lexicographic optimization"
```

---

### Task 5: 计划解码、指标复算与独立验证

**Files:**
- Create: `m4_optimizer/metrics.py`
- Create: `m4_optimizer/validation.py`
- Create: `tests/test_m4_optimizer_validation.py`

**Interfaces:**
- Consumes: `OptimizationRequest`、`PlanPoint`、`CandidateMetrics`、`CandidateResult`、`BuiltModel` 和求解向量。
- Produces: `materialize_plan(request, built, x)`、`calculate_metrics(request, plan)`、`validate_candidate(request, candidate, tolerance=1e-6)`、`ResultValidationError`。

- [ ] **Step 1: 编写复算和篡改识别失败测试**

```python
import unittest

from m4_optimizer.metrics import calculate_metrics, materialize_plan
from m4_optimizer.model import build_model
from m4_optimizer.solver import solve_milp
from m4_optimizer.validation import ResultValidationError, validate_candidate
from tests.m4_optimizer_test_support import make_candidate, make_request


class M4OptimizerValidationTests(unittest.TestCase):
    def test_materialized_plan_recomputes_soc_and_power_balance(self):
        request = make_request()
        built = build_model(request)
        solved = solve_milp(
            built.problem,
            built.objectives["throughput"],
            locks=(),
            time_limit_seconds=2.0,
            mip_rel_gap=0.0,
        )
        plan = materialize_plan(request, built, solved.x)
        candidate = make_candidate(request, plan, calculate_metrics(request, plan))
        validate_candidate(request, candidate)

    def test_validator_rejects_tampered_soc(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][10]["expected_soc_pct"] += 3.0
        tampered = type(candidate).model_validate(payload)
        with self.assertRaisesRegex(ResultValidationError, "SOC state"):
            validate_candidate(request, tampered)

    def test_validator_rejects_simultaneous_import_and_export(self):
        request = make_request()
        candidate = make_candidate_from_optimizer(request)
        payload = candidate.model_dump()
        payload["plan"][4]["grid_import_kw"] = 10.0
        payload["plan"][4]["grid_export_kw"] = 10.0
        tampered = type(candidate).model_validate(payload)
        with self.assertRaisesRegex(ResultValidationError, "import and export"):
            validate_candidate(request, tampered)
```

在测试支持文件中增加以下辅助函数；它们只调用已经实现的模型与求解器，不调用 Task 6 的服务入口：

```python
def make_candidate(
    request: OptimizationRequest,
    plan: list[PlanPoint],
    metrics: CandidateMetrics,
) -> CandidateResult:
    return CandidateResult(
        profile_id="balanced",
        profile_version="test-v1",
        status="optimal",
        solver_message="test solution",
        solve_seconds=0.0,
        plan=plan,
        metrics=metrics,
        layers=[],
        risk_codes=[],
    )


def make_candidate_from_optimizer(request: OptimizationRequest) -> CandidateResult:
    built = build_model(request)
    solved = solve_milp(
        built.problem,
        built.objectives["throughput"],
        locks=(),
        time_limit_seconds=2.0,
        mip_rel_gap=0.0,
    )
    if solved.x is None:
        raise AssertionError(f"test fixture did not solve: {solved.message}")
    plan = materialize_plan(request, built, solved.x)
    return make_candidate(request, plan, calculate_metrics(request, plan))
```

- [ ] **Step 2: 运行验证测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_validation.py -v
```

Expected: FAIL，原因是 `metrics.py` 和 `validation.py` 尚不存在。

- [ ] **Step 3: 实现计划解码与指标计算**

`materialize_plan()` 从内部两个非负功率变量生成无符号输出：

```python
if charge_kw > tolerance:
    mode = "charge"
    target_power_kw = charge_kw
elif discharge_kw > tolerance:
    mode = "discharge"
    target_power_kw = discharge_kw
else:
    mode = "idle"
    target_power_kw = 0.0
```

`expected_soc_pct` 使用 `energy[t+1] / energy_capacity_kwh * 100`。小于数值容差的非负变量归零，但不得用裁剪掩盖超出硬约束的结果。

`calculate_metrics()` 必须仅根据请求和公开计划点复算：

```python
import_cost = sum(
    point.grid_import_kw * source.buy_price_per_kwh * 0.25
    for point, source in zip(plan, request.points, strict=True)
)
export_revenue = sum(
    point.grid_export_kw * source.sell_price_per_kwh * 0.25
    for point, source in zip(plan, request.points, strict=True)
)
energy_cost = import_cost - export_revenue
charge_energy = sum(
    point.target_power_kw * 0.25 for point in plan if point.mode == "charge"
)
discharge_energy = sum(
    point.target_power_kw * 0.25 for point in plan if point.mode == "discharge"
)
throughput = charge_energy + discharge_energy
cycle_cost = throughput * request.constraints.cycle_cost_per_kwh
demand_exceed_energy = sum(point.demand_exceed_kw * 0.25 for point in plan)
grid_export_energy = sum(point.grid_export_kw * 0.25 for point in plan)
pv_unabsorbed_energy = sum(point.pv_unabsorbed_kw * 0.25 for point in plan)
total_pv_energy = sum(source.pv_forecast_kw * 0.25 for source in request.points)
pv_self_use = total_pv_energy - grid_export_energy - pv_unabsorbed_energy
preferred_soc_deviation = sum(
    (
        max(request.constraints.preferred_soc_min_pct - point.expected_soc_pct, 0.0)
        + max(point.expected_soc_pct - request.constraints.preferred_soc_max_pct, 0.0)
    )
    * 0.25
    for point in plan
)
```

光伏自用量为 `sum((pv - export - unabsorbed) * 0.25)`，总光伏为 0 时自用率定义为 1.0；否则自用率为自用量除以总光伏，并仅在数值容差内裁剪到 `[0,1]`。

- [ ] **Step 4: 实现独立候选验证器**

`validate_candidate()` 对非 `optimal`/`feasible` 候选只验证其计划为空且指标为空；对有效候选逐点执行：

```python
charge_kw = point.target_power_kw if point.mode == "charge" else 0.0
discharge_kw = point.target_power_kw if point.mode == "discharge" else 0.0
balance_error = (
    source.pv_forecast_kw
    + point.grid_import_kw
    + discharge_kw
    - source.load_forecast_kw
    - charge_kw
    - point.grid_export_kw
    - point.pv_unabsorbed_kw
)
expected_energy = (
    current_energy
    + request.capability.charge_efficiency * charge_kw * 0.25
    - discharge_kw * 0.25 / request.capability.discharge_efficiency
)
```

若功率平衡误差、SOC 状态误差、SOC/功率/进线硬边界、终端 SOC、设备可用性、反送配置、需量超限量或购电反送互斥超出 `tolerance`，抛出带时间点和规则名的 `ResultValidationError`。最后比较 `calculate_metrics()` 与候选指标，确保所有指标可复算。

- [ ] **Step 5: 运行验证测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_validation.py -v
```

Expected: 所有验证测试 PASS。

- [ ] **Step 6: 提交指标与验证器**

```bash
git add m4_optimizer/metrics.py m4_optimizer/validation.py tests/m4_optimizer_test_support.py tests/test_m4_optimizer_validation.py
git commit -m "feat(m4): validate optimizer results independently"
```

---

### Task 6: 三候选优化服务

**Files:**
- Create: `m4_optimizer/profiles.py`
- Create: `m4_optimizer/service.py`
- Modify: `m4_optimizer/__init__.py`
- Create: `tests/test_m4_optimizer_scenarios.py`

**Interfaces:**
- Consumes: `build_model()`、`solve_profile()`、`materialize_plan()`、`calculate_metrics()`、`validate_candidate()` 和三份 `ObjectiveProfile`。
- Produces: `M4Optimizer.optimize(request: OptimizationRequest) -> OptimizationResult`，并从包根导出 `M4Optimizer`。

- [ ] **Step 1: 编写三候选服务失败测试**

```python
import unittest

from m4_optimizer import M4Optimizer
from tests.m4_optimizer_test_support import make_request


class M4OptimizerScenarioTests(unittest.TestCase):
    def test_optimizer_returns_three_candidates_in_fixed_order(self):
        result = M4Optimizer(model_version="m4-milp-v1").optimize(make_request())
        self.assertEqual(result.station_id, "station-1")
        self.assertEqual(
            [candidate.profile_id for candidate in result.candidates],
            ["balanced", "cost", "pv"],
        )
        self.assertTrue(
            all(candidate.status in {"optimal", "feasible"} for candidate in result.candidates)
        )
        self.assertTrue(all(len(candidate.plan) == 96 for candidate in result.candidates))

    def test_each_candidate_keeps_its_profile_version(self):
        request = make_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        expected = {profile.profile_id: profile.profile_version for profile in request.profiles}
        self.assertEqual(
            {item.profile_id: item.profile_version for item in result.candidates},
            expected,
        )
```

- [ ] **Step 2: 运行服务测试并确认失败**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

Expected: FAIL，原因是 `M4Optimizer` 尚未导出。

- [ ] **Step 3: 实现配置顺序和优化服务**

在 `profiles.py` 中定义固定展示顺序，但不定义任何目标权重：

```python
from m4_optimizer.contracts import ObjectiveProfile, OptimizationRequest


PROFILE_ORDER = ("balanced", "cost", "pv")


def ordered_profiles(request: OptimizationRequest) -> tuple[ObjectiveProfile, ...]:
    by_id = {profile.profile_id: profile for profile in request.profiles}
    return tuple(by_id[profile_id] for profile_id in PROFILE_ORDER)
```

在 `service.py` 中实现：

```python
from datetime import datetime, timezone
from importlib.metadata import version

from m4_optimizer.contracts import CandidateResult, OptimizationRequest, OptimizationResult
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.metrics import calculate_metrics, materialize_plan
from m4_optimizer.model import build_model
from m4_optimizer.profiles import ordered_profiles
from m4_optimizer.validation import validate_candidate


class M4Optimizer:
    def __init__(self, *, model_version: str) -> None:
        if not model_version:
            raise ValueError("model_version is required")
        self.model_version = model_version

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        started_at = datetime.now(timezone.utc)
        built = build_model(request)
        candidates: list[CandidateResult] = []
        for profile in ordered_profiles(request):
            solved = solve_profile(
                built,
                profile,
                request.solver_time_limit_seconds,
                request.solver_mip_rel_gap,
            )
            if solved.x is None:
                candidate = CandidateResult(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    status=solved.status,
                    solver_message=solved.message,
                    solve_seconds=solved.solve_seconds,
                    plan=[],
                    metrics=None,
                    layers=list(solved.layers),
                    risk_codes=[],
                )
            else:
                plan = materialize_plan(request, built, solved.x)
                metrics = calculate_metrics(request, plan)
                risks = []
                if metrics.pv_unabsorbed_energy_kwh > 1e-6:
                    risks.append("PV_UNABSORBED")
                candidate = CandidateResult(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    status=solved.status,
                    solver_message=solved.message,
                    solve_seconds=solved.solve_seconds,
                    plan=plan,
                    metrics=metrics,
                    layers=list(solved.layers),
                    risk_codes=risks,
                )
            validate_candidate(request, candidate)
            candidates.append(candidate)
        return OptimizationResult(
            request_id=request.request_id,
            station_id=request.station_id,
            plan_start_at=request.plan_start_at,
            input_observed_at=request.input_observed_at,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            model_version=self.model_version,
            solver_name="scipy-highs",
            solver_version=version("scipy"),
            source_versions=request.source_versions,
            candidates=candidates,
        )
```

不要在此服务中加入 AI、EMS、数据库、日志持久化或人工计划逻辑。求解状态是预期结果，不用异常模拟；只有输入/配置错误和程序错误可以抛出异常。

在 `m4_optimizer/__init__.py` 导出 `M4Optimizer` 和稳定契约。

- [ ] **Step 4: 运行三候选服务测试并确认通过**

Run:

```bash
uv run python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

Expected: 当前两个服务测试 PASS。

- [ ] **Step 5: 提交三候选服务**

```bash
git add m4_optimizer/profiles.py m4_optimizer/service.py m4_optimizer/__init__.py tests/test_m4_optimizer_scenarios.py
git commit -m "feat(m4): generate three optimizer candidates"
```

---

### Task 7: 业务场景回归与阶段 A 说明

**Files:**
- Modify: `tests/m4_optimizer_test_support.py`
- Modify: `tests/test_m4_optimizer_scenarios.py`
- Create: `m4_optimizer/README.md`
- Create: `m4/M4数学优化器阶段A验收说明.md`

**Interfaces:**
- Consumes: 完整 `M4Optimizer` 公共接口。
- Produces: 可重复的业务验收场景、模块使用说明和阶段 A 验收映射。

- [ ] **Step 1: 增加业务场景失败测试**

在 `tests/test_m4_optimizer_scenarios.py` 增加以下独立场景：

```python
def test_cost_profile_charges_in_valley_and_discharges_at_peak(self):
    request = make_price_arbitrage_request()
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    candidate = candidate_by_id(result, "cost")
    self.assertGreater(sum_power(candidate, "charge", range(0, 24)), 0.0)
    self.assertGreater(sum_power(candidate, "discharge", range(48, 72)), 0.0)
    self.assertLessEqual(
        abs(candidate.metrics.terminal_soc_pct - request.capability.initial_soc_pct),
        request.constraints.terminal_soc_tolerance_pct + 1e-6,
    )

def test_pv_profile_stores_midday_surplus_before_export(self):
    request = make_midday_pv_request()
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    candidate = candidate_by_id(result, "pv")
    self.assertGreater(sum_power(candidate, "charge", range(40, 56)), 0.0)
    self.assertLess(
        candidate.metrics.grid_export_energy_kwh
        + candidate.metrics.pv_unabsorbed_energy_kwh,
        no_storage_unused_pv_energy(request),
    )

def test_demand_profile_reports_unavoidable_exceedance(self):
    request = make_demand_peak_request(max_discharge_kw=20.0)
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    candidate = candidate_by_id(result, "balanced")
    self.assertGreater(candidate.metrics.peak_demand_exceed_kw, 0.0)
    self.assertLessEqual(max(point.target_power_kw for point in candidate.plan), 20.0 + 1e-6)

def test_disabled_storage_returns_an_idle_plan(self):
    result = M4Optimizer(model_version="m4-milp-v1").optimize(
        make_request(available=False)
    )
    for candidate in result.candidates:
        self.assertTrue(all(point.mode == "idle" for point in candidate.plan))

def test_station_requests_do_not_share_identity_or_results(self):
    optimizer = M4Optimizer(model_version="m4-milp-v1")
    station_1 = optimizer.optimize(make_request(station_id="station-1", load_kw=100.0))
    station_2 = optimizer.optimize(make_request(station_id="station-2", load_kw=300.0))
    self.assertEqual(station_1.station_id, "station-1")
    self.assertEqual(station_2.station_id, "station-2")
    self.assertNotEqual(
        candidate_by_id(station_1, "balanced").metrics.max_grid_import_kw,
        candidate_by_id(station_2, "balanced").metrics.max_grid_import_kw,
    )
```

同时加入以下边界回归：

```python
def test_sufficient_battery_power_eliminates_demand_exceedance(self):
    request = make_demand_peak_request(max_discharge_kw=200.0)
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    candidate = candidate_by_id(result, "balanced")
    self.assertLessEqual(candidate.metrics.peak_demand_exceed_kw, 1e-6)

def test_disabled_export_reports_unabsorbed_pv_without_export_command(self):
    request = make_midday_pv_request(
        grid_export_enabled=False,
        grid_export_limit_kw=0.0,
    )
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    candidate = candidate_by_id(result, "pv")
    self.assertIn("PV_UNABSORBED", candidate.risk_codes)
    self.assertTrue(all(point.grid_export_kw <= 1e-7 for point in candidate.plan))

def test_zero_sell_price_never_reports_export_revenue(self):
    request = make_midday_pv_request(sell_price_per_kwh=0.0)
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    for candidate in result.candidates:
        self.assertEqual(candidate.metrics.export_revenue, 0.0)

def test_final_solution_respects_every_recorded_objective_lock(self):
    request = make_price_arbitrage_request()
    result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
    profiles = {profile.profile_id: profile for profile in request.profiles}
    for candidate in result.candidates:
        profile = profiles[candidate.profile_id]
        for layer, record in zip(
            profile.objective_order,
            candidate.layers,
            strict=True,
        ):
            final_value = metric_objective_value(candidate.metrics, layer)
            self.assertLessEqual(
                final_value,
                record.best_value + record.lock_tolerance + 1e-6,
            )
```

`metric_objective_value()` 必须使用以下公开指标映射复算层目标：

```python
def metric_objective_value(metrics, layer):
    values = {
        "demand_peak": metrics.peak_demand_exceed_kw,
        "demand_duration": metrics.demand_exceed_energy_kwh,
        "soc_preferred_deviation": metrics.preferred_soc_deviation,
        "energy_cost": metrics.energy_cost,
        "pv_unused": (
            metrics.grid_export_energy_kwh
            + metrics.pv_unabsorbed_energy_kwh
        ),
        "throughput": metrics.throughput_energy_kwh,
    }
    return sum(weight * values[name] for name, weight in layer.terms.items())
```

- [ ] **Step 2: 运行完整 M4 优化器测试并确认新增场景失败**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v
```

Expected: 新增业务场景至少一项 FAIL，失败原因是测试工厂尚未提供对应场景数据或现有模型行为不满足断言。

- [ ] **Step 3: 完成场景工厂并修正最小实现**

在 `tests/m4_optimizer_test_support.py` 增加确定性工厂：

- `make_price_arbitrage_request()`：前 24 点低价、中间 24 点平价、48–71 点高价，其余平价；光伏为 0，需量上限足够高；
- `make_midday_pv_request()`：40–55 点光伏高于原生负荷，其他点光伏为 0；
- `make_demand_peak_request(max_discharge_kw)`：32–35 点负荷高于需量上限，其余负荷低于上限；
- `candidate_by_id()`、`sum_power()` 和 `no_storage_unused_pv_energy()`：只做测试查询与基线计算。

测试默认目标配置必须严格表达：

```python
balanced = [
    layer("demand-peak", {"demand_peak": 1.0}),
    layer("demand-duration", {"demand_duration": 1.0}),
    layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
    layer("balanced-value", {"energy_cost": 0.01, "pv_unused": 0.001}),
    layer("throughput", {"throughput": 1.0}),
]
cost = [
    layer("demand-peak", {"demand_peak": 1.0}),
    layer("demand-duration", {"demand_duration": 1.0}),
    layer("energy-cost", {"energy_cost": 1.0}),
    layer("pv-unused", {"pv_unused": 1.0}),
    layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
    layer("throughput", {"throughput": 1.0}),
]
pv = [
    layer("demand-peak", {"demand_peak": 1.0}),
    layer("demand-duration", {"demand_duration": 1.0}),
    layer("pv-unused", {"pv_unused": 1.0}),
    layer("energy-cost", {"energy_cost": 1.0}),
    layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
    layer("throughput", {"throughput": 1.0}),
]
```

这些权重只存在于测试请求，不能复制为生产代码默认值。若场景暴露模型错误，只修改对应约束、目标、指标或验证器；不得降低测试断言或放宽安全边界来获得通过。

- [ ] **Step 4: 编写使用说明和验收映射**

`m4_optimizer/README.md` 必须包含：

- 模块职责与明确非职责；
- 最小 Python 调用示例：构建 `OptimizationRequest` 后调用 `M4Optimizer(model_version="m4-milp-v1").optimize(request)`；
- 三类候选、状态和 `mode + target_power_kw` 输出解释；
- 输入数据必须完整且时间对齐的说明；
- 运行单元测试和完整 M4 测试的命令；
- 明确“此模块不会连接 AI、EMS 或真实设备”。

`m4/M4数学优化器阶段A验收说明.md` 必须把本任务中的业务场景逐项映射到测试方法名，并声明阶段 A 只证明数学候选生成和确定性复算，不证明真实节费比例、AI 选择效果或 EMS 设备执行效果。

- [ ] **Step 5: 运行 M4 优化器测试并确认通过**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v
```

Expected: 所有 M4 优化器测试 PASS，无跳过项。

- [ ] **Step 6: 运行项目 Python 回归测试**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: 项目 Python 测试 PASS。若存在与本变更无关的既有失败，保留完整命令、失败测试名和错误输出，不得声称全量回归通过。

- [ ] **Step 7: 记录求解性能基线**

Run:

```bash
/usr/bin/time -p uv run python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

Expected: 命令完成并输出 `real/user/sys`。把实际机器、Python/SciPy 版本、场景数量和耗时写入阶段 A 验收说明，只作为当前环境基线，不冒充生产 SLA。

- [ ] **Step 8: 检查差异并提交阶段 A**

Run:

```bash
git diff --check
git status --short
```

Expected: 无空白错误；状态中只包含本计划相关文件以及用户原有改动。

Commit:

```bash
git add m4_optimizer tests/m4_optimizer_test_support.py tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_lexicographic.py tests/test_m4_optimizer_validation.py tests/test_m4_optimizer_scenarios.py m4/M4数学优化器阶段A验收说明.md pyproject.toml uv.lock
git commit -m "test(m4): cover optimizer acceptance scenarios"
```

---

## 实施完成条件

- `m4_optimizer` 可以在不启动 FastAPI、不连接数据库、不调用 AI/EMS 的情况下生成三套候选；
- 三套候选共享硬约束，并按外部配置执行分层优化；
- 输出包含 96 点计划、可复算 SOC、功率平衡和完整业务指标；
- 输入错误、不可行、超时和未证明最优状态被准确区分；
- 独立验证器能识别被篡改结果；
- 双站使用不同请求时没有共享状态；
- 所有 M4 优化器测试通过；
- 阶段 A 验收说明记录实际测试和性能证据；
- 未调用真实 AI、EMS 或设备，未修改现有 M4 静态页面。
