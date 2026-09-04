# M4 数学优化器阶段 A 验收说明

## 验收边界

阶段 A 只证明：`M4Optimizer` 能根据完整请求生成三套数学候选，公开 96 点计划可独立复算，硬约束、分层目标锁和状态语义可由自动化测试重复验证。

阶段 A 不证明真实节费比例、AI 候选选择效果或 EMS 设备执行效果；本阶段没有连接 AI、EMS、数据库或真实设备，也没有修改 M4 静态页面。

## 业务场景映射

| 业务场景 | 自动化测试方法 | 验收口径 |
| --- | --- | --- |
| 峰谷套利 | `test_cost_profile_charges_in_valley_and_discharges_at_peak` | 成本候选在前 24 点谷段充电、48–71 点峰段放电，并满足终端 SOC 边界。 |
| 午间光伏消纳 | `test_pv_profile_stores_midday_surplus_before_export` | 光伏候选在 40–55 点吸收盈余，反送与未吸收电量之和低于无储能手算基线 `400 kWh`。默认场景中购电与反送总量均为正，测试非真空地确认了逐点不同时购售电，并要求风险代码与说明对齐。 |
| 放电能力不足时的需量超限 | `test_demand_profile_reports_unavoidable_exceedance` | 峰段原始负荷 `180 kW`、需量上限 `100 kW`、储能功率上限 `20 kW`，手算最小峰值超限为 `60 kW`；测试同时复核公开指标约为 `60 kW`，以及 32–35 点均约以 `20 kW` 放电。 |
| 储能不可用 | `test_disabled_storage_returns_an_idle_plan` | 三类候选均为成功状态、各有 96 点计划，且全时段 `mode=idle`、`target_power_kw=0`。 |
| 双站隔离 | `test_station_requests_do_not_share_identity_or_results` | 同一优化器实例保持各站标识，且不同负荷得到不同复算结果。 |
| 放电能力充足时的需量治理 | `test_sufficient_battery_power_eliminates_demand_exceedance` | 同一 `80 kW` 峰段缺口在 200 kW 功率上限下可完全覆盖，峰值需量超限不大于 `1e-6 kW`。 |
| 禁止反送 | `test_disabled_export_reports_unabsorbed_pv_without_export_command` | 结果包含 `PV_UNABSORBED` 风险，所有计划点反送功率不大于 `1e-7 kW`。 |
| 零上网收益 | `test_zero_sell_price_never_reports_export_revenue` | 三类候选的上网收益均严格为 0。 |
| 分层目标锁 | `test_final_solution_respects_every_recorded_objective_lock` | 通过公开指标复算每一层目标，最终值不超过记录最优值、锁容差和数值复算容差之和。 |
| PV 反送来源 | `test_export_and_unabsorbed_are_jointly_bounded_by_available_pv`<br>`test_zero_pv_zero_load_with_export_enabled_never_exports`<br>`test_abnormal_buy_sell_spread_cannot_create_export_without_pv` | 模型、指标复算与独立验证共同实施逐点 `grid_export + pv_unabsorbed <= pv`。零 PV/零负荷且允许反送时不外送，异常购售价差也不能构造无 PV 反送。 |
| 电池与 PV 同时供能归因 | `test_battery_can_serve_load_while_pv_is_exported` | 在符合功率平衡与 PV 来源上限时，允许电池服务本地负荷、同点 PV 外送，未通过粗暴禁用同时流动解决来源问题。 |
| 求解证明状态 | `test_success_status_with_nonzero_mip_gap_is_only_feasible`<br>`test_success_status_with_zero_or_near_zero_mip_gap_is_optimal` | SciPy `status=0` 仅在 gap 未报告或 `abs(mip_gap) <= 1e-9` 时映射为 `optimal`；超过该近零阈值的有限 incumbent 为 `feasible`。 |
| 分层部分 incumbent | `test_total_time_exhaustion_between_layers_returns_last_incumbent`<br>`test_later_timeout_without_x_returns_last_incumbent`<br>`test_later_error_or_infeasible_is_not_disguised_as_feasible` | 层间总时限耗尽或后续层超时无新解时，保留最后 incumbent 和已完成层，返回 `feasible` 并标注部分完成；真实 `error`/`infeasible` 仍失败。 |
| 候选错误隔离 | `test_candidate_processing_errors_do_not_stop_later_profiles`<br>`test_unexpected_candidate_programming_error_is_not_swallowed` | 解码、指标或验证的可预期 `ValueError` 只使当前 profile 变为可审计的 `error` 空候选，后续 profile 继续；未知 `RuntimeError` 不被吞掉。 |
| 请求与结果审计 | `test_request_requires_a_valid_utc_offset_on_every_timestamp`<br>`test_request_rejects_mixed_utc_offsets_even_for_the_same_instants`<br>`test_request_rejects_empty_or_blank_source_versions`<br>`test_same_input_returns_deterministic_auditable_candidates` | 请求头与每点均需有有效、相同 UTC offset，来源版本映射及键值非空；排除时间戳与耗时后，相同输入的公开结果一致，每个候选都有确定 `plan_version` 与对齐风险说明。 |
| 公开失败输出 | `test_public_failure_statuses_have_empty_outputs_and_stable_versions` | `infeasible`/`timeout`/`error` 均输出空计划、空指标和确定的失败候选版本。 |

公共接口的候选固定顺序和 profile 版本透传另由 `test_optimizer_returns_three_candidates_in_fixed_order`、`test_each_candidate_keeps_its_profile_version` 覆盖；三个测试 profile 使用不同版本号，可识别错配。

## 测试证据

以下结果均来自最终审查修复提交前的实际命令输出：

- 聚焦最终审查回归：`.venv/bin/python -m unittest tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_lexicographic.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_validation.py tests/test_m4_optimizer_scenarios.py -v`，`Ran 66 tests in 7.299s`，0 失败，0 跳过。
- 完整 M4 优化器回归：`.venv/bin/python -m unittest discover -s tests -p 'test_m4_optimizer*.py' -v`，`Ran 66 tests in 9.248s`，0 失败，0 跳过。
- 项目 Python 全量回归：`.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`，`Ran 536 tests in 40.142s`，即 532 项通过、4 项既有显式跳过。运行时出现既有 Starlette `httpx` 弃用告警及预期的故障分支日志，不影响退出码 0。

## 性能基线

基线命令：

```bash
/usr/bin/time -p .venv/bin/python -m unittest tests/test_m4_optimizer_scenarios.py -v
```

- 机器：Apple Silicon `arm64`，macOS 15.7.1（Darwin 24.6.0）。
- Python：3.12.12。
- SciPy / HiGHS 接口：SciPy 1.18.1。
- 场景数量：17 个测试方法，包含正向业务场景、公开失败状态和候选错误隔离。
- 测试框架耗时：`7.567 s`。
- `/usr/bin/time -p`：`real 8.24 s`、`user 10.56 s`、`sys 1.36 s`。

该数据只作为当前开发环境的求解基线，不是生产 SLA，也不能据此推导真实站点的成本或节费表现。

## 已知范围与风险

- 求解结果依赖调用方提供的数据完整性、时间对齐、能力边界和目标配置；本阶段不验证外部数据生产链路。
- 阶段 A 采用单请求固定 UTC offset；跨 DST 跳变的窗口需在输入层转换为 UTC 或固定 offset，优化器不猜测重复/缺失时段。
- `optimal` 使用 `1e-9` 的近零 MIP gap 证明阈值；`feasible` 可表示未证明最优的完整分层结果，也可表示后续层超时后保留的已复验部分 incumbent。公开 `layers` 未暴露逐层 gap，但最终状态保守汇总所有已完成层。
- 从未取得 incumbent 的 `timeout`、`infeasible` 和 `error` 不携带可执行计划；后续真实错误不会被旧 incumbent 掩盖。
- `plan_version` 由请求、模型与 profile 版本组成；调用方必须确保修改输入时使用新的 `request_id`/来源版本，本阶段未将整份输入内容哈希纳入计划版本。
- 数学候选仍需后续 AI/业务规则选择、EMS 安全校核和设备闭环验证后，才能进入真实控制链路。
