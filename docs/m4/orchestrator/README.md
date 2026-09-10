# M4 离线编排层

2026-09-08：新输出为 `m4-orchestration-v2` / `pending_selection`，候选由 Pyomo/HiGHS 生成，最终选择位于独立数学决策模块。仅接受当前 v2 契约，旧 v1 编排文件须重新求解。

`m4.orchestrator` 是阶段 B1 的 Mock/离线编排包。它从独立 JSON 文件加载严格的 `OptimizationRequest`，逐站调用现有 `M4Optimizer`，并把站级结果写成统一 JSON。它不是常驻服务，也不连接网络、数据库、AI、EMS、设备或其他外部系统。

## CLI 使用

在仓库根目录运行 Task 5 使用的完整命令：

```bash
.venv/bin/python -m m4.orchestrator \
  --input m4/mock/orchestration/station-1.json \
  --input m4/mock/orchestration/station-2.json \
  --output m4/mock/orchestration/orchestration-result.json \
  --model-version m4-stage-a-v1 \
  --orchestrator-version m4-orchestrator-b1-v1
```

`--input` 可重复，且至少提供一次；`--output` 和 `--model-version` 必填且只能提供一次。`--orchestrator-version` 默认是 `m4-orchestrator-b1-v1`。CLI 按参数顺序加载输入；输出中的成功站点先按 `station_id` 排序，错误站点随后按 `input_ref` 稳定排序。

退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 已写入结果，`overall_status=completed` |
| `1` | 已写入 `partial_failure`/`failed` 结果，或结果文件写入失败 |
| `2` | `argparse` 参数错误，例如缺少必填参数或重复提供单值参数 |

## Python 使用

```python
from pathlib import Path

from m4.orchestrator import M4Orchestrator, load_station_input

station_inputs = [
    load_station_input(
        Path("m4/mock/orchestration/station-1.json"),
        input_ref="input-1",
    ),
    load_station_input(
        Path("m4/mock/orchestration/station-2.json"),
        input_ref="input-2",
    ),
]

result = M4Orchestrator(
    model_version="m4-stage-a-v1",
    orchestrator_version="m4-orchestrator-b1-v1",
).run(station_inputs)
```

`load_station_input()` 负责文件读取和严格输入校验；`M4Orchestrator.run()` 不读写文件、不读取环境变量，并要求非空输入序列。

## 输出契约

| 层级 | 字段 | 说明 |
|---|---|---|
| 顶层 | `schema_version`、`run_id` | 契约版本与单次运行标识 |
| 顶层 | `started_at`、`finished_at` | 带 UTC 偏移的运行时间 |
| 顶层 | `orchestrator_version`、`model_version` | 编排层与优化模型版本 |
| 顶层 | `overall_status` | `completed`、`partial_failure` 或 `failed` |
| 顶层 | `stations`、`errors` | 各输入的站级结果；顶层错误仅用于重复站点等整批错误 |
| 站级 | `input_ref`、`station_id`、`request_id` | 安全输入标识与业务标识；非法输入无法安全提取时业务标识可为 `null` |
| 站级 | `status`、`error` | 站级状态与稳定、脱敏的错误对象 |
| 站级 | `input_summary` | 有效输入的时间、版本、容量、SOC 与核心约束摘要 |
| 站级 | `optimization_result` | 优化器完整结果；调用失败或输入失败时为 `null` |
| 站级 | `selection_status`、`selected_candidate_id` | 新输出固定为 `pending_selection`、`null` |
| 站级 | `dispatch_status`、`ems_task_id` | 固定为 `not_dispatched`、`null` |

顶层状态：

- `completed`：全部站点均为 `optimized`。
- `partial_failure`：至少一个站点为 `optimized`，但另有站点失败或无可用候选。
- `failed`：没有任何 `optimized` 站点，或存在重复 `station_id` 顶层错误。

站级状态：

- `optimized`：优化结果至少含一个 `optimal` 或 `feasible` 候选。
- `no_usable_candidate`：优化调用完成，但所有候选均不可用；保留优化结果及 `NO_USABLE_CANDIDATE` 错误。
- `input_error`：文件、编码、JSON、请求校验失败，或因重复 `station_id` 整批拒绝。
- `optimization_error`：有效输入的优化调用异常或返回的站点/请求标识不一致。

即使站点失败，选择/EMS 字段仍保持上述固定空状态；候选快照不表示最终选择，也不表示下发。

## 安全、隔离与确定性规则

- 输入必须是 UTF-8 JSON，并严格符合 `m4.optimizer.OptimizationRequest`；路径、JSON 原文、内部异常和堆栈不会进入公共错误。
- 每份输入独立加载、逐站求解。单站输入或优化失败不阻断健康站点。
- 任意有效输入出现重复 `station_id` 时，在所有求解前整批拒绝，避免站点结果串用。
- writer 在目标目录创建临时文件，写入、刷新并关闭后通过 `os.replace()` 原子替换；失败时保留旧目标并清理本次临时文件。
- JSON 使用 UTF-8、稳定键顺序、固定缩进和单个末尾换行，禁止 NaN。
- 相同输入和固定依赖下，站点/候选顺序与业务约束应保持一致；`run_id`、运行时间及 `solve_seconds` 属于运行元数据。同目标值可能存在多套合法计划，不承诺跨 Pyomo/HiGHS 版本逐点或逐字节一致。

## 未来适配边界

- FastAPI：未来路由只承担认证、请求/响应适配和调用 `M4Orchestrator.run()`，不得复制站点隔离、候选判断或错误映射逻辑。
- 数学决策：`m4.selection.solver_selection` 位于 `optimization_result` 与最终选择之间，只能选择已有且有效的 `profile_id`，不能修改 96 点计划。
- EMS：适配器位于候选明确选择且完成确定性复验之后；只有有效候选才能转换为下发 DTO。

数学决策及本地 FastAPI 适配已在独立包中实现，见[求解器决策说明](../docs/m4/求解器决策说明.md)。本包不包含这些服务或 EMS 适配器。

## 阶段限制

仓库中的[输入样例](../m4/mock/orchestration/station-1.json)与[输出样例](../m4/mock/orchestration/orchestration-result.json)仅用于 Mock/离线开发和接口验收。结果不是生产调度计划，不可用于 EMS 或设备执行，也不构成真实节费结论或节费承诺。
