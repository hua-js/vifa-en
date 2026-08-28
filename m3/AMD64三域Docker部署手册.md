# M3 AMD64 三域 Docker 部署手册

> 这是唯一现行生产部署手册。M3 已于 2026-08-28 按本文架构部署；后续仅将本文用于
> 重部署、升级、检查和回退。

适用架构：

```text
数据源、四个 M3 结果/明细集合与验收控制/汇总表：https://vifa.hlszh.com
NocoBase 页面与普通 iframe：https://ems.lvkpower.com
Node-RED 页面与 API：https://opdash.lvkpower.com
Docker 主机：Linux AMD64，与 Node-RED 同机
部署目录：/userdata/holo/pyfiles/vifa-m3
```

本次不修改四个既有 M3 结果/明细集合、M1/M2 或反向代理，也不重启 Node-RED。

## 1. 服务器前置检查

```bash
uname -m
docker --version
docker compose version
```

`uname -m` 必须输出 `x86_64`。确认 Node-RED 与 M3 Docker 在同一台主机，Node-RED
运行用户能访问 `/userdata/holo/pyfiles/vifa-m3/run`。

## 2. 上传与解压

```bash
mkdir -p /userdata/holo/pyfiles/vifa-m3
tar -xzf /tmp/vifa-m3-code_0.1.0.tar.gz \
  -C /userdata/holo/pyfiles/vifa-m3
cd /userdata/holo/pyfiles/vifa-m3
```

代码至少包含：

```text
Dockerfile
compose.yaml
pyproject.toml
uv.lock
m3_worker/
m3/requirements.lock.txt
m3/deploy/container-entrypoint.sh
m3/node_red/m3_production_gateway_flow.json
m3/nocobase/m3_nocobase_iframe_manifest.json
```

## 3. 镜像

已有 AMD64 镜像归档时：

```bash
docker load -i /tmp/vifa-m3_0.1.0_linux-amd64.tar.gz
```

也可以直接在 AMD64 生产机使用代码构建：

```bash
docker build --platform linux/amd64 -t vifa-m3:0.1.0 .
```

确认：

```bash
docker image inspect vifa-m3:0.1.0 \
  --format '{{.Os}}/{{.Architecture}}'
```

必须输出 `linux/amd64`。

## 4. 运行目录和原始源 Token

```bash
mkdir -p /userdata/holo/pyfiles/vifa-m3/run /etc/vifa-m3
chown 10001:10001 /userdata/holo/pyfiles/vifa-m3/run
chmod 700 /userdata/holo/pyfiles/vifa-m3/run
vi /etc/vifa-m3/raw-source.token
```

`raw-source.token` 只保存 Token 本身，不加 `Bearer `、引号或变量名：

```bash
chown root:10001 /etc/vifa-m3/raw-source.token
chmod 640 /etc/vifa-m3/raw-source.token
```

## 5. Worker 配置

```bash
cp m3/deploy/m3.env.example /etc/vifa-m3/m3.env
vi /etc/vifa-m3/m3.env
```

固定地址：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/raw-source.token
M3_SOURCE_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_ACCEPTANCE_ENABLED=false
M3_TIMEZONE=Asia/Shanghai
```

填写两个真实完整电站 ID、四个结果/明细集合写入 Key 和验收任务最小权限 Key。`M3_SOURCE_API_TOKEN` 与
`M3_ADMIN_API_TOKEN` 当前可分别使用 `openssl rand -hex 32` 生成不同随机值，以满足启动合同。

## 6. Dashboard 配置

```bash
cp m3/deploy/dashboard.env.example /etc/vifa-m3/dashboard.env
vi /etc/vifa-m3/dashboard.env
```

固定地址：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/raw-source.token
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_TIMEZONE=Asia/Shanghai
```

`M3_STATIONS_JSON` 必须与 Worker 完全相同；填写四个结果/明细集合只读 Key。

```bash
chown root:root /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
chmod 600 /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
grep -n 'REPLACE_' /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
```

最后一条命令正常时没有输出。

## 7. 启动两个容器

```bash
cd /userdata/holo/pyfiles/vifa-m3
docker compose config -q
docker compose up -d --no-build
docker compose ps
```

同一个 `vifa-m3:0.1.0` 镜像会运行 `vifa-m3-worker` 和 `vifa-m3-dashboard` 两个容器。

## 8. 检查 Socket 和日志

```bash
ls -l run/worker.sock run/dashboard.sock
curl --fail --silent --show-error \
  --unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock \
  http://localhost/health
curl --fail --silent --show-error \
  --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock \
  http://localhost/health
docker compose logs --tail=100 vifa-m3-worker
docker compose logs --tail=100 vifa-m3-dashboard
```

如果 Node-RED 运行在另一个容器中，它必须已经把宿主机 `run` 目录映射为同一路径。不能通过
放开整个部署目录权限解决 Socket 访问问题。

## 9. 人工导入 Node-RED

只导入 `m3/node_red/m3_production_gateway_flow.json`。导入前搜索并停用旧的同名 `/ett` 和
`/energy-forecast-api` 路由。确认 Flow 变量：

```text
M3_AUTH_MODE=server_token
M3_AUTH_BASE_URL=https://ems.lvkpower.com
M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com
```

确认两个 Exec 命令固定访问 `/userdata/holo/pyfiles/vifa-m3/run/*.sock`，然后选择
`Deploy Modified Flows`；不得重启 Node-RED。

后续仅启用连续七日验收时，不需要重新导入 `m3_production_gateway_flow.json`；生产 Node-RED 保持不变。

## 10. 人工配置 NocoBase iframe

在 `https://ems.lvkpower.com` 创建普通 iframe/HTML 区块：

```text
标题：场站未来能耗预测
URL：https://opdash.lvkpower.com/ett
Header：不配置
```

该模式为公开只读：任何能访问 OPDash 地址的人都能查看两站预测数据。不得把 Dashboard
只读 Key、管理员 Token或其他凭据写入 iframe URL。详细步骤见
`m3/nocobase/M3普通iframe配置说明.md`。

## 11. 人工验收

1. 无 Header 访问 `https://opdash.lvkpower.com/ett` 应返回 200；
2. 从 EMS 页面打开普通 iframe；
3. 浏览器 Network 中 `/energy-forecast-api` 返回 200；
4. 浏览器请求不携带 Token，也不调用 EMS `/api/auth:check`；
5. 页面显示 1#、2# 电站及各自总负荷和 SOC；
6. 浏览器没有 Mixed Content、CSP、X-Frame-Options 或 CORS 错误；
7. Node-RED 和 Docker 日志没有 Token。

## 12. 回退

1. 停用 NocoBase M3 iframe；
2. 停用新 M3 Node-RED Flow并发布 Modified Flows；
3. 在部署目录执行 `docker compose stop`。

不要删除四个 M3 结果/明细集合、验收任务表、预测历史、env 或 Token 文件。

## 13. 连续七日验收生产启用

四个既有结果/明细集合为 `energy_forecast_latest`、`energy_forecast_batches`、`energy_forecast_points`、`energy_forecast_evaluations`。第五个集合 `energy_forecast_acceptance_runs` 是控制/汇总表，不重复 metrics；详细证据仍保留在 batches、points、evaluations。

操作员创建任务身份、窗口和控制字段，Worker 仅更新汇总字段。以下是示例，不是生产 ID：

```text
station_id:          ES01-FULL-ID-EXAMPLE
acceptance_run_id:   acceptance-20260829-station1
window_start:        2026-08-29T01:00:00+08:00
window_end:          2026-09-05T01:00:00+08:00
control_state:       active
completed_days:      0
result_state:        pending
calculated_at:       留空
```

日期仅为示例。每个真实任务从上海时间 01:00 开始，恰好七天后结束，并必须在首日 01:02 调度槽之前创建。

在 NocoBase 角色 UI 中核验更新权限，不得以修改生产任务作部署探针。以下读取探针只过滤指定站和 `control_state=active`，并只请求获准字段：

```bash
read -rsp 'Worker NocoBase token: ' M3_PROBE_TOKEN
echo
read -rp 'Full station ID: ' M3_STATION_ID
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${M3_PROBE_TOKEN}" \
  --get 'https://vifa.hlszh.com/api/energy_forecast_acceptance_runs:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"control_state\":\"active\"}" \
  --data-urlencode 'fields=id,station_id,acceptance_run_id,window_start,window_end,control_state,completed_days,result_state,calculated_at' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
unset M3_PROBE_TOKEN M3_STATION_ID
```

按此顺序启用：

1. 核验表约束和 Worker 角色权限。
2. 在 `M3_ACCEPTANCE_ENABLED=false` 下部署新镜像。
3. 为每个需要启用的电站创建一个活动任务。
4. 核验每个活动/空电站查询。
5. 在 `/etc/vifa-m3/m3.env` 中设置 `M3_ACCEPTANCE_ENABLED=true`。
6. 在首个 01:02 槽之前只重建 `vifa-m3-worker`。
7. 在 01:02 后检查首个完整 batch 和任务汇总。
8. 在最终实绩回填和评估完成前保持 `control_state=active`。
9. 仅在 Dashboard 显示终态 7/7 结果后将任务标记为 `completed`。

## 14. 验证范围

本地交付只执行静态 JSON、Compose 和脚本检查；没有启动生产容器，也没有执行自动化或端到端
测试。上线后的 Socket、公开路由、iframe 页面和两站数据由人工确认。
