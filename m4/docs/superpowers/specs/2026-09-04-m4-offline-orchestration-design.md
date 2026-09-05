# M4 离线编排层设计

日期：2026-09-04
状态：已确认
范围：阶段 B1，Mock/离线数据编排

## 1. 背景

M4 阶段 A 已完成独立的 `m4_optimizer` 数学优化核心：单个电站输入一份完整的 96 点请求，输出 `balanced`、`cost`、`pv` 三套候选，并对计划和指标执行确定性复验。

下一步需要在数学优化器与未来前端、AI、EMS 之间建立稳定的编排边界。本阶段只完成离线编排：读取两个电站的独立 Mock JSON，逐站调用真实优化器，并生成统一结果 JSON。

本阶段不修改 HTML，不调用 AI，不连接 EMS、数据库、HTTP 服务或真实设备。

## 2. 目标

- 在仓库内新增独立的 `m4_orchestrator` Python 包，但不拆分独立微服务；
- 从外部 JSON 文件读取每个电站的优化请求，不把站点输入写死在 Python 中；
- 对任意数量的唯一 `station_id` 独立编排，本次 Mock 和验收固定覆盖两个电站；
- 每个成功电站保留三套候选的完整 96 点计划、指标、风险和审计信息；
- 明确输出 AI 尚未介入、EMS 尚未下发；
- 单站失败不阻断其他站，重复 `station_id` 则整体拒绝；
- 为未来 FastAPI 路由、AI 选择器和 EMS 适配器提供可复用的稳定入口。

## 3. 非目标

- 不修改 `m4/M4优化调度控制台-线上版.html` 或其他前端文件；
- 不实现真实或模拟 AI 选择，不生成选择理由；
- 不实现 EMS 计划下发、接收状态、执行日志或设备控制；
- 不读取 M1、M2、M3、NocoBase 或生产配置；
- 不建设定时任务、常驻进程、HTTP API、数据库表或消息队列；
- 不改变 `m4_optimizer` 的数学模型、候选优先级或安全约束。

## 4. 方案选择

### 4.1 采用方案

采用“独立 Python 包 + 严格 JSON 输入 + 单次 CLI + 统一 JSON 输出”。

```text
station-1.json ─┐
                ├─ 输入加载与逐站校验
station-2.json ─┘
                       ↓ station_id 独立执行
                M4Optimizer.optimize()
                       ↓
                三套完整候选
                       ↓
           编排结果组装与站级错误隔离
                       ↓
            orchestration-result.json
```

`m4_orchestrator` 是仓库内可导入模块，不是独立部署单元。CLI 是第一个调用方；未来 FastAPI 只需调用同一个服务入口并序列化同一输出模型。

### 4.2 未采用方案

- 单文件脚本：初期代码少，但输入校验、错误隔离、CLI 和未来 API 边界容易耦合；
- 当前直接建设 FastAPI：暂时没有实时调用需求，会过早引入部署和服务治理成本；
- 把 Mock 写进 Python：不利于与 EMS、预测和电价团队确认接口，也违反数据与代码分离原则。

## 5. 包结构

```text
m4_orchestrator/
├── __init__.py       # 导出稳定公共入口和输出模型
├── __main__.py       # 支持 python -m m4_orchestrator
├── cli.py            # 参数解析、退出码和文件输出
├── contracts.py      # 编排层严格输入/输出契约
├── loader.py         # 独立读取并校验每个 JSON 文件
├── service.py        # M4Orchestrator 与站级错误隔离
└── writer.py         # 确定性 JSON 序列化和原子替换

m4/mock/orchestration/
├── station-1.json
├── station-2.json
└── orchestration-result.json

tests/
├── test_m4_orchestrator_contracts.py
├── test_m4_orchestrator_loader.py
├── test_m4_orchestrator_service.py
└── test_m4_orchestrator_cli.py
```

保持文件职责单一。CLI 不包含业务判断，loader 不调用优化器，writer 不理解候选内容。

## 6. 输入设计

### 6.1 文件格式

每个 `--input` 文件包含一份完整的 `m4_optimizer.OptimizationRequest` JSON。字段名、严格类型、96 点连续时间轴、来源版本、能力快照、约束和三套目标配置均复用阶段 A 契约，不建立第二套近似输入模型。

Mock 文件分别描述电站 1 和电站 2，并使用不同的：

- `request_id`、`station_id` 和来源版本；
- 负荷与光伏曲线；
- 初始 SOC、容量和最大充放电能力；
- 独立需量上限与允许反送配置；
- 三套候选 profile version。

由此可以验证两站不会共享请求、需量、SOC、候选或计划。

### 6.2 加载规则

- CLI 按命令行中 `--input` 的顺序读取文件；
- 单个文件不存在、不是 UTF-8、不是合法 JSON 或不能构造 `OptimizationRequest` 时，形成该输入来源的站级 `input_error`，继续处理其他文件；
- 若原始对象含合法非空 `station_id`，错误记录保留该值；否则使用安全的输入文件名作为 `input_ref`，`station_id` 为 `null`；
- 文件路径只用于本地 CLI 诊断，不进入优化器；错误信息不包含文件内容、异常堆栈或未来可能出现的凭据；
- 多份有效输入出现重复 `station_id` 时，无法证明站点隔离，整体标记 `failed`，不调用任何站点优化器。

`M4Orchestrator` 不写死电站数量或具体设备编号；“两个电站”由本次 Mock 文件和验收用例保证。

## 7. 公共服务入口

稳定核心入口为同步、无外部 I/O 的服务对象：

```python
result = M4Orchestrator(
    model_version="m4-stage-a-v1",
    orchestrator_version="m4-orchestrator-b1-v1",
).run(station_inputs)
```

其中 `station_inputs` 是 loader 逐文件产生的输入项集合，每项包含：

- 安全的 `input_ref`；
- 成功解析的 `OptimizationRequest`，或结构化输入错误；
- 能够从原始顶层安全提取时的 `station_id` 和 `request_id`。

服务自身不打开文件、不写文件、不读取环境变量，也不持有跨运行的站点状态。未来 FastAPI 可把请求体适配为相同输入项后直接调用 `run()`。

## 8. 输出契约

### 8.1 顶层结果

`M4OrchestrationResult` 包含：

| 字段 | 说明 |
|---|---|
| `schema_version` | 编排结果 JSON 契约版本 |
| `run_id` | 每次运行的唯一标识，不参与业务确定性比较 |
| `started_at` / `finished_at` | UTC 编排时间 |
| `orchestrator_version` | 编排层版本 |
| `model_version` | 传给数学优化器的模型版本 |
| `overall_status` | `completed`、`partial_failure` 或 `failed` |
| `errors` | 仅用于重复站点等顶层错误 |
| `stations` | 各输入来源的独立结果，成功项按 `station_id` 排序，错误项按 `input_ref` 稳定排序 |

状态计算规则：

- 所有站点至少有一个 `optimal` 或 `feasible` 候选：`completed`；
- 部分站点可用、部分站点失败或无可用候选：`partial_failure`；
- 没有任何可用站点，或出现重复 `station_id`：`failed`。

### 8.2 单站结果

每个 `StationOrchestrationResult` 包含：

| 字段 | 说明 |
|---|---|
| `input_ref` | CLI 输入来源的安全标识 |
| `station_id` / `request_id` | 无法从非法输入安全提取时可为 `null` |
| `status` | `optimized`、`no_usable_candidate`、`input_error` 或 `optimization_error` |
| `input_summary` | 成功输入的时间窗、来源版本、SOC、能力和核心约束摘要 |
| `optimization_result` | 成功调用阶段 A 服务后的完整结果，包含三候选及各自 96 点计划；调用失败时为 `null` |
| `selection_status` | 本阶段固定为 `pending_ai` |
| `selected_candidate_id` | 本阶段固定为 `null` |
| `dispatch_status` | 本阶段固定为 `not_dispatched` |
| `ems_task_id` | 本阶段固定为 `null` |
| `error` | 失败时的安全错误代码与说明，成功时为 `null` |

即使某站输入或优化失败，`selection_status` 仍表示“本阶段没有调用 AI”，不是表示该站已有可供 AI 选择的候选。是否存在可用候选必须读取 `status` 和候选状态。

### 8.3 错误契约

错误对象只提供稳定代码和安全说明。首版代码包括：

- `INPUT_NOT_FOUND`；
- `INPUT_READ_ERROR`；
- `INPUT_ENCODING_ERROR`；
- `INPUT_JSON_ERROR`；
- `INPUT_VALIDATION_ERROR`；
- `DUPLICATE_STATION_ID`；
- `OPTIMIZATION_ERROR`；
- `NO_USABLE_CANDIDATE`。

错误说明不得包含 JSON 原文、完整 Python 异常、绝对路径或堆栈。测试可以检查内部异常分类，但公共结果只输出稳定信息。

## 9. 编排流程

1. CLI 校验至少提供一个 `--input`、一个 `--output` 和非空版本参数；
2. loader 独立读取每个输入文件，生成成功输入项或结构化错误输入项；
3. service 在任何求解前检查有效输入的 `station_id` 是否重复；
4. 若重复，整体拒绝并生成失败结果，不调用优化器；
5. 否则逐站调用 `M4Optimizer.optimize()`；
6. 某站调用异常时转为该站 `optimization_error`，继续下一站；
7. 正常结果至少有一个 `optimal` 或 `feasible` 候选时标记 `optimized`，否则标记 `no_usable_candidate`；
8. 组装固定的 AI/EMS 空状态并计算顶层状态；
9. writer 将完整 Pydantic 结果序列化为 UTF-8 JSON；
10. 先在目标文件同目录创建临时文件，写入、刷新并关闭后使用 `os.replace()` 原子替换；
11. CLI 在 `completed` 时退出 0，在 `partial_failure` 或 `failed` 时退出 1；参数错误沿用 `argparse` 的退出码 2，结果文件写入失败也退出 1。

本阶段是单次同步离线运行，不实现重试、并行求解、定时触发或缓存。

## 10. 确定性与版本

- `run_id`、顶层开始/结束时间、优化器运行时间和求解耗时属于运行元数据，允许变化；
- 相同输入和固定依赖下，站点排序、候选排序、`plan_version`、候选状态、指标和 96 点计划应在数值容差内保持一致；
- 输出 JSON 使用固定缩进、UTF-8、末尾换行和稳定键顺序，便于人工比较；
- `schema_version`、`orchestrator_version`、`model_version`、来源版本和 profile version 必须完整保留；
- 本阶段不把整个结果文件做内容哈希，也不承诺跨 SciPy/HiGHS 版本的逐字节相同。

## 11. Mock 数据

Mock 输入使用固定计划日期和固定时区，避免每天打开项目都产生不同业务时间窗。两站至少体现：

- 电站 1：较小容量、较紧需量约束，能够展示需量削峰和峰谷套利；
- 电站 2：较大容量、明显午间光伏余量，能够展示光伏充电、外送或不可吸收风险；
- 两站均包含完整 96 点负荷、光伏、买卖电价和三套目标配置；
- 所有标识明确为 Mock，不使用真实 EMS 设备序列号、凭据或生产地址。

仓库提交一份由当前实现生成的 `orchestration-result.json`，作为后续前端接口样例。该文件只能作为演示数据，不能视为现场策略、真实节费证据或 EMS 可执行计划。

## 12. 未来扩展边界

### 12.1 接前端

未来新增 FastAPI 路由时，路由只负责认证、请求/响应序列化和调用 `M4Orchestrator.run()`；不得在路由中复制站点隔离、候选判断或错误映射逻辑。前端消费与离线 JSON 相同的输出契约。

### 12.2 接 AI

在 `optimization_result` 与 `selection_status` 之间增加候选选择适配器。AI 只能返回已有且有效的 `profile_id`，不能修改 96 点计划。接入前本阶段字段保持 `pending_ai` 和 `null`。

### 12.3 接 EMS

EMS 适配器必须位于选择与下发前确定性复验之后。只有明确选中的有效候选才能形成下发 DTO。接入前本阶段字段保持 `not_dispatched` 和 `null`。

## 13. 测试与验收

核心规则采用 TDD。至少覆盖：

### 13.1 契约与加载

- 合法输入文件构造严格的 `OptimizationRequest`；
- 文件不存在、编码错误、JSON 错误和 Pydantic 校验错误分别映射到稳定错误码；
- 公共错误不泄露绝对路径、JSON 原文或堆栈；
- 编排结果可以从生成的 JSON 重新通过严格输出模型校验；
- `selection_status`、`selected_candidate_id`、`dispatch_status` 和 `ems_task_id` 固定为空状态。

### 13.2 服务

- 两个 Mock 电站分别产生三套候选，每个有效候选包含 96 点计划；
- 两站的请求、SOC、容量、需量、来源版本、profile version、指标和计划不串用；
- 单站输入错误不阻断另一站；
- 单站优化异常不阻断另一站；
- 无可用候选得到 `no_usable_candidate`；
- 重复 `station_id` 在求解前整体拒绝；
- 任一可用站加任一失败站得到 `partial_failure`，全部失败得到 `failed`；
- 相同输入重复运行时，排除运行元数据后的业务结果一致。

### 13.3 CLI 与写入

- CLI 可以从两个输入文件生成可重新校验的结果 JSON；
- `completed` 返回 0，部分或全部失败返回 1，参数错误返回 2；
- 原子替换成功后目标文件完整，替换前失败不会破坏已有目标文件；
- 输出目录不存在时给出稳定本地错误，不留下临时文件；
- CLI 不发起网络请求，不读取数据库，不调用 AI 或 EMS。

### 13.4 回归与性能

- 继续执行全部 M4 优化器测试；
- 执行项目全量 Python 测试；
- 记录两个 Mock 电站完成六次候选求解的本机耗时，只作为开发基线，不声明生产 SLA；
- 执行 `compileall` 和 `git diff --check`。

## 14. 完成标准

- `m4_orchestrator` 可作为 Python 包导入，并可通过 `python -m m4_orchestrator` 运行；
- 两个电站 Mock 输入独立、完整且不写死在 Python；
- 统一结果包含每站三候选的完整计划，以及明确的 AI/EMS 空状态；
- 站级错误隔离和重复站点整体拒绝均有自动化测试；
- 结果文件原子生成并能通过自身契约反序列化；
- 不修改 HTML，不出现网络、AI、EMS、数据库或设备写入；
- 新增测试、现有 M4 测试和项目回归均通过；
- 文档明确阶段 B1 只是离线编排与接口样例，不构成生产闭环。
