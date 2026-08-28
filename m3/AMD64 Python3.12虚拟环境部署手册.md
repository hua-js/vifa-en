# M3 AMD64 Python 3.12 虚拟环境部署手册

日期：2026-08-28  
目标平台：Ubuntu 20.04、x86_64 / AMD64  
生产代码目录：`/userdata/holo/pyfiles/vifa-m3`  
运行方式：宿主机隔离 Python 3.12、项目 `.venv`、双 systemd 服务、Unix Socket

本文是 M3 的非 Docker 生产部署手册。它保留已经确认的三域名职责和 Node-RED Unix Socket
接口，只把 M3 Worker/Dashboard 的运行载体从 Docker 改为宿主机虚拟环境。

> **互斥要求：** 本方案不得与 Docker 方案同时运行。执行本手册前，必须确认没有任何 M3
> Docker Worker 或 Dashboard 容器运行；切换回 Docker 前，也必须先停止并禁用本文的两个
> systemd 服务。

## 1. 架构与不变边界

```text
https://vifa.hlszh.com
  ├─ t_es_data:list 原始数据
  └─ M3 四表

同一台 AMD64 生产服务器
  ├─ Node-RED / https://opdash.lvkpower.com
  ├─ vifa-m3-worker.service
  │    └─ /userdata/holo/pyfiles/vifa-m3/run/worker.sock
  └─ vifa-m3-dashboard.service
       └─ /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock

https://ems.lvkpower.com
  └─ NocoBase 页面、当前用户和 GET /api/auth:check
```

本方案：

- 不修改 Ubuntu 自带的 Python 3.8.10；
- 不执行 `apt install python3.12`，也不替换 `/usr/bin/python3`；
- 使用 uv 托管的 Python 3.12 创建 `/userdata/holo/pyfiles/vifa-m3/.venv`；
- 不发布 M3 TCP 端口，不安装 Nginx；
- 不修改 M1/M2，不重建 M3 四表；
- 不重启 Node-RED 或 NocoBase。

uv 官方依据：

- [安装 uv](https://docs.astral.sh/uv/getting-started/installation/)
- [安装和管理 Python](https://docs.astral.sh/uv/guides/install-python/)
- [创建虚拟环境](https://docs.astral.sh/uv/pip/environments/)
- [锁文件同步](https://docs.astral.sh/uv/concepts/projects/sync/)

## 2. 开始前阻断检查

以下项目必须全部确认：

- [ ] 生产主机同时运行 Node-RED 和本文的 M3 服务；Unix Socket 不跨服务器；
- [ ] M3 最新代码已经上传到 `/userdata/holo/pyfiles/vifa-m3`；
- [ ] `pyproject.toml`、`uv.lock`、`m3_worker/` 和 `m3/deploy/` 均存在；
- [ ] 测试机器 M3 Docker 已由用户停止；
- [ ] 本生产主机没有 M3 Docker 容器运行；
- [ ] `/etc/vifa-m3` 中的生产凭据已经准备好或可在启动前填写；
- [ ] 已避开每小时 `02/17/32/47` 分、`00:30` 和 `01:02`；
- [ ] 已确认 Node-RED 的实际运行用户。

### 生产环境终端

```bash
uname -m
dpkg --print-architecture
python3 --version
systemctl --version
ps -eo user=,group=,pid=,args= | grep '[n]ode-red'
docker ps --format '{{.Names}} {{.Image}}' | grep -E 'vifa-m3|omnipower_vifa' || true
```

预期：

- `uname -m` 为 `x86_64`；
- `dpkg --print-architecture` 为 `amd64`；
- 系统 Python 可以仍为 3.8.10；
- 最后一条没有列出正在运行的 M3 容器；
- 记录 Node-RED 进程第一列的用户，后续用于 Socket 权限验证。

如果仍有 M3 Docker 容器运行，停止部署。不得让 Docker Worker 与 systemd Worker 同时写表。

## 3. 创建专用服务用户和目录

本节以 root 执行，只创建 M3 专用账号和目录。

### 生产环境终端

```bash
getent group vifa-m3 >/dev/null || groupadd --system vifa-m3
id -u vifa-m3 >/dev/null 2>&1 || useradd --system --gid vifa-m3 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin vifa-m3

install -d -o root -g root -m 0755 /userdata/holo/pyfiles/vifa-m3
install -d -o root -g root -m 0755 /userdata/holo/pyfiles/vifa-m3/runtime
install -d -o vifa-m3 -g vifa-m3 -m 0770 /userdata/holo/pyfiles/vifa-m3/run
install -d -o vifa-m3 -g vifa-m3 -m 0770 /userdata/holo/pyfiles/vifa-m3/cache
install -d -o vifa-m3 -g vifa-m3 -m 0770 /userdata/holo/pyfiles/vifa-m3/cache/numba
install -d -o root -g root -m 0700 /etc/vifa-m3
```

不要把整个 `/userdata/holo/pyfiles` 递归改属主或权限。

## 4. 安装独立 uv

使用 uv 官方独立安装器，把 uv 固定安装到 M3 目录，不修改 shell profile，也不依赖系统
Python。`UV_UNMANAGED_INSTALL` 会关闭该安装的自更新；后续升级 uv 必须由管理员重新执行并验证。

### 生产环境终端

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/vifa-m3-uv-install.sh
env UV_UNMANAGED_INSTALL=/userdata/holo/pyfiles/vifa-m3/runtime/bin sh /tmp/vifa-m3-uv-install.sh
/userdata/holo/pyfiles/vifa-m3/runtime/bin/uv --version
```

如果 `curl` 或 CA 证书不存在，只安装必要工具：

### 生产环境终端

```bash
apt-get update
apt-get install --yes ca-certificates curl
```

然后重新执行本节安装命令。不要用系统 Python 3.8 的 pip 安装 M3 依赖。

## 5. 安装托管 Python 3.12 和 `.venv`

官方等价命令是 `uv python install 3.12`、`uv venv --python 3.12` 和
`uv sync --frozen --no-dev`。本手册额外固定 Python、缓存和虚拟环境位置。

### 生产环境终端

```bash
cd /userdata/holo/pyfiles/vifa-m3

M3_UV_BIN=/userdata/holo/pyfiles/vifa-m3/runtime/bin/uv
M3_UV_PYTHON_DIR=/userdata/holo/pyfiles/vifa-m3/runtime/python
M3_UV_CACHE_DIR=/userdata/holo/pyfiles/vifa-m3/cache/uv

env UV_PYTHON_INSTALL_DIR="$M3_UV_PYTHON_DIR" UV_CACHE_DIR="$M3_UV_CACHE_DIR" "$M3_UV_BIN" python install 3.12
env UV_PYTHON_INSTALL_DIR="$M3_UV_PYTHON_DIR" UV_CACHE_DIR="$M3_UV_CACHE_DIR" "$M3_UV_BIN" venv --python 3.12 /userdata/holo/pyfiles/vifa-m3/.venv
env UV_PYTHON_INSTALL_DIR="$M3_UV_PYTHON_DIR" UV_CACHE_DIR="$M3_UV_CACHE_DIR" "$M3_UV_BIN" sync --frozen --no-dev
```

`--frozen` 保证使用仓库内 `uv.lock`，不会在生产机自动改锁文件。安装完成后验证：

### 生产环境终端

```bash
/userdata/holo/pyfiles/vifa-m3/.venv/bin/python --version
/userdata/holo/pyfiles/vifa-m3/.venv/bin/python -c 'import fastapi, numpy, pandas, pydantic, statsforecast, uvicorn; print(statsforecast.__version__)'
test "$(/userdata/holo/pyfiles/vifa-m3/.venv/bin/python -c 'import sys; print(sys.version_info[:2] == (3, 12))')" = True
```

预期 Python 为 3.12.x，StatsForecast 为 2.1.1。不得把其他机器的 `.venv` 直接复制到生产。

## 6. 配置生产凭据

### 6.1 原始数据 Token

非 Docker 服务直接读取 `/etc/vifa-m3/raw-source.token`。不要再使用容器路径
`/run/secrets/raw-source.token`。

如果安全上传文件位于 `/tmp/raw-source.token.upload`：

### 生产环境终端

```bash
install -o root -g vifa-m3 -m 0640 /tmp/raw-source.token.upload /etc/vifa-m3/raw-source.token
test -s /etc/vifa-m3/raw-source.token
stat -c '%n %U:%G %a %s bytes' /etc/vifa-m3/raw-source.token
```

不要使用 `cat`、`head` 或 Debug 节点输出 Token。

### 6.2 Worker 配置

编辑 `/etc/vifa-m3/m3.env`：

### 生产环境终端

```bash
nano /etc/vifa-m3/m3.env
```

结构必须为：

```text
M3_STATIONS_JSON='[{"station_id":"<完整ES01>","station_key":"station_1","station_name":"1# 电站"},{"station_id":"<完整ES02>","station_key":"station_2","station_name":"2# 电站"}]'
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/etc/vifa-m3/raw-source.token
M3_SOURCE_BASE_URL=https://opdash.lvkpower.com
M3_SOURCE_API_TOKEN=<Source控制面凭据>
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_API_KEY=<Worker四表写入Key>
M3_ADMIN_API_TOKEN=<Worker管理Key>
M3_ACCEPTANCE_ENABLED=false
M3_TIMEZONE=Asia/Shanghai
```

### 6.3 Dashboard 配置

编辑 `/etc/vifa-m3/dashboard.env`：

### 生产环境终端

```bash
nano /etc/vifa-m3/dashboard.env
```

结构必须为：

```text
M3_STATIONS_JSON='[{"station_id":"<完整ES01>","station_key":"station_1","station_name":"1# 电站"},{"station_id":"<完整ES02>","station_key":"station_2","station_name":"2# 电站"}]'
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/etc/vifa-m3/raw-source.token
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_DASHBOARD_NOCOBASE_API_KEY=<Dashboard四表只读Key>
M3_TIMEZONE=Asia/Shanghai
```

两个文件的 `M3_STATIONS_JSON` 必须完全相同。第一阶段必须保持
`M3_ACCEPTANCE_ENABLED=false`。

### 6.4 权限和占位符阻断

### 生产环境终端

```bash
chown root:root /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
chmod 0600 /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
chown root:vifa-m3 /etc/vifa-m3/raw-source.token
chmod 0640 /etc/vifa-m3/raw-source.token

if grep -q 'REPLACE_\|<完整\|<Source\|<Worker\|<Dashboard' /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env; then echo 'ERROR：配置仍有占位符'; exit 1; fi
grep -q '^M3_ACCEPTANCE_ENABLED=false$' /etc/vifa-m3/m3.env || { echo 'ERROR：验收开关必须为 false'; exit 1; }
for M3_ENV_FILE in /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env; do grep -q '^M3_RAW_SOURCE_API_TOKEN_FILE=/etc/vifa-m3/raw-source.token$' "$M3_ENV_FILE" || { echo "ERROR：$M3_ENV_FILE 的宿主机 Token 路径错误"; exit 1; }; done
```

不要执行会打印完整环境变量的命令。

## 7. 配置 Node-RED 的 Socket 访问权限

如果第 2 节确认 Node-RED 以 root 运行，它可以访问 M3 Socket，不需要额外修改。

如果 Node-RED 以非 root 用户运行，不要把 `run/` 改成 0777，也不要为了加入新用户组而重启
Node-RED。使用 ACL 给现有进程用户授予目录和新建 Socket 的权限。把下面值替换为实际用户：

### 生产环境终端

```bash
apt-get update
apt-get install --yes acl

M3_NODE_RED_USER='<Node-RED实际运行用户>'
id "$M3_NODE_RED_USER"
setfacl -m "u:${M3_NODE_RED_USER}:rwx" /userdata/holo/pyfiles/vifa-m3/run
setfacl -d -m "u:${M3_NODE_RED_USER}:rwx" /userdata/holo/pyfiles/vifa-m3/run
getfacl /userdata/holo/pyfiles/vifa-m3/run
```

ACL 对现有 Node-RED 进程立即生效，不需要重启 Node-RED。

## 8. 安装双 systemd 服务

先确认宿主机没有 M3 Docker 容器，也没有旧的单 Worker `vifa-m3.service` 在运行。

### 生产环境终端

```bash
systemctl disable --now vifa-m3.service 2>/dev/null || true

install -o root -g root -m 0644 /userdata/holo/pyfiles/vifa-m3/m3/deploy/vifa-m3-worker.service /etc/systemd/system/vifa-m3-worker.service
install -o root -g root -m 0644 /userdata/holo/pyfiles/vifa-m3/m3/deploy/vifa-m3-dashboard.service /etc/systemd/system/vifa-m3-dashboard.service
chmod 0755 /userdata/holo/pyfiles/vifa-m3/m3/deploy/host-entrypoint.sh

systemd-analyze verify /etc/systemd/system/vifa-m3-worker.service /etc/systemd/system/vifa-m3-dashboard.service
systemctl daemon-reload
```

`systemctl daemon-reload` 只让 systemd 读取新的 M3 Unit，不会重启 Node-RED。

## 9. 首次启动

再次确认“测试机器 M3 Docker 已停止”和“生产不存在 M3 Docker 容器”后启动：

### 生产环境终端

```bash
systemctl enable --now vifa-m3-worker.service vifa-m3-dashboard.service
systemctl status --no-pager vifa-m3-worker.service vifa-m3-dashboard.service
```

Worker 首次拉取历史和初始化模型可能比 Dashboard 慢。不要用固定睡眠判断成功，以 Socket 和
健康接口为准。

### 生产环境终端

```bash
curl --fail --silent --show-error --unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock http://localhost/health
curl --fail --silent --show-error --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock http://localhost/health
```

如果 Node-RED 是非 root 用户，再以该用户验证 Dashboard Socket：

### 生产环境终端

```bash
M3_NODE_RED_USER='<Node-RED实际运行用户>'
sudo -u "$M3_NODE_RED_USER" curl --fail --silent --show-error --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock http://localhost/health
```

## 10. Node-RED 与 NocoBase 人工接入

Node-RED 继续使用 `m3/node_red/m3_production_gateway_flow.json`，其中固定调用同机
`dashboard.sock` 和 `worker.sock`，无需修改 Flow。

人工步骤：

1. 从 Node-RED 编辑器导出当前 Flow 备份；
2. 导入 `m3/node_red/m3_production_gateway_flow.json`；
3. 确认 `/ett` 和 `/energy-forecast-api` 各只有一个启用路由；
4. 选择 **Deploy → Modified Nodes**；
5. 不重启 Node-RED；
6. 在 `https://ems.lvkpower.com` 创建 JavaScript 区块，粘贴
   `m3/nocobase/m3_forecast_iframe_block.production.js`；
7. 使用普通登录用户完成 `https://ems.lvkpower.com/api/auth:check` 和页面端到端检查。

Flow 环境变量保持：

```text
M3_AUTH_MODE=postmessage
M3_AUTH_BASE_URL=https://ems.lvkpower.com
M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com
```

## 11. 日志和故障排查

### 生产环境终端

```bash
journalctl -u vifa-m3-worker.service -n 100 --no-pager
journalctl -u vifa-m3-dashboard.service -n 100 --no-pager
systemctl show -p ActiveState -p SubState -p MainPID vifa-m3-worker.service vifa-m3-dashboard.service
ls -l /userdata/holo/pyfiles/vifa-m3/run/worker.sock /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock
```

常见阻断：

| 现象 | 优先检查 |
|---|---|
| 服务报 Python 缺失 | `.venv/bin/python --version` 和第 5 节 |
| 服务报 Token 文件不可读 | `/etc/vifa-m3/raw-source.token` 是否 `root:vifa-m3 0640` |
| Node-RED 报 Socket permission denied | 第 7 节 ACL 和以 Node-RED 用户执行的 curl |
| Node-RED 返回 502 | Dashboard 服务、`dashboard.sock` 和 Dashboard Key |
| Worker 不写新批次 | Worker 日志、双站绑定、原始 Token、Worker 写 Key |
| 配置启动失败 | 是否仍有占位符，是否缺少 `M3_ACCEPTANCE_ENABLED=false` |

日志不得包含 Token、完整 Authorization、env 内容或 NocoBase Key。

## 12. 代码和依赖升级

升级前先备份代码并确认没有切换到 Docker 方案。

### 生产环境终端

```bash
systemctl stop vifa-m3-worker.service vifa-m3-dashboard.service
```

上传并展开新代码后，使用同一 uv 和托管 Python 重新同步：

### 生产环境终端

```bash
cd /userdata/holo/pyfiles/vifa-m3

M3_UV_BIN=/userdata/holo/pyfiles/vifa-m3/runtime/bin/uv
M3_UV_PYTHON_DIR=/userdata/holo/pyfiles/vifa-m3/runtime/python
M3_UV_CACHE_DIR=/userdata/holo/pyfiles/vifa-m3/cache/uv

env UV_PYTHON_INSTALL_DIR="$M3_UV_PYTHON_DIR" UV_CACHE_DIR="$M3_UV_CACHE_DIR" "$M3_UV_BIN" sync --frozen --no-dev
systemd-analyze verify /etc/systemd/system/vifa-m3-worker.service /etc/systemd/system/vifa-m3-dashboard.service
systemctl start vifa-m3-worker.service vifa-m3-dashboard.service
```

升级后重新执行两个 Socket `/health` 检查和一次普通用户页面验证。

## 13. 回滚

### 13.1 回滚本次 systemd 发布

### 生产环境终端

```bash
systemctl stop vifa-m3-worker.service vifa-m3-dashboard.service
systemctl disable vifa-m3-worker.service vifa-m3-dashboard.service
```

恢复部署前代码备份，重新执行第 5 节依赖同步和第 8 节 Unit 安装，然后仅在配置验证通过后启动。

如果回滚 Node-RED/NocoBase：

1. 在 Node-RED 编辑器停用本次 M3 Flow并恢复备份，选择 **Deploy → Modified Nodes**；
2. 在 NocoBase 停用本次 M3 JavaScript 区块；
3. 不重启 Node-RED，不删除 M3 四表和历史预测。

### 13.2 切换回 Docker

必须先执行并确认：

### 生产环境终端

```bash
systemctl disable --now vifa-m3-worker.service vifa-m3-dashboard.service
systemctl is-active vifa-m3-worker.service vifa-m3-dashboard.service
```

两个服务都应为 inactive，之后才能按 Docker 手册操作。本文不会自动启动 Docker，也不会处理
测试机器。

## 14. 完成记录

保存以下证据：

- `uname -m`、Python 3.12.x 和 StatsForecast 2.1.1 结果；
- `uv --version`、代码包校验值和 `uv.lock` 校验值；
- 两个 Unit 的 `systemd-analyze verify` 结果；
- 两个 systemd 服务状态和 Socket `/health` 结果；
- Node-RED 运行用户及必要时的 ACL 验证结果；
- Node-RED **Modified Nodes** 部署记录；
- 普通登录用户端到端验证结果；
- 两个电站首个成功预测批次时间和批次标识。

本手册只描述人工操作；编写和验证文档不会自动连接或修改生产服务器。
