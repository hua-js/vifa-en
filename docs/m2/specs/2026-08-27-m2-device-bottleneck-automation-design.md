# M2 逐设备瓶颈自动识别设计

日期：2026-08-27

## 1. 目标

在现有三条能效链路分钟计算基础上，接通逐设备瓶颈自动识别流程：

- 每台光伏逆变器独立判断低负载；
- 每个电池柜独立判断最高电芯温升；
- 三条链路分别判断效率是否持续低于 85%；
- 三类规则彼此独立，任意一类满足即可产生瓶颈事件；
- Node-RED 每分钟启动一次 Python 进程时，连续分钟状态不会丢失；
- 看板读取 NocoBase 中的真实事件，不再固定返回空事件数组。

本阶段不增加 PCS 温度规则，也不把单分钟无运行数据视为异常。

## 2. 已有数据与新增数据

### 2.1 `t_emu`

`t_emu` 继续提供场站三条链路计算所需的 PCS、BMS、电网、负载和光伏汇总数据。

电池柜最高电芯温度后续也从 `t_emu` 获取。温度字段名称尚未配置时，程序必须正常计算效率和逆变器规则，只跳过电池温升判断。

电池柜逐设备标识使用对应的 `emu_sn`。`battery_power` 原始单位为 W，保存设备分钟数据前转换为 kW；负数表示充电，正数表示放电。

### 2.2 `t_growall`

ES02 的 9 台光伏逆变器从 `t_growall:list` 获取：

- 设备标识：`sn`；
- 实际输出功率：`a35`，按 kW 使用；
- 每台额定功率：60 kW；
- 逆变器清单：`emu1`、`emu2`、`emu3`、`emu4`、`emu5`、`emu21`、`emu22`、`emu23`、`emu24`。

ES01 没有光伏，不产生逆变器低负载事件。

## 3. 分钟时间与采样选择

所有业务时间使用 `Asia/Shanghai`，持久化时间使用带时区时间。

每次场站分钟计算产生一个秒和微秒归零的 `data_time`。逐设备记录必须同时保存：

- `data_time`：本次归属的分钟桶；
- `source_time`：原始接口的真实采样时间。

每台设备的采样选择规则：

1. 同一设备同一分钟有多条记录时，选择可用的最新记录；
2. 不使用晚于本次计算时点的未来记录；
3. 采样时间与本次计算时间相差不超过 2 分钟；
4. 超过 2 分钟、时间无效或功率无效时，该设备本分钟没有有效记录；
5. 缺少记录既不计入连续触发，也不计入连续恢复。

重复执行同一场站同一分钟时使用 Upsert，不新增重复行。

## 4. NocoBase 逐设备分钟表

新增集合及物理表：`t_efficiency_device_points`。

| 字段 | PostgreSQL 建议类型 | 可空 | 说明 |
|---|---|---:|---|
| `id` | bigint identity | 否 | 主键 |
| `station_id` | text | 否 | `ES01` 或 `ES02` |
| `device_type` | text | 否 | `pv_inverter` 或 `battery_cabinet` |
| `device_id` | text | 否 | 逆变器 `sn` 或电池柜 `emu_sn` |
| `device_name` | text | 否 | 页面展示名称 |
| `subdevice_id` | text | 是 | 提供最高温的电池簇编号；接口未提供时为空 |
| `data_time` | timestamptz | 否 | 分钟桶时间 |
| `source_time` | timestamptz | 否 | 原始采样时间 |
| `active_power_kw` | numeric | 是 | 逆变器 `a35` |
| `rated_power_kw` | numeric | 是 | 逆变器固定为 60 |
| `load_rate_pct` | numeric | 是 | 逆变器负载率 |
| `battery_power_kw` | numeric | 是 | 电池柜充放电功率 |
| `temperature_c` | numeric | 是 | 电池柜最高电芯温度 |
| `created_at` | timestamptz | 否 | 创建时间 |
| `updated_at` | timestamptz | 否 | 更新时间 |

唯一约束：

```text
(station_id, device_type, device_id, data_time)
```

该唯一键同时支持按场站、设备类型、设备和时间范围读取最近样本。写入通过同一组字段执行原子 Upsert。

设备分钟数据只保留最近 30 天。三条链路分钟数据和瓶颈事件不随该清理任务删除。

## 5. Python 配置

敏感配置只保存在仓库根目录的 `.local/energy_efficiency_local_config.py`（服务器按部署说明安装），不得写入 Git、日志或 JSON 响应。

新增或补充以下配置：

```text
GROWALL_URL
GROWALL_TOKEN
PV_INVERTER_SNS_BY_STATION
PV_INVERTER_RATED_POWER_KW = 60
DEVICE_SAMPLE_MAX_AGE_MINUTES = 2
DEVICE_POINT_RETENTION_DAYS = 30
BATTERY_MAX_TEMPERATURE_FIELD = ""
BATTERY_HOT_CLUSTER_FIELD = ""
EVENT_OUTBOX_PATH
BOTTLENECK_RULES
```

`BATTERY_MAX_TEMPERATURE_FIELD` 为空表示温度数据尚未接入，不属于服务器配置错误。`BATTERY_HOT_CLUSTER_FIELD` 可空；为空时仍可按电池柜最高温判断，只是事件中不能显示具体电池簇编号。

规则初始值：

```text
enabled = true
chain_low_efficiency_threshold_pct = 85
chain_low_efficiency_trigger_minutes = 2
chain_low_efficiency_recovery_minutes = 2
inverter_min_running_power_kw = 5
inverter_low_load_threshold_pct = 20
inverter_trigger_minutes = 3
inverter_recovery_minutes = 2
temperature_rise_window_minutes = 5
temperature_rise_threshold_c = 3
temperature_trigger_minutes = 2
temperature_recovery_minutes = 2
```

规则保留版本号。新事件使用当前规则；已打开事件继续使用打开时写入 `evidence` 的规则快照，直到恢复。

## 6. 三类独立瓶颈规则

### 6.1 链路低效率

三条链路分别判断：

- `pv_storage`：光→储；
- `storage_load`：储→用；
- `pv_load`：光→用。

只有非空的有效效率参与判断。效率连续 2 个有效分钟低于 85% 时打开事件；连续 2 个有效分钟达到或高于 85% 时恢复。无运行链路或效率为空时，不触发且不恢复。

事件属性：

- `event_type = chain_low_efficiency`；
- `device_id` 使用链路编号；
- `device_name` 使用链路中文名称；
- `observed_value` 保存事件期间最低效率；
- `threshold_value = 85`；
- `observed_unit = %`；
- `impact_chain` 只包含当前链路。

事件在第二个连续低效率分钟确认打开，但 `start_time` 记录第一个满足条件的分钟。

### 6.2 逆变器低负载

每台逆变器独立计算：

```text
load_rate_pct = active_power_kw / 60 × 100
```

一个分钟同时满足以下条件时，记为低负载分钟：

```text
active_power_kw >= 5
且
load_rate_pct < 20
```

连续 3 个有效低负载分钟后打开事件。连续 2 个有效分钟不满足低负载条件后恢复。功率低于 5 kW 视为退出运行判断区间，可计入恢复，但不能触发夜间停机低负载事件。缺少记录不计入恢复。

事件影响“光→储”和“光→用”。

### 6.3 电池温升

每个电池柜独立判断。一个柜有多个电池簇时，使用各簇最高电芯温度中的最大值，并在可用时保存对应 `subdevice_id`。

```text
temperature_rise_c = 当前分钟最高电芯温度 - 5 分钟前最高电芯温度
```

温升达到 3℃并连续满足 2 个有效分钟后打开事件；窗口温升连续 2 个有效分钟低于 3℃后恢复。窗口起点或中间分钟缺失时，不形成有效判断。

事件影响“光→储”和“储→用”。本阶段不判断绝对高温，也不使用 PCS 温度。

### 6.4 规则关系

三类规则是独立的“或者”关系。任意一类满足即可产生自己的事件，不要求链路低效率、逆变器低负载和电池温升同时发生。

因此：

- 储→用效率持续为 80% 时，即使没有温度异常，也会产生链路低效率事件；
- 电池温升达到条件时，即使链路效率仍高于 85%，也会产生电池温升事件；
- 逆变器低负载达到条件时，即使链路效率仍高于 85%，也会产生逆变器低负载事件。

## 7. 链路低效率的设备现场快照

链路低效率事件确认打开时，在 `evidence.trigger_device_snapshot` 中保存一次相关设备现场，后续不得覆盖。

保存范围：

| 链路 | 触发快照内容 |
|---|---|
| 光→用 | 9 台逆变器的实际功率、额定功率、负载率和采样时间 |
| 光→储 | 9 台逆变器负载数据，以及各电池柜最高电芯温度和采样时间 |
| 储→用 | 各电池柜最高电芯温度和采样时间 |

事件在第二个低效率分钟确认，因此快照使用确认分钟的设备数据。`start_time` 仍指向第一个低效率分钟。

`evidence` 同时保存：

- `diagnosed_causes`：确认时已经满足的相关逆变器低负载或电池温升原因；
- `cause_status`：找到原因时为 `diagnosed`，否则为 `pending`；
- `trigger_device_snapshot`：确认分钟的设备现场；
- `rule`：打开事件时的规则快照。

没有找到设备原因时，页面显示“链路低效率，原因待判断”，但仍展示触发快照。温度字段尚未接入时显示“温度数据暂未提供”，不得把缺失温度展示为 0℃。

事件持续期间可更新最低效率、连续分钟数和 `last_seen_time`，但不能覆盖 `trigger_device_snapshot`。完整逐分钟变化从保留 30 天的设备分钟表查询。

## 8. 事件生命周期与幂等性

`t_efficiency_bottleneck_events` 继续保存活动和已恢复事件，并新增允许的 `event_type`：`chain_low_efficiency`。

同一逻辑事件的幂等键为：

```text
(station_id, event_type, device_id, start_time)
```

同一场站、事件类型和设备最多存在一个 `active` 事件。链路低效率事件的 `device_id` 是链路编号，因此三条链路可以各自存在一条活动事件。

事件持续期间更新 `last_seen_time` 和最不利观测值。达到恢复条件时写入 `end_time` 并把 `status` 改为 `recovered`。恢复事件永久保留。

NocoBase 事件 Upsert 失败时，按同一事件幂等键把最新完整 payload 写入本机 SQLite outbox；同键的 recovered payload 覆盖旧 active payload。每次同站 minute 在评估当前事件前先补写该站 outbox，成功后删除，仍失败则保留。SQLite 使用短事务和 busy timeout，使 ES01、ES02 并发进程共享同一文件时不互相破坏。

## 9. 每分钟自动流程

Node-RED 每分钟分别执行：

```bash
python3 m2/energy-efficiency-api.py minute ES01
python3 m2/energy-efficiency-api.py minute ES02
```

单场站处理顺序：

1. 获取 `t_emu`；
2. 计算并 Upsert `t_efficiency_points`；
3. 从 `t_emu` 构造每个电池柜分钟数据；
4. ES02 从 `t_growall` 构造 9 台逆变器分钟数据；
5. Upsert 有效的 `t_efficiency_device_points`；
6. 按设备和链路读取规则所需的最近有效分钟；
7. 查询现有活动事件；
8. 补写该站本机事件 outbox；
9. 独立执行三类规则；
10. 打开、更新或恢复 `t_efficiency_bottleneck_events`，失败时写入 outbox；
11. 输出一行 JSON 供 Node-RED 处理。

JSON 结果至少区分 `ok`、`partial` 和 `error`，并返回分钟点、设备点数量、事件计算更新数量、`event_persistence.attempted/saved/failed/outbox_pending` 和不含密钥的警告信息。计算更新数量不能称为成功保存数量。

## 10. 看板读取

`dashboard` 操作继续获取实时计算和当天分钟曲线，并增加真实事件查询：

- 查询所有仍为 `active` 的本场站事件，即使事件从前一天开始；
- 查询当天恢复的本场站事件；
- 按事件开始时间排序；
- 将事件传给现有 HTML，不再调用 `build_dashboard(..., points, [])`。

前端增加 `chain_low_efficiency` 的中文名称、状态样式和证据展示。现有三条链路实时效率卡片与今日曲线口径不改变。

## 11. 自动清理

Node-RED 每天执行一次：

```bash
python3 m2/energy-efficiency-api.py cleanup
```

清理命令只删除 `data_time` 早于当前北京时间减 30 天的 `t_efficiency_device_points`。清理失败不得阻断分钟任务，也不得删除 `t_efficiency_points` 或 `t_efficiency_bottleneck_events`。

## 12. 错误处理

- `t_emu` 失败：本场站本分钟返回 `error`，不保存该分钟；
- `t_growall` 失败：三条链路效率照常保存，逆变器设备点和该分钟逆变器判断跳过，返回 `partial`；
- 温度字段未配置或为空：效率和其他规则照常运行，温升判断跳过；
- 单台设备记录过期或无效：只跳过该设备；
- 设备点写入部分失败：保留已经成功的幂等写入，返回 `partial`；
- 事件写入失败：最新 payload 留在本机 SQLite outbox，返回 `partial`，下一次同站 minute 先补写；
- 重复执行：所有分钟点和事件通过幂等键更新，不产生重复记录；
- 标准输出：最后一行必须始终是合法 JSON，日志不得包含 Token、密码或完整授权头。

## 13. 代码边界

- `m2/energy-efficiency-api.py`：CLI 参数、配置加载、统一 JSON 输出；
- `m2/station_efficiency_job.py`：分钟任务和清理任务编排；
- `m2/station_energy_data_adapter.py`：现有场站汇总数据适配及 `t_emu` 电池柜字段读取；
- 新的逐设备适配模块：将 `t_growall` 和 `t_emu` 记录转换为统一设备分钟结构；
- `m2/station_efficiency_history.py`：三类纯计算规则和事件转换；
- `m2/station_efficiency_nocobase.py`：设备分钟点、活动事件、当天事件和清理请求；
- `m2/web/场站三条能效链路能流图.html`：第三类事件和触发快照展示。

数据转换和规则函数保持纯 Python，网络访问集中在 NocoBase 适配层，CLI 只负责组合流程。

## 14. 验证标准

自动测试必须覆盖：

1. 9 台逆变器按 `sn` 分开保存和计算；
2. `a35 / 60 × 100` 的负载率计算；
3. 2 分钟以内采样可用，超过 2 分钟或未来采样不可用；
4. 同一分钟重复执行只保留一条设备记录；
5. 逆变器连续 3 分钟低负载触发，连续 2 分钟恢复；
6. 电池温度未配置时不报错；
7. 电池温升窗口、连续触发、连续恢复和断档行为；
8. 三条链路连续 2 分钟低于 85% 分别触发，连续 2 分钟恢复；
9. 三类规则任意一条可独立打开事件；
10. 低效率事件快照只保存相关设备，且持续更新不会覆盖；
11. 未找到原因时输出“原因待判断”及可用现场数据；
12. 看板包含跨日持续事件和当天恢复事件；
13. 30 天清理不影响效率点和事件表；
14. `t_emu`、`t_growall` 和写入部分失败时返回约定状态；
15. CLI 的最后一行始终是合法 JSON，输出不泄漏密钥。

真实联调顺序：先以只读方式核对 `t_emu` 与 `t_growall` 的采样时间和单位，再对新建测试记录执行 Upsert，最后接通 Node-RED 定时任务。

## 15. 实施前置条件

在真实写入联调前，用户需要：

1. 在 NocoBase 创建 `t_efficiency_device_points` 的上述字段；
2. 建立 `(station_id, device_type, device_id, data_time)` 组合唯一约束；
3. 如果瓶颈事件表的 `event_type` 是下拉选项或数据库枚举，把 `chain_low_efficiency` 加入允许值。

第一次执行任何真实 `minute` 前，必须现场核对第 2、3 项，并确认 Node-RED 运行用户可写 outbox 目录且该文件已进入备份方案。只读 dashboard 即使事件为空且返回成功，也不能证明事件允许值或设备点唯一键可写。

电池最高温字段可以稍后加入 `t_emu`；这不会阻塞逆变器低负载、链路低效率和看板事件读取的实施。
