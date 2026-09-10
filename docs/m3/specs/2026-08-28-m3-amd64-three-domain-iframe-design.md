# M3 AMD64 三域 iframe 部署设计

日期：2026-08-28\
状态：用户已确认

## 架构

生产职责固定为：

| Origin | 职责 |
| --- | --- |
| `https://vifa.hlszh.com` | 原始数据接口和 M3 四表 |
| `https://ems.lvkpower.com` | NocoBase 页面和普通 iframe 容器 |
| `https://opdash.lvkpower.com` | Node-RED 的 `/ett` 与 `/energy-forecast-api` |

M3 Docker 运行在 `linux/amd64` 主机。Worker 与 Dashboard 继续使用同一个镜像、两个容器和
两个 Unix Socket，不开放新的 TCP 端口。Node-RED 与 M3 Docker 位于同一台主机，并通过固定
宿主机路径 `/userdata/holo/pyfiles/vifa-m3/run` 访问 Socket。

## 页面与认证

NocoBase 使用普通 iframe/HTML 区块嵌入：

```text
https://opdash.lvkpower.com/ett
```

iframe 只配置 URL，不携带 Header 或 Token：

```text
https://opdash.lvkpower.com/ett
```

Node-RED `/ett` 直接返回公开页面。页面同源请求公开只读 `/energy-forecast-api`，Node-RED
通过固定 `curl --unix-socket` 请求 Dashboard。Dashboard 使用容器环境中的四表只读 Key；
该 Key 不进入浏览器、URL 或 Flow。

不使用 NocoBase JavaScript Block、`postMessage`、nonce、浏览器 Token、Header Token或 URL
Token。用户已明确接受任何能访问 OPDash 地址的人查看两个电站预测数据。

## 安全与响应策略

- `/ett` CSP 的 `frame-ancestors` 只允许 `https://ems.lvkpower.com`；
- 页面只连接自身 origin，即 `https://opdash.lvkpower.com`；
- Node-RED 不执行当前用户认证；
- Dashboard 只读 Key 不进入 URL、Node-RED context、页面、Debug 或 Docker 日志；
- Node-RED 不运行宿主机 Python，不调用 Docker CLI；
- Node-RED Exec 只执行固定 Dashboard/Worker Unix Socket curl；
- 不修改 M1/M2，不创建或修改 M3 四表，不重启 Node-RED。

## Docker

Compose 固定：

```text
platform: linux/amd64
image: vifa-m3:0.1.0
```

Worker 和 Dashboard 的数据源/四表地址保持 `https://vifa.hlszh.com`。平台变化不改变 Python
模型、StatsForecast 逻辑、环境变量合同或 Socket 路径。

## 交付物

- `m3/deploy/compose.yaml`：AMD64 双容器；
- `m3/node_red/m3_production_gateway_flow.json`：`server_token` 公开只读模式和 OPDash 页面；
- `docs/m3/nocobase/m3_nocobase_iframe_manifest.json`：三域普通 iframe 人工配置清单；
- `docs/m3/nocobase/M3普通iframe配置说明.md`：NocoBase 操作说明；
- `docs/m3/AMD64三域Docker部署手册.md`：生产部署操作；
- `m3/部署架构流程图.md`、`m3/人工部署手册.md`、`docs/m3/部署说明.md`：统一入口文档。

## 验收

- 镜像架构为 `linux/amd64`；
- `/ett` 与 `/energy-forecast-api` 由 `https://opdash.lvkpower.com` 提供；
- 无 Header 请求 `/ett` 返回 200；
- `/energy-forecast-api` 无需浏览器 Token并且不调用 EMS `/api/auth:check`；
- 原始数据和四表只使用 `https://vifa.hlszh.com`；
- iframe 页面展示两个电站，浏览器无 Mixed Content、CSP 或 X-Frame-Options 错误；
- Node-RED 未重启，M1/M2 未改动。
