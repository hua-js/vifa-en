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

新增覆盖文件 `deploy/m4-m3-mape.override.yaml`，与原 Compose/PV 覆盖文件共同使用，保留原网络与 PV 挂载。该文件已固定新版镜像摘要，必须放在原 PV 覆盖文件之后，不需修改 .env。M3 Worker 使用专用管理 Token，不能假定与 NocoBase/PV Token 相同。

服务器由用户准备只读凭据副本（默认 M4 运行 UID/GID 10001）：

```sh
install -d -m 0750 -o 10001 -g 10001 /userdata/holo/pyfiles/m4/secrets
install -m 0400 -o 10001 -g 10001 /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token /userdata/holo/pyfiles/m4/secrets/m3-admin.token
```

M4 挂载 M3 run 目录至 `/run/vifa-m3`，以及凭据至 `/run/secrets/m3_admin_token`，均只读。Worker Token 轮换后需同步副本并重建容器。M3 socket 必须允许 M4 的运行用户连接。本机开发可用 `M4_M3_SOCKET_PATH`、`M4_M3_ADMIN_TOKEN_FILE` 指定本机真实资源；未配置时会阻断，不伪造 MAPE。

将新版覆盖文件放到现有 backend 目录、准备凭据后使用：

```sh
docker compose -f compose.yaml -f m4-pv.override.yaml -f m4-m3-mape.override.yaml up -d --no-build --pull always m4-api
```

此处未执行生产部署或读取生产 M3 Worker；实际 MAPE 值与生产连通性仍需部署后核验。

## 本轮验证

37项门槛、接口适配、候选与按需PV相关测试通过，Python/页面JS语法和HTML/Flow一致性检查通过。另跑25项运行输入测试，22通过、3个旧历史光伏预期测试失败，保留记录；未进行页面实际渲染检查。未重启本地8846服务，它仍运行上一版逻辑。
