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
