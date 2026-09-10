# M3 双电站独立预测与 Dashboard 设计

日期：2026-08-26\
状态：已确认

## 1. 背景与决策

源接口 `t_es_data:list` 中的两个 `es_sn` 代表两个独立电站，不是同一电站内的两台设备。此前本地联调把 ES01、ES02 的功率合并为一个 `station_total_load`，该身份假设错误，相关联调服务已停止。

本设计采用按电站分组的结构：1# 电站和 2# 电站同时展示，每个电站分别预测总负荷与 SOC。四条序列完全独立，不跨站聚合、训练、选模、预测或覆盖结果。

本设计取代既有 M3 文档中“一个电站包含一条总负荷和两条 SOC 序列”的部分。M1、M2 不受影响。

## 2. 目标与非目标

### 2.1 目标

- 将 ES01、ES02 识别为两个独立电站。
- 每个电站预测 `station_total_load` 和 `storage_soc` 两条序列。
- 所有预测统一使用 StatsForecast，粒度为 15 分钟，未来 24 小时共 96 点。
- 同一 HTML 页面同时展示两个电站，且视觉上明确分区。
- Worker 主动通过 HTTP 拉取源数据；浏览器请求不得触发模型。
- 生产结果按完整 `es_sn` 对应的 `station_id` 独立写入和读取。
- 连续七日验收按电站独立保存与计算。

### 2.2 非目标

- 不计算跨电站总负荷。
- 不把 `solar_power` 加入总负荷。
- 不引入跨电站特征、联合模型或层级预测。
- 不修改 M1、M2。
- 本次不实现 Node-RED 或 NocoBase 工程配置。
- 不在浏览器中运行 StatsForecast、保存凭据或直接访问源接口。

## 3. 电站身份与配置

Worker 使用服务端配置建立完整 `es_sn` 到公开逻辑身份的固定映射：

| 源身份规则 | 公开逻辑身份 | 页面名称 |
|---|---|---|
| 完整 `es_sn`，后缀为 `ES01` | `station_1` | `1# 电站` |
| 完整 `es_sn`，后缀为 `ES02` | `station_2` | `2# 电站` |

约束：

- 完整 `es_sn` 仅用于 Worker 配置、内部处理和结果表 `station_id`，不得返回浏览器。
- 页面与公开 Dashboard API 只使用 `station_1`、`station_2` 和显示名称。
- 源数据必须恰好匹配两个已配置完整 `es_sn`；仅凭后缀自动接纳未知设备不允许用于生产。
- 显示名称是配置项，修改名称不得改变内部 `station_id`、历史数据或模型身份。

## 4. 原始数据映射与 15 分钟聚合

每条源记录只归属于其自身 `es_sn` 对应的电站：

- `station_total_load = load_power`
- `storage_soc = emus_soc`
- `solar_power` 不参与负荷计算。

负荷和 SOC 在每个电站内部单独聚合：

- 时间区间采用 Asia/Shanghai 的左闭右开 15 分钟桶。
- 总负荷取桶内 `load_power` 的均值。
- SOC 取桶内最后一个及时值。
- 有效桶至少需要 12 个分钟样本，首样本不得晚于桶开始后 2 分钟，末样本不得早于桶结束前 2 分钟。
- 负荷小于 0、SOC 不在 0..100、非有限值、缺失或覆盖不足时，该站该序列该桶记为 `invalid`，不得用另一个电站的数据补齐。
- 原始实绩保持不变；仅训练副本允许按既有规则插值连续 1–2 个缺口。

每个电站分别以自己的最新源时间向下取整到 15 分钟，得到该站 `as_of`。训练数据仅包含 `data_time < as_of` 的完整桶。一个电站数据延迟不得强迫另一个电站回退时间窗口。

## 5. 模型与预测隔离

系统同时维护四个独立模型序列：

1. `station_1 / station_total_load`
2. `station_1 / storage_soc`
3. `station_2 / station_total_load`
4. `station_2 / storage_soc`

每条序列独立执行历史模式判断、模型选择、训练、预测、回退和裁剪：

- 少于 7 天：`insufficient_history`，不发布预测点。
- 7 天至不足 28 天：`warming_up`，使用 `SeasonalNaive(96)`。
- 至少 28 天：在既有 StatsForecast 候选集内进行时间序列交叉验证并选择冠军模型。
- 预测粒度固定为 `freq="15min"`，`h=96`。
- 发布负荷裁剪到 `>=0`，SOC 裁剪到 `0..100`，同时保存原始值和 `is_clipped`。
- 某站某序列失败只影响该序列及该站状态，不得改变另一电站的冠军模型或预测。

用于隔离性的核心测试是：只改变 ES01 的源值时，ES02 的训练集、冠军模型输入和 96 点预测必须逐点保持不变。

## 6. Dashboard HTTP 契约

浏览器继续从同源固定路径 `GET /energy-forecast-api` 读取数据。成功响应使用以下嵌套拓扑。为便于阅读，下例仅展开 `station_1` 并省略点数组内容；它用于说明字段结构，不是可直接作为测试夹具的完整响应：

```json
{
  "status": "ok",
  "data": {
    "operation": "forecast_dashboard",
    "system": {
      "state": "initializing",
      "generated_at": "2026-08-26T10:00:00+08:00",
      "healthy_station_count": 2,
      "station_count": 2
    },
    "stations": [
      {
        "station_key": "station_1",
        "station_name": "1# 电站",
        "range": {
          "timezone": "Asia/Shanghai",
          "history_start": "2026-08-25T10:00:00+08:00",
          "history_end": "2026-08-26T10:00:00+08:00",
          "actual_latest": "2026-08-26T09:45:00+08:00",
          "forecast_start": "2026-08-26T10:00:00+08:00",
          "forecast_end": "2026-08-27T10:00:00+08:00",
          "interval_seconds": 900,
          "history_hours": 24,
          "forecast_hours": 24,
          "display_hours": 48,
          "now_separator": "2026-08-26T10:00:00+08:00"
        },
        "system": {
          "state": "initializing",
          "mode": "normal",
          "generated_at": "2026-08-26T10:00:00+08:00",
          "stale": false
        },
        "series": [
          {
            "unique_id": "station_total_load",
            "unit": "kW",
            "model_name": "SeasonalNaive",
            "status": "warming_up",
            "fallback_reason": null,
            "actual": [],
            "forecast": []
          },
          {
            "unique_id": "storage_soc",
            "unit": "%",
            "model_name": "SeasonalNaive",
            "status": "warming_up",
            "fallback_reason": null,
            "actual": [],
            "forecast": []
          }
        ],
        "acceptance": null
      }
    ]
  }
}
```

完整响应必须满足：

- `stations` 长度固定为 2，顺序固定为 `station_1`、`station_2`。
- 每站 `series` 长度固定为 2，顺序固定为 `station_total_load`、`storage_soc`。
- 正常或预热序列各有最多 96 个历史实绩点和恰好 96 个未来预测点。
- 实绩和预测点字段沿用既有严格 Dashboard 点位契约。
- 每站 `range`、`system` 和 `acceptance` 相互独立。
- 顶层 `system.generated_at` 取本次公开响应的生成时间。
- `healthy_station_count` 统计 `ready` 或 `initializing` 且未陈旧的电站。
- 顶层 `state` 按严重性汇总：任一站 `error` 时为 `degraded`；无错误但任一站陈旧时为 `stale`；任一站预热时为 `initializing`；否则为 `ready`。
- 完整 `es_sn`、源 URL、Token、缓存内部对象和结果表凭据不得出现在响应中。

## 7. 页面结构与刷新

页面顶部显示总体状态、健康电站数、总电站数和最近更新时间。主体固定为两个电站区域：

- `1# 电站` 区域包含总负荷指标卡、SOC 指标卡、负荷 48 小时图和 SOC 48 小时图。
- `2# 电站` 区域使用相同结构。
- 每张图仅绘制本电站的一条实绩线和一条预测线，不跨站叠加，也不使用双坐标轴。
- 实绩使用实线，预测使用虚线，缺失实绩保持断点。
- 桌面端每站两张图并排；窄屏按负荷图、SOC 图纵向排列。
- 两个电站使用不同区域强调色，但负荷/SOC 的单位和线型含义保持一致。

页面首次加载后立即读取 `/energy-forecast-api`，之后每 60 秒重新读取。该 GET 只读取服务端缓存或结果表，绝不运行模型。重复请求应复用同一已发布快照，直到 Worker 主动刷新或结果表产生新版本。

单站异常时，该站区域展示明确状态并保留最后一次有效结果；健康电站继续正常显示。只有从未产生过有效结果的站才显示空状态。页面不得用演示值补齐异常站。

## 8. 主动刷新、持久化与七日验收

Worker 继续主动拉取数据并运行预测。浏览器、Node-RED 和 Dashboard API 不触发预测。

- 本地联调服务在启动时主动预测一次，之后每 15 分钟刷新缓存。
- 生产最新结果按完整 `station_id` 独立写入，保持每站一个滚动最新快照。
- 两个电站允许拥有不同 `as_of`、`source_data_end` 和模型状态。
- 写入键必须包含 `station_id`，ES01 的写入不得更新或覆盖 ES02。
- 连续七日验收的批次、点位、回填、评估和结论全部按 `station_id` 隔离。
- 每个电站分别评估负荷和 SOC 两条序列；不计算跨站平均 MAPE，也不使用一个站的有效点补足另一个站的门槛。
- 所有生产读写继续通过 HTTP；本设计不增加数据库直连。

## 9. 状态与错误处理

- 源接口整体不可用：保留两个站的最后有效结果；超过 30 分钟后分别标记陈旧。
- 单站无数据或数据合同错误：只将该站标记为 `error` 或 `stale`，另一个站继续预测和发布。
- 单序列历史不足：该序列为 `insufficient_history`，同站另一序列可继续发布。
- 单序列冠军模型失败：按既有规则回退 SeasonalNaive，并记录安全的 `fallback_reason`。
- 启动时从未成功预测：该站返回严格空拓扑，不伪造预测。
- 日志只记录安全错误代码、公开逻辑站点键和时间，不记录完整 `es_sn`、Token、请求头或原始响应正文。

## 10. 安全边界

- Token 只由服务端密钥文件或生产环境变量读取，保持 `SecretStr`/等效遮蔽语义。
- 浏览器只访问同源 `/energy-forecast-api`。
- 源 URL 是固定部署配置，不接受浏览器或入站请求提供动态目标。
- HTTP 客户端禁止跟随重定向，设置响应大小、分页、超时和重试上限。
- 公开 `station_key` 只能从固定允许集合产生，不从未验证的 `es_sn` 动态拼接。
- 结果表读写权限仍按站点/租户隔离；本次不扩大任何 NocoBase ACL。

## 11. 测试与验收

### 11.1 数据与模型测试

- 断言 ES01 总负荷只等于 ES01 的 `load_power`。
- 断言 ES02 总负荷只等于 ES02 的 `load_power`。
- 加入任意 `solar_power` 不得改变两站负荷结果。
- 改变 ES01 任意输入不得改变 ES02 聚合结果、训练数据或预测。
- 两站分别验证缺口、负值、SOC 越界、时间覆盖和不同 `as_of`。
- 真实 StatsForecast 集成必须输出四条独立序列，每条正常/预热序列 96 点。

### 11.2 API 与持久化测试

- 严格验证两个电站嵌套拓扑、顺序、字段和公开身份。
- 验证完整 `es_sn` 和 Token 不出现在响应、HTML或日志中。
- 验证浏览器连续 GET 不增加模型运行次数。
- 验证 ES01/ES02 最新结果写入键互不覆盖。
- 验证七日验收按站点和两条序列分别计算。
- 验证单站失败时健康站仍返回完整数据。

### 11.3 浏览器测试

- 桌面端验证两个站区、四张指标卡、四张趋势图全部可见。
- 390px 窄屏验证无页面级横向溢出，站内图表按设计排列。
- 验证实绩/预测线型、模型状态、时间窗口、空状态和陈旧状态。
- 验证 60 秒轮询只读取接口且不触发模型。
- 浏览器控制台不得出现错误。

## 12. 变更范围与迁移

实施时需要修改以下 M3 边界：

- 原始数据聚合：从跨设备三序列改为两个站分别两序列（合计四序列）。
- M3 序列合同：从每站三序列改为每站两序列。
- 训练、预测、最新快照和验收枚举：使用 `(station_id, unique_id)` 作为完整业务身份。
- Dashboard API：从顶层三序列改为 `stations[]` 嵌套结构。
- HTML：从一个场站的三张卡/两张图改为两个站区、四张卡/四张图。
- 测试：替换所有依赖旧三序列拓扑的 M3 断言，并新增跨站隔离负例。

迁移期间，旧三序列 Dashboard 响应不得与新页面混用。联调和生产必须同时部署新 HTML 与新 `/energy-forecast-api` 契约；任一侧仍为旧版本时应明确失败，不进行兼容性猜测。

## 13. 上线门槛

- 双电站数据映射、四序列 StatsForecast、嵌套 API 和浏览器 E2E 全部通过。
- 使用真实接口验证 ES01、ES02 各自负荷实绩与首个预测，确认负荷未加太阳能且未跨站求和。
- 单站异常隔离测试通过。
- Token、完整 `es_sn` 和凭据泄漏扫描通过。
- 原错误联调服务不得在修正完成前重新启动。
- M1、M2 文件和行为保持不变。
