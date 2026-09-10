# M3 systemd Worker 与 Node-RED Dashboard 混合部署设计

日期：2026-08-26\
状态：已确认并实现

## 1. 背景与最终决策

M3 与已经在线验证的 M2 存在一个关键差异：M2 的单次 Python 入口只进行轻量实时计算和当日数据读取，M3 则为两个电站分别维护约 90 天训练缓存、每站两条序列的冠军模型以及确定性调度状态。若每 15 分钟通过 Node-RED Exec 新建一个 M3 预测进程，进程必须反复拉取历史并恢复或重选模型，会增加源接口压力、运行时间和超时风险。

本设计采用混合部署：

- systemd 独立托管一个常驻 M3 Worker，负责模型状态、调度、预测、验收和 NocoBase 写入；
- Node-RED 不启动、不停止、不重启预测 Worker，也不触发模型；
- Node-RED 仅仿照已验证的 M2 方式，为 NocoBase iframe 提供 `GET /energy-forecast-api`，并通过一次性 Python `dashboard` 命令组装公开 JSON；
- NocoBase 继续承载 iframe HTML 与四张 M3 结果表；
- Worker 只监听本机 Unix Domain Socket，不占用 TCP 端口。

本设计覆盖生产进程管理、Dashboard 数据入口与 Node-RED 边界。双电站身份、StatsForecast 模型、15 分钟粒度、未来 96 点、七日验收和 `data.stations[]` Dashboard 合同以当前 `m3.worker` 严格合同与 `m3/contracts/nocobase_collections.json` 为准。M1、M2 不受影响。

## 2. 目标与非目标

### 2.1 目标

- Worker 崩溃或服务器重启后由 systemd 自动恢复。
- 生产环境始终只有一个 M3 调度器实例。
- 保留内存中的训练缓存和冠军模型，避免每 15 分钟重新初始化完整模型状态。
- 不新增 TCP 监听端口。
- 复用现场已经验证的 Node-RED HTTP In、有限 Exec、一行 JSON 和退出码模式。
- iframe 的每次刷新只读公开展示数据，不运行 StatsForecast、不写预测结果。
- 两个电站独立预测、独立失败、独立写入、同时展示。
- 所有数据访问继续通过 HTTP；不新增数据库直连。

### 2.2 非目标

- 不让 Node-RED 托管常驻 Python 子进程。
- 不让 Node-RED 负责 M3 预测、选模或验收定时。
- 不在 NocoBase 中开发新的预测算法或调度插件。
- 不把源接口 Token、NocoBase 写 Key 或完整内部电站 ID 发送给浏览器。
- 不修改 M1、M2 代码、Flow、页面或部署方式。
- 不把现有旧三序列 `m3/node_red/energy_forecast_flow.json` 用于当前双电站部署。

## 3. 总体架构

```mermaid
flowchart TD
    SD[systemd] --> W[M3 Worker\n单进程 Uvicorn + 内部调度器]
    W -->|主动 GET 历史与实绩| RAW[原始能源 HTTP 接口]
    W -->|HTTP 写入| NB[(NocoBase M3 四张结果表)]

    PAGE[NocoBase iframe HTML] -->|GET /energy-forecast-api\n当前用户 Bearer| NR[Node-RED HTTP In]
    NR -->|GET /api/auth:check| AUTH[NocoBase 当前用户校验接口]
    NR -->|固定 Exec，不附加消息参数| CLI[一次性 Python dashboard 命令]
    CLI -->|只读最新预测与验收| NB
    CLI -->|只读最近 24h 实绩| RAW
    CLI -->|一行公开 JSON| NR
    NR --> PAGE

    NR -->|本机健康检查| SOCK[/run/vifa-m3/worker.sock]
    SOCK --> W
```

预测链路和展示链路相互隔离。Dashboard 命令读取源实绩是为了绘制最近 24 小时曲线，但不得调用 `select_champion`、`forecast_one`、`run_forecast`、`run_baseline` 或任何写表动作。

## 4. 组件职责

### 4.1 systemd

systemd 是 M3 Worker 唯一的生命周期管理者，负责：

- 开机启动；
- 异常退出自动重启；
- 创建 `/run/vifa-m3`；
- 注入受保护环境配置；
- 将 SIGTERM 直接交给 Uvicorn；
- 通过 journal 保存 Worker 安全日志；
- 保证服务单实例运行。

systemd 不负责预测时点。预测时点仍由 Worker 内部 `SchedulerRunner` 决定。

### 4.2 M3 Worker

Worker 使用现有 `m3.worker.main:app`，启动时执行恢复：

1. 校验锁定的 StatsForecast 版本；
2. 两站分别拉取和缓存历史；
3. 两站分别恢复或选择冠军模型；
4. 恢复未完成的验收写入批次；
5. 启动唯一调度线程。

生产调度固定使用 Asia/Shanghai：

| 时点 | 操作 |
|---|---|
| 每天 `00:30` | 两站分别执行模型选择 |
| 每天 `01:02` | 两站分别执行七日验收基线 |
| 每小时 `02/17/32/47` 分 | 两站分别执行预测并回填已完成实绩 |

单站操作继续使用站内锁；一个站失败不得阻止另一站执行。生产不得使用 Uvicorn `--reload`，不得设置多 Worker，也不得同时启动另一份 `m3.worker.main:app`。

### 4.3 一次性 Dashboard Python 入口

新增一个与已验证 M2 入口风格一致的薄入口。生产命令只接受：

```text
/userdata/holo/pyfiles/vifa-m3/.venv/bin/python /userdata/holo/pyfiles/vifa-m3/m3-forecast-api.py dashboard
```

入口职责：

- 只从 Node-RED systemd 注入的 Dashboard 环境变量读取配置；
- 创建只读 HTTP 客户端；
- 读取两站最近 24 小时实绩；
- 读取 NocoBase 中两站最新预测、当前七日验收批次和评估结果；
- 映射完整内部 ID 为 `station_1`、`station_2`；
- 构造并严格校验现有 `DashboardEnvelope`；
- stdout 恰好输出一行紧凑 JSON；
- 无论成功或失败都关闭所属 HTTP 客户端；
- 通过进程退出码告诉 Node-RED 请求结果。

入口不持有长期缓存，不创建 Scheduler、JobService 或 FastAPI 应用，不运行模型，也不写任何 NocoBase 集合。

### 4.4 Node-RED

Node-RED 只承担三个有限职责：

1. 提供 `GET /energy-forecast-api`；
2. 在 Exec 前通过 NocoBase `auth:check` 验证 iframe 传入的当前用户 API Token；
3. 定期通过 Unix Socket检查 Worker 健康并记录或通知异常。

Node-RED 不承担预测调度，也不根据 `msg.payload`、query、header 或 URL 动态构造 shell 命令。

### 4.5 NocoBase

NocoBase 负责：

- 承载 iframe HTML 区块；
- 提供当前用户身份验证；
- 保存 M3 四张结果表；
- 为 Worker 提供最小写权限 Key；
- 为 Dashboard 命令提供独立只读 Key。

所有能打开 iframe 的已登录用户都可以查看两个电站，不建立用户—电站映射，不按角色裁剪站点数组。

## 5. Worker 运行边界

### 5.1 Linux 布局

生产采用以下固定布局：

```text
/userdata/holo/pyfiles/               现场既有 Python 程序根目录，M2 保持原位
/userdata/holo/pyfiles/vifa-m3/       M3 独立程序目录
/userdata/holo/pyfiles/vifa-m3/.venv/ M3 专用 Python 3.12 虚拟环境
/etc/vifa-m3/m3.env                   Worker 非公开环境配置
/etc/vifa-m3/dashboard.env            Dashboard 只读配置
/etc/vifa-m3/raw-source.token         原始源只读 Token
/run/vifa-m3/worker.sock              Uvicorn Unix Socket
```

`/userdata/holo/pyfiles` 是 M2 已验证的现有服务器路径；M3 只新增 `vifa-m3` 子目录，不覆盖、移动或复用任何 M2 脚本、虚拟环境或配置。Worker 使用独立低权限 Linux 用户 `vifa-m3`。Node-RED 当前以 root 运行，可以读取 Socket 健康状态，但 root 身份不传递给 Worker。配置文件权限固定为 root 可写、运行用户可读；Token 不放在命令行、Flow JSON、HTML 或仓库。

### 5.2 systemd 服务语义

服务必须满足：

- `User=vifa-m3`、`Group=vifa-m3`；
- `WorkingDirectory=/userdata/holo/pyfiles/vifa-m3`；
- `EnvironmentFile=/etc/vifa-m3/m3.env`；
- `RuntimeDirectory=vifa-m3`；
- `Restart=on-failure`、`RestartSec=5s`；
- 启动命令使用 `/userdata/holo/pyfiles/vifa-m3/.venv/bin/python -m uvicorn m3.worker.main:app`；
- Uvicorn 参数固定为一个 Worker、`--uds /run/vifa-m3/worker.sock`、`--no-access-log`；
- 停止超时必须覆盖调度线程的安全停止和 HTTP 客户端关闭；
- 不使用 shell 拼接环境值。

Worker 正常 SIGTERM 退出不被当作崩溃；非零退出和异常信号由 systemd 重启。Unix Socket 存在但 `/health` 不是 `status=ok` 时，systemd 进程状态和 journal 是首要排障依据，Node-RED 不自行 kill 或重启 Worker。

## 6. Dashboard 数据组装

### 6.1 数据来源

一次 `dashboard` 命令按以下顺序工作：

1. 校验固定配置和两个完整电站绑定；
2. 分别读取 `energy_forecast_latest` 的两站最新行；
3. 从原始源读取两站各自最近 24 小时实绩并执行现有 15 分钟聚合；
4. 读取当前验收 run 的 complete 批次和最新评估；
5. 将每站实绩、未来 96 点预测和验收指标组合为公开站点对象；
6. 生成固定顺序的两站响应并执行严格模型校验。

总负荷仍只使用本站 `load_power`，SOC 仍只使用本站 `emus_soc`。Dashboard 读取不得引入 `solar_power`、跨站求和或另一站补数。

### 6.2 公开响应

成功时 stdout 恰好包含一行：

```json
{"status":"ok","data":{"operation":"forecast_dashboard","system":{},"stations":[]}}
```

上例只表示顶层形状；实际 `system`、`stations` 及点数组必须完整满足现有双电站 `DashboardEnvelope`，不得返回空占位对象。

错误时 stdout 仍恰好包含一行安全 JSON：

```json
{"status":"error","error":{"code":"source_error","message":"预测展示数据读取失败"}}
```

公开错误不得包含异常类名、URL、Token、完整电站 ID、响应正文、文件路径或栈信息。

### 6.3 退出码

| 退出码 | 含义 | Node-RED HTTP 状态 |
|---:|---|---:|
| `0` | 成功 | `200` |
| `2` | 固定命令参数错误 | `400`，仅用于运维误配置，浏览器不能触发 |
| `3` | 服务器配置缺失或无效 | `500` |
| `4` | 原始实绩读取失败 | `502` |
| `5` | NocoBase 只读查询失败 | `502` |
| `6` | 持久化数据或 Dashboard 合同无效 | `502` |
| `1` | 未分类内部错误 | `500` |

Node-RED 只依据退出码和解析后的 `status` 决定 HTTP 状态，不把 stderr 作为浏览器响应。

### 6.4 部分失败

- 某站最新预测不存在：该站返回严格 `initializing` 空拓扑，另一站正常展示。
- 某站原始实绩不可用：保留其最新预测，实绩数组为空，并将该站标记为 `degraded`；另一站不受影响。
- 尚无验收批次时该站 `acceptance=null`；已有不足七天批次或最终评估尚未生成时返回 `in_progress`，都不影响预测展示。
- 最新预测超过 30 分钟：该站标记 `stale`，仍返回最后有效预测。
- 两站最新预测均无法读取或顶层合同无法构造：命令失败，Node-RED 返回 `502`。
- iframe 请求失败：页面保留已渲染的最后有效内容并显示刷新失败，不用演示值覆盖。

## 7. Node-RED Flow 合同

### 7.1 Dashboard 请求

Flow 固定顺序：

```text
HTTP In GET /energy-forecast-api
  → 校验 Authorization 形状
  → 固定 GET /api/auth:check 校验 NocoBase 当前用户
  → 固定 Exec dashboard 命令
  → 解析 stdout 的唯一 JSON 行
  → 映射退出码和 Content-Type
  → HTTP Response
```

Exec 节点要求：

- 使用有限执行模式，不使用 spawn；
- 命令固定为 `/userdata/holo/pyfiles/vifa-m3/.venv/bin/python /userdata/holo/pyfiles/vifa-m3/m3-forecast-api.py dashboard`；
- `addpay=false`，不得附加 `msg.payload`；
- 不允许 query、header、iframe变量进入命令；
- 超时为 30 秒；
- stdout 最大接受一个受限 JSON 响应；
- stderr 仅进入安全运维日志；
- 返回码必须进入错误映射节点。

### 7.2 身份验证

NocoBase 官方把 `API token` 定义为可用于验证当前用户身份的界面变量。本部署中的 iframe HTML 区块在渲染时把该变量注入页面的闭包内存；源 HTML 文件本身不包含 Token。HTML 只通过 `Authorization: Bearer ...` 请求同源 `/energy-forecast-api`，Token 不进入 URL、可见 DOM 文本、日志、持久缓存或仓库。若现场安装版本不能在 iframe HTML 区块内安全解析当前 API Token，则认证联调不通过，禁止退化为 query token 或公开接口。

Node-RED 把该 Bearer 原样发送给固定的 `GET ${M3_NOCOBASE_BASE_URL}/api/auth:check`，不接受浏览器提供的目标 URL。只有上游返回 `2xx`、响应为受限大小的 JSON，且安装版本约定的当前用户对象存在时才放行；`401/403`、超时、非 JSON 或合同不符均拒绝且不执行 Python。认证失败对浏览器统一返回 `401`，NocoBase 自身不可用返回 `503`，避免把基础设施故障误报为用户登出。

现场当前使用默认认证器，Flow 不接收浏览器传入的 `X-Authenticator`。若以后启用 OIDC、SAML 等其他认证器，必须在服务器侧配置固定允许值并重新完成 `auth:check` 合同测试；不得让请求头动态选择任意认证器。用户 Token 在校验后立即从 `msg` 删除，不传给 Python Dashboard 命令，Python 使用独立、最小权限、只读的服务端 NocoBase Key。

若现场已验证的 M2 iframe 网关已经实现等价的当前用户校验，M3 复用该认证边界；不得复用 M2 的业务查询或计算 Flow。Node-RED 1880 管理端口和 HTTP In 不直接暴露公网，外部只通过现有 HTTPS 反向代理访问。

### 7.3 Worker 健康检查

Node-RED 每 60 秒执行固定命令：

```text
/usr/bin/curl --fail --silent --unix-socket /run/vifa-m3/worker.sock http://localhost/health
```

`status=ok` 表示恢复完成、StatsForecast 版本正确且调度器运行。`initializing` 在启动恢复期间允许出现；连续三次非 `ok` 才触发运维告警。Node-RED 不负责重启服务，重启由 systemd 完成。

## 8. iframe HTML 行为

HTML 保留同源固定路径 `/energy-forecast-api` 和 60 秒刷新间隔，只调整认证请求边界：

- 首次进入立即请求；
- 通过 iframe HTML 区块的 NocoBase `API token` 变量获得当前用户 Token，复制到闭包后立即清空临时注入值；
- 使用闭包中的当前用户 Token 设置 Authorization；
- 保留 `cache: "no-store"`、10 秒浏览器超时和单次 in-flight 防重；
- 不访问原始源、NocoBase集合 API、Worker UDS 或人工预测接口；
- 不把 Token 放入 query string；
- 页面隐藏时不主动刷新；
- 401 显示登录失效，502/504 显示数据暂不可用；
- 刷新失败不清除上一次成功数据。

HTML 与 Node-RED 返回合同必须同步发布。任一侧仍为旧三序列合同即失败关闭。

## 9. 配置与凭据分离

Worker 和 Dashboard 使用不同权限的凭据：

| 凭据 | 使用者 | 权限 |
|---|---|---|
| 原始源只读 Token | Worker、Dashboard CLI | 只读固定能源记录接口 |
| NocoBase Worker Key | Worker | 四张 M3 表必需的最小写入、更新与查询动作 |
| NocoBase Dashboard Key | Dashboard CLI | latest、batches、evaluations 三表固定字段的只读 list；不读取 points |
| Worker Admin Token | Worker 运维 API | 仅 `/v1` 人工运维路由；浏览器和 Dashboard CLI 不使用 |
| 当前用户 API Token | iframe → Node-RED → NocoBase 校验 | 只验证当前用户，校验后立即丢弃 |

不得用 NocoBase 管理员 Key 代替任何上述凭据。不得在 Node-RED Debug、systemd Environment 行、进程命令行、HTML、Git 或工单中打印明文 Token。

## 10. 故障恢复与可维护性

- Worker 进程崩溃：systemd 5 秒后重启；恢复阶段重新构建两站缓存和冠军模型并协调 writing 批次。
- Node-RED 重启：不影响 Worker 和模型状态；HTTP In 恢复后 Dashboard 查询继续。
- NocoBase 短时不可用：Worker 保留内存状态并按既有错误策略告警；已发布结果不被较旧结果覆盖。
- 原始源短时不可用：单站保留最后成功预测；超过 30 分钟对外标记陈旧。
- Dashboard CLI 超时：Node-RED 终止有限子进程并返回 `504`，不会影响 Worker。
- 服务器重启：systemd 先恢复 Worker；Node-RED 页面可先展示 NocoBase 最后结果，待新实绩可读后自动刷新。
- 多用户同时查看：请求彼此只读；不触发预测或写表。若现场并发超过源接口承载能力，再增加不超过 60 秒的安全公开响应缓存，不在首版提前引入缓存状态机。

## 11. 实施文件边界

实施阶段预计：

- 新增一次性 Dashboard 入口 `m3/forecast_api.py`；
- 新增持久化结果到 `DashboardEnvelope` 的纯组装服务；
- 新增当前双电站专用 Node-RED Dashboard Flow，保留旧合同 Flow 不动；
- 新增 systemd service、环境示例和服务器准备脚本；
- 小幅修改 HTML 的认证请求和刷新失败保留行为；
- 更新 `docs/m3/部署说明.md`；
- 新增对应 Python 和浏览器测试；Node-RED Flow 按用户决定只做现场联调，不新增自动化测试。

不修改 `m2/**`、`m2/**`、M2 Flow 或 M2 HTML。

## 12. 上传清单

服务器部署包包含：

```text
/userdata/holo/pyfiles/vifa-m3/
├── pyproject.toml
├── uv.lock
├── m3/worker/**
├── m3/forecast_api.py
├── m3/deploy/**
├── m3/node_red/m3_dashboard_exec_flow.json
└── 场站未来能耗预测.html
```

上述清单描述上传后的目标位置；打包源文件仍为：

```text
pyproject.toml
uv.lock
m3/worker/**
m3/forecast_api.py
m3/deploy/**
m3/node_red/m3_dashboard_exec_flow.json
场站未来能耗预测.html
```

不上传：

```text
.venv/**
__pycache__/**
.pytest_cache/**
m3/worker/密钥.txt
任何真实 env、Token、API Key 或完整站点配置备份
```

精确完整电站 ID 和凭据只在服务器 `/etc/vifa-m3` 中配置。

## 13. 测试与验收

### 13.1 Worker

- 启动恢复后 `/health` 为 `ok`；
- 只有一个 Uvicorn Worker 和一个调度线程；
- 调度时点精确为 `00:30`、`01:02` 和每小时 `02/17/32/47`；
- systemd 异常重启后可恢复两站并继续写入；
- ES01 故障不阻止 ES02；
- 重复执行不会产生冲突记录。

### 13.2 Dashboard 入口

- 只接受单个 `dashboard` 参数；
- stdout 恰好一行合法 JSON；
- 每个失败类别返回固定安全错误码和退出码；
- 不调用任何模型、调度或 NocoBase 写动作；
- 只改变 ES01 实绩或结果时，ES02 输出逐字段不变；
- 完整内部 ID、Token、URL、响应正文和异常文本不出现在输出或日志；
- 两站顺序和每站两序列顺序固定；
- 最新预测、最近 24 小时实绩和七日验收正确组合。

### 13.3 Node-RED

- 无 Authorization、无效 Token、过期 Token 均返回 `401` 且不执行 Python；
- NocoBase `auth:check` 不可达返回 `503` 且不执行 Python；
- 认证响应超限、非 JSON 或没有当前用户对象均失败关闭；
- Exec 命令固定且 `addpay=false`；
- stdout 非 JSON 返回 `500`；
- 数据源或 NocoBase 错误返回 `502`；
- 30 秒超时返回 `504`；
- 浏览器参数无法改变命令、URL、站点或文件路径；
- Worker 健康异常只告警，不产生第二个 Worker。

### 13.4 iframe

- 已登录用户可看到两个电站；
- 未登录或会话失效用户不可读取 Dashboard；
- Token 不出现在地址栏、页面文本或控制台；
- 页面每 60 秒只读刷新且从不触发模型；
- 失败时保留上一版成功数据；
- 桌面和 390px 窄屏通过现有 Dashboard E2E。

## 14. 上线门槛与回滚

上线前必须完成：

1. 两站真实源读取、StatsForecast 预测和 NocoBase 写入联调；
2. systemd 杀进程重启演练；
3. Node-RED 认证拒绝和固定命令验证；
4. iframe 登录、刷新、断源和单站失败验证；
5. 连续七日保存与验收数据检查；
6. 凭据泄漏扫描；
7. M3 全量回归，确认 M1/M2 未修改。

回滚时同步恢复上一版 Worker 程序、Node-RED M3 Flow 和 HTML。NocoBase 历史结果不删除；旧 Worker 不得与新 Worker 同时运行。回滚完成后通过 Unix Socket 健康检查和两站最新结果时间确认服务恢复。

## 15. 外部合同依据

- NocoBase 官方变量文档将 `API token` 定义为访问 NocoBase API、验证当前用户身份的凭据：<https://docs.nocobase.com/cn/interface-builder/variables>
- NocoBase 官方认证扩展文档列出 `auth:check` 为“检查用户是否已登录”的标准资源动作，并说明扩展认证器使用 `X-Authenticator`：<https://docs.nocobase.com/auth-verification/auth/dev/>
- NocoBase 官方安全指南说明默认 API 鉴权使用 JWT，并要求服务端访问控制而非仅依赖前端可见性：<https://docs.nocobase.com/cn/security/guide>

最终请求和响应字段仍以现场已安装 NocoBase 版本的 API Documentation 与联调抓取结果为准；任何版本差异必须通过合同测试收敛，不能在 Flow 中做宽松猜测。
