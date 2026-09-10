# M3 AMD64 Three-Domain iframe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 ARM64 单域交付物改为 AMD64 三域普通 iframe 生产部署包。

**Architecture:** 数据和四表使用 `vifa.hlszh.com`，NocoBase 普通 iframe 使用
`ems.lvkpower.com`，Node-RED 公开只读页面/API 使用 `opdash.lvkpower.com`。浏览器不传 Token，
Node-RED 直接访问 Dashboard Unix Socket，Dashboard 只读 Key 保留在容器环境中。

**Tech Stack:** Docker Compose、Node-RED core nodes、NocoBase iframe/HTML block、FastAPI UDS、Markdown/Mermaid。

**Spec:** `docs/m3/specs/2026-08-28-m3-amd64-three-domain-iframe-design.md`

## Global Constraints

- 平台固定 `linux/amd64`；镜像标签保持 `vifa-m3:0.1.0`。
- 不修改 M1/M2、M3 四表、模型逻辑和 Socket 路径。
- 不使用 JS Block、postMessage、URL Token、固定浏览器 Token或宿主机 Python桥接。
- 不重启 Node-RED，不修改反向代理。
- 按用户要求不运行自动化或端到端测试，只做 JSON、Compose、脚本语法和敏感值静态检查。
- 工作区根目录不是 Git 仓库，不执行提交。

---

### Task 1: AMD64 容器交付

**Files:**
- Modify: `m3/deploy/compose.yaml`
- Modify: `docs/m3/deploy/production-env/README.md`

- [ ] 将 Compose 平台从 `linux/arm64` 改为 `linux/amd64`。
- [ ] 保留同一镜像、两个容器、两个 Unix Socket、无 TCP ports 的现有结构。
- [ ] 将生产环境说明的平台改为 AMD64。

### Task 2: 三域 Node-RED Flow

**Files:**
- Modify: `m3/node_red/m3_production_gateway_flow.json`

- [ ] 设置 `M3_AUTH_MODE=server_token`。
- [ ] 保留 `M3_AUTH_BASE_URL=https://ems.lvkpower.com` 作为非活动回退配置。
- [ ] 设置 `M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com`。
- [ ] `/ett` CSP 固定 `frame-ancestors https://ems.lvkpower.com`。
- [ ] `/energy-forecast-api` 公开只读并保留固定 UDS curl。
- [ ] 更新 Flow 说明，不加入任何 Token。

### Task 3: NocoBase 普通 iframe 清单

**Files:**
- Modify: `docs/m3/nocobase/m3_nocobase_iframe_manifest.json`
- Modify: `docs/m3/nocobase/M3普通iframe配置说明.md`

- [ ] 设置 NocoBase origin 为 `https://ems.lvkpower.com`。
- [ ] 设置 Node-RED origin 和 iframe URL 为 `https://opdash.lvkpower.com` 与 `/ett`。
- [ ] 保持数据 origin 为 `https://vifa.hlszh.com`。
- [ ] 明确 iframe 只配置 URL，不使用 Header、JS Block或 postMessage。

### Task 4: AMD64 三域部署文档

**Files:**
- Create: `docs/m3/AMD64三域Docker部署手册.md`
- Modify: `m3/人工部署手册.md`
- Modify: `docs/m3/部署说明.md`
- Modify: `m3/部署架构流程图.md`
- Modify: `m3/deploy/m3.env.example`
- Modify: `m3/deploy/dashboard.env.example`

- [ ] 写清三个域名职责、AMD64 镜像构建/导入、双容器启动和 UDS 检查。
- [ ] 写清 Node-RED 人工导入和 NocoBase 普通 iframe 配置。
- [ ] Worker/Dashboard 的原始数据和四表地址保持 VIFA。
- [ ] 删除当前交付入口中的 ARM64、单域、e606pro 和 VIFA iframe 表述。

### Task 5: 静态交付检查

**Files:**
- Check all files modified above.

- [ ] 解析两个 JSON 文件。
- [ ] 解析 Compose，并验证 `linux/amd64`、两个服务、无 ports。
- [ ] 静态检查 iframe 内嵌 JavaScript 语法。
- [ ] 检查两个生产路由各只有一个，Exec 只访问两个固定 Socket。
- [ ] 扫描生产交付物中的密码、JWT、固定 Token、旧平台和错误 origin。
- [ ] 记录未运行自动化/端到端测试。
