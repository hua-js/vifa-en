# 场站级三条能效链路 Python 后端设计

> 当前实现边界：`m2/station_energy_backend.py` 只包含纯 Python 计算与数据转换函数，不负责命令行、文件、标准输入输出、HTTP 或 NocoBase 访问。数据源边界与逻辑数据结构见 `2026-08-25-station-energy-source-access-design.md`。

日期：2026-08-24

## 1. 目标与范围

实现单个纯 Python 文件 `m2/station_energy_backend.py`，依据《场站级三条能效链路后端计算表》完成以下能力：

- 光→储、储→用、光→用三条场站级能效链路计算；
- 光伏逆变器、PCS、储能柜的分段效率计算；
- 光伏、储能、电网的能源来源分配；
- 功率平衡、数据质量和计算可信度判断；
- 单时刻实时计算和时间序列能量累计计算；
- 单母线分别计算及多母线按分子、分母汇总；
- 输出可供 HTTP/API 适配层直接使用的 Python 字典。

现已接入用户提供的 `场站三条能效链路能流图.html`，但本模块不实现 iframe 外壳、HTTP 服务、数据库存储或真实 NocoBase 表字段映射。

## 2. 部署架构

```text
NocoBase / 其他数据源
    ↓ 外部数据访问与字段映射层
标准逻辑记录（Python dict）
    ↓ normalize_source_record() / build_dashboard_payload()
m2/station_energy_backend.py 纯计算模块
    ↓ dashboard_request() 等纯函数返回 Python dict
未来 HTTP/API 适配层
    ↓ JSON response
HTML 看板
```

生产文件可被任意 Python Web 服务或数据适配器导入。它不启动进程服务、不访问文件或网络，也不读写 stdin/stdout。

## 3. 运行约束

- 仅使用 Python 3 标准库；
- 生产计算逻辑单文件交付；
- 不包含 CLI、mock、文件 I/O、标准输入输出、HTTP 请求或连接配置；
- 输入和输出均为 Python 对象，由外部适配层负责 JSON 编解码；
- 不内置 token、密码或 NocoBase 地址；
- 所有功率单位统一为 kW，能量单位统一为 kWh，效率输出为百分数。

## 4. Python 调用接口

调用方导入纯函数并传入 Python 字典：

```python
from m2.station_energy_backend import (
    build_dashboard_payload,
    dashboard_request,
    dispatch_request,
    normalize_source_record,
)

record = normalize_source_record(raw_record)
payload = build_dashboard_payload(records)
result = dashboard_request(payload)
```

`dispatch_request(payload)` 仍可按 `operation` 在 `calculate`、`aggregate` 和 `dashboard` 三种纯计算路径间路由。JSON 编解码与 HTTP 状态码由未来适配层处理。

测试代码独立存放于 `m2/tests/test_station_energy_backend.py`，不包含在生产脚本中。运行全部后端测试：

```bash
python3 -m unittest m2.tests.test_station_energy_backend -v
```

没有真实数据表和字段映射前，不实现虚构的 NocoBase 请求。

## 5. 请求契约

### 5.1 单母线实时计算

```json
{
  "operation": "calculate",
  "sample": {
    "bus_id": "bus-1",
    "data_time": "2026-08-24T12:00:00+08:00",
    "pv_dc_power": 100.0,
    "pv_ac_power": 96.0,
    "load_power": 94.0,
    "cabinet_charge_power": 0.0,
    "cabinet_discharge_power": 0.0,
    "pcs_charge_power": 0.0,
    "pcs_discharge_power": 0.0,
    "bms_charge_power": 0.0,
    "bms_discharge_power": 0.0,
    "grid_import_power": 0.0,
    "grid_export_power": 0.0,
    "storage_aux_power": 0.0,
    "device_status": {},
    "source_times": {}
  },
  "config": {}
}
```

所有方向功率字段均为非负数。负数、布尔值、非有限数和不能转换为数值的内容属于无效输入，不由计算脚本猜测厂家符号约定。

### 5.2 多母线实时计算

```json
{
  "operation": "calculate",
  "buses": [
    {"bus_id": "bus-1", "data_time": "2026-08-24T12:00:00+08:00", "pv_ac_power": 80},
    {"bus_id": "bus-2", "data_time": "2026-08-24T12:00:00+08:00", "pv_ac_power": 50}
  ],
  "config": {}
}
```

每条母线独立执行来源分配和质量判断。场站效率通过累加各母线对应链路的有效分子和分母后相除，禁止直接平均各母线效率。

### 5.3 时间序列累计

```json
{
  "operation": "aggregate",
  "samples": [
    {"data_time": "2026-08-24T00:00:00+08:00", "pv_dc_power": 100, "pv_ac_power": 96, "load_power": 94},
    {"data_time": "2026-08-24T00:00:30+08:00", "pv_dc_power": 110, "pv_ac_power": 105, "load_power": 100}
  ],
  "config": {}
}
```

相邻样本使用前一条样本功率乘以下一条与本条的时间差进行矩形积分。最后一条没有后继时间，不单独贡献能量。时间必须严格递增；重复、倒序或非正时间间隔返回错误。

### 5.4 HTML 看板数据

```json
{
  "operation": "dashboard",
  "current": {"bus_id": "bus-1", "data_time": "2026-08-24T12:00:00+08:00"},
  "trend_samples": [
    {"bus_id": "bus-1", "data_time": "2026-08-23T12:00:00+08:00"},
    {"bus_id": "bus-1", "data_time": "2026-08-24T12:00:00+08:00"}
  ],
  "config": {}
}
```

`trend_samples` 必须与 `current` 属于同一母线、使用同一时区偏移、时间严格递增、相邻间隔不超过 1 小时，并精确覆盖 24 小时；最后一个历史点还必须与 `current` 处于同一计算窗口。响应包含 `realtime`、`summary_24h`、从窗口起点计时的 25 点 `trend`，以及按质量码、设备和影响链路合并起止时间的 `events`，可由 HTML 直接渲染。看板中的 `trend.pvStorage` 使用 `pv_storage_dc_efficiency`，与“光伏总输入 → BMS电池”的可见链路边界一致并包含逆变环节；AC 侧 `pv_storage_efficiency` 仍保留在实时和累计结果中供分段分析。

### 标准数据源记录

未来 NocoBase 响应必须先转换为 `2026-08-25-station-energy-source-access-design.md` 定义的完整标准记录。`normalize_source_record(record)` 要求 `bus_id`、带时区的 `data_time` 和全部功率字段；缺失或非法功率返回 `source_mapping_error`，不会补零。`build_dashboard_payload(records)` 接受无序记录，选择最新时刻并构造精确的最近 24 小时 `current + trend_samples` 契约。

## 6. 配置

默认配置如下，调用方可在请求 `config` 中覆盖：

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `min_power_kw` | 1.0 | 参与效率计算的最小分母功率 |
| `balance_error_limit_percent` | 5.0 | 功率平衡误差上限 |
| `max_efficiency_percent` | 105.0 | 判为明显超限的效率上限 |
| `time_tolerance_seconds` | 30.0 | 同一计算窗口允许的最大时间差 |

配置值必须有限且非负；效率上限必须大于零。

## 7. 计算模型

### 7.1 功率平衡

```text
balance_delta = pv_ac + grid_import + cabinet_discharge
                - load - cabinet_charge - grid_export - storage_aux

balance_error_percent = abs(balance_delta)
    / max(pv_ac + grid_import + cabinet_discharge, min_power_kw) * 100
```

正的 `balance_delta` 视为未被下游计量捕获的交流侧损耗或误差。

### 7.2 能源来源分配

遵循“光伏优先供负载，光伏余量充储；储能其次供负载；电网最后补充”的统计规则：

```text
pv_surplus = max(pv_ac - load - storage_aux, 0)
pv_to_storage = min(pv_surplus, cabinet_charge)
grid_to_storage = max(cabinet_charge - pv_to_storage, 0)

pv_to_load = min(pv_ac, load)
remaining_load = max(load - pv_to_load, 0)
storage_to_load = min(cabinet_discharge, remaining_load)
grid_to_load = max(load - pv_to_load - storage_to_load, 0)
```

有两种及以上能源参与同一去向时，结果标记为 `estimated`；单一来源且计量完整时标记为 `measured`。

### 7.3 文档示例 B/C 的计量口径处理

原计算表的来源分配公式使用“负载实际接收功率”，但示例 B/C 的效率分母使用“送往负载方向的全部上游功率”，两者不能用同一个字段表达。实现中保留两套内部量：

- `*_to_load_power`：负载实际接收且归属于该能源的功率，用作链路分子；
- `*_to_load_input_power`：该能源在上游送往负载方向的功率，用于损耗归属和链路分母分摊。

当场站供给大于显式负载、充电和上网之和时，正的功率平衡差作为交流侧损耗，按能源优先级归入负载链路上游输入。由此：

- 示例 B：BMS 输出 100、柜表输出 92、负载接收 90，完整储→用效率为 `90 / 100 = 90%`；
- 示例 C：光伏 DC 100、AC 96、负载接收 94，光→用 AC 效率为 `94 / 96`，DC 效率为 `94 / 100`。

如果存在同时供负载和上网，BMS 放电功率按柜表层的“送负载上游输入 / 柜表总放电”分摊，避免把上网部分计入储→用。

### 7.4 链路与分段效率

- 光→储 AC：光伏归属 BMS 充电功率 / 光伏进入储能柜功率；
- 光→储 DC：光伏归属 BMS 充电功率 / 光伏分配至储能的 DC 功率；
- 储→用：负载中储能实际供电功率 / 归属于负载方向的 BMS 放电功率；
- 光→用 AC：负载中光伏实际供电功率 / 光伏送往负载方向的 AC 输入功率；
- 光→用 DC：负载中光伏实际供电功率 / 光伏送往负载方向的 DC 输入功率；
- 光伏逆变器：PV AC / PV DC；
- PCS 充电：BMS 充电 / PCS 充电；
- 储能柜充电：BMS 充电 / 柜表充电；
- PCS 放电：PCS 放电 / BMS 放电；
- 储能柜放电：柜表放电 / BMS 放电。

PV DC 分配以对应 PV AC 去向占 PV AC 总输出的比例换算。分母低于阈值时效率为 `null`，不能返回 0%。

## 8. 数据质量

`quality_codes` 为数组，可以同时包含多个质量码：

- `low_power`
- `zero_denominator`
- `invalid_efficiency`
- `direction_conflict`
- `mixed_battery_direction`
- `time_misaligned`
- `power_balance_error`
- `estimated`
- `device_abnormal`

如果功率平衡超限、时间错位、设备异常或方向冲突影响某条链路，该链路的效率返回 `null`，但保留原始输入、中间分配功率和诊断数据。无效效率同样不伪装成正常值。

设备状态采用保守判断：`fault`、`offline`、`alarm`、`communication_error` 或显式 `abnormal=true` 触发 `device_abnormal`。BMS、PCS 在同一时刻的有效充放电方向不一致触发 `direction_conflict`；BMS 充电和放电同时超过阈值触发 `mixed_battery_direction`。

## 9. 响应契约

成功响应：

```json
{
  "status": "ok",
  "data": {
    "operation": "calculate",
    "result": {},
    "calculated_at": "2026-08-24T12:00:00+08:00"
  }
}
```

失败响应：

```json
{
  "status": "error",
  "error": {
    "code": "invalid_input",
    "message": "字段 pv_ac_power 不能为负数",
    "details": {"field": "pv_ac_power"}
  }
}
```

业务错误以 `BackendError` 抛出，调用方可通过 `to_dict()` 转换成安全的结构化错误对象。

输出字段至少包括原计算表第 17 节建议字段，并额外包含：

- `pv_storage_dc_efficiency`
- `pv_load_dc_efficiency`
- `quality_codes`
- `quality_by_metric`
- `balance_delta_power`
- `intermediate`（用于人工复核的链路分子、分母）

## 10. 测试与验收

独立的 `m2/tests/test_station_energy_backend.py` 使用标准库 `unittest` 覆盖：

1. 文档示例 A：光→储整柜效率 92.23%；
2. 文档示例 B：储→用效率 90.00%；
3. 文档示例 C：光→用 AC 97.92%、DC 94.00%；
4. 文档示例 D：80/50/20 kW 来源分配；
5. 分母为零和低功率返回 `null`；
6. 负数、NaN、Infinity 和错误输入被拒绝；
7. 功率平衡超限使受影响效率失效；
8. 充放电方向冲突和设备异常；
9. 时间错位；
10. 多母线按分子、分母汇总而非平均效率；
11. 时间序列按能量累计而非瞬时效率平均；
12. 直接执行模块不会启动 CLI 或产生 I/O 副作用；
13. 看板历史窗口、母线和时区约束；
14. 连续质量事件合并、设备异常定向屏蔽及零阈值安全；
15. HTML 同源 token 透传、空链路显示、刷新去重和错误态。

完成前执行：

```bash
python3 -m py_compile m2/station_energy_backend.py
python3 -m unittest m2.tests.test_station_energy_backend -v
```

## 11. 后续对接

拿到真实 NocoBase 表名、字段名、数据单位和关联关系后，在独立的数据访问/API 适配层中完成鉴权、查询、分页和原始字段映射。适配层构造标准逻辑记录后调用本模块的纯函数，不把网络访问或连接配置加入 `m2/station_energy_backend.py`。

当前 HTML 固定请求同源 `GET /energy-efficiency-api`，并仅把 iframe URL 中的 `token` 原样附加到该同源请求。未来 HTTP 层负责调用本模块并返回 `application/json`；不要允许前端查询参数覆盖 API 域名。
