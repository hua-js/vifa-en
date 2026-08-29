# M3 自定义预测任务与持久化设计

日期：2026-08-29

## 1. 目标

M3 页面允许用户选择场站、历史数据窗口、预测天数和输出粒度，提交一次独立预测任务，并在同一时间轴上查看实际值与预测值。任务及结果必须持久化，Worker 重启后仍可查询；MAPE 只能在配置可比的预测任务之间统计。

已经确认的业务规则：

- 历史数据量为 7–90 天整数。
- 预测时长为 1–7 天整数。
- 输出粒度允许 1 小时、30 分钟、15 分钟、5 分钟、1 分钟和 30 秒，对应每天 24、48、96、288、1,440、2,880 点。
- 历史少于 28 天时只启用 `SeasonalNaive`。
- 历史达到 28 天后启用 `SeasonalNaive`、`AutoETS`、`AutoARIMA`、`MSTL` 完整选模。
- 负载和 SOC 都输出预测曲线；后续实际值到达后按相同目标时间回填。
- 不修改现有固定 15 分钟、未来 24 小时的 `energy_forecast_latest` 链路，也不复用固定七日验收表。

## 2. 独立任务边界

新增 `custom_forecast` 任务类型。现有 `forecast` 和 `model_selection` 任务仍按原合同运行。

浏览器提交：

```json
{
  "history_start": "2026-07-31T00:00:00+08:00",
  "history_end": "2026-08-29T00:00:00+08:00",
  "forecast_days": 1,
  "interval_seconds": 900,
  "idempotency_key": "由浏览器生成的单次提交标识"
}
```

服务端验证历史窗口为完整的 7–90 个上海自然日，预测时长为 1–7 天，粒度属于固定白名单，并计算：

- `history_days`
- `points_per_day = 86400 / interval_seconds`
- `expected_points_per_series = forecast_days * points_per_day`
- `model_policy = seasonal_naive_only | full_selection`
- `forecast_start = history_end`
- `forecast_end = forecast_start + forecast_days`

同一 `station_id + idempotency_key` 只能创建一个任务。任务 ID 使用对外不可枚举的文本标识，数据库内部仍使用 `bigint identity` 主键。

## 3. 数据源与时间粒度

现有 Source API 和 `ObservationPoint` 合同固定为 15 分钟，不能直接扩展后继续声称兼容。新增 v2 自定义预测数据源合同，显式传递 `interval_seconds`，并返回相同粒度的有序负载/SOC 点。

源数据聚合规则：

- 负载使用输出桶内有效样本均值。
- SOC 使用输出桶内最后一个有效样本。
- 桶必须拥有足够覆盖率；覆盖率阈值由源采样周期计算，不能继续使用“每 15 分钟至少 12 点”的固定规则。
- 所有时间使用 `Asia/Shanghai` 的显式偏移，桶边界必须与所选粒度对齐。
- 一个请求窗口仍限制为不超过 7 天，Worker 对 7–90 天历史分块拉取。

v1 固定 15 分钟接口保持不变，避免影响当前正式预测和验收。

## 4. 模型参数化

自定义预测使用独立的训练与预测合同，不修改 `ForecastPoint`、`ForecastSeries`、`LatestSnapshot` 的 96 点约束。

计算参数：

- `points_per_day = 86400 / interval_seconds`
- 日周期为 `points_per_day`
- 周周期为 `7 * points_per_day`
- 预测步数为 `forecast_days * points_per_day`
- 交叉验证 horizon 使用一天点数；窗口数最多 7 个，并受可用完整历史约束。

历史少于 28 天时不运行完整选模，负载与 SOC 均直接使用 `SeasonalNaive`。达到 28 天后分别选出负载和 SOC 的最优模型。模型失败时允许回退至 `SeasonalNaive`，并持久化回退原因。

30 秒粒度的完整选模计算和结果量最大。Worker 对自定义预测保持有界队列和按站并发限制，避免挤占现有定时预测。

## 5. 持久化模型

### 5.1 `energy_forecast_manual_runs`

一行表示一次不可变的预测请求和可变的执行摘要。

核心字段：

- `id bigint identity primary key`
- `run_id text unique not null`
- `station_id text not null`
- `idempotency_key text not null`
- `history_start/history_end timestamptz not null`
- `history_days smallint not null`
- `forecast_start/forecast_end timestamptz not null`
- `forecast_days smallint not null`
- `interval_seconds integer not null`
- `points_per_day integer not null`
- `expected_points_per_series integer not null`
- `model_policy text not null`
- `status text not null`
- `model_manifest jsonb`
- `source_manifest jsonb`
- `content_hash text`
- `error_code text`
- `requested_by text`
- `started_at/completed_at/evaluated_at timestamptz`
- NocoBase 系统审计字段：`createdAt`、`createdBy`、`updatedAt`、`updatedBy`

唯一约束为 `run_id` 及 `station_id + idempotency_key`。主要查询索引为 `(station_id, createdAt desc)` 和 `(status, createdAt)`。

### 5.2 `energy_forecast_manual_points`

每行表示一个序列在一个目标时间的预测与后续实绩。

核心字段：

- `id bigint identity primary key`
- `run_pk bigint not null`，外键关联任务，删除策略为 `restrict`
- `unique_id text not null`
- `target_time timestamptz not null`
- `horizon_step integer not null`
- `model_name text not null`
- `raw_forecast/forecast_value numeric(14,6) not null`
- `is_clipped boolean not null`
- `actual_value numeric(14,6)`
- `actual_quality text`
- `actual_source_revision bigint`
- `actual_recorded_at/evaluated_at timestamptz`
- `absolute_percentage_error numeric(14,8)`

唯一约束为 `(run_pk, unique_id, target_time)`，同时覆盖按任务、序列、时间读取。外键列必须有索引覆盖。

### 5.3 `energy_forecast_manual_evaluations`

每次任务最多保存负载、SOC 和 overall 三行摘要。字段包含任务外键、`evaluation_key`、期望/有效/零值点数、MAPE/MAE/sMAPE/WAPE/中位 APE/P90 APE、SeasonalNaive 基线 MAPE、相对基线提升、计算时间和结果状态。

MAPE 展示遵循以下可比键：

```text
station_id + unique_id + interval_seconds + forecast_days + model_policy
```

“近 7 日 MAPE”只聚合目标时间落在最近七日、已有有效实绩、且可比键一致的预测点。历史窗口长度保留在每次任务中展示，但不把不同输出粒度或预测时长混在同一个 MAPE 中。

## 6. 状态与恢复

状态流：

```text
queued -> running -> succeeded -> evaluated
                  \-> failed
```

- 创建任务时先持久化 `queued`，再进入有界执行队列。
- Worker 启动时恢复 `queued/running` 任务；旧 `running` 任务重新排队，依靠任务身份和点唯一约束幂等续写。
- 预测点完整写入、数量和哈希回读一致后才能把任务标记为 `succeeded`。
- 实绩可用后回填预测点并写评估摘要，完成后标记为 `evaluated`。
- 失败只保存稳定错误码，不向浏览器暴露上游正文、令牌或堆栈。

## 7. API 与页面行为

新增受保护接口：

- `POST /v1/stations/{station_id}/runs/custom-forecast`
- `GET /v1/custom-forecast-runs/{run_id}`
- `GET /v1/custom-forecast-runs/{run_id}/result`
- `GET /v1/stations/{station_id}/custom-forecast-performance`

页面通过同源网关提交，不获取 Worker 管理令牌。POST 请求沿用当前 NocoBase 当前用户身份校验，并增加固定路径、请求体白名单、响应体上限和幂等键。当前生产未启用，因此先完成 Worker 合同与持久化，再接 Node-RED 同源代理。

按钮行为：

- 输入合法且身份可用时启用。
- 提交后显示 `排队中/预测中`，禁止重复点击。
- 轮询任务状态；成功后读取该 run 的实际值、预测值、模型摘要和 MAPE。
- 场站切换不丢失已完成 run，可按场站恢复最近一次任务。
- 接口未部署时保持明确的不可用状态，不使用演示数据伪装成功。

## 8. 权限与非目标

- 浏览器只能创建和读取自己被授权场站的自定义预测任务。
- Worker 使用独立最小权限角色写三张新表；不授予删除、导入、导出或任意集合访问。
- 第一阶段不改变生产自动预测计划、不改变正式七日验收口径、不迁移旧快照。
- 按用户要求，本阶段不新增或运行自动化测试；交付仅做静态合同、语法、模板同步和接口冒烟验证。
