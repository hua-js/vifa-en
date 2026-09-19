# NocoBase AI 员工 M1–M4 数据映射草案

核对日期：2026-09-19。本映射先按本地源码整理，后续已读取 main 数据源业务表，并使用独立测试身份进行查询验收。以下宽于本次接入范围的条目仍为候选；已接入对象与证据见第 7 节及任务书第 1.2 节。

关联任务书：[实施任务书](NocoBase-AI员工实施任务书.md)。所有候选仅建议 Read；本文件不授予实际访问权限。

## 1. 身份及权限映射

当前项目配置使用内部电站 ID 与上游 `source_code`；核对依据为 [项目配置](../config/projects/vifa.json)。不得假定所有表里的 `station_id` 就是 `ES01/ES02`，现场须核对实际值与映射。

| 数据链路 | 本地可见关联 | 现场必须确认 |
|---|---|---|
| M1 基础拓扑 | `en_realtime.fk_en → en.id`，`en.fk_es → es.id`，`es.fk_site` | 目标场站身份、关联过滤支持、明细行及字段权限 |
| 业务运行表 | `t_es_data.es_sn`；`t_emu.f_es_sn + emu_sn` | 上游数据源是否已纳入当前 NocoBase；关联或文本字段的真实类型及授权条件 |
| M2 | 结果与事件均有 `station_id` | 该值与场站/电站授权关系；不能仅依赖查询参数 |
| M3 负荷/SOC | runs 的 `station_id`；points 的 `run_pk` 关联 runs 的 `id` | points 是否可独立访问，是否按父批次所属站限制 |
| M3 光伏 | runs 的 `es_sn`；points 的 `run_pk` | 光伏批次与明细的关联 ACL，ES02 数据归属 |
| M4 | API 路径 `station_id`；`t_model.es_sn` | API 调用者权限链；数组/关系归属的实际过滤语义 |

同一业务代码在不同客户或数据源可能重复，因此仅 `ES01/ES02` 文本匹配不是客户隔离证明。字段不够表达授权时列缺口，不自动改表。

## 2. M1：实际运行观测

依据：[dashboard_energy_api.py](../m1/dashboard_energy_api.py) 的 `fetch_raw_data` 拓扑读取、`fetch_station_load_sources`、`fetch_station_storage_sources`、`fetch_growatt_rows` 及 `build_pv_inverters` 输出映射。

| 用途 | 数据对象与关键字段 | 时间 / 单位 | 粒度、使用限制 | AI 接入状态 |
|---|---|---|---|---|
| 柜级 SOC、运行状态 | `en_realtime`: `fk_en,soc,run_status`；关联 `en` | `timestamp`；SOC % | M1 直接透传 SOC；不能自行平均成正式站级 SOC；静态状态回退不证明实时健康 | 本地读取已核对；Collection 可见性及 ACL 待现场确认 |
| 电站负荷 | `t_es_data`: `es_sn,load_power` | `timestamp`；kW | 完整电站 SN 精确匹配，读取最新记录；不用 `en_realtime.power` 替代 | 同上 |
| 储能柜功率 | `t_emu`: `f_es_sn,emu_sn,latest_power` | `last_time_iso`；kW | 按项目柜体清单去重；保留每柜时间；充放电符号含义须对照现场，不能猜测 | 同上 |
| M1 光伏汇总 | `t_growall`: `sn,a35,a1,a0` | `timestamp`；代码按 kW 展示 | `a35` 交流输出，`a1` 直流输入，`a0` 状态；每台纳管逆变器取最新，缺设备不能声称完整 | 外部来源；是否已在 AI 数据源注册待核实 |

M1 基础拓扑、负荷/储能、Growatt 使用不同来源配置；本轮不记录域名或凭据，不认定单一 Data query 能跨源关联。M1 没有提供已核实的站级 SOC 聚合接口；首个验收问题建议采用柜级 SOC，避免人为增加新口径。

其他模块可能读取 `t_emu.latest_soc` 或 `t_es_data.emus_soc`；它们与 M1 柜级 SOC、M4 参与柜 SOC 不是自动等价的替代来源。采用前须核对当前模块的正式读取及聚合范围。

## 3. M2：正式效率与异常

依据：[station_efficiency_history.py](../m2/station_efficiency_history.py) 的 `CHAIN_COLUMNS`、`build_minute_point`、`build_minute_upsert`、`build_event_upsert`；[持久化适配](../m2/station_efficiency_nocobase.py)。

| 对象 | 关键字段 | 时间 / 身份 | 查询与解释要求 |
|---|---|---|---|
| `t_efficiency_points` | `pv_storage_efficiency,pv_storage_input_kw,pv_storage_output_kw`；`storage_load_*`；`pv_load_*`；`formula_version` | `station_id,data_time,calculated_at` | 三链路依次为光伏→储能、储能→负荷、光伏→负荷；功率 kW。效率数值展示比例须结合正式页面/接口确认，不由字段名决定乘 100 |
| `t_efficiency_bottleneck_events` | `event_type,device_id,status,observed_value` 及事件证据字段 | `station_id,start_time,last_seen_time,end_time` | 异常类型含逆变器低负载、电池温升、链路低效率；单位随类型，不能统一解释为效率百分比 |
| `t_efficiency_device_points` | 设备分钟观测，具体字段按持久化构造核对 | 设备/站点与采样时间需现场补齐 | 仅在解释事件需要时追加最小字段；本轮不作为默认 AI 数据集 |

结果表与瓶颈事件表已验证独立测试身份可读；设备分钟表未接入。分钟效率不能简单平均为“今天效率”；若没有已确认的正式日汇总，回答最新分钟结果及其时间，或说明日汇总口径尚未接入。事件为空不证明整天没有异常，须确认事件数据覆盖及持久化状态。

## 4. M3：负荷/SOC 与光伏分开查询

| 对象 | 关键字段与关联 | 时间 / 单位 / 版本 | 读取规则 |
|---|---|---|---|
| `energy_forecast_manual_runs` | `id,run_id,station_id,status,interval_seconds,forecast_days,model_manifest,source_manifest` | `forecast_start,forecast_end,started_at,completed_at` | 优先匹配当前页面采用的有效任务、站点及窗口；不只取最后创建记录 |
| `energy_forecast_manual_points` | `run_pk,unique_id,forecast_value,raw_forecast,baseline_forecast_value,actual_value` | `target_time,horizon_step`；`station_total_load` 为 kW，`storage_soc` 为 % | `run_pk → runs.id`；同一任务内按序列和目标时间查询，实测与预测分别展示 |
| `energy_forecast_manual_evaluations` | `run_pk,run_id,station_id,evaluation_key,current_score` | `window_start,window_end,calculated_at` | 读取现有正式评分，不自行定义或从其他任务借用评分 |
| `energy_forecast_latest`、`energy_forecast_batches`、`energy_forecast_points` | 另一路 latest/验收批次发布结构 | `issued_at`、预测起止；点包含 `unique_id,data_time,target_time,forecast_value` | sink 中仍有实现；不得与 manual 任务混用。是否为现行业务入口，现场待确认 |
| `energy_pv_forecast_runs` | `id,run_id,es_sn,run_kind,status,model_name,model_version` | `as_of,generated_at,forecast_start,forecast_end,interval_minutes` | 光伏独立批次；采用正式 operational、completed 结果，排除未来发布及过期结果误用 |
| `energy_pv_forecast_points` | `run_pk,target_time,horizon_step,forecast_kw,raw_forecast_kw,baseline_kw,lower_kw,upper_kw` | kW；同一 `run_pk` | 读取正式 `forecast_kw`，不将原始值或区间边界当作正式点预测 |

源码依据：

- [manual 仓库与字段](../m3/worker/services/custom_forecast_repository.py)、[序列契约](../m3/worker/contracts.py)、[查询路由](../m3/worker/api/routes.py)。
- [latest/验收发布](../m3/worker/sinks/forecast_sink.py)：部分发布有完整性门槛，不能只看批次存在就认为点数据齐全。
- [光伏表契约](../m3/contracts/pv_forecast_schema.py)、[光伏发布脚本](../m3/scripts/publish-pv-forecast.py)、[光伏读取服务](../m3/worker/services/pv_query.py)。这些仅为阅读依据，本轮不执行建表或发布脚本。
- [光伏读取路由](../m3/worker/pv_query_app.py) 定义 `GET /api/pv/ES02/latest`，含有效性和完整性处理；可作为原生查询不足时的候选，部署和授权待核实。

Data query 可以成为上述表的候选读取方式，但点表的父批次关联权限与批次完整性必须一起满足。M3 当前任务、历史验收和光伏独立批次不可合并称为单一“最新预测表”。

## 5. M4：完整计划主要在服务与文件中

| 对象 / 入口 | 存储、字段及版本 | 含义和接入限制 |
|---|---|---|
| `GET /m4-api/stations/{station_id}/daily-plan` | 日计划 JSON；`station_id,run_id,status,request,selected_candidate`；响应附 `rolling,rolling_history,ems_table_write` | 日计划、滚动与写表状态的候选读取入口；不是 NocoBase Collection。接口存在不证明已部署或继承 NocoBase ACL |
| `GET /m4-api/stations/{station_id}/decision-history` 与 `/decision-results/{run_id}` | 决策文件及指定版本快照 | 历史与决策依据候选入口；不把文件生成成功当作设备执行 |
| `t_model` | `id,es_sn,start_time,end_time,type,kw,repeat,updatedAt,m4_run_id,m4_plan_date` | EMS 计划表，`type` 含 charge/discharge；`kw` 为计划功率字段，具体站/柜口径按现场现行契约确认，不沿用旧文档换算；不等于求解原始计划或设备实测 |
| M4 settings SQLite | `m4_station_settings` 的设置文档；路径可配置 | 参数存储，不是计划或执行表；本轮不接入 AI 原始 SQLite |
| 设备实际执行 | 本轮未确认完整可用的读取对象及关联链 | 不能把 `t_model` 回读、业务执行标签或实时某一功率点当作完整执行证明 |

源码依据：[API](../m4/settings/api.py)、[日计划存储](../m4/settings/daily_plans.py)、[滚动计划](../m4/settings/rolling_plans.py)、[控制源字段](../m4/settings/control_sources.py)、[设置存储](../m4/settings/store.py)。

日计划目录由 API 中 `decision_results.root.parent / 'daily-comparisons'` 构造，日计划以站点 JSON 和按 `run_id` 归档 JSON 保存；以部署配置为准，不硬编码生产文件路径。既有 `outputs/m4/solver-decisions/` 是历史查询证据，不能清理或批量复制作为 AI 知识库。

项目允许计划表回读确认后显示业务“执行中/已执行”，但设备实测仍须独立证据。查询原因与偏差应关联 `run_id`、计划日期、站点和相同时段，不能拼接不同滚动版本冒充实际执行。

原任务书“0 行业务代码修改”仍是优先方向；M4 若不能通过已注册数据源直接读取，只先记录缺口，不因此新建表、同步任务或工具。

## 6. 现场最小补充清单

无需提供模型密钥、数据库密码或完整配置。后续现场提供以下脱敏结果即可逐项推进：

1. 拟使用的业务角色及已测试的业务查询范围。通用 Skills/Tools 已经 API 核对，员工已保留 admin 并新增独立验收角色；正式业务用户及真实页面权限仍需单独核实。
2. 本草案各表在哪个 NocoBase 数据源中可见，尤其是 M1 多来源及 M3 光伏表。
3. 目标场站、内部 station ID 与 ES01/ES02 的正式映射；各角色实际允许范围。
4. 先确认一组 ES02 柜级 SOC 字段及数据时间，并与正式页面对照；如果采用其他正式来源，明确记录替换依据。
5. 确认 M3 points→runs 关联 ACL；确认 M4 完整结果是否已有注册的只读入口。

## 7. 本次 main 数据源实际接入范围

- M1：t_es、t_es_data、t_emu 已授予限定字段。柜级 SOC 实际查询使用 t_emu.latest_soc / last_time_iso，ES02 仅 emu21–26；emu27 为光伏计量设备。没有证明该值等同所有 M1 页面拓扑源，en_realtime、Growatt 外部来源未接入。
- M2：t_efficiency_points 和 t_efficiency_bottleneck_events 的 station_id 现场值为 ES01/ES02；结果与事件均可读，正式日汇总及全天异常覆盖未验收。
- M3：manual runs/points 和 pv runs/points 均可读；points 权限按 run.station_id 或 run.es_sn 过滤。负荷/SOC 的真实越权 ID 负向测试通过；光伏仅完成正向与关联配置核对，没有另一站光伏样本的负向验收。evaluations、旧 latest/batches 链路未授权接入。
- M4：仅 t_model 计划表可读；es_sn 为数组，权限采用精确集合 ES01、ES02 或两者。完整日计划、滚动历史、决策原因及设备执行闭环仍未接入。
- 身份：账号 vifa_ai_test_20260919 仅有独立角色 vifa_ai_acceptance_20260919，main 的 10 张表仅 view；实际测试范围与限制见任务书第 1.2 节。
- 缺口：普通 REST 展开 run 时会返回授权站父记录的额外字段，关联字段隔离未通过。AI 查询内核输出的专门投影测试未返回这些字段，但不能据此宣称平台接口已修复。

本次仅修改测试员工、独立账号/角色并创建测试对话；没有修改业务代码、业务数据、调度任务或设备状态。
