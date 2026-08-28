# M3 七日独立验收任务控制面设计

日期：2026-08-28

## 1. 背景

M3 已实现正式七日验收的基线生成、实绩回填、指标计算和结果展示，但生产配置保持 `M3_ACCEPTANCE_ENABLED=false`。原始实现预留了通过 Node-RED 固定 HTTP 接口读取外部 EMS 验收上下文的路径；仓库和当前生产部署均未落地该外部控制面，而且现行生产明确不导入包含该路径的旧 `energy_forecast_flow.json`。

生产已在 NocoBase 创建 `energy_forecast_acceptance_runs` 表并安装字段与数据库约束。本设计将该表作为验收任务主表：授权人员在 NocoBase 后台创建和控制任务，Worker 使用现有固定 NocoBase 客户端直接读取活动任务，继续向现有明细表写入验收证据，并回写任务进度摘要。生产 Node-RED Flow 不参与验收控制面。

## 2. 目标与非目标

目标：

- 1号、2号电站分别创建验收任务，互不影响。
- 每个电站最多存在一个活动任务。
- 授权人员通过 NocoBase 后台启动、完成或取消任务。
- Worker 每天上海时间 01:02 生成一个正式基线批次。
- Worker 根据持久化明细恢复并同步任务进度，重启不丢状态。
- 任务主表只保存控制状态和结果摘要；详细预测、实绩与指标继续保存在现有明细表。
- Worker 和 Dashboard 使用职责分离的最小权限凭据。

非目标：

- 不新增浏览器端或公开的验收启动接口。
- 不允许 Worker 修改验收窗口、运行标识或控制状态。
- 不把 1,344 个七日预测点或完整指标复制到任务主表。
- 不自动将 `control_state` 改为 `completed`；授权人员确认最终结果后人工完成任务。
- 不改变模型选择、预测算法、Ready 判定或普通十五分钟预测计划。

## 3. 数据模型

### 3.1 任务主表

NocoBase 集合与 PostgreSQL 物理表均命名为 `energy_forecast_acceptance_runs`。

| 字段 | PostgreSQL 类型 | 可空 | 写入方 | 说明 |
|---|---|---:|---|---|
| `id` | bigint identity | 否 | 数据库 | 主键 |
| `station_id` | text | 否 | 管理员 | 完整电站 ID |
| `acceptance_run_id` | text | 否 | 管理员 | 运行标识，匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$` |
| `window_start` | timestamptz | 否 | 管理员 | 上海时间首日 01:00 |
| `window_end` | timestamptz | 否 | 管理员 | `window_start + 7 days` |
| `control_state` | text | 否 | 管理员 | `active`、`completed` 或 `cancelled` |
| `completed_days` | smallint | 否 | Worker | 已完整持久化的每日基线数，0–7 |
| `result_state` | text | 否 | Worker | `pending`、`in_progress`、`passed`、`failed` 或 `insufficient_data` |
| `calculated_at` | timestamptz | 是 | Worker | 最终结果计算时间 |
| `createdAt` | timestamptz | 否 | NocoBase | 系统创建时间；物理字段使用带引号的驼峰名 |
| `updatedAt` | timestamptz | 否 | NocoBase | 系统更新时间；物理字段使用带引号的驼峰名 |

已安装的数据库不变量：

- `station_id, acceptance_run_id` 唯一。
- 部分唯一索引保证每站最多一行 `control_state='active'`。
- 窗口严格为七天，并按上海时间 01:00 对齐。
- `completed_days` 在 0–7 之间。
- `pending` 只能对应 0 天且 `calculated_at` 为空。
- `in_progress` 对应 1–7 天且 `calculated_at` 为空。
- 最终结果对应 7 天，且 `calculated_at >= window_end`。
- `completed` 控制状态只能对应最终结果。

### 3.2 现有明细表

- `energy_forecast_batches`：每站每日一个不可变正式基线批次。
- `energy_forecast_points`：保存预测点，并在目标时间过去后回填实绩。
- `energy_forecast_evaluations`：保存两条序列及整体结果。
- `energy_forecast_latest`：普通运行的最新预测快照，不作为正式验收证据。

第一版不修改这些表的结构。任务主表与验收明细通过 `station_id + acceptance_run_id` 关联，Worker 在每次读写时验证两项身份一致。

## 4. 权限模型

### 4.1 NocoBase 管理员

管理员通过 NocoBase 后台创建任务并写入：

- `station_id`
- `acceptance_run_id`
- `window_start`
- `window_end`
- `control_state`

新任务必须显式设为 `active`，`completed_days` 使用默认值 0，`result_state` 使用默认值 `pending`。管理员看到最终结果后将 `control_state` 改为 `completed`；需要中止时改为 `cancelled`。

### 4.2 Worker 角色

Worker 只使用一个专用 `M3_NOCOBASE_API_KEY`。该 Key 绑定一个合并后的最小权限角色，同时承载既有明细写入、新增的限定读取和任务摘要更新；不得为验收任务表另配第二个 Worker Key。

合并角色的限定读取为：

- `energy_forecast_batches.list`：只读 `id`、任务身份、批次窗口、状态、`write_state`、模型清单、内容哈希和点模板；只按 `station_id`、`acceptance_run_id`、`write_state` 过滤，只按 `issued_at` 排序。这一字段并集恰好覆盖按站恢复 `writing` 批次和计算完整日数的两个固定调用。
- `energy_forecast_evaluations.list`：只读 `station_id`、`acceptance_run_id`、`evaluation_key`、`window_start`、`window_end`、`outcome`、`calculated_at`；只按 `station_id`、`acceptance_run_id` 过滤。

同一角色保留四个明细集合现有的限定写动作，并对任务表只允许：

- `list`：读取 `id`、任务身份、窗口、控制状态和结果摘要；按 `station_id`、`acceptance_run_id`、`control_state` 过滤。
- `update`：以 `id` 为记录键，只写 `completed_days`、`result_state`、`calculated_at`。

Worker 不得写 `station_id`、`acceptance_run_id`、窗口或 `control_state`，也不得创建、删除、导入或导出任务。

### 4.3 Dashboard 角色

Dashboard 继续从批次和评估表构建详细七日验收区域。第一版不要求 Dashboard 直接读取任务主表，避免扩大公开只读面的权限。

## 5. Worker 任务仓储

新增独立的 Worker 服务边界负责读写任务主表。该服务只接收现有 `NocoBaseApiClient`，不接受调用方提供 URL、集合名或任意字段列表。

活动任务查询固定包含：

- `station_id=<配置中的完整电站 ID>`
- `control_state=active`
- 明确的字段列表：`id`、任务身份、窗口、控制状态和结果摘要

返回规则：

- 0 行：返回无活动任务，调度器正常跳过该站。
- 1 行：严格校验记录主键、运行标识、七日窗口、上海时间 01:00 边界、控制状态和摘要状态。
- 超过 1 行、字段缺失、额外字段、非法时间或非法状态：抛出 `acceptance_context_invalid`，不选择任意一行。
- NocoBase 401/403、非成功状态、重定向、超时或超限响应：沿用固定 Sink 错误，不泄露 API Key 或上游响应正文。

旧 Node-RED 验收上下文客户端不再参与 `AcceptanceService` 装配。NocoBase 是唯一验收任务控制源；`M3_SOURCE_BASE_URL` 和 `M3_SOURCE_API_TOKEN` 只供现有运行告警客户端使用。

## 6. Worker 行为

### 6.1 非活动电站

`M3_ACCEPTANCE_ENABLED=true` 是全局功能开关，活动任务按站决定。每日 01:02 调度到一个没有活动任务的电站时，Worker 任务仓储返回空结果，Worker 正常跳过，不产生 `acceptance_inactive` 告警。一个电站未启用不影响另一个电站。

### 6.2 每日基线与进度

活动任务窗口内，每日 01:02 继续执行现有正式基线流程。基线以 `writing → complete` 协议完成后，Worker 查询同一 `station_id + acceptance_run_id` 下的完整批次数：

- 0 批：`completed_days=0`，`result_state=pending`。
- 1–7 批：`completed_days` 等于去重后的完整日批次数，`result_state=in_progress`。
- 超过 7 批、重复日期、批次落在窗口外或任务身份不一致：拒绝摘要更新并告警。

摘要更新入口和发出更新请求前都必须重新读取任务行，要求任务身份、主键、窗口不变且 `control_state` 仍为 `active`，再以主键 `id` 更新三个 Worker 字段。更新响应必须回显相同主键、仍为 `active` 的控制状态和期望摘要值。

当前固定 NocoBase `update` 接口只接受 `filterByTk=id`，没有可用的控制状态 CAS 条件。因此取消会阻止后续调度，并在摘要更新请求发出前立即被检查；若取消与已经发出的更新请求并发，该请求仍可能完成三个摘要字段的写入，但绝不修改 `control_state`。这是无 CAS 条件下保留的单个在途竞态。

### 6.3 实绩回填与最终结果

普通预测任务继续每十五分钟回填已经完成的正式点。七日窗口结束且最后实绩可用后，Worker 计算两条序列及整体结果，并先写 `energy_forecast_evaluations`。

三条评估记录 `station_total_load`、`storage_soc`、`overall` 全部成功并经过回读验证后，Worker 才将任务摘要更新为：

- `completed_days=7`
- `result_state=overall.outcome`
- `calculated_at=评估计算时间`

终态恢复必须按同一 `station_id + acceptance_run_id` 一次列出评估，并严格要求上述三个键各一行：不得缺失、重复或出现额外键；每行身份和七日窗口必须匹配任务，三个 `calculated_at` 必须一致且不早于 `window_end`，`overall.outcome` 必须等于两条序列结果按现有合并规则得到的结果。评估写入失败、不完整或不一致时，任务不得先显示最终摘要。

### 6.4 恢复与迟到修订

Worker 启动恢复以及后续活动任务处理会从完整批次和严格验证的三行最终评估重新计算摘要。启动时每个电站先独立恢复该站 `writing` 批次，再恢复该站任务摘要；某站失败只告警该站，其他站继续恢复，但任一站失败都会使启动 readiness 保持 `false`。任务主表不是详细结果的事实源；摘要与明细冲突时，以经过严格验证的批次和评估为准，并通过限定字段更新修复摘要。

任务在最终结果出现前必须保持 `active`，以允许迟到实绩继续回填。授权人员看到 Dashboard 的 7/7 最终结果后再将 `control_state` 改为 `completed`。

## 7. 状态流

```text
管理员创建 active 任务
  pending / 0 天
        ↓ 每日完整基线
  in_progress / 1–7 天
        ↓ 七日结束、实绩回填、评估完成
  passed | failed | insufficient_data / 7 天
        ↓ 管理员确认
  control_state = completed
```

管理员可在最终结果前将任务改为 `cancelled`。取消后 Worker 的活动任务查询返回空结果，不再创建新基线或回填该任务；每个摘要写边界也要求任务仍为 `active`。已经写入的证据不删除；受限于无 CAS 的固定更新接口，仅已经发出的摘要更新请求可能与取消并发完成，且该请求不能改变控制状态。

## 8. 失败处理与告警

需要保留或新增以下稳定错误码：

- `acceptance_context_invalid`：任务字段、身份或状态不合法。
- `acceptance_window_invalid`：窗口不是严格七天或未按 01:00 对齐。
- `acceptance_summary_invalid`：批次数、日期拓扑或摘要状态非法。
- `acceptance_run_update_incomplete`：任务摘要更新未完整落库或回显不一致。
- `sink_unauthorized`：Worker 对 NocoBase 的任务表权限不足。

非活动任务是正常状态，不触发告警。任务合同错误、活动任务基线失败、进度不可达、恢复失败和最终结果写入失败都按站告警，另一电站继续运行；启动恢复中任一站失败都会保持 readiness 为 `false`。

## 9. 代码与交付范围

需要修改：

- `m3/contracts/nocobase_collections.json`：加入第五张表及 Worker 最小权限。
- `m3_worker/services/acceptance_run_service.py`：新增固定任务仓储、严格记录验证和限定字段摘要更新。
- `m3_worker/services/acceptance_service.py`：使用任务仓储，跳过非活动任务，计算并同步摘要，恢复时校正。
- Worker 资源装配及必要的 DTO/客户端边界。
- 生产部署说明：记录第五张表权限和启用顺序；不新增验收 URL 或 Token。

不修改：

- StatsForecast 模型和选型逻辑。
- 现有四张明细表结构。
- Dashboard 对外 JSON 合同和页面布局。
- 生产 `m3_production_gateway_flow.json` 与其他 Node-RED Flow。
- M1、M2、反向代理和公开路由。

## 10. 验证与上线顺序

遵循当前交付约束，不新增、修改或执行自动化测试；仍引用 `context_source` / `SourceApiClient` 的旧测试接口在本次范围内明确保持未解决。实现阶段执行：

- JSON 语法和精确合同静态检查。
- Python 语法/导入检查。
- 仅在所需受保护 env 已存在时执行 Compose 配置检查，不为验证创建或修改 env。
- Git diff whitespace 检查。

生产启用顺序：

1. 确认任务表字段、约束和角色权限已安装。
2. 使用同一个 Worker Key 对任务表、批次和评估表执行固定字段/过滤只读权限探测，并在角色 UI 核验既有明细写动作和任务摘要更新字段；确认无活动任务时任务查询返回空列表。
3. 部署新 Worker 镜像，保持 `M3_ACCEPTANCE_ENABLED=false`。
4. 在 NocoBase 创建首个活动任务，窗口从计划首日上海时间 01:00 开始。
5. 使用相同 Worker Key 验证该站可读取唯一活动任务，另一站仍返回空列表。
6. 在首日 01:02 前设置 `M3_ACCEPTANCE_ENABLED=true` 并重建 Worker 容器。
7. 每日核对任务摘要和完整批次数；最终结果生成后人工将任务标记为 `completed`。
