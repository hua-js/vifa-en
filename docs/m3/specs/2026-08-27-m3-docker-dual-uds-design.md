# M3 Docker 双容器双 Unix Socket 部署设计

日期：2026-08-27\
状态：用户已确认，实施中\
目标测试机：Ubuntu 20.04、ARM64、Docker 27.3.1、Docker Compose 2.29.7

## 1. 背景与现场事实

测试机是全新 M3 环境，没有 M1、M2 或既有 M3 文件。目标目录
`/userdata/holo/pyfiles/vifa-m3` 当前不存在，将在部署时新建并从本地工作区完整上传。

宿主机只有 Python 3.8.10，不能运行要求 Python 3.12 的 M3。NocoBase 运行在 Docker
容器中并使用 host 网络；Node-RED 并不在 Docker 中，而是由宿主机
`nodered.service` 以 root 用户运行，监听 1880。测试机为 ARM64，约有 6.2 GiB
可用内存和 89 GiB 可用磁盘。

## 2. 目标与非目标

目标：

- 用 ARM64 兼容的 Python 3.12 容器运行全部 M3 Python 代码；
- 保留两个电站、`station_total_load` 和 `storage_soc` 两条独立序列；
- 保留 StatsForecast、NocoBase HTTP 写入、连续七天基线与六项指标；
- 保留 NocoBase 当前用户鉴权和同源 `/energy-forecast-api`；
- 不发布新的 TCP 端口，不依赖宿主机 Python，不向 Node-RED 暴露 Docker Socket；
- Worker 与 Dashboard 使用不同凭据和独立故障边界。

非目标：

- 不修改 NocoBase 四表及已建立的唯一索引；
- 不修改 iframe HTML 的数据合同；
- 不在本次工作中升级 Ubuntu、Docker、NocoBase 或 Node-RED；
- 不创建 M1/M2 兼容层；
- 不运行 Node-RED 自动化测试，仍以现场联调为准。

## 3. 方案选择

采用两个常驻容器和两个 Unix Socket：

```text
NocoBase iframe
  -> 宿主机 Node-RED :1880
     -> NocoBase auth:check
     -> 固定 curl --unix-socket dashboard.sock
        -> vifa-m3-dashboard
           -> HTTP 只读 NocoBase 已保存预测/验收
           -> HTTP 只读最近 24h 实绩

vifa-m3-worker
  -> HTTP 拉取两站历史/实绩
  -> StatsForecast 选模与预测
  -> HTTP 写 NocoBase 四表
```

两个容器使用同一个不可变镜像，但以不同命令、环境文件和非 root 用户启动。
Worker 监听容器内 `/run/vifa-m3/worker.sock`，Dashboard 监听
`/run/vifa-m3/dashboard.sock`。宿主目录
`/userdata/holo/pyfiles/vifa-m3/run` 分别挂载到两个容器的 `/run/vifa-m3`，使宿主机
Node-RED 可以通过固定路径访问 Dashboard Socket。

## 4. 容器与进程

### 4.1 公共镜像

- 基础镜像：`python:3.12-slim-bookworm`，Docker 自动拉取 ARM64 变体；
- 依赖：从 `m3/deploy/requirements.lock.txt` 使用 `--require-hashes` 安装；
- 应用路径：`/app`；
- 最终运行用户：固定非 root UID/GID；
- 镜像不包含 env、Token、`.local/密钥.txt`、venv、Git 元数据或测试缓存；
- Compose 构建一次，两个服务复用同一镜像。

### 4.2 `vifa-m3-worker`

- 命令：单 Uvicorn worker，加载 `m3.worker.main:app`；
- 监听：`/run/vifa-m3/worker.sock`；
- 配置：只加载 Worker 写权限环境和原始源 Token 文件；
- 权限：无 Docker Socket、无宿主机 Python、无宿主机 TCP 端口；
- 生命周期：Compose `restart: unless-stopped`；
- 健康检查：通过 Worker Socket 请求 `/health`。

### 4.3 `vifa-m3-dashboard`

- 新增导入安全的 FastAPI 应用，复用现有 `DashboardSettings`、
  `PersistedDashboardService` 和 Dashboard JSON 合同；
- 监听：`/run/vifa-m3/dashboard.sock`；
- 接口：固定 `GET /dashboard`，无 query、无 body；
- 每次请求只读 NocoBase 和最近 24 小时原始实绩，不运行模型、不写表；
- 配置：只加载 Dashboard 只读 Key 和原始源只读 Token；
- 健康检查：固定 `GET /health`，不访问外部依赖；
- 不提供 Swagger、Redoc、OpenAPI 或管理接口。

## 5. Node-RED 边界

现有 Flow 的浏览器入口保持 `GET /energy-forecast-api`：

1. 拒绝 query、body 和异常 Authorization；
2. 用当前用户 Bearer 调用固定 NocoBase `/api/auth:check`；
3. 只接受响应中正整数 `data.id`；
4. 删除当前用户 Token；
5. 执行固定命令：

```text
/usr/bin/curl --fail --silent --show-error \
  --connect-timeout 2 --max-time 30 \
  --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock \
  http://localhost/dashboard
```

浏览器输入不得影响命令、Socket、目标路径、collection、station、filter 或 sort。
stderr 不返回浏览器。Node-RED 不执行 `docker`、`docker exec`、Python、模型命令或容器重启。

Node-RED 的真实 systemd unit 是 `nodered.service`，所以环境 drop-in 必须使用
`/etc/systemd/system/nodered.service.d/`，不能使用旧设计中的
`node-red.service.d/`。Node-RED 只加载一个新的最小配置文件，其中只有
`M3_NOCOBASE_BASE_URL`，不再加载 Dashboard Key 或原始源 Token。

## 6. 网络和凭据

两个服务不配置 `ports`。Unix Socket 是 Node-RED 到 M3 的唯一入站边界。容器保留必要的
HTTP/HTTPS 出站访问，以读取原始数据和访问 NocoBase。

现场构建证明测试机内核缺少 veth 和 POSIX mqueue，默认 Docker bridge 和默认 OCI
`/dev/mqueue` 挂载均不可用；现有三个容器也都使用 host 网络并将 `/dev/mqueue` 覆盖为
tmpfs。因此 M3 构建/运行使用 host 网络，并以 tmpfs 覆盖 `/dev/mqueue`。M3 Uvicorn
仍只监听 Unix Socket，不新增 TCP 监听。容器访问宿主机 NocoBase 时使用 `127.0.0.1`。

配置仍放在宿主机 `/etc/vifa-m3`。Compose 通过 `env_file` 读取两个环境文件，原始
Token 作为只读文件挂载：

- Worker：`m3.env`、`raw-source.token`；
- Dashboard：`dashboard.env`、`raw-source.token`；
- Node-RED：单独的 `nodered.env`，只包含 NocoBase 固定 origin；
- Worker 写 Key 不进入 Dashboard 容器；
- Dashboard 只读 Key 不作为浏览器响应或 HTML 变量；
- 当前用户 Bearer 只存在于 Node-RED 鉴权链路，不传入 Dashboard 容器。

既有 `M3_SOURCE_BASE_URL` 验收上下文和告警合同保持不变。普通预测联调只依赖原始源与
NocoBase；正式启动每日 01:02 七天验收前，必须确认 acceptance-context 和 alerts 路由可用。

## 7. 文件改动

新增：

- 根目录 `m3/deploy/Dockerfile`；
- 根目录 `m3/deploy/compose.yaml`；
- `.dockerignore`；
- `m3/worker/persisted_dashboard_app.py`；
- `m3/deploy/nodered.env.example`；
- Docker 部署/健康检查测试。

修改：

- `m3/node_red/m3_dashboard_exec_flow.json`：Python Exec 改为固定 Socket curl；
- `m3/deploy/*`：新增 Docker 所需目录和修正 `nodered.service` drop-in；
- `docs/m3/部署说明.md`：以 Docker Compose 全新部署为主流程；
- Dashboard API、配置和部署合同测试。

不修改预测算法、StatsForecast 模型、NocoBase 四表、HTML、M1 或 M2。

## 8. 启动、验证与回滚

部署顺序：创建目录与配置、上传代码、构建镜像、启动两个服务、验证两个 Socket、安装
Node-RED drop-in、导入 Flow、联调同源接口。首次构建必须验证 ARM64 wheel 安装成功和
StatsForecast 版本为 2.1.1。

验证包括：

- 本地只验证本次修改的 Dashboard API、Compose、m3/deploy/Dockerfile、部署脚本和 Flow JSON；
- 测试机 `docker compose build` 和容器健康状态；
- Worker `/health`、Dashboard `/health`、Dashboard `/dashboard`；
- NocoBase 表写入和只读结果；
- Node-RED 现场鉴权、错误码和超时联调；
- iframe 展示两个站并保留最后一次成功结果。

回滚只停止并移除 M3 Compose 服务、恢复上一份 M3 Flow；不删除 NocoBase 数据，不修改
NocoBase/Node-RED 容器和现有数据库。由于测试机此前没有 M3，首次回滚等价于停止并移除
M3 容器和专用目录，配置凭据保留供排查，除非用户明确要求删除。

## 9. 完成条件

- 两个容器均健康且重启后自动恢复；
- 宿主机没有 M3 Python/venv 依赖；
- 没有新 TCP 监听端口；
- Node-RED 不接触 Docker Socket；
- Worker 能为两个站独立写入最新预测；
- Dashboard 能通过 Node-RED 返回两站预测和验收信息；
- 正式验收启用前，七天上下文/告警依赖通过现场检查。
