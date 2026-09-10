# 电站2光伏：生产 Node-RED 手动 Flow

导入 `pv_manual_production_flow.json`，部署配套 Python 服务后，即可手动采集天气、生成一次光伏预测、查询结果。**没有定时器或启动执行；本轮未部署生产环境。**

## 操作入口

| Flow 按钮/接口 | 行为 |
|---|---|
| 1 · 手动采集并保存天气 | 从 Open-Meteo Forecast API 获取 current 和 50 个小时标签，通过 NocoBase 保存天气并回读核验 |
| 2 · 手动生成一次预测 | 自动获取一批新天气，使用已验证训练快照拟合 WeatherRidge，生成未来24小时96点，写预测批次与结果并核验 |
| 查看最近一次手动任务 | 查看 queued / running / completed / failed / interrupted；提交返回202仅代表已接收 |
| 3 · 查询最新已完成预测 | 返回最新完整 completed 批次、96点、积分电量、峰值、训练截止时间和新鲜度 |
| GET `/pv-forecast-api/ES02/latest` | 对外只读查询；须携带 EMS 当前用户的 `Authorization: Bearer …`，不接受查询参数 |

天气采集和预测生成是两个独立按钮。只需生成预测时直接点击按钮2，它会自行取新天气，不必先点按钮1。任务执行期间再次点击写入按钮返回409及当前任务；任务完成后再点表示明确发起新任务。

Node-RED 只通过 Unix Socket 调用服务，模型计算在 Python 侧。按用户确认，Node-RED 与 Python 均复用现有 M3 Token 文件。公网没有新增生成任务的 POST 路由，写入按钮由有编辑权限的 Node-RED 操作员使用。

## 配套服务与文件

交付压缩包包含 Flow、服务源码、m3/pv/Dockerfile、独立 Compose、训练快照及逐文件 SHA-256。按用户最新确认，以现有 AMD64 镜像 `vifa-m3:0.1.0` 为基础合并为 `vifa-m3-pv:2026-09-09`，同一镜像支持三个运行模式：`worker`、`dashboard`、`pv`。前两种转交原 M3 入口，光伏模式启动私有 PV Socket 服务；默认模式为 `pv`。

三个模式分别运行在三个容器中，共用同一个镜像。原 M3 两个容器继续使用各自的 command、env 和 run 挂载，光伏容器使用本目录 Compose 的独立配置。推送镜像不自动升级或重启生产容器。

生产目录约定：

| 文件/目录 | 用途 |
|---|---|
| `/userdata/holo/pyfiles/vifa-pv/app` | 解压后的部署包根目录（含 `m3/pv/compose.yaml`） |
| `/userdata/holo/pyfiles/vifa-pv/run/pv.sock` | Node-RED 调用的服务 Socket；没有 TCP 监听端口 |
| `/userdata/holo/pyfiles/vifa-pv/run/manual/jobs/<job_id>` | 每次手动任务的天气原文、预测工件、发布核验和任务状态 |
| `/etc/vifa-m3/raw-source.token` | 复用原 M3 同一个 Token；保持现有 `root:10001`、`0640` 配置 |
| 容器内 `/run/secrets/raw-source.token` | 上述文件的只读挂载，天气/预测入库、查询、服务认证全部读取此路径 |

生产实查：`nodered` 容器以root运行，已挂载整个 `/userdata/holo/pyfiles`，可见PV Socket，但不可见 `/etc/vifa-m3/raw-source.token`。无需新增挂载。Flow改为读取共享目录中的同值副本 `/userdata/holo/pyfiles/vifa-pv/run/.raw-source.token`，由宿主机执行：

```bash
install -o root -g root -m 600 /etc/vifa-m3/raw-source.token /userdata/holo/pyfiles/vifa-pv/run/.raw-source.token
```

沿用同一个Token，不生成新令牌；Python继续读取原文件，Node-RED读取该副本。原Token轮换后需再次执行复制命令同步副本。Token通过stdin传给curl，不写到进程参数、Flow或镜像。该权限适用于已确认的Node-RED root运行账户。

Python 接受现有 `0640` 和原 `0600` 文件（及各自去掉写权限的模式），校验文件属主/组与进程的访问范围，拒绝其他用户可读或组可写的文件。无需修改原 M3 文件权限。

NocoBase Key 运行时需要 `energy_weather_points:list/create`、`energy_pv_forecast_runs:list/create/update`、`energy_pv_forecast_points:list/create`，不再调用 `collections:get`。运行前通过业务list请求检查字段可访问性；空表允许首次写入，写入响应、最终回读和哈希仍必须完整匹配。字段物理类型/索引/约束的管理元数据检查留在独立建表部署工具，不宣称运行时仍验证这些元数据。本版运行不查询实测表，也不创建表、修改角色或删除记录。

## 部署顺序

1. 将压缩包解压到上面的 `app` 目录，核对包内 `manifest.json`。确认服务器已有 `vifa-m3:0.1.0` AMD64 镜像；未安装时先按既有 M3 部署手册构建该基础镜像。
2. 创建 `/userdata/holo/pyfiles/vifa-pv/run`，设为 UID/GID 10001、权限0770。给 Node-RED 运行账户授予该目录及 Socket 所属组的访问权限。Node-RED 在容器内时，将这个目录映射到同一绝对路径。
3. Python沿用 `/etc/vifa-m3/raw-source.token` 的原只读挂载；按上述命令把同值副本放入已共享run目录供Node-RED读取，不改原文件权限或新增Docker挂载。
4. 在部署包根目录执行：

   ```bash
   docker compose -f m3/pv/compose.yaml config --quiet
   docker compose -f m3/pv/compose.yaml build vifa-pv
   docker compose -f m3/pv/compose.yaml up -d vifa-pv
   docker compose -f m3/pv/compose.yaml ps
   ```

5. Node-RED 导入包内 `m3/pv/pv_manual_production_flow.json`，新增独立“电站2 · 光伏手动预测”标签。默认用户认证地址为 `https://ems.lvkpower.com/api/auth:check`，可沿用已有 `M3_AUTH_BASE_URL`。Flow 只使用内置节点和宿主机 `/usr/bin/curl`。
6. 部署这一个新增 Flow。先点击“查询最新已完成预测”，确认已有96点；再按需要点击天气或预测按钮，并手动查看任务状态。导入及 Deploy 本身不会创建天气/预测数据。

服务支持容器自动重启，**自动重启只恢复 API，不会执行任务**。未完成任务重启后记为 interrupted，保留工件等待人工核对。

本机采用 Buildx 时，选择可见本机基础镜像的 Docker 驱动，例如 `docker buildx build --builder orbstack --platform linux/amd64 -f m3/pv/Dockerfile -t vifa-m3-pv:2026-09-09 --load .`。独立 docker-container builder 无法直接读取 Docker daemon 内的本地基础镜像；使用远程基础镜像时须显式指定 `M3_BASE_IMAGE`。

## 当前模型和训练范围

- 电站固定 ES02，纬度23、经度113、Asia/Shanghai。
- WeatherRidge 固定9特征、训练集RMS、无截距、lambda=0.01；输出十五分钟平均交流功率，非负截断；未设置未知额定容量上限。
- 包内保留61,189条原始实测和历史天气来源，得到2,136个合格十五分钟训练样本，训练结束时间为2026-09-09 22:00（右端不含）。每次手动预测从该快照重新拟合，不会自动刷新历史。
- 9月历史天气已补采并入库215小时，09-01 00:00至09-09 22:00；原2,952小时历史完整保留。
- 未来天气统一使用 [Open-Meteo Forecast API](https://open-meteo.com/en/docs)。current 是模型当前值；辐照采用前一小时平均值语义，与既有训练对齐。未知模型发布时间保留 NULL。
- 不返回未经估计的预测区间，不将回测误差当作真实在线精度。后续更新训练源时应重新验证来源、打包并部署，已有预测证据保持原样。

## 查询结果与异常处理

查询响应沿用 `status/data`。没有完成批次返回 `status=empty, data=null`；上游不可用或96点/哈希校验失败返回502。`freshness` 为 `fresh`、`stale`（生成口径超过2小时）或 `expired`（窗口已结束）；过期结果仍携带原窗口，不伪装成新的未来24小时。`remaining_full_points` 表示尚未开始的完整区间数量。

运行接口只接受无请求体、无查询参数的固定操作：`POST /api/pv/ES02/weather`、`POST /api/pv/ES02/runs`、`GET /api/pv/ES02/job`、`GET /api/pv/ES02/latest`。除 `/health` 外均需私有服务令牌；这些路径只通过私有 Socket 提供。

预测写入先 running、写足并核验96点后 completed；写失败不自动重试 POST。失败时按 job_id 查看 `run/manual/jobs/<job_id>/weather` 或 `forecast` 工件。已有 `forecast/run.json` 时先用原发布工具回读核验；如果预测窗口已经开始且批次未完成，保留旧工件并明确发起新任务，不能修改旧点冒充恢复成功。多次 NocoBase 调用不是数据库事务。

本版没有历史自动补采、每小时触发、模型自动升级、实测误差自动回填、前端页面或 M4/EMS 接线。

## 本地验证与生产待验

Python 手动任务/接口测试、Node-RED Function 行为与图连接检查、Compose 配置解析，以及现有光伏/天气回归检查在本机执行；详见交付核验报告。本轮没有连接生产主机、导入生产 Node-RED 或下发设备指令。生产 Docker 构建、Socket 账户权限和真实 Node-RED 运行仍需在部署时验收。

Flow 的 stdout/退出码使用同一请求的 `msg.parts.id` 分组，避免并发查询串包；其行为参照 [Node-RED Join 实现](https://github.com/node-red/node-red/blob/master/packages/node_modules/%40node-red/nodes/core/sequence/17-split.js)。
