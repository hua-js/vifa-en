# 场站能效后端数据源访问设计

日期：2026-08-25

## 1. 目标

`m2/station_energy_backend.py` 保持为纯 Python 单文件计算模块。它接收标准逻辑记录，完成数据校验、稳定结构转换和三条能效链路计算，并返回 HTML 看板所需的 Python 字典。

本阶段只实现与 API 无关的逻辑数据结构、数据完整性规则和数据源适配边界。NocoBase 的地址、路径、鉴权内容、分页方式和原始字段映射在真实 API 确定后实现，不在本阶段虚构。

本设计替代此前的命令行与 Node-RED 进程调用边界；原有计算公式、逻辑记录和看板响应字段契约继续有效。

## 2. 已确认的架构

```text
NocoBase 原始记录
    ↓ 字段映射、符号与单位转换
外部数据访问/API 适配层
    ↓
标准逻辑记录
    ↓ current + 最近 24 小时 trend_samples
m2/station_energy_backend.py 中的纯函数
    ↓ Python dict
外部 HTTP 层 → HTML 看板
```

生产计算模块不承担网络、文件、进程或 JSON 传输职责。外部适配层负责数据访问与 HTTP 响应，但不重复实现能效计算公式。

## 3. 数据源配置边界

NocoBase 地址、路径、令牌和字段映射不放入 `m2/station_energy_backend.py`。真实 API 确定后，由独立适配层持有连接配置并生成标准逻辑记录。任何响应或异常详情均不得包含令牌。

生产计算模块不提供模拟数据入口。开发和浏览器测试所需样本仅位于 `tests` 目录。

## 4. 标准逻辑记录

每条记录表示同一场站或同一母线在一个计量时刻的归一化值。功率单位统一为 kW，所有方向功率均为非负数。

```json
{
  "bus_id": "station-1",
  "data_time": "2026-08-25T12:00:00+08:00",

  "pv_dc_power": 190.0,
  "pv_ac_power": 183.0,
  "load_power": 80.0,

  "cabinet_charge_power": 103.0,
  "cabinet_discharge_power": 0.0,
  "pcs_charge_power": 100.0,
  "pcs_discharge_power": 0.0,
  "bms_charge_power": 95.0,
  "bms_discharge_power": 0.0,

  "grid_import_power": 0.0,
  "grid_export_power": 0.0,
  "storage_aux_power": 3.0,

  "device_status": {},
  "source_times": {}
}
```

### 4.1 字段含义

| 字段 | 含义 |
|---|---|
| `bus_id` | 场站或电气隔离母线标识 |
| `data_time` | 带显式时区的 ISO 8601 统一计算时间 |
| `pv_dc_power` | 全部光伏逆变器 DC 输入 |
| `pv_ac_power` | 全部光伏逆变器 AC 输出 |
| `load_power` | 场站生产/用户负载，不重复包含储能充电 |
| `cabinet_charge_power` | 全部柜内电表交流充电输入 |
| `cabinet_discharge_power` | 全部柜内电表交流放电输出 |
| `pcs_charge_power` | 全部 PCS 交流充电输入 |
| `pcs_discharge_power` | 全部 PCS 交流放电输出 |
| `bms_charge_power` | 全部电池簇直流充电接收功率 |
| `bms_discharge_power` | 全部电池簇直流放电输出功率 |
| `grid_import_power` | 关口表购电方向功率 |
| `grid_export_power` | 关口表上网方向功率 |
| `storage_aux_power` | 未被柜内电表或负载表覆盖的储能辅助用电 |
| `device_status` | 逆变器、PCS、BMS 等设备状态 |
| `source_times` | 各数据来源的原始采集时间 |

## 5. 三条链路的数据边界

```text
光→储：pv_dc_power
       → pv_ac_power
       → cabinet_charge_power
       → pcs_charge_power
       → bms_charge_power

储→用：bms_discharge_power
       → pcs_discharge_power
       → cabinet_discharge_power
       → load_power

光→用：pv_dc_power / pv_ac_power
       → load_power
```

`grid_import_power`、`grid_export_power` 和 `storage_aux_power` 用于共享交流侧的来源分配与功率平衡，不作为页面节点重复展示。

## 6. 数据完整性规则

本阶段不引入独立的字段级状态。NocoBase 适配层生成的标准逻辑记录必须明确提供全部功率字段；每个值必须是有限非负数，数值 `0` 表示设备确认当前功率为零。

任何功率字段缺失、为 `null`、无法转换为数值或超出合法范围时，整条原始记录视为无法映射，并返回 `source_mapping_error`，不得把缺失字段自动补为零。

`source_times` 继续用于判断不同来源是否处于同一计算窗口，`device_status` 继续用于屏蔽设备异常影响的指标。本阶段不增加额外的字段级状态规则。

严格的全字段要求适用于数据源适配层输出的标准逻辑记录；直接计算入口仍按各自现有输入规则校验。

## 7. 与 API 无关的数据源组件

### 7.1 `normalize_source_record(record)`

职责：

- 校验标准逻辑字段类型、单位、方向和时间；
- 要求全部功率字段存在且为有限非负数；
- 保留真实零值，拒绝把缺失值补成零；
- 不执行任何 HTTP 请求。

### 7.2 `build_dashboard_payload(records)`

职责：

- 接受 API 返回的无序记录，并按 `data_time` 升序排序；
- 拒绝重复时间；
- 使用最新标准记录作为 `current`；最新原始记录若无法通过完整性校验，应返回映射错误，不得回退到旧记录伪装成实时值；
- 构造截至 `current` 的最近 24 小时 `trend_samples`；
- 复用现有完整 24 小时、同一母线、同一时区和最大间隔校验；
- 输出可直接交给 `dashboard_request()` 的对象。

### 7.3 未来适配层的 `fetch_nocobase_records()`

真实 API 确定后，该函数负责：

- 使用适配层配置访问 NocoBase；
- 执行超时、HTTP 状态和 JSON 格式校验；
- 处理真实分页与排序方式；
- 使用适配层字段映射转换字段名、正负号和单位；
- 生成完整功率字段、`device_status` 与 `source_times`；
- 返回标准逻辑记录列表。

该函数不属于 `m2/station_energy_backend.py`，并且不得包含三条链路公式。API 字段变化只能影响适配层，不应修改 `dashboard_request()` 或计算函数。

## 8. Python 模块边界

生产文件只暴露可导入调用的计算与转换函数，不提供 stdin、stdout、JSON 文件、mock 或命令行参数。典型调用链为：

```python
from m2.station_energy_backend import build_dashboard_payload, dashboard_request

payload = build_dashboard_payload(records)
result = dashboard_request(payload)
```

测试样本、数据夹具和浏览器联调序列化代码均位于 `tests` 目录，不进入生产模块。

## 9. 错误契约

| 错误码 | 触发条件 |
|---|---|
| `source_mapping_error` | 原始记录无法转换成标准逻辑记录 |
| `insufficient_history` | 无法构造符合要求的最近 24 小时序列 |

计算与转换错误以 `BackendError` 抛出，并可通过 `to_dict()` 转为结构化对象。外部适配层自行定义网络错误，且错误详情不得包含令牌、完整授权头或含敏感查询参数的 URL。

## 10. 测试策略

新增后端测试覆盖：

- 标准记录中的零值被保留为真实零功率；
- 标准记录缺少任一功率字段时返回 `source_mapping_error`；
- `null`、负数、布尔值和非有限数不能通过标准记录校验；
- 记录排序、最新点选择和最近 24 小时构造；
- 历史不足、时间重复、跨母线和时间断档返回明确错误；
- 直接执行生产模块不会启动 CLI 或产生 I/O 副作用；
- 浏览器端到端夹具通过导入纯函数生成结果。

现有 Python 单元测试与浏览器端到端测试必须继续通过。

## 11. 本阶段范围外

以下内容明确推迟到真实 NocoBase API 确定后：

- 真实 URL、集合名、接口路径和鉴权令牌；
- NocoBase 分页、筛选和排序参数；
- 原始字段名、厂家正负号和单位换算；
- 网络请求实现及真实接口联调；
- 具体 HTTP 服务部署与 NocoBase iframe 配置。
