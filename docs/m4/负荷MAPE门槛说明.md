# 求解前当前负荷MAPE门槛

两站的滚动候选（含后台 candidate-jobs、滚动决策链）和自然日计划共用门槛：MAPE >30% 阻断，≤30% 允许；缺失或无法读取/评估时阻断。光伏功率输入和已有 SOC 实测时效、参与柜及上下限规则保留，不加入光伏误差或 SOC 预测误差门槛。

## 与 M3 页面一致的口径

读取 M3 默认的15分钟、1天、weekly_load_v2 当前任务，即覆盖北京时间今天零点的最新可用任务。使用结果接口返回的 station_total_load 序列：实测质量 valid、预测及实测有限、实测非零的点，计算 `mean(abs(forecast-actual)/abs(actual)*100)`。零实测与无效点不进入分母。至少一个有效点就能计算；未回填完整96点时显示“暂估”和有效对比点数。少量样本的数值会随实测回填变化，不代表未来一天已被验证。

不再读取七日验收评估，也不使用训练 CV 分数。若在 M3 页面手动选择其他日期、粒度或历史任务，需核对任务 ID，不能直接比较不同任务的 MAPE。

M3 结果接口在查询时只读叠加最新实测，所以数据库预测点 actual_value 为空不能证明页面没有 MAPE。M4 通过共享 Unix Socket 顺序发出两个 GET：

- `/v1/stations/ES02/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1`
- `/v1/custom-forecast-runs/{run_id}/result`

校验站点、任务、日期、粒度、负荷序列与96点结构。证据与规则绑定来源版本，取数结束及求解前复核；不触发 M3 预测或写入数据库。单次 HTTP Socket 操作超时15秒；失败显示读取失败并阻断。

## 部署配置

已发布 M4 镜像 `ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m4-current-mape-b474eb527596-amd64`，远端摘要 `sha256:f3d23f4ad0bea17d1e97d1cecd202c77764328863d64bc00c1d9d080dde402b9`，平台 linux/amd64。HTML 独立替换，本次镜像更新未复制 HTML。

生产覆盖文件已合并为单份 `m4/deploy/backend/m4-production.override.yaml`，随发布包下发到 `backend/`。它一次提供 `default` 与外部 `1panel-network`（别名 `m4-api`）网络、M3 run 目录与专用管理 Token 的只读挂载，并把镜像声明为 `image: ${M4_IMAGE:?…}`——镜像只由 `backend/.env` 的 `M4_IMAGE` 决定，未设置时 compose 直接报错退出。旧的 `m4-pv`、`m4-m3-mape`、`m4-station-power` 覆盖文件已移除，内容全部并入本文件；服务器上须把它们移出启动列表。M3 Worker 使用专用管理 Token，不能假定与 NocoBase/PV Token 相同。

服务器由用户准备只读凭据副本（默认 M4 运行 UID/GID 10001）：

```sh
install -d -m 0750 -o 10001 -g 10001 /userdata/holo/pyfiles/m4/secrets
install -m 0400 -o 10001 -g 10001 /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token /userdata/holo/pyfiles/m4/secrets/m3-admin.token
```

M4 挂载 M3 run 目录至 `/run/vifa-m3`，以及凭据至 `/run/secrets/m3_admin_token`，均只读。Worker Token 轮换后需同步副本并重建容器。M3 socket 必须允许 M4 的运行用户连接。本机开发可用 `M4_M3_SOCKET_PATH`、`M4_M3_ADMIN_TOKEN_FILE` 指定本机真实资源；未配置时会阻断，不伪造 MAPE。

将新版覆盖文件放到现有 backend 目录、在 `.env` 设好 `M4_IMAGE`、准备凭据后使用：

```sh
docker compose -f compose.yaml -f m4-production.override.yaml up -d --no-build --pull always m4-api
```

此处未执行生产部署或读取生产 M3 Worker；实际 MAPE 值与生产连通性仍需部署后核验。

## 本机通过平台网关读取（2026-09-10）

没有 M3 Worker Socket 的本机开发环境可显式设置 `M4_M3_TRANSPORT=platform_gateway`。该模式只向已配置的平台 `https://opdash.lvkpower.com` 发出固定的当前任务和结果 GET，请求通过既有平台用户凭据认证，响应按 `status=ok/data` 解封装；禁止重定向、限制响应大小，不使用 Worker 管理 Token，也不触发预测。未设置时仍为原 Socket 模式，不自动回退口径。

当前本机启动方式（旧 8846 服务保留）：

```sh
M4_M3_TRANSPORT=platform_gateway .venv/bin/python -m uvicorn m4.settings.api:create_app --factory --host 127.0.0.1 --port 8848
python3 m4/scripts/preview_console.py --port 8847 --upstream-port 8848
```

平台凭据由现有本机配置读取，不放在命令参数或文档中。生产部署仍推荐已有 Socket/专用凭据配置；平台网关模式需要其当前认证配置接受对应用户凭据。

已只读核对：电站 1 任务 `3414fb4a-bf5a-4566-b94f-2b064a6488b5` 前 59 个有效点为 11.2852%（显示 11.29%）；更新至 60/96 点后为 11.1339%。电站 2 当前任务 60/96 点为 7.8578%。两站 MAPE 均通过 30% 门槛。电站 1 的零点 SOC 超出当前安全范围是另一项阻断；电站 2 全天输入可比较。数据会随实际值回填变化，不固定上述数值。

## 本轮验证

37项门槛、接口适配、候选与按需PV相关测试通过，Python/页面JS语法和HTML/Flow一致性检查通过。另跑25项运行输入测试，22通过、3个旧历史光伏预期测试失败，保留记录；未进行页面实际渲染检查。未重启本地8846服务，它仍运行上一版逻辑。
