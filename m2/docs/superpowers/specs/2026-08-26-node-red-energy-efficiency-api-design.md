# Node-RED 能效分析 Python 入口设计

更新日期：2026-08-26

## 1. 目标

新增服务器入口文件 `energy-efficiency-api.py`，由 Node-RED 的 `exec` 节点按请求启动一次。入口文件读取一个场站的实时数据，调用现有 M2 计算模块，并向标准输出返回 HTML 看板能够直接使用的单行 JSON。

本入口不监听 HTTP 端口。HTTP 地址 `/energy-efficiency-api`、参数检查、HTTP 状态码和最终响应均由 Node-RED 负责。

## 2. 文件位置

仓库中的入口文件放在项目根目录：

```text
energy-efficiency-api.py
```

服务器部署位置：

```text
/userdata/holo/pyfiles/energy-efficiency-api.py
```

现有 M2 模块部署在：

```text
/userdata/holo/pyfiles/m2/
```

入口文件位于 `m2` 的上一级，因此可以正常导入 `m2.station_energy_data_adapter`、`m2.station_energy_backend` 和 `m2.station_efficiency_history`。

## 3. Node-RED 调用格式

第一版只支持 `dashboard` 操作：

```bash
python3 /userdata/holo/pyfiles/energy-efficiency-api.py dashboard ES01
python3 /userdata/holo/pyfiles/energy-efficiency-api.py dashboard ES02
```

规则：

- 参数数量必须准确；
- 操作只允许 `dashboard`；
- 场站只允许 `ES01` 或 `ES02`；
- 不接受 `--token`、数据源地址或任意附加命令参数；
- Node-RED 也必须先对白名单场站进行检查，Python 再检查一次作为第二层保护。

## 4. 服务器配置

Python 只从服务器环境变量读取数据源配置：

| 环境变量 | 必填 | 说明 |
|---|---:|---|
| `VIFA_EMU_URL` | 是 | 完整的 `t_emu:list` 地址 |
| `VIFA_EMU_TOKEN` | 是 | 请求 `t_emu:list` 的 Bearer token |
| `M2_TIMEZONE` | 否 | 看板时区，默认 `Asia/Shanghai` |
| `M2_REQUEST_TIMEOUT_SECONDS` | 否 | 请求超时秒数，默认 `15` |

令牌不允许出现在命令参数、HTML、iframe 地址、标准输出、标准错误或 Git 文件中。

## 5. 单次执行流程

```text
检查命令参数
  → 读取服务器环境变量
  → 调用 fetch_station_source_record()
  → 从 t_emu:list 读取并转换指定场站
  → 调用 calculate_bus()
  → 组成 realtime.inputs 和 realtime.result
  → 调用 build_calendar_day_dashboard()
  → 第一版传入空的 points 和 events
  → 输出一行 JSON
```

数据转换继续使用已经确认的规则：

- `latest_grid_power` 按 W 除以 1000；
- `battery_power` 按 W 除以 1000；
- 储能净功率为全部储能柜 `latest_power` 之和；
- 负载为“市电 + 储能净功率 + 交流光伏”；
- ES02 光伏从 `emu27` 读取；
- 推算负载小于或等于 0 时，本次执行失败且不保存数据；
- 设备时间允许相差 120 秒。

## 6. 标准输出

成功时只输出一行 JSON：

```json
{"status":"ok","data":{"operation":"dashboard"}}
```

实际 `data` 使用 `build_calendar_day_dashboard()` 的完整结果，至少包含：

- `operation`；
- `range`；
- `realtime`；
- `summary_today`；
- `trend`；
- `events`。

第一版尚未查询 NocoBase 历史表，因此：

```json
{"trend":[],"events":[]}
```

实时三条链路必须使用真实 `t_emu` 数据，不能回退到模拟数据。

## 7. 错误输出

所有可预期错误也必须向标准输出返回一行 JSON：

```json
{"status":"error","error":{"code":"invalid_arguments","message":"参数不正确"}}
```

错误码范围：

| 错误码 | 含义 |
|---|---|
| `invalid_arguments` | 操作或场站参数错误 |
| `missing_config` | 服务器缺少环境变量 |
| `source_error` | `t_emu` 请求或字段转换失败 |
| `calculation_error` | 三条链路无法计算 |
| `internal_error` | 未预期的内部错误 |

要求：

- 错误消息使用简短中文；
- 不输出 Python traceback 给 Node-RED；
- 不输出令牌、密码、完整 Authorization 请求头或包含令牌的 URL；
- 成功退出码为 `0`，错误退出码为非 `0`；
- 标准错误仅允许记录不含秘密的运行信息，第一版默认不输出日志。

## 8. Node-RED 对接

Node-RED 流程使用已经生成的：

```text
m2/node-red-energy-efficiency-api-flow.json
```

流程职责：

1. 接收 `GET /energy-efficiency-api?station_id=ES01|ES02`；
2. 默认场站为 ES02；
3. 检查场站白名单；
4. 使用固定 Exec 命令并仅追加白名单场站；
5. 解析 Python 返回的完整标准输出；
6. Python 成功时返回 HTTP 200；
7. 参数错误返回 HTTP 400；
8. Python 或上游错误返回 HTTP 502；
9. 无法解析 Python JSON 时返回 HTTP 500。

## 9. 第一版明确不做

- 不由 Python 提供 HTTP 服务；
- 不从网页或 Node-RED 命令参数接收 token；
- 不在页面刷新时写入分钟效率表；
- 不查询当日分钟曲线；
- 不查询或计算瓶颈事件；
- 不实现每分钟自动任务；
- 不支持 ES01、ES02 之外的场站；
- 不加入第三方 Python 依赖。

分钟保存、当日曲线和瓶颈事件在实时页面跑通后，作为后续独立步骤接入。

## 10. 验证标准

- 缺少参数、错误操作和未知场站均返回安全的单行错误 JSON；
- 缺少环境变量时不发起网络请求；
- ES01、ES02 均能调用真实数据适配器和计算函数；
- 成功结果可以被现有 HTML 的 `renderDashboard()` 直接处理；
- 标准输出只有一行合法 JSON；
- 返回内容和错误中不包含配置令牌；
- 真实只读试算不写入任何 NocoBase 表；
- 现有 Python 测试和 HTML 浏览器测试继续通过。
