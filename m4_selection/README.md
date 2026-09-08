# M4 确定性候选选择

输入已经求解的三套候选，程序复验并按明确配置选择，输出中文模板和审计信息。无需 AI，不再求解，不发送设备指令。该模块和 CLI 已可用于本地预览，并通过 `m4_settings.selection.LiveSelectionService` 接入本地实时 API 和页面，见[选择偏好配置说明](../m4/选择偏好配置说明.md)。B1 原有 `pending_ai` 状态不变，选择结果独立返回。

## 选择规则

1. 严格重验请求、结果及策略结构，绑定站点、请求、时间、来源版本、目标版本和计划版本；必须包含 balanced/cost/pv 各一次。不合法直接报错。
2. 只允许 optimal/feasible 候选；独立验证每个计划的 96 点、功率/SOC/光伏约束与指标。复验失败的候选排除并记录原因。选择使用重新计算的指标，不接受外部 validated 标记。
3. 设备不可用、全部候选不可用或缺少策略分别返回 `device_unavailable`、`no_usable_candidate`、`pending_policy`；仅一套可用时也不会绕过策略配置。
4. 依次最小化需量峰值超限 kW、累计超限 kWh，再比较配置的偏好指标；每层保留与本层最小值之差 **≤ 该层容差** 的候选。将浮点数的十进制表示转为精确有理数比较，不受外部 Decimal 精度影响，不按相邻值连锁合并。
5. 最终组内按配置 `tie_order` 选择。候选输入顺序、optimal/feasible 标签不构成额外偏好。输出三层比较、排除原因、完整策略、输入 SHA256、计划引用与 ≤50 字中文解释。

三种指标偏好均为越小越好；另支持按候选名称顺序选择：

| metric | 含义 | 容差单位 |
|---|---|---|
| `energy_cost` | 净电费＝购电费－外送收入，可能为负，不含循环成本 | 元 |
| `preferred_soc_deviation` | 推荐 SOC 区间累计偏离，不是安全越限 | 百分点·小时 |
| `pv_unabsorbed_energy_kwh` | 未吸收光伏，不包含合法外送 | kWh |
| `profile_priority` | 需量两层筛选后，按 `tie_order` 选首个剩余候选 | 排名容差必须为 0 |

`profile_priority` 第三层审计记录配置顺序的 0/1/2 排名，使用 `selector_version=demand-then-profile-v1`；原指标模式保持 `demand-then-preference-v1`。balanced 指候选 ID，不等同 SOC 指标。排名模式仍先复验并筛选两层需量；balanced 可能因需量较差或复验失败而被淘汰。

这里的累计超限层沿用数学核心的第二需量目标。旧 AI 摘要实验仅比较峰值需量，两者并非完全相同的选择契约。

## Python 调用

```python
from m4_selection import select_candidate, SelectionPolicy

# request/result 是现有严格 OptimizationRequest/OptimizationResult。
preview = select_candidate(request, result)  # 缺偏好，不选择
# 有经确认的站级配置后才传入 SelectionPolicy。
preview = select_candidate(request, result, policy)
```

`SelectionPolicy` 所有字段必填，无默认客户偏好。以下仅为**本地评测示例**，不代表正式站级配置；station_id 必须与输入一致：

```json
{
  "station_id": "station-1",
  "policy_id": "evaluation-only-cost",
  "version": "v1",
  "metric": "energy_cost",
  "demand_peak_tolerance_kw": 0.000001,
  "demand_energy_tolerance_kwh": 0.000001,
  "metric_tolerance": 0.000001,
  "tie_order": ["cost", "balanced", "pv"]
}
```

选择容差与计划独立验证的固定 1e-6 数值容差用途不同；增大选择容差不能放松安全校验。修改运营配置须更新 version；当前模块不负责配置持久化或审批。

## 命令行

在项目 `vifa/` 下运行，参数指向本地 JSON：

```sh
.venv/bin/python -m m4_selection --request INPUT.json --result RESULT.json
.venv/bin/python -m m4_selection --request INPUT.json --result RESULT.json --policy POLICY.json
```

`RESULT.json` 是 OptimizationResult（三候选结果），不是 B1 外层对象。B1 使用 `stations[*].optimization_result` 及匹配的原始请求。CLI 只输出 stdout，不修改输入文件；退出码 0 表示选出预览、1 表示门禁状态、2 表示文件/数据/配置错误。错误只写 stderr，重复 JSON 字段与非有限值拒绝。

## 边界与验证

- 所有结果固定 `usage=preview_only`、`dispatch_status=not_dispatched`。只返回计划引用，不生成新的功率/SOC 或单柜指令。候选的原始风险信息仍位于输入结果，须结合引用读取。
- `input_sha256` 对严格重验后的 `{request,result}` 进行键排序、紧凑 UTF-8 JSON 编码后计算，绑定完整快照。数组顺序变化会改变哈希，但不会改变选择和比较结论。策略全文另存于输出。
- 支持历史回放，**不判断快照在当前墙钟是否过期**，不保证当前现场状态或经济最优。实时 API 已在选择前后复用时效、逐柜、配置/来源版本校验，并确认候选快照未变化；该门禁不改变核心模块的历史回放行为。
- 电站 1 已按用户明确的 balanced 优先规则保存本地偏好；电站 2 尚未配置。本地评测规则不能自动进入生产配置。
- 新测试：`.venv/bin/python -m unittest discover -s tests -p 'test_m4_selection*.py'`。真实保存工况的回放记录见 `m4/run/deterministic-selection-debug/`（本地生成，不作为单测依赖）。
