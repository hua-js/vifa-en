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
m3/deploy/Dockerfile
m3/deploy/compose.yaml
pyproject.toml
uv.lock
m3/worker/
m3/deploy/requirements.lock.txt
m3/deploy/container-entrypoint.sh
m3/node_red/m3_production_gateway_flow.json
m3/node_red/m3_production_gateway_template.html
docs/m3/nocobase/M3普通iframe配置说明.md
```

## 3. 镜像

已有 AMD64 镜像归档时：

```bash
docker load -i /tmp/vifa-m3_0.1.0_linux-amd64.tar.gz
```

也可以直接在 AMD64 生产机使用代码构建：

```bash
docker build -f m3/deploy/Dockerfile --platform linux/amd64 -t vifa-m3:0.1.0 .
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

填写两个真实完整电站 ID 和一个专用 `M3_NOCOBASE_API_KEY`。该 Worker Key 的单一角色合并四个结果/明细集合既有权限、批次/评估限定读取及验收任务汇总更新权限，不另配验收任务 Key。`M3_SOURCE_API_TOKEN` 与
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
docker compose -f m3/deploy/compose.yaml config -q
docker compose -f m3/deploy/compose.yaml up -d --no-build
docker compose -f m3/deploy/compose.yaml ps
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
docker compose -f m3/deploy/compose.yaml logs --tail=100 vifa-m3-worker
docker compose -f m3/deploy/compose.yaml logs --tail=100 vifa-m3-dashboard
```

如果 Node-RED 运行在另一个容器中，它必须已经把宿主机 `run` 目录映射为同一路径。不能通过
放开整个部署目录权限解决 Socket 访问问题。

## 9. 人工导入 Node-RED

只导入 `m3/node_red/m3_production_gateway_flow.json`。导入前搜索并停用旧的同名 `/ett`、
`/energy-forecast-api`、`/energy-forecast-api/custom-runs/*` 和
`/energy-forecast-api/custom-performance/*` 路由。

普通 iframe 通过当前用户 Token 访问，确认 Flow 变量：

```text
M3_AUTH_MODE=query_token
M3_AUTH_BASE_URL=https://ems.lvkpower.com
M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com
M3_STATIONS_JSON=<与 Worker 完全相同的两站 JSON>
```

从 Worker 环境安全生成 Node-RED 专用令牌文件，命令不会输出令牌：

```bash
sudo /bin/sh -c '
set -a
. /etc/vifa-m3/m3.env
set +a
umask 077
printf %s "$M3_ADMIN_API_TOKEN" > /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token
chown 10001:10001 /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token
chmod 600 /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token
'
docker exec nodered test -r /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token
```

`.worker-admin.token` 只允许保存在服务端共享运行目录中，不得写入 HTML、浏览器脚本、Node-RED Flow、
URL、命令行参数或仓库。更新 `/etc/vifa-m3/m3.env` 中的 `M3_ADMIN_API_TOKEN` 后，必须重新生成该文件。
`query_token` 模式从 iframe URL 接收当前用户 Token，API 请求使用 Bearer Header。Node-RED 校验当前用户后，
通过固定 UDS、固定方法和白名单参数调用 Worker。`M3_AUTH_BASE_URL` 必须指向签发 `ctx.token` 的 NocoBase；
默认是 EMS。保留 `postmessage` 兼容模式；显式设为 `server_token` 会恢复原公开访问模式。

确认两个 Exec 命令固定访问 `/userdata/holo/pyfiles/vifa-m3/run/*.sock`，然后选择
`Deploy Modified Flows`；不得重启 Node-RED。

后续仅启用连续七日验收时不需要重新导入；启用本次自定义预测接口时必须导入新版
`m3_production_gateway_flow.json` 并选择 `Deploy Modified Flows`，不得重启 Node-RED。

完整导入 Flow 后，使用 `m3/node_red/m3_production_gateway_template.html` 覆盖
`m3_prod_page_template` 节点。仅更新页面时直接替换该 HTML，不更新仓库中的 Flow JSON。

## 10. 人工配置 NocoBase iframe

在 `https://ems.lvkpower.com` 创建普通 iframe/HTML 区块：

```text
标题：场站未来能耗预测
URL：https://opdash.lvkpower.com/ett?token={{ ctx.token }}
Header：不配置
```

`{{ ctx.token }}` 应由 NocoBase 解析成当前登录用户 Token，不能手工粘贴固定管理员 Token。页面读取后清除
自身 URL 参数，失效或直接刷新 iframe 后需从 NocoBase 重新打开。入口访问日志应隐藏 token 参数。
Dashboard 只读 Key 和 Worker 管理令牌不得写入 iframe URL。详细步骤见
`docs/m3/nocobase/M3普通iframe配置说明.md`。

## 11. 人工验收

1. 无 Token 访问 `https://opdash.lvkpower.com/ett` 应返回 401；
2. 从 EMS 页面打开普通 iframe；
3. 浏览器 Network 中 `/energy-forecast-api` 返回 200；
4. API 请求携带 Bearer Token；由 Node-RED 向 EMS `/api/auth:check` 校验；
5. 选择有效参数后“开始预测”可用；无效或过期 Token 不得访问看板或提交任务；
6. 页面显示 1#、2# 电站及各自总负荷和 SOC；
7. 浏览器没有 Mixed Content、CSP、X-Frame-Options 或 CORS 错误；
8. Node-RED 和 Docker 日志没有 Token。

## 12. 回退

1. 停用 NocoBase M3 iframe；
2. 停用新 M3 Node-RED Flow并发布 Modified Flows；
3. 在部署目录执行 `docker compose -f m3/deploy/compose.yaml stop`。

不要删除四个 M3 结果/明细集合、验收任务表、预测历史、env 或 Token 文件。

## 13. 连续七日验收生产启用

四个既有结果/明细集合为 `energy_forecast_latest`、`energy_forecast_batches`、`energy_forecast_points`、`energy_forecast_evaluations`。第五个集合 `energy_forecast_acceptance_runs` 是控制/汇总表，不重复 metrics；详细证据仍保留在 batches、points、evaluations。

操作员创建并拥有任务身份、窗口和控制字段；Worker 列出任务行，且只能更新 `completed_days`、`result_state` 和 `calculated_at` 三个汇总字段。同一个专用 Worker Key 承担既有明细写入、新增批次/评估限定读取和任务汇总更新。以下是示例，不是生产 ID：

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

以下探针用同一个 Worker Key 覆盖任务、完整批次、`writing` 恢复批次和评估的固定读取合同。Authorization 头从 stdin 传给 curl，不出现在 curl argv。不得用生产记录测试写入；在 NocoBase 角色 UI 中核验同一 Key 只有：latest `list/updateOrCreate`、batches `list/update/firstOrCreate`、points `list/update/firstOrCreate`、evaluations `list/updateOrCreate`、acceptance runs `list/update`，并逐项核对机器合同中的字段/过滤/排序/写入/记录键。

```bash
read -rsp 'Worker NocoBase token: ' M3_PROBE_TOKEN
echo
read -rp 'Full station ID: ' M3_STATION_ID
read -rp 'Acceptance run ID: ' M3_ACCEPTANCE_RUN_ID
printf 'Authorization: Bearer %s\n' "${M3_PROBE_TOKEN}" | curl --fail --silent --show-error \
  --header @- \
  --get 'https://vifa.hlszh.com/api/energy_forecast_acceptance_runs:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"control_state\":\"active\"}" \
  --data-urlencode 'fields=id,station_id,acceptance_run_id,window_start,window_end,control_state,completed_days,result_state,calculated_at' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
printf 'Authorization: Bearer %s\n' "${M3_PROBE_TOKEN}" | curl --fail --silent --show-error \
  --header @- \
  --get 'https://vifa.hlszh.com/api/energy_forecast_batches:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"acceptance_run_id\":\"${M3_ACCEPTANCE_RUN_ID}\",\"write_state\":\"complete\"}" \
  --data-urlencode 'fields=station_id,acceptance_run_id,issued_at,forecast_start_time,forecast_end_time,write_state' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
printf 'Authorization: Bearer %s\n' "${M3_PROBE_TOKEN}" | curl --fail --silent --show-error \
  --header @- \
  --get 'https://vifa.hlszh.com/api/energy_forecast_batches:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"write_state\":\"writing\"}" \
  --data-urlencode 'fields=id,station_id,acceptance_run_id,issued_at,forecast_start_time,forecast_end_time,status,write_state,model_manifest,content_hash,point_templates' \
  --data-urlencode 'sort=issued_at' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
printf 'Authorization: Bearer %s\n' "${M3_PROBE_TOKEN}" | curl --fail --silent --show-error \
  --header @- \
  --get 'https://vifa.hlszh.com/api/energy_forecast_evaluations:list' \
  --data-urlencode "filter={\"station_id\":\"${M3_STATION_ID}\",\"acceptance_run_id\":\"${M3_ACCEPTANCE_RUN_ID}\"}" \
  --data-urlencode 'fields=station_id,acceptance_run_id,evaluation_key,window_start,window_end,outcome,calculated_at' \
  --data-urlencode 'page=1' \
  --data-urlencode 'pageSize=1000'
unset M3_PROBE_TOKEN M3_STATION_ID M3_ACCEPTANCE_RUN_ID
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

本地交付只执行静态 JSON、Python、ripgrep 和 Git 检查；仅在受保护 env 已存在时才允许执行 Compose 配置检查。没有启动生产容器，也没有执行自动化或端到端测试。上线后的 Socket、公开路由、iframe 页面和两站数据由人工确认。


## 权限遗留项

2026-08-28 的部署记录曾注明 Worker 临时使用 root 角色 Key；本次文件整理没有核实该遗留项
是否已经关闭，不能将文档合并视为权限整改完成。后续维护时应按
`m3/contracts/nocobase_collections.json` 核对专用 `m3.worker` 角色的动作与字段权限，
关闭全局默认权限。三张自定义预测表分别需要 manual_runs 的 `list/update/firstOrCreate`、
manual_points 的 `list/create`、manual_evaluations 的 `list/updateOrCreate`。

关闭条件：Worker 配置使用专用最小权限 Key，任务、完整批次、writing 批次、评估四个只读探针通过，
root Key 已从 Worker 配置移除，不再使用的临时 Key 已撤销。具体探针和启用顺序见第 13 节。
