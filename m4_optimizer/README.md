# M4 数学优化器

## 职责边界

`m4_optimizer` 接收一个站点完整、时间对齐的 96 点预测、储能能力快照、硬约束和三套外部目标配置，在不启动 FastAPI 的情况下生成 `balanced`、`cost`、`pv` 三类数学候选。模块负责 MILP 建模、分层目标求解、公开计划解码、指标复算和结果校验。

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
- 候选状态包括 `optimal`、`feasible`、`infeasible`、`timeout` 和 `error`。`optimal` 要求 SciPy 成功且 MIP gap 未报告或不超过 `1e-9`；有可行解但 gap 超过该阈值、或后续目标层超时而保留了前层 incumbent 时为 `feasible`。从未取得 incumbent 的超时才返回 `timeout`；后续 `error`/`infeasible` 不会被旧 incumbent 掩盖。只有 `optimal` 或 `feasible` 候选包含 96 点 `plan` 和 `metrics`。
- 每个计划点以 `mode + target_power_kw` 表达储能动作：`charge` 为充电功率、`discharge` 为放电功率、`idle` 的目标功率为 0。并同时给出预期 SOC、电网进出功率、未吸收光伏和需量超限值，便于独立复算。
- `grid_export_kw` 与 `pv_unabsorbed_kw` 都按 PV 归因，任一点二者之和不超过该点 `pv_forecast_kw`。这会阻止购电或电池存量被记作光伏外送，但允许电池服务本地负荷的同时把当点 PV 外送。
- 每个候选都有由请求、模型和 profile 版本组成的稳定 `plan_version`，失败候选也不例外。`risk_codes` 与 `risk_messages` 按索引一一对应；当前 `PV_UNABSORBED` 的说明明确它只是风险提示，不是光伏限发指令。
- 若某个 profile 的计划解码、指标复算或独立复验出现可预期数据异常，该候选返回 `error`、空 `plan`、空 `metrics`，同时保留版本、已完成目标层和审计消息；其余 profiles 继续处理。未预期的程序错误不会被该隔离逻辑吞掉。

排除 `started_at`、`finished_at` 与 `solve_seconds` 这些运行时字段后，固定输入和版本的公开候选内容可重复验证；本文不对求解耗时做确定性承诺。

## 输入要求

请求必须提供完整的 96 个 15 分钟点。`plan_start_at`、`input_observed_at` 和每个点都必须有可计算的 UTC offset，且同一请求全部使用相同 offset；混用 UTC 与 `+08:00` 会被拒绝。阶段 A 采用固定 offset 口径，跨 DST 跳变的窗口应由调用方先转换为 UTC 或固定 offset，优化器不会猜测 DST 重复/缺失时段。

负荷、光伏、电价、能力快照、约束和目标版本必须来自同一规划窗口并完成时间对齐；`source_versions` 映射不能为空，键和值也不能是空字符串或纯空白。过期能力快照、缺点、错位时间线、非法容量/效率或不完整目标配置会被拒绝。

公开 `layers` 记录每层目标值与锁容差，不公开逐层 MIP gap；最终候选状态会保守汇总所有层，任一层未证明最优就不会把整个候选标为 `optimal`。

## 测试

运行单元测试：

```bash
.venv/bin/python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

运行完整 M4 优化器测试：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v
```
