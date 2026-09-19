# NocoBase 关联权限插件生产安装步骤

版本：`@vifa/plugin-association-read-guard@0.1.1`。本文件是供运维执行的安装说明，本次未执行生产安装。

用户已暂停测试，并指出此前测试数据库不符合预期。2.2.2 最小模拟库的后端和资源检查通过，仅证明已覆盖的兼容性；正确业务数据库、实际页面和真实模型对话尚未完成验收。不能将下面步骤视为生产验收通过。

## 1. 确认作用范围和生产实例

此包只针对 `vifa_ai_acceptance_20260919` 角色，限制以下八张表的关联读取：

`t_emu`、`t_es`、`t_efficiency_points`、`t_efficiency_bottleneck_events`、`energy_forecast_manual_runs`、`energy_forecast_manual_points`、`energy_pv_forecast_points`、`t_model`。

普通查询继续经过原 ACL；插件不新增场站权限，也不会自动保护其他业务角色。若受限角色的页面需要这些表的关联列，请求会被拒绝。插件参与应用加载，即使只限制一个角色，安装或资源错误仍可能影响整个应用。

在生产服务器执行，选择实际应用容器，不能照搬测试容器名：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
read -r -p '生产 NocoBase 应用容器名：' NB_APP

docker inspect "$NB_APP" --format 'image={{.Config.Image}} image_id={{.Image}}'
docker inspect "$NB_APP" --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'

docker exec "$NB_APP" node -e 'for (const n of ["server","plugin-ai","plugin-acl","plugin-data-source-main","plugin-data-source-manager"]) { const p=require("@nocobase/"+n+"/package.json"); console.log(n,p.version,p.gitHead); }'
docker exec "$NB_APP" node -e 'for (const k of ["DB_DIALECT","DB_HOST","DB_PORT","DB_DATABASE","DB_USER","DB_SCHEMA"]) console.log(k,process.env[k] || "<unset>");'
```

将数据库主机、库名、schema 与实际生产部署配置核对，并在管理界面核对目标角色和业务表。上述 DB_* 若未设置，需从实际配置确认连接身份，不能猜测默认值。不要输出 DB_PASSWORD、APP_KEY 或含密码的连接串。

已知生产 API 曾报告核心及相关插件 2.2.2。若当前版本、定制或连接库不同，先解决差异。确认 `/app/nocobase/storage` 使用持久化存储，否则重建容器可能丢失插件。

## 2. 准备维护窗口和备份

按既有生产备份流程取得可恢复的数据库备份、storage 和部署配置，并确认恢复方式。在线 PostgreSQL 备份使用一致性备份方式，不能直接复制正在写入的数据目录作为有效备份。

安排低峰维护窗口。测试机完整重启恢复约 3 分钟，生产时间不能据此保证。应用重载时原有工作流会按既有配置恢复，需纳入维护安排。本次安装不需要同时升级 NocoBase。

## 3. 上传并校验 0.1.1 安装包

本机文件：`outputs/ai-employee/test-host/20260919/vifa-association-read-guard-0.1.1.tgz`。

用既有上传方式将其放到生产服务器：
`/tmp/vifa-association-read-guard-0.1.1.tgz`。

不要使用 0.1.0，它缺少动态前端入口。

```bash
NB_PKG=/tmp/vifa-association-read-guard-0.1.1.tgz
NB_SHA=ac540004e49bd6264de81451b56224e921febda4a85cb84d93d4a61cb6e33ee2
printf '%s  %s\n' "$NB_SHA" "$NB_PKG" | sha256sum -c -
tar -tzf "$NB_PKG"
```

校验必须显示 OK；包中应有 `package.json`、三个 server 文件、`client.js`、`client-v2.js`，以及 `dist/client/index.js`、`dist/client-v2/index.js`。失败时不要继续。

```bash
docker cp "$NB_PKG" "$NB_APP":/app/nocobase/storage/vifa-association-read-guard-0.1.1.tgz
```

## 4. 安装并启用

先在插件管理界面确认该插件是否已安装。首次安装执行：

```bash
docker exec "$NB_APP" yarn nocobase pm add /app/nocobase/storage/vifa-association-read-guard-0.1.1.tgz &&
docker exec "$NB_APP" yarn nocobase pm enable @vifa/plugin-association-read-guard
```

若已经安装该插件，使用更新命令，不能再次按首次安装处理：

```bash
docker exec "$NB_APP" yarn nocobase pm update /app/nocobase/storage/vifa-association-read-guard-0.1.1.tgz
```

更新后在插件管理界面核对启用状态；若原来停用且本次要启用，再执行 `pm enable`。记录命令退出状态，失败时先查看日志，不要反复安装或重启。

## 5. 等待恢复并验收

```bash
curl --max-time 10 -fsS https://vifa.hlszh.com/api/app:getInfo
docker exec "$NB_APP" node -p 'require("@vifa/plugin-association-read-guard/package.json").version'
```

重载期间可能出现 502 或 503 / APP_COMMANDING。等 API 恢复并返回预期核心版本后继续，不能把维护错误当作权限拦截。插件版本应为 0.1.1，管理界面应显示已安装、已启用。

检查两套动态入口，均应为 200 且为 JavaScript：

```bash
curl --max-time 10 -f -o /dev/null -w 'client %{http_code} %{content_type}\n' https://vifa.hlszh.com/static/plugins/@vifa/plugin-association-read-guard/dist/client/index.js
curl --max-time 10 -f -o /dev/null -w 'client-v2 %{http_code} %{content_type}\n' https://vifa.hlszh.com/static/plugins/@vifa/plugin-association-read-guard/dist/client-v2/index.js
```

实际登录并打开页面。分别使用未包含受限角色的管理员和独立测试账号，确认登录、导航、已有页面与 AI 员工入口。只返回首页 200 不代表页面验收通过。

使用独立测试账号的 Bearer Token 和 `X-Role: vifa_ai_acceptance_20260919`，通过已有 API 测试工具做只读对照；不能拿管理员 Token 代替：

| 请求（生产 `/api/` 下） | 预期 |
| --- | --- |
| `energy_forecast_manual_points:list?fields=id,run_pk&pageSize=1` | 200，仅授权普通字段 |
| `energy_forecast_manual_points:list?fields=id,run_pk&appends[]=run&pageSize=1` | 403，`VIFA_ASSOCIATION_READ_BLOCKED` |
| `energy_forecast_manual_points:list?fields=run.requested_by&pageSize=1` | 403，`VIFA_ASSOCIATION_READ_BLOCKED` |
| 使用同一测试身份读取已确认存在、但无权访问的场站记录 | 不返回该记录 |
| 管理员正常读取、普通业务页面查询 | 保持原先授权行为 |

验证行隔离必须用真实存在的无权记录；查询不存在的 ES99 返回空不能证明生产隔离有效。业务模型对话按既有模型和数据外发授权单独验收，不因本安装步骤自动扩大授权。

完成正确业务库和实际页面验收前，保留“未完成生产验收”状态，不扩展受限角色名单。

## 6. 出现异常时停用回退

登录页打不开、原有普通查询异常，或出现不能接受的页面影响时，通过服务器 CLI 停用：

```bash
docker exec "$NB_APP" yarn nocobase pm disable @vifa/plugin-association-read-guard
```

等待应用恢复，再检查原页面和 API。停用会撤去新增关联保护，原权限缺口也会恢复；因此不要在回退后扩大测试账号使用范围。正常插件回退无需还原整个数据库或恢复旧核心补丁。若停用命令失败，查看 `docker logs --tail 100 "$NB_APP"` 定位，再按既有恢复流程处理，避免直接删除插件目录造成加载故障。
