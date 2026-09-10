# M3 ARM64 同域 iframe 部署设计

日期：2026-08-28\
状态：待用户复核

## 1. 背景与最终约束

M3 改为部署在承载现有业务的 ARM64 机器上。NocoBase、Node-RED、原始数据接口和 M3
结果表均通过 `https://vifa.hlszh.com` 提供服务。现场 NocoBase 没有 JavaScript 区块，只有
普通 iframe/HTML 区块；本次不修改现有反向代理配置。

现场已经验证 Node-RED 可以创建 `http in` 路由并返回 HTML；本次部署必须在导入 Flow 后
现场验证同域 `/ett` 确实到达该 Node-RED 节点。NocoBase iframe 请求能够携带当前用户
Token。当前目标优先完成联调，Token 隐藏和长期认证
加固不属于本次范围，但 Node-RED 仍须用当前 Token 调用 NocoBase `GET /api/auth:check`，不得
使用匿名公开的预测 API。

本设计取代 `2026-08-28-m3-production-import-package-design.md` 中以下生产假设：

- NocoBase 位于 `ems.lvkpower.com`；
- Node-RED 位于 `opdash.lvkpower.com`；
- NocoBase 使用 JavaScript 区块和 `postMessage` 传递 Token。

StatsForecast 模型、双电站隔离、15 分钟粒度、未来 24 小时预测和 M3 四表结构不变。

## 2. 组件架构

```text
https://vifa.hlszh.com
├─ NocoBase
│  ├─ 当前登录用户与 GET /api/auth:check
│  ├─ 原始表 t_es_data
│  ├─ M3 四张结果表
│  └─ 普通 iframe 区块
│       └─ 加载 Node-RED GET /ett，并携带当前用户 Token
│
├─ Node-RED
│  ├─ GET /ett：返回预测看板 HTML
│  ├─ GET /energy-forecast-api：验证当前用户并返回预测 JSON
│  └─ Exec：固定 curl 到 dashboard.sock
│
└─ ARM64 Docker
   ├─ vifa-m3-worker
   │  ├─ 定时读取 t_es_data
   │  ├─ StatsForecast 训练、选模和预测
   │  └─ 写入 M3 四表
   └─ vifa-m3-dashboard
      ├─ 读取 M3 最新预测和最近 24 小时实绩
      └─ 通过 dashboard.sock 返回严格 Dashboard JSON
```

两个容器使用同一个 ARM64 镜像，但运行不同入口。Docker 不开放 TCP 端口；Worker 和
Dashboard 仅在宿主机绑定目录中创建 Unix Socket。Node-RED 不调用 Docker CLI，也不依赖
宿主机 Python 虚拟环境。

## 3. Docker 部署

部署目录固定为：

```text
/userdata/holo/pyfiles/vifa-m3
```

Compose 保留两个服务：

- `vifa-m3-worker`，命令 `worker`；
- `vifa-m3-dashboard`，命令 `dashboard`。

共享挂载关系：

| 宿主机路径 | 容器路径 | 权限与用途 |
|---|---|---|
| `/userdata/holo/pyfiles/vifa-m3/run` | `/run/vifa-m3` | Worker/Dashboard Socket |
| `/etc/vifa-m3/raw-source.token` | `/run/secrets/raw-source.token` | 只读原始数据 Token |

环境文件：

- `/etc/vifa-m3/m3.env` 供 Worker 使用；
- `/etc/vifa-m3/dashboard.env` 供 Dashboard 使用。

两份环境文件中的原始数据地址和 NocoBase 地址统一为：

```text
https://vifa.hlszh.com
```

原始读取接口固定为：

```text
https://vifa.hlszh.com/api/t_es_data:list
```

第一阶段继续设置 `M3_ACCEPTANCE_ENABLED=false`。`M3_SOURCE_API_TOKEN` 和
`M3_ADMIN_API_TOKEN` 仅为现有配置契约的启动必填值，当前普通预测页面不依赖它们。原始源
Token、Worker 四表写入 Key 和 Dashboard 四表只读 Key 仍分别配置，不混用。

镜像必须在 ARM64 机器本机构建，或使用包含 `linux/arm64` manifest 的镜像。启动后通过
`docker image inspect` 确认架构为 `arm64`。

## 4. Node-RED 页面与 API

### 4.1 `GET /ett`

`/ett` 由 Node-RED `http in`、校验 Function、Template 和 `http response` 节点组成。
NocoBase iframe 加载该地址时携带当前登录用户的：

```http
Authorization: Bearer <current-user-token>
```

联调阶段，Function 节点只接受格式正确、长度受限的 Bearer Header，将 Token 以
`JSON.stringify` 后的值交给固定 HTML Template。Template 不拼接用户可控 URL。Token 会
出现在当次 `/ett` HTML 响应的 JavaScript 变量和 iframe 页面内存中，但不写 Node-RED
context、文件或数据库。这是本次明确接受的临时联调风险。

HTML 固定请求同源：

```text
GET /energy-forecast-api
```

并将同一个 Token 放入请求的 `Authorization` Header。页面每 60 秒刷新数据，重复刷新不会
触发模型训练。

这一直接传递方式仅用于当前联调。后续 Token 加固可以替换传递机制，但不得改变 Dashboard
JSON 契约、Docker Socket 边界或 NocoBase 页面结构。

### 4.2 `GET /energy-forecast-api`

Node-RED 仅接受无查询参数的 GET 请求。处理顺序固定为：

1. 校验 Bearer Header 的结构与大小；
2. 使用相同 Header 请求 `https://vifa.hlszh.com/api/auth:check`；
3. 仅当响应为 2xx 且 `data.id` 是正整数时继续；
4. 立即从 `msg.req.headers`、`msg.headers` 和临时对象删除 Token；
5. Exec 执行固定命令请求 Dashboard Socket；
6. 校验 stdout 为一行、大小受限、结构正确的 JSON；
7. 返回 `application/json; charset=utf-8` 和 `Cache-Control: no-store`。

Exec 命令固定为：

```bash
/usr/bin/curl --fail --silent --show-error --connect-timeout 2 --max-time 30 --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock http://localhost/dashboard
```

请求参数、Token、URL 或 Node-RED 消息不得改变该命令。Node-RED 不直接运行
`m3/forecast_api.py`，不调用宿主机 Python，也不调用 `docker exec`。

### 4.3 Worker 健康检查

Node-RED 可保留每 60 秒一次的固定 Worker Socket 健康检查：

```bash
/usr/bin/curl --fail --silent --show-error --connect-timeout 2 --max-time 10 --unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock http://localhost/health
```

连续三次失败后写一条脱敏告警。健康检查不重启容器，也不重启 Node-RED。

## 5. NocoBase iframe 配置

目标页面使用普通 iframe/HTML 区块，不依赖 JavaScript 区块插件。iframe 地址固定指向现场
能够进入 Node-RED `GET /ett` 的同域地址：

```text
https://vifa.hlszh.com/ett
```

iframe 配置使用 NocoBase 当前用户变量向 `/ett` 请求添加 Authorization Header。该能力是
本设计的硬性前置条件：若现场普通 iframe 区块不能添加 Header，则本方案停止实施，不得把
Token 改放 URL 作为未评审的绕过。所有能打开该页面且登录有效的用户均可查看两个配置电站。
普通用户不需要 M3 四表或 `t_es_data` 的直接读取权限，因为所有数据由 Dashboard 容器使用
服务端只读 Key 读取。

导入 Node-RED Flow 后还必须从最终用户浏览器验证
`https://vifa.hlszh.com/ett` 命中 Node-RED 的 `/ett` 节点。若仍由 NocoBase 返回 404 或其他
页面，则在“不修改反向代理”的约束下本方案被基础设施阻塞，不能通过继续修改 Flow 解决。

页面不得嵌入 Node-RED 编辑器地址、宿主机 IP、Docker 端口或 Socket 路径。M1、M2 页面和
权限保持不变。

## 6. 数据流与职责

Worker 是唯一运行模型和写预测表的组件：

1. 按两个完整 `es_sn` 分别读取 `t_es_data`；
2. 按既有规则聚合 `load_power` 和 `emus_soc`；
3. 使用 StatsForecast 独立训练四条序列；
4. 生成未来 96 个 15 分钟点；
5. 写入 `energy_forecast_latest`、`energy_forecast_batches`、
   `energy_forecast_points` 和 `energy_forecast_evaluations`。

Dashboard 容器只读，不训练模型：

1. 从 `energy_forecast_latest` 读取每站最新预测；
2. 从 `t_es_data` 读取预测起点之前 24 小时实绩；
3. 在服务端按相同 15 分钟规则聚合实绩；
4. 返回两个电站各自的负荷和 SOC 实绩、预测、模型与状态。

浏览器请求与模型调度解耦。频繁刷新页面不会新增预测批次、改变冠军模型或写入 M3 表。

## 7. 错误处理

| 场景 | 外部行为 |
|---|---|
| `/ett` 缺少或携带非法 Header | 返回 401，不渲染看板 |
| 当前用户失效 | `/energy-forecast-api` 返回 401 |
| `auth:check` 不可用 | 返回 503，不执行 Dashboard |
| Dashboard Socket 不存在或超时 | 返回 502，日志只记录安全错误码 |
| 单站数据不足或异常 | 该站显示 `initializing`、`degraded` 或 `insufficient_history` |
| 两站均不可用 | 页面显示总体错误，不填充演示数据 |
| Worker 首次训练中 | 容器保持 `starting`，页面显示初始化状态 |

Node-RED Debug、Docker 日志和 HTTP 错误体不得输出 Token、Authorization Header、NocoBase
Key、原始响应正文或完整 `es_sn`。

## 8. 实施范围

本次后续实施仅修改 M3 相关内容：

- ARM64 Compose 和生产环境示例；
- Node-RED 人工导入 Flow JSON；
- `/ett` HTML 的 Header Token 传递方式；
- NocoBase iframe 人工配置说明；
- ARM64 同域部署手册和架构流程图；
- 与上述行为直接相关的自动化测试。

不修改 StatsForecast 模型逻辑、M3 四表结构、M1、M2、NocoBase 工程代码、Node-RED 工程
源码或反向代理配置。

## 9. 验证与验收

部署前和部署后至少验证：

1. 镜像架构为 `arm64`；
2. Worker 和 Dashboard 两个容器均运行，且健康检查最终通过；
3. Node-RED 进程能够读取 `worker.sock` 和 `dashboard.sock`；
4. 未登录或无 Header 请求 `/ett` 返回 401；
5. 普通有效用户能够通过 NocoBase iframe 打开 `/ett`；
6. `/energy-forecast-api` 在执行 Dashboard 前完成同域 `auth:check`；
7. 页面明确区分两个电站，并分别显示总负荷和 SOC；
8. 页面展示最近 24 小时实绩和未来 24 小时预测；
9. 页面刷新不会触发模型或新增批次；
10. Token 和服务端 Key 不出现在 Node-RED Debug、Docker 日志或
    `/energy-forecast-api` JSON 响应中；已接受 Token 存在于当次 `/ett` HTML 响应；
11. M1、M2、现有 NocoBase 页面和其他 Node-RED Flow 不受影响。

第一阶段继续关闭连续七日验收，因此本次只验收普通预测、结果落表和页面联调，不宣称模型
已经通过七日业务验收。

## 10. 回退

回退时只执行以下动作：

1. 在 NocoBase 停用 M3 iframe 区块；
2. 在 Node-RED 停用 M3 `/ett`、`/energy-forecast-api` 和健康检查组；
3. 停止 M3 Worker/Dashboard 两个容器。

不删除 M3 四表，不清理已写入的预测历史，不修改 M1/M2，也不重启 Node-RED。
