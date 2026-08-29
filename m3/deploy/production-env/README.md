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
```

Compose 同时运行 `vifa-m3-worker` 和 `vifa-m3-dashboard`。两个服务不开放 TCP
端口，只通过共享 Unix Socket 与宿主机 Node-RED 通信。两个环境文件必须配置完全相同的
两个完整电站 ID。

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
`autoGenId` 后字段元数据中仍缺少 `id`。三张表均包含由服务端维护的 `created_at`、`updated_at`；脚本还会
为每个字段提交与存储类型匹配的 `interface`、中文标题和 `uiSchema`，确保字段能够在 NocoBase 数据源管理
界面完整显示。离线 `--show-payloads` 输出包含 schema summary，其中会明确列出物理列、显式 `id` 主键和
关联元数据字段。

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
