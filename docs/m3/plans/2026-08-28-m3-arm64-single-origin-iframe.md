# M3 ARM64 Single-Origin iframe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 M3 收敛为 ARM64 双容器、`vifa.hlszh.com` 单域、NocoBase 普通 iframe 和 Node-RED Exec 固定访问 Dashboard Unix Socket 的可人工部署版本。

**Architecture:** Worker 与 Dashboard 使用同一 ARM64 Docker 镜像、不同入口和共享 Socket 目录。NocoBase iframe 请求 `/ett` 时携带当前用户 Authorization Header；Node-RED 返回固定 HTML，验证用户后以固定 curl 命令读取 `dashboard.sock`，不调用宿主机 Python或 Docker CLI。

**Tech Stack:** Docker Compose v2、Python 3.12 容器、FastAPI/Uvicorn Unix Socket、Node-RED Flow JSON、NocoBase iframe 区块、StatsForecast。

**Spec:** `docs/m3/specs/2026-08-28-m3-arm64-single-origin-iframe-design.md`

## Global Constraints

- 生产机器固定为 `linux/arm64`。
- NocoBase、Node-RED、原始数据和结果表统一使用 `https://vifa.hlszh.com`。
- 不修改反向代理，不开放 M3 Docker TCP 端口。
- 保留 `vifa-m3-worker` 和 `vifa-m3-dashboard` 两个容器。
- Node-RED Exec 只允许固定 curl 到 `/userdata/holo/pyfiles/vifa-m3/run/*.sock`。
- 不运行宿主机 `m3/forecast_api.py`，不增加 Python 桥接或宿主机虚拟环境。
- NocoBase 只使用普通 iframe，不依赖 JavaScript 区块。
- 当前用户 Token 可在联调阶段进入 `/ett` HTML 内存，但不得写入 context、文件、数据库、Debug 或 Docker 日志。
- 保持 `M3_ACCEPTANCE_ENABLED=false`，不修改模型、四表、M1、M2 或反向代理。
- 按用户要求不新增或运行自动化/端到端测试；只执行 JSON、Compose 和文档静态检查。
- 当前工作目录不是 Git 仓库，不执行提交步骤。

---

### Task 1: ARM64 Compose 与单域环境模板

**Files:**
- Modify: `m3/deploy/compose.yaml`
- Modify: `docs/m3/deploy/production-env/m3.env`
- Modify: `docs/m3/deploy/production-env/dashboard.env`
- Modify: `m3/deploy/m3.env.example`
- Modify: `m3/deploy/dashboard.env.example`
- Modify: `docs/m3/deploy/production-env/README.md`

**Interfaces:**
- Consumes: 既有镜像入口 `worker`、`dashboard` 和容器路径 `/run/vifa-m3`。
- Produces: `linux/arm64` Compose 和只指向 `vifa.hlszh.com` 的运行配置。

- [ ] **Step 1: 固定 Compose 平台**

在 `x-m3-service` 公共锚点加入：

```yaml
platform: linux/arm64
```

保留两个服务、`network_mode: host`、只读根文件系统、`10001:10001` 用户、共享 `./run` 和只读 Token 挂载；不得增加 `ports`、Docker Socket 或宿主机 Python 挂载。

- [ ] **Step 2: 切换 Worker 生产地址**

保留现场密钥值不变，只统一这些值：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_SOURCE_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_ACCEPTANCE_ENABLED=false
```

不得打印、替换或重新生成现有 `M3_SOURCE_API_TOKEN`、`M3_NOCOBASE_API_KEY` 或 `M3_ADMIN_API_TOKEN`。

- [ ] **Step 3: 切换 Dashboard 生产地址**

保留只读 Key 不变并确认：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
```

- [ ] **Step 4: 同步无密钥示例和 README**

示例文件使用同一固定域名并保留 `REPLACE_` 凭据。README 精确记录：

```text
宿主机 /userdata/holo/pyfiles/vifa-m3/run -> 容器 /run/vifa-m3
宿主机 /etc/vifa-m3/raw-source.token -> 容器 /run/secrets/raw-source.token
镜像平台 linux/arm64
```

- [ ] **Step 5: 静态检查 Compose**

运行：

```bash
docker compose -f m3/deploy/compose.yaml config -q
```

预期退出码 `0`，并且没有 `ports:`、`docker.sock`、`opdash.lvkpower.com` 或 `ems.lvkpower.com`。

---

### Task 2: Node-RED 同域 Header 认证和固定 UDS Flow

**Files:**
- Modify: `m3/node_red/m3_production_gateway_flow.json`

**Interfaces:**
- Consumes: `/ett` 请求的 `Authorization: Bearer <token>` Header 和 `dashboard.sock`。
- Produces: `GET /ett` HTML、`GET /energy-forecast-api` JSON、Worker 健康检查。

- [ ] **Step 1: 替换 Flow 变量**

将 tab 环境变量改为：

```json
[
  {"name":"M3_AUTH_MODE","value":"header","type":"str"},
  {"name":"M3_AUTH_BASE_URL","value":"https://vifa.hlszh.com","type":"str"},
  {"name":"M3_NOCOBASE_PAGE_ORIGIN","value":"https://vifa.hlszh.com","type":"str"}
]
```

Flow 不得包含固定 Token、API Key 或完整电站 ID。

- [ ] **Step 2: 修改 `/ett` Header 校验**

Function 节点实现以下边界：

```javascript
const authorization = msg.req?.headers?.authorization;
if (
  env.get("M3_AUTH_MODE") !== "header"
  || typeof authorization !== "string"
  || authorization.length > 4103
  || !/^Bearer [\x21-\x7e]{1,4096}$/.test(authorization)
) {
  msg.statusCode = 401;
  msg.headers = { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" };
  msg.payload = "登录状态无效";
  return [null, msg];
}
msg.m3DashboardApiTokenJson = JSON.stringify(authorization.slice(7));
delete msg.req.headers.authorization;
msg.statusCode = 200;
msg.headers = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors https://vifa.hlszh.com",
};
return [msg, null];
```

不得将 Header 写入 Debug 或 Node-RED context。

- [ ] **Step 3: 将 HTML 改为 Header 模式**

Template 初始化：

```javascript
const apiToken = {{{m3DashboardApiTokenJson}}};
const API_PATH = "/energy-forecast-api";
```

删除 `parentOrigin`、nonce、`requestAuthentication()`、`receiveAuthentication()` 和 message 监听。`loadDashboard()` 固定创建：

```javascript
const requestHeaders = {
  Accept: "application/json",
  Authorization: `Bearer ${apiToken}`,
};
```

页面启动时直接调用 `startRefreshTimer(); void loadDashboard();`，并保留现有 JSON 校验、双站渲染、60 秒刷新和错误状态。

- [ ] **Step 4: 修改 API 认证模式**

将入口允许值改成 `header`。继续拒绝查询参数和非法 Bearer Header，用相同 Header 请求：

```text
https://vifa.hlszh.com/api/auth:check
```

仅接受 2xx 且 `data.id` 为正整数；成功后删除 Authorization 再进入 Exec。失效用户返回 401，认证服务异常返回 503。

- [ ] **Step 5: 保持固定 Exec 命令**

Dashboard：

```text
/usr/bin/curl --fail --silent --show-error --connect-timeout 2 --max-time 30 --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock http://localhost/dashboard
```

Worker 健康检查：

```text
/usr/bin/curl --fail --silent --show-error --connect-timeout 2 --max-time 10 --unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock http://localhost/health
```

- [ ] **Step 6: 静态验证 Flow**

```bash
python3 -m json.tool m3/node_red/m3_production_gateway_flow.json >/dev/null
rg -n "opdash\.lvkpower|ems\.lvkpower|postmessage|server_token|docker exec|m3-forecast-api\.py" m3/node_red/m3_production_gateway_flow.json
```

预期 JSON 有效、搜索无匹配、两个固定 Socket 命令各存在一次。

---

### Task 3: NocoBase 普通 iframe 人工配置

**Files:**
- Modify: `docs/m3/nocobase/m3_nocobase_iframe_manifest.json`
- Create: `docs/m3/nocobase/M3普通iframe配置说明.md`

**Interfaces:**
- Consumes: `https://vifa.hlszh.com/ett` 和 NocoBase iframe Header 配置能力。
- Produces: 无 JavaScript 区块依赖的人工配置与回退清单。

- [ ] **Step 1: 改造 manifest**

保留 `nocobase_native_import.importable=false`，将 `block` 改为：

```json
{
  "type": "iframe_html",
  "title": "场站未来能耗预测",
  "iframe_url": "https://vifa.hlszh.com/ett",
  "authorization_header": "Bearer + NocoBase UI current-user token variable"
}
```

三个 origin 全部为 `https://vifa.hlszh.com`。删除 JavaScript 区块、`postMessage`、nonce 和 `ctx.getVar` 的安装要求。

- [ ] **Step 2: 编写人工说明**

说明必须给出：

1. 新增普通 iframe/HTML 区块；
2. 标题填“场站未来能耗预测”；
3. URL 固定填 `https://vifa.hlszh.com/ett`；
4. Header 名填 `Authorization`；
5. Header 值用 UI 变量选择器组合 `Bearer ` 与当前用户 Token；
6. 不复制固定管理员 Token；
7. 若区块没有 Header UI，停止实施，不改用查询参数；
8. 若 `/ett` 没命中 Node-RED，停止实施，不修改 M1/M2。

- [ ] **Step 3: 静态验证 manifest**

```bash
python3 -m json.tool docs/m3/nocobase/m3_nocobase_iframe_manifest.json >/dev/null
```

预期不再引用旧域名、生产 JS 文件或 `postMessage`。

---

### Task 4: ARM64 同域部署文档

**Files:**
- Modify: `m3/人工部署手册.md`
- Modify: `docs/m3/部署说明.md`
- Modify: `m3/部署架构流程图.md`
- Create: `m3/ARM64同域Docker部署手册.md`

**Interfaces:**
- Consumes: Tasks 1–3 的域名、路径、Flow 和 iframe 配置。
- Produces: 从构建镜像到人工导入和回退的唯一操作顺序。

- [ ] **Step 1: 更新现有部署文档**

删除旧双域和 NocoBase JavaScript 区块步骤，统一说明：

```text
NocoBase/Node-RED/API: https://vifa.hlszh.com
NocoBase: 普通 iframe + 当前用户 Authorization Header
Node-RED: /ett + /energy-forecast-api + dashboard.sock Exec
Docker: linux/arm64 双容器
```

保留“四表已存在、不建表、不修改 M1/M2、不重启 Node-RED”。

- [ ] **Step 2: 更新 Mermaid 流程图**

流程必须表示：

```text
NocoBase iframe -> Node-RED /ett -> /energy-forecast-api
-> NocoBase /api/auth:check -> dashboard.sock -> Dashboard container
Worker container -> t_es_data -> StatsForecast -> M3 four tables
```

不得出现 `postMessage`、JavaScript 区块、公开 Docker 端口或宿主机 Python。

- [ ] **Step 3: 创建 ARM64 操作手册**

按顺序写出：确认 `aarch64`、准备目录和权限、填写两个 env 和 Token 文件、本机构建镜像、确认 `arm64`、启动双容器、检查两个 Socket、人工导入 Flow、人工配置 iframe、停止双容器回退。不得包含现场 Token、密码、完整 `es_sn` 或 API Key。

- [ ] **Step 4: 文档静态检查**

```bash
rg -n "ems\.lvkpower|opdash\.lvkpower|postMessage|JavaScript 区块" \
  m3/人工部署手册.md docs/m3/部署说明.md m3/部署架构流程图.md \
  m3/ARM64同域Docker部署手册.md docs/m3/nocobase/M3普通iframe配置说明.md
```

预期仅允许带“旧方案/不再使用”否定语义的历史说明命中。

---

### Task 5: 交付前静态收口

**Files:**
- Review only: Tasks 1–4 的全部修改文件

**Interfaces:**
- Consumes: Compose、Flow、iframe 清单和部署文档。
- Produces: 可供人工上传、导入和部署的一致交付包。

- [ ] **Step 1: 检查敏感内容**

```bash
rg -l "Bearer eyJ|holo123|Ctd159951" m3/deploy/compose.yaml m3 docs/m3/specs docs/m3/plans
```

只报告命中文件名，不显示内容。生产 env 若命中只提醒人工复核，不能把值写入交付报告。

- [ ] **Step 2: 检查架构不变量**

人工确认：

```text
Compose 服务数：2
公开 Docker ports：0
Node-RED 路由：/ett、/energy-forecast-api 各 1
Dashboard/Worker 固定 Socket 命令：各 1
生产域名：https://vifa.hlszh.com
NocoBase 区块：普通 iframe
宿主机 Python入口：0
```

- [ ] **Step 3: 输出交接清单**

最终只列出上传文件、Node-RED 人工导入 JSON、NocoBase iframe 字段、Docker 启停和回退命令，并明确声明未执行测试。不得宣称生产已验证、模型通过七日验收或 Token 已安全加固。
