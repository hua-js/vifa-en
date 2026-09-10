# M3 双域 HTTPS 入口设计（已废弃）

> 2026-08-28：用户已改回 `opdash.lvkpower.com` + `ems.lvkpower.com` 的 AMD64 三域普通
> iframe 架构。本文件不再用于实施，现行设计见
> `2026-08-28-m3-amd64-three-domain-iframe-design.md`。

日期：2026-08-28

## 1. 背景

M3 的生产系统实际分布如下：

- NocoBase、当前用户认证、原始数据接口和 M3 四表位于 `https://vifa.hlszh.com`；
- Node-RED 位于 `http://e606pro.hlszh.com:1081`；
- M3 Worker 与 Dashboard 继续运行于 ARM64 Docker 主机，通过 Unix Socket 与 Node-RED 连接。

HTTPS NocoBase 页面不能直接嵌入 HTTP Node-RED 页面。浏览器会将其作为 Mixed Content
拦截，因此必须为 Node-RED 提供 HTTPS 入口。

## 2. 目标

新增 `https://e606pro.hlszh.com` 作为 Node-RED 的公开 HTTPS 入口，同时满足：

- 不改变 Node-RED 的内部监听地址 `http://127.0.0.1:1081`；
- 不重启 Node-RED；
- 不修改 NocoBase 的反向代理；
- 不公开 Node-RED 编辑器、Worker 健康检查或 Unix Socket；
- 不将 Token 写入 URL、OpenResty 配置、Node-RED Flow、日志或代码库；
- 不修改 M1、M2、M3 四表以及 M3 模型逻辑。

## 3. 域名职责

| 地址 | 职责 |
| --- | --- |
| `https://vifa.hlszh.com` | NocoBase 页面、当前用户校验、原始数据接口、M3 四表 |
| `https://e606pro.hlszh.com` | M3 iframe 页面和预测 Dashboard API 的公开 HTTPS 入口 |
| `http://127.0.0.1:1081` | Node-RED 内部上游，不直接用于 iframe |
| `/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock` | Node-RED 到 Dashboard 容器的本机 Unix Socket |
| `/userdata/holo/pyfiles/vifa-m3/run/worker.sock` | Node-RED 健康检查到 Worker 容器的本机 Unix Socket |

## 4. HTTPS 入口

现有 80/443 由 OpenResty 提供服务。只在现有
`server_name e606pro.hlszh.com` 的 HTTPS `server` 中增加两个精确路由：

```text
location = /ett
location = /energy-forecast-api
```

两条路由代理到同名 Node-RED 路径：

```text
https://e606pro.hlszh.com/ett
  -> http://127.0.0.1:1081/ett

https://e606pro.hlszh.com/energy-forecast-api
  -> http://127.0.0.1:1081/energy-forecast-api
```

代理必须保留 `Authorization`，传递 `Host`、`X-Forwarded-For` 和
`X-Forwarded-Proto=https`，关闭响应缓存。配置不得输出请求 Header。其他路径沿用现场现有规则，
不得使用覆盖整个站点的 `location /`。

配置部署前运行 OpenResty 配置检查，成功后只做平滑 reload；不得重启 Node-RED。

## 5. 页面与认证流程

1. 已登录用户打开 `https://vifa.hlszh.com` 的 NocoBase 页面；
2. 普通 iframe/HTML 区块请求 `https://e606pro.hlszh.com/ett`，携带当前用户
   `Authorization: Bearer ...`；
3. OpenResty 将 Header 原样转发至 Node-RED；
4. Node-RED `/ett` 校验 Header 格式，将 Token 注入仅本次响应页面的 JavaScript 内存，随后从
   Node-RED 消息对象中删除 Authorization；
5. iframe 页面同源请求
   `https://e606pro.hlszh.com/energy-forecast-api`；
6. Node-RED 服务端使用该 Header 请求
   `https://vifa.hlszh.com/api/auth:check`；
7. 认证成功后，Node-RED Exec 使用固定 `curl --unix-socket` 请求 `dashboard.sock`；
8. Dashboard 从 `https://vifa.hlszh.com` 的原始接口和 M3 四表组织只读展示结果。

iframe 页面与预测 API 同源，因此页面内请求不依赖跨域 CORS。NocoBase 与 iframe 是跨域嵌入，
Node-RED 页面响应的 CSP 必须包含：

```text
frame-ancestors https://vifa.hlszh.com
```

OpenResty 不得为这两个响应增加 `X-Frame-Options: SAMEORIGIN` 或 `DENY`。

## 6. Node-RED Flow

唯一生产导入文件仍为：

```text
m3/node_red/m3_production_gateway_flow.json
```

Flow 保持：

```text
M3_AUTH_MODE=header
M3_AUTH_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_PAGE_ORIGIN=https://vifa.hlszh.com
```

Node-RED 不需要知道公开的自身域名，因为 HTML 使用相对路径
`/energy-forecast-api`。Flow 中只更新架构说明，不添加固定 Token。

## 7. NocoBase 配置

普通 iframe/HTML 区块改为：

```text
URL=https://e606pro.hlszh.com/ett
Header=Authorization
Header value=Bearer + 当前登录用户 Token 变量
```

如果现场区块不能给 iframe 首次导航添加 Header，应立即停止上线。不得把 Token 改放 URL，
不得将管理员 Token 固定写入 HTML。

## 8. Docker 与运行配置

Docker 架构不变：同一个 `vifa-m3:0.1.0` ARM64 镜像启动 Worker 和 Dashboard 两个容器。
Compose 不开放 TCP 端口，继续共享 `run` 目录中的两个 Unix Socket。

以下地址继续保持 `https://vifa.hlszh.com`：

```text
M3_RAW_SOURCE_URL
M3_SOURCE_BASE_URL
M3_NOCOBASE_BASE_URL
M3_AUTH_BASE_URL
M3_NOCOBASE_PAGE_ORIGIN
```

HTTPS 入口变化不要求重新构建模型镜像；只有部署资料、OpenResty 示例、NocoBase 清单和 Flow
说明发生变化。

## 9. 错误处理与安全边界

- 未携带或格式错误的 Authorization：Node-RED 返回 `401`；
- NocoBase 用户校验不可用：返回 `503`，不得回退到固定管理员 Token；
- Dashboard Socket 不可用或超时：返回现有 Dashboard 错误响应；
- OpenResty 只代理两个精确路径，不公开 Node-RED 编辑器；
- 原始 `1081` 端口的公网收口属于独立加固项，不纳入本次变更；
- 访问日志不得包含 Authorization Header 或查询参数 Token；
- `/ett` 与 `/energy-forecast-api` 不缓存。

## 10. 部署与回退

部署顺序：

1. 备份现有 OpenResty 站点配置；
2. 添加两个精确代理路由；
3. 执行 OpenResty 配置检查；
4. 平滑 reload OpenResty；
5. 导入或更新 Node-RED 生产 Flow，选择 Deploy Modified Flows；
6. 在 NocoBase 中将 iframe URL 改为 HTTPS Node-RED 地址；
7. 使用普通已登录用户人工检查页面。

回退顺序：

1. NocoBase iframe 恢复为停用状态；
2. 停用新导入的 M3 Node-RED Flow；
3. 恢复 OpenResty 站点配置备份；
4. 配置检查成功后平滑 reload OpenResty。

回退不停止 M3 Worker/Dashboard，不删除 M3 四表或预测历史。

## 11. 验收标准

- `https://e606pro.hlszh.com/ett` 不再自重定向；
- 无 Header 请求 `/ett` 返回 `401`；
- 已登录普通用户从 NocoBase 页面加载 iframe 成功；
- 页面请求 `/energy-forecast-api` 成功并显示两个电站；
- `/api/auth:check` 仍只请求 `https://vifa.hlszh.com`；
- 浏览器控制台没有 Mixed Content、CSP、X-Frame-Options 或 CORS 错误；
- Node-RED、OpenResty、Docker 日志中没有 Token；
- Node-RED 未重启，M1/M2 未改动。
