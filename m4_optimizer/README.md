# M4 数学优化器

## 职责边界

`m4_optimizer` 接收一个站点完整、时间对齐的 96 点预测、储能能力快照、硬约束和三套外部目标配置，在不启动 FastAPI 的情况下生成 `balanced`、`cost`、`pv` 三类确定性数学候选。模块负责 MILP 建模、分层目标求解、公开计划解码、指标复算和结果校验。

模块不负责预测数据生产、候选策略选择、真实节费承诺、EMS 指令编排、数据库持久化或设备执行。此模块不会连接 AI、EMS 或真实设备。

## 最小调用

```python
from datetime import datetime, timedelta

from m4_optimizer import (
    CapabilitySnapshot,
    ForecastPoint,
    M4Optimizer,
    ObjectiveLayer,
    ObjectiveProfile,
    OptimizationConstraints,
    OptimizationRequest,
)

start = datetime.fromisoformat("2026-09-04T00:00:00+08:00")


def layer(name, terms):
    return ObjectiveLayer(
        name=name,
        terms=terms,
        absolute_tolerance=1e-6,
        relative_tolerance=0.0,
    )


common = [
    layer("demand-peak", {"demand_peak": 1.0}),
    layer("demand-duration", {"demand_duration": 1.0}),
]
tail = [
    layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
    layer("energy-cost", {"energy_cost": 1.0}),
    layer("pv-unused", {"pv_unused": 1.0}),
    layer("throughput", {"throughput": 1.0}),
]
profiles = [
    ObjectiveProfile(
        profile_id=profile_id,
        profile_version=f"example-{profile_id}-v1",
        objective_order=common + tail,
    )
    for profile_id in ("balanced", "cost", "pv")
]
request = OptimizationRequest(
    request_id="request-001",
    station_id="station-001",
    plan_start_at=start,
    input_observed_at=start - timedelta(minutes=5),
    max_input_age_seconds=1800,
    interval_minutes=15,
    horizon_points=96,
    source_versions={"forecast": "forecast-v1", "capability": "ems-v1"},
    points=[
        ForecastPoint(
            timestamp=start + timedelta(minutes=15 * index),
            load_forecast_kw=100.0,
            pv_forecast_kw=20.0,
            buy_price_per_kwh=0.8,
            sell_price_per_kwh=0.0,
        )
        for index in range(96)
    ],
    capability=CapabilitySnapshot(
        available=True,
        initial_soc_pct=50.0,
        energy_capacity_kwh=200.0,
        max_charge_kw=100.0,
        max_discharge_kw=100.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
    ),
    constraints=OptimizationConstraints(
        soc_min_pct=10.0,
        soc_max_pct=90.0,
        preferred_soc_min_pct=20.0,
        preferred_soc_max_pct=80.0,
        terminal_soc_tolerance_pct=5.0,
        demand_limit_kw=120.0,
        grid_import_limit_kw=150.0,
        grid_export_enabled=False,
        grid_export_limit_kw=0.0,
        cycle_cost_per_kwh=0.01,
    ),
    profiles=profiles,
    solver_time_limit_seconds=30.0,
    solver_mip_rel_gap=0.01,
)

result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
```

示例只展示接口形状，目标层顺序和权重应由调用方按已审批策略提供，生产代码没有内置业务权重。

## 输出解释

- `balanced`：按调用方配置生成综合候选；`cost`：按调用方配置生成成本候选；`pv`：按调用方配置生成光伏消纳候选。三者共享同一组硬约束，不代表模块替调用方选择最终方案。
- 候选状态包括 `optimal`、`feasible`、`infeasible`、`timeout` 和 `error`。只有 `optimal` 或 `feasible` 候选包含 96 点 `plan` 和 `metrics`。
- 每个计划点以 `mode + target_power_kw` 表达储能动作：`charge` 为充电功率、`discharge` 为放电功率、`idle` 的目标功率为 0。并同时给出预期 SOC、电网进出功率、未吸收光伏和需量超限值，便于独立复算。

## 输入要求

请求必须提供完整的 96 个 15 分钟点，时间戳从 `plan_start_at` 起连续、带时区且顺序一致。负荷、光伏、电价、能力快照、约束和目标版本必须来自同一规划窗口并完成时间对齐；过期能力快照、缺点、错位时间线或不完整目标配置会被拒绝。

## 测试

运行单元测试：

```bash
.venv/bin/python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

运行完整 M4 优化器测试：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v
```
