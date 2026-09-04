# M4 数学优化器阶段 A 验收说明

## 验收边界

阶段 A 只证明：`M4Optimizer` 能根据完整请求生成三套数学候选，公开 96 点计划可独立复算，硬约束、分层目标锁和状态语义可由自动化测试重复验证。

阶段 A 不证明真实节费比例、AI 候选选择效果或 EMS 设备执行效果；本阶段没有连接 AI、EMS、数据库或真实设备，也没有修改 M4 静态页面。

## 业务场景映射

| 业务场景 | 自动化测试方法 | 验收口径 |
| --- | --- | --- |
| 峰谷套利 | `test_cost_profile_charges_in_valley_and_discharges_at_peak` | 成本候选在前 24 点谷段充电、48–71 点峰段放电，并满足终端 SOC 边界。 |
| 午间光伏消纳 | `test_pv_profile_stores_midday_surplus_before_export` | 光伏候选在 40–55 点吸收盈余，反送与未吸收电量之和低于无储能手算基线 `400 kWh`。默认场景允许反送但限功率，因此同时经过允许反送和剩余光伏风险分支。 |
| 放电能力不足时的需量超限 | `test_demand_profile_reports_unavoidable_exceedance` | 峰段原始负荷 `180 kW`、需量上限 `100 kW`、储能功率上限 `20 kW`，手算最小峰值超限为 `60 kW`；结果如实报告超限且计划不越过设备功率边界。 |
| 储能不可用 | `test_disabled_storage_returns_an_idle_plan` | 三类候选均输出全时段 `idle`。 |
| 双站隔离 | `test_station_requests_do_not_share_identity_or_results` | 同一优化器实例保持各站标识，且不同负荷得到不同复算结果。 |
| 放电能力充足时的需量治理 | `test_sufficient_battery_power_eliminates_demand_exceedance` | 同一 `80 kW` 峰段缺口在 200 kW 功率上限下可完全覆盖，峰值需量超限不大于 `1e-6 kW`。 |
| 禁止反送 | `test_disabled_export_reports_unabsorbed_pv_without_export_command` | 结果包含 `PV_UNABSORBED` 风险，所有计划点反送功率不大于 `1e-7 kW`。 |
| 零上网收益 | `test_zero_sell_price_never_reports_export_revenue` | 三类候选的上网收益均严格为 0。 |
| 分层目标锁 | `test_final_solution_respects_every_recorded_objective_lock` | 通过公开指标复算每一层目标，最终值不超过记录最优值、锁容差和数值复算容差之和。 |

公共接口的候选固定顺序和 profile 版本透传另由 `test_optimizer_returns_three_candidates_in_fixed_order`、`test_each_candidate_keeps_its_profile_version` 覆盖；三个测试 profile 使用不同版本号，可识别错配。

## 测试证据

以下结果均来自验收提交前的实际命令输出：

- 聚焦业务场景：`.venv/bin/python -m unittest tests/test_m4_optimizer_scenarios.py -v`，11 项通过，0 失败，0 跳过，测试框架耗时 `8.087 s`。
- 完整 M4 优化器回归：`.venv/bin/python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v`，42 项通过，0 失败，0 跳过，测试框架耗时 `8.410 s`。
- 项目 Python 全量回归：`.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`，512 项通过，0 失败，4 项既有显式跳过，测试框架耗时 `39.707 s`。运行时出现既有 Starlette `httpx` 弃用告警及预期的故障分支日志，不影响退出码 0。

## 性能基线

基线命令：

```bash
/usr/bin/time -p .venv/bin/python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

- 机器：Apple Silicon `arm64`，macOS 15.7.1（Darwin 24.6.0）。
- Python：3.12.12。
- SciPy / HiGHS 接口：SciPy 1.18.1。
- 场景数量：11 个测试方法，其中本任务新增 9 个业务验收场景。
- 测试框架耗时：`8.036 s`。
- `/usr/bin/time -p`：`real 8.69 s`、`user 11.20 s`、`sys 0.91 s`。

该数据只作为当前开发环境的求解基线，不是生产 SLA，也不能据此推导真实站点的成本或节费表现。

## 已知范围与风险

- 求解结果依赖调用方提供的数据完整性、时间对齐、能力边界和目标配置；本阶段不验证外部数据生产链路。
- `feasible` 表示时间限制内存在可用候选，不等同于已证明全局最优；`timeout`、`infeasible` 和 `error` 不携带可执行计划。
- 数学候选仍需后续 AI/业务规则选择、EMS 安全校核和设备闭环验证后，才能进入真实控制链路。
