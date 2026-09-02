# M3 AMD64 三域生产环境配置编辑区

本目录只用于本地填写生产配置，不进入 Docker 镜像。

需要编辑：

- `m3.env`：Worker 写入、管理和 Source 凭据；
- `dashboard.env`：Dashboard 四表只读凭据。

规则：

1. 两个文件的 `M3_STATIONS_JSON` 必须完全相同；
2. `station_1` 的完整 `station_id` 必须以 `ES01` 结尾；
3. `station_2` 的完整 `station_id` 必须以 `ES02` 结尾；
4. 删除全部 `REPLACE_` 占位符后才能上传；
5. 不要在本目录保存原始数据 Token；Token 必须作为单独文件安全上传；
6. 不要把填写后的 `.env` 提交到版本库、放入代码压缩包或发送到聊天中。

上传目标：

```text
m3.env       -> /etc/vifa-m3/m3.env
dashboard.env -> /etc/vifa-m3/dashboard.env
```

生产文件权限必须为 `root:root 0600`。

固定部署边界：

```text
镜像平台：linux/amd64
原始数据和结果表：https://vifa.hlszh.com
NocoBase 页面和当前用户认证：https://ems.lvkpower.com
Node-RED 看板页面和 API：https://opdash.lvkpower.com
宿主机 /userdata/holo/pyfiles/vifa-m3/run -> 容器 /run/vifa-m3
宿主机 /etc/vifa-m3/raw-source.token -> 容器 /run/secrets/raw-source.token
宿主机 /userdata/holo/pyfiles/vifa-m3/run/.worker-admin.token -> Node-RED 固定 Worker 管理令牌文件
```

Compose 同时运行 `vifa-m3-worker` 和 `vifa-m3-dashboard`。两个服务不开放 TCP
端口，只通过共享 Unix Socket 与宿主机 Node-RED 通信。两个环境文件必须配置完全相同的
两个完整电站 ID。

## 验收查询修复与每日自动预测发布顺序

本次发布先让自定义预测和评估稳定运行，再恢复正式验收。必须按以下顺序执行；每一步成功后才进入下一步，
并且不在文档、命令历史或版本库中记录 API Key、Token 或生产密钥。

1. 使用 `M3_ACCEPTANCE_ENABLED=false` 发布 Worker，先确认 `m3_daily_scheduler` 已在上海时区
   每天 00:17 为每个电站的每一种已完成预测时间粒度创建任务，并确认自定义预测评估可以独立完成。
   已有的手工完成任务仅作为模板；自动任务沿用其预测时长、预测粒度和历史天数，不修改旧任务。
2. 在 NocoBase 权限中只授予机器契约所需的直接筛选字段，不能因为方便而放开关联字段筛选或额外写权限。
3. 使用最低权限账号探测 `energy_forecast_batches`：查询条件只使用直接字段 `station_id`、
   `acceptance_run_id`、`write_state`，确认空结果和有结果都返回正常业务响应。
4. 使用同一账号探测 `energy_forecast_points`：查询条件只使用直接字段 `batch_id`、`unique_id`、
   `data_time`。不得使用 `batch.*`；关联筛选会触发 NocoBase 500，不能作为生产回读或验收恢复方式。
5. 将未完成的 `acceptance-20260829-station1` 和 `acceptance-20260829-station2` 标记为已取消，
   但保留批次、点位和告警证据，禁止删除历史记录或以删除重跑作为补救。
6. 在未来一个上海时区 01:00 起点新建七日验收任务，避免与正在运行或已结束的历史窗口重叠。
7. 将 Worker 配置改回 `M3_ACCEPTANCE_ENABLED=true`，只重建并重启 Worker；随后观察各阶段独立告警，
   包括 forecast、acceptance_backfill、custom_evaluation 和 daily_custom_forecast，确认其中一个阶段失败
   不会阻塞其他阶段。
8. 在每个电站新验收窗口的首个 01:02 基线后，核对每个序列恰有一个完整批次，并且每个序列有 96 个
   15 分钟点位；异常时保留响应和告警上下文后再处理，不删除证据。

发布完成后继续观察每日自动预测：只有同一电站、同一时间粒度不存在当天或未来的成功手工任务时，
才会按最近完成任务的配置自动创建任务；因此操作人员完成一次手工预测后，不需要每天再次点击预测。

## 创建自定义预测集合

`create-custom-forecast-collections.py` 通过固定的
`POST /api/collections:create` 创建以下三张表：

1. `energy_forecast_manual_runs`；
2. `energy_forecast_manual_points`；
3. `energy_forecast_manual_evaluations`。

脚本直接读取 `m3/contracts/nocobase_collections.json` 并转换成 NocoBase 的
collection、field、association 和 index 配置，不在脚本里维护第二份表结构。默认只生成离线计划，不连接
NocoBase：

```bash
python3 m3/deploy/create-custom-forecast-collections.py
python3 m3/deploy/create-custom-forecast-collections.py --show-payloads
```

三张集合都在 `fields` 中显式创建非空自增 `bigint id` 主键，并关闭 `autoGenId`，避免仅设置
`autoGenId` 后字段元数据中仍缺少 `id`。三张表均显式包含 NocoBase 管理的 `createdAt`、`createdBy`、
`updatedAt`、`updatedBy`；其中创建人和修改人关联 `users.id`，对应物理外键列为 `createdById`、
`updatedById`。脚本还会为每个字段提交与存储类型匹配的 `interface`、标题和 `uiSchema`，确保字段能够在
NocoBase 数据源管理界面完整显示。离线 `--show-payloads` 输出包含 schema summary，其中会明确列出物理列、
显式 `id` 主键和关联元数据字段。

以后实际执行时，单独创建一个短期 Schema 管理 API Key。不要复用 Worker 的
`M3_NOCOBASE_API_KEY`，也不要把管理 Key 放在命令行或仓库中：

```bash
install -o root -g root -m 0600 schema-admin.token /etc/vifa-m3/schema-admin.token
export M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
export M3_NOCOBASE_SCHEMA_API_KEY_FILE=/etc/vifa-m3/schema-admin.token
python3 m3/deploy/create-custom-forecast-collections.py --execute
unset M3_NOCOBASE_BASE_URL M3_NOCOBASE_SCHEMA_API_KEY_FILE
```

执行模式先回读三张表；只要任何同名表已存在，就在发送 POST 前整体停止。之后按依赖顺序逐表创建并
回读核验精确字段集合、`id` 主键属性、字段类型、字段界面配置与索引。请求失败或结果不确定时不会自动
重试，也不会自动删除已经创建的表，必须先人工检查 NocoBase 再决定后续操作。

`collections:create` 能创建列、主键、复合索引和 `run_pk` 关系外键，但不能表达契约中的全部命名
SQL `CHECK` 约束。当前链路由 Worker 的严格输入、领域和持久化校验保证这些不变量；如果以后允许其他
写入方直接访问这些表，需要再增加独立数据库迁移来落物理 `CHECK` 约束。
