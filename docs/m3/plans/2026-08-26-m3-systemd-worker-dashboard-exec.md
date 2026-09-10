# M3 systemd Worker 与 Dashboard Exec 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有双电站 M3 改造成由 systemd 托管单实例常驻 Worker、由 Node-RED 有限 Exec 读取 NocoBase 持久化预测并向 iframe 输出 Dashboard JSON 的可部署实现。

**Architecture:** `m3.worker.main:app` 继续独占模型缓存与调度，通过 Unix Socket 运行；新增只读持久化 Dashboard 服务和一次性 CLI，不在页面请求中运行模型。Node-RED 只做 NocoBase 用户认证、固定 Exec 和 HTTP 响应；M1/M2 保持不变。

**Tech Stack:** Python 3.12、FastAPI 0.141.1、httpx 0.28.1、Pydantic 2.13.4、StatsForecast 2.1.1、systemd、Node-RED、原生 HTML/JavaScript。

**Spec:** `docs/m3/specs/2026-08-26-m3-systemd-worker-node-red-dashboard-design.md`

## Global Constraints

- 只修改根目录 M3 工件；不得修改 `m2/**`、`m2/**` 或任何 M2 Flow/HTML。
- Worker 始终只有一个 Uvicorn Worker 和一个内部调度器；Node-RED 不启动、停止、重启或调度模型。
- 生产程序目录固定为 `/userdata/holo/pyfiles/vifa-m3`，Unix Socket 固定为 `/run/vifa-m3/worker.sock`。
- 两站固定按 `station_1`、`station_2` 输出；每站固定按 `station_total_load`、`storage_soc` 输出。
- 总负荷只读取本站 `load_power`，SOC 只读取本站 `emus_soc`；禁止 `solar_power`、跨站求和或跨站补数。
- Dashboard CLI 只读原始源和 NocoBase，不能创建 Scheduler、JobService、FastAPI 应用或执行任何写表动作。
- stdout 恰好一行 JSON；Token、完整内部电站 ID、内部 URL、响应正文和异常栈不得进入公开响应。
- 按用户要求，不新增或运行 Node-RED 自动化测试；Flow 作为待现场导入联调工件交付。

---

### Task 1: 持久化完整七日验收指标

**Files:**
- Modify: `m3/worker/services/acceptance_service.py`
- Modify: `m3/contracts/nocobase_collections.json`
- Modify: `m3/tests/test_m3_acceptance_service.py`
- Modify: `m3/tests/test_m3_collection_contract.py`

**Interfaces:**
- Consumes: `MetricResult` 已有 `wape_percent`、`median_ape_percent`、`p90_ape_percent`。
- Produces: `energy_forecast_evaluations` 每个序列行持久化六项指标；overall 行六项指标全部为 `null`。

- [ ] **Step 1: 写失败测试**

在验收服务测试中断言序列 upsert 同时包含：

```python
self.assertEqual(values["wape_percent"], 2.4)
self.assertEqual(values["median_ape_percent"], 1.9)
self.assertEqual(values["p90_ape_percent"], 5.2)
```

并在集合合同测试中断言三个字段存在于字段定义、Worker upsert 权限和 Dashboard list 权限。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_acceptance_service m3.tests.test_m3_collection_contract -v`

Expected: FAIL，指出三项指标尚未写入或尚未出现在集合合同中。

- [ ] **Step 3: 实现完整指标持久化**

把 `_validate_metric()`、`_series_evaluation()`、`_validate_evaluation_response()` 和 overall 行的指标集合统一为：

```python
METRIC_FIELDS = (
    "mape_percent", "mae", "smape_percent",
    "wape_percent", "median_ape_percent", "p90_ape_percent",
)
```

序列行从 `MetricResult` 逐字段写入；overall 行对 `METRIC_FIELDS` 全部写 `None`。同步扩展 `nocobase_collections.json` 的字段、约束和两类角色权限。

- [ ] **Step 4: 运行目标测试**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_acceptance_service m3.tests.test_m3_collection_contract -v`

Expected: PASS。

### Task 2: 独立 Dashboard 配置与持久化只读组装服务

**Files:**
- Modify: `m3/worker/config.py`
- Modify: `m3/worker/services/live_dashboard_service.py`
- Create: `m3/worker/services/persisted_dashboard_service.py`
- Create: `tests/test_m3_persisted_dashboard_service.py`
- Modify: `m3/tests/test_m3_contracts.py`

**Interfaces:**
- Consumes: `RawEnergySourceClient.list_observations(station_id, start, end)`、`NocoBaseApiClient.list_records(...)`、`DashboardCache`、`DashboardStationResult`。
- Produces: `DashboardSettings.from_env()` 与 `PersistedDashboardService.build() -> DashboardEnvelope`。

- [ ] **Step 1: 写失败测试覆盖配置隔离和双站读取**

测试 `DashboardSettings` 只要求以下变量，不要求 Worker 控制面和写入凭据：

```text
M3_STATIONS_JSON
M3_RAW_SOURCE_URL
M3_RAW_SOURCE_API_TOKEN 或 M3_RAW_SOURCE_API_TOKEN_FILE
M3_NOCOBASE_BASE_URL
M3_DASHBOARD_NOCOBASE_API_KEY
M3_TIMEZONE=Asia/Shanghai
```

使用 Fake API/Source 验证：最新行的 `series_payload` 被严格恢复成 `LatestSnapshot`；实绩窗口为首个预测点之前 24 小时；ES01 失败时 ES02 仍返回；两站缺行时返回严格 initializing；最新结果超过 30 分钟时返回 stale；Dashboard 服务从不调用写动作。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_persisted_dashboard_service m3.tests.test_m3_contracts -v`

Expected: FAIL，模块或 `DashboardSettings` 尚不存在。

- [ ] **Step 3: 添加配置和只读服务**

`DashboardSettings` 使用独立的 `SecretStr dashboard_nocobase_api_key`。`PersistedDashboardService` 每站执行：

```python
latest = api.list_records(
    "energy_forecast_latest",
    filter={"station_id": binding.station_id},
    fields=["station_id", "as_of", "generated_at", "source_data_end",
            "status", "series_payload", "model_manifest", "content_hash"],
)
forecast_start = snapshot.series[0].points[0].data_time
actual = source.list_observations(
    binding.station_id,
    forecast_start - timedelta(hours=24),
    forecast_start,
)
```

验收读取固定筛选 complete batches 和相同 run 的 evaluations。七日未完成返回 `in_progress` 空结果；完成后严格恢复两条序列的六项指标。通过 `DashboardCache` 保持双站独立失败和 30 分钟 stale 语义。

- [ ] **Step 4: 运行目标测试**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_persisted_dashboard_service m3.tests.test_m3_contracts -v`

Expected: PASS。

### Task 3: Node-RED 一次性 Dashboard CLI

**Files:**
- Create: `m3/worker/dashboard_cli.py`
- Create: `m3/forecast_api.py`
- Create: `m3/tests/test_m3_dashboard_cli.py`

**Interfaces:**
- Consumes: `DashboardSettings.from_env()`、`PersistedDashboardService.build()`。
- Produces: `execute(argv, environ, builder=...) -> tuple[dict, int]`、`main(...) -> int` 和服务器命令 `m3/forecast_api.py dashboard`。

- [ ] **Step 1: 写失败测试覆盖参数、输出和退出码**

测试只接受一个参数 `dashboard`；成功 stdout 恰好一行；配置错误退出 3；原始源错误退出 4；NocoBase 读取错误退出 5；合同错误退出 6；未知错误退出 1；所有错误响应只包含安全 code/message。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_dashboard_cli -v`

Expected: FAIL，CLI 模块尚不存在。

- [ ] **Step 3: 实现薄入口和资源清理**

`m3.worker.dashboard_cli.main()` 使用 `json.dumps(..., allow_nan=False, separators=(",", ":"))`，并在 `finally` 中关闭两个 `httpx.Client`。根目录脚本只包含：

```python
#!/usr/bin/env python3
from m3.worker.dashboard_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行目标测试**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_dashboard_cli -v`

Expected: PASS。

### Task 4: iframe 登录认证与最后有效画面保留

**Files:**
- Modify: `场站未来能耗预测.html`
- Modify: `m3/tests/m3_dashboard_e2e.js`

**Interfaces:**
- Consumes: iframe HTML 区块在 `window.__NOCOBASE_API_TOKEN__` 注入当前 NocoBase API Token。
- Produces: 固定同源 GET，带 `Authorization: Bearer ...`，失败刷新保留最后成功内容。

- [ ] **Step 1: 修改 E2E 形成失败断言**

通过 `page.addInitScript()` 注入 `window.__NOCOBASE_API_TOKEN__ = "current-user-token"`，并断言首个请求只有固定 path、无 query/body/cookie，Authorization 精确为 `Bearer current-user-token`。把超时断言改为图表和预测值保持上一版，同时错误提示可见。

- [ ] **Step 2: 运行 E2E 确认失败**

Run: `node m3/tests/m3_dashboard_e2e.js`

Expected: FAIL，当前请求没有 Authorization 且失败时清空内容。

- [ ] **Step 3: 实现闭包 Token 和最后有效内容**

页面启动时将 `window.__NOCOBASE_API_TOKEN__` 校验为受限可打印字符串，复制到闭包后删除全局临时值。fetch headers 固定为：

```javascript
headers: {
  Accept: "application/json",
  Authorization: `Bearer ${apiToken}`,
}
```

增加 `hasRenderedDashboard`；首次加载失败使用完整错误态，已有成功画面后的刷新失败只显示错误条，不调用 `clearSection()`。

- [ ] **Step 4: 运行 E2E**

Run: `node m3/tests/m3_dashboard_e2e.js`

Expected: PASS。

### Task 5: systemd 与服务器目录工件

**Files:**
- Create: `m3/deploy/vifa-m3.service`
- Create: `m3/deploy/m3.env.example`
- Create: `m3/deploy/dashboard.env.example`
- Create: `m3/deploy/node-red-vifa-m3-dashboard.conf`
- Create: `m3/deploy/prepare-server.sh`
- Create: `m3/tests/test_m3_deployment_artifacts.py`

**Interfaces:**
- Produces: 单实例 UDS Worker、Node-RED Exec 子进程环境、幂等目录准备命令。

- [ ] **Step 1: 写失败测试锁定路径和进程边界**

断言 service 使用 `User=vifa-m3`、`WorkingDirectory=/userdata/holo/pyfiles/vifa-m3`、`RuntimeDirectory=vifa-m3`、`--workers 1`、`--uds /run/vifa-m3/worker.sock`、`Restart=on-failure`，且不存在 `--reload`、`--host` 或 `--port`。断言准备脚本只创建 M3 子目录，不 chown/chmod M2 根目录。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v`

Expected: FAIL，部署工件尚不存在。

- [ ] **Step 3: 创建部署工件**

Node-RED systemd drop-in 只增加：

```ini
[Service]
EnvironmentFile=-/etc/vifa-m3/dashboard.env
```

Worker service 的 ExecStart 固定使用 `/userdata/holo/pyfiles/vifa-m3/.venv/bin/python -m uvicorn m3.worker.main:app --workers 1 --uds /run/vifa-m3/worker.sock --no-access-log`。

- [ ] **Step 4: 运行目标测试**

Run: `.venv/bin/python -m unittest m3.tests.test_m3_deployment_artifacts -v`

Expected: PASS。

### Task 6: 当前双站 Node-RED Dashboard Flow

**Files:**
- Create: `m3/node_red/m3_dashboard_exec_flow.json`

**Interfaces:**
- Consumes: 当前用户 Authorization、`${M3_NOCOBASE_BASE_URL}/api/auth:check`、固定 CLI 命令。
- Produces: `GET /energy-forecast-api`。

- [ ] **Step 1: 创建独立 Flow**

Flow 固定为 HTTP In → Authorization 形状校验 → NocoBase `auth:check` → 当前用户对象校验 → Exec → 单行 JSON 解析 → HTTP Response。Exec 使用：

```text
/userdata/holo/pyfiles/vifa-m3/.venv/bin/python /userdata/holo/pyfiles/vifa-m3/m3-forecast-api.py dashboard
```

并固定 `addpay=false`、`useSpawn=false`、`timer=30`。浏览器 query/header/payload 均不能进入命令。

- [ ] **Step 2: 明确现场验证边界**

按用户要求不创建、不修改、不运行任何 Node-RED 自动化测试。Flow 仅完成代码审阅，实际 `auth:check` 响应合同、Exec 三输出行为和 HTTP 状态映射由服务器导入后联调。

### Task 7: 部署文档与 M3 回归

**Files:**
- Modify: `docs/m3/部署说明.md`
- Modify: `docs/m3/specs/2026-08-26-m3-systemd-worker-node-red-dashboard-design.md`

**Interfaces:**
- Produces: root 用户可执行的准备、上传、建 venv、配置、启动、健康检查、iframe 发布和回滚步骤。

- [ ] **Step 1: 更新部署说明**

写明不上传 `.venv`；服务器运行 `python3.12 -m venv .venv` 后安装锁定依赖；安装 service；Node-RED 加载 dashboard.env；通过 UDS curl 检查 `/health`；列出四张表新增验收指标字段及 ACL。

- [ ] **Step 2: 运行 M3 Python 与 HTML 回归**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_m3_*.py' -q`

Run: `node m3/tests/m3_dashboard_e2e.js`

Run: `.venv/bin/python -m compileall -q m3/worker m3/forecast_api.py`

Expected: 全部 PASS/exit 0。按用户要求不运行 `tests/test_m3_node_red_contract.js`，也不对新 Flow 做自动化测试。

- [ ] **Step 3: 安全与范围扫描**

Run: `rg -n 'storage_1_soc|storage_2_soc|solar_power|/opt/vifa-m3|--reload|addpay=true' m3.worker m3 m3/forecast_api.py '场站未来能耗预测.html'`

Expected: 只允许文档中明确说明的旧工件/禁止项描述；新生产代码、部署文件和新 Flow 无旧三序列或动态 Exec 命中。
