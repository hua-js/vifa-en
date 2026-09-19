# NocoBase 1.6.24 → 2.2.2 原部署升级手册

更新日期：2026-09-18。用户已确认本地验证完成；本文用于待升级机器原有 Compose 实例的正式操作。

**保留原 host 网络、原数据库、原 storage 和原访问地址，只替换 holobase 应用镜像。** PostgreSQL 不升级。不创建测试实例，不配置额外访问转发或 SSH 隧道。工作流随原数据库保留，新版启动时自动更新数据库结构及内部字段。

本文只交付命令，没有再次操作服务器。目标机器 IP 未提供，从你已登录的目标机器 root 终端开始。已升级的测试机无需再执行本手册。

本次上传副本测试已通过核心升级与健康检查，但其数据库归档曾在运行中打包，不能用来替代正式升级前的新备份。**正式升级必须重新生成有效数据库备份；本手册采用停库冷备份。** 若无法暂停外部同步和停库，暂不执行本手册的备份与升级阶段，应另行采用适配 TimescaleDB 的在线备份恢复流程。


## 1. 确认适用环境

| 项目 | 本手册命令使用的值 |
|---|---|
| 操作终端 | 当前目标机器的 root 终端；普通用户先执行 `sudo -i` |
| CPU 架构 | ARM64 / aarch64 |
| Compose 项目 | `nbdb1624` |
| Compose 目录 | `/home/opt/1panel/docker/compose/nbdb1624` |
| Compose 文件 | `docker-compose.yml` |
| 应用服务 / 容器 | `holobase` / `nbdb1624-holobase-1` |
| 数据库服务 / 容器 | `holodb` / `nbdb1624-holodb-1` |
| 原应用镜像 | `ccr.ccs.tencentyun.com/taidai-holobase-168/holobase:1.6.24` |
| 目标应用镜像 | `registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full` |
| 原数据库镜像 | `ccr.ccs.tencentyun.com/taidai-holobase-168/holodb:latest`，此次不升级 |
| 数据库 | 已确认 PostgreSQL 17.4；库名 holodb，用户 postgres；扩展版本在第 3 节读取 |
| storage 宿主机目录 | `/userdata/holo/nb/storage` |
| PostgreSQL 宿主机目录 | `/userdata/holo/postgresql` |
| 数据盘 | Docker 根目录 `/userdata`；历史检查剩余 84 GB，执行时重新检查 |
| 根分区 | 归档不放根分区，统一放 `/userdata/nocobase-backups` |
| 网络 | 沿用原 Compose 的 host 网络；镜像自带 Nginx 提供原网页入口 |

已确认容器对应关系：

- 应用：`0d6728f2ac66` → `nbdb1624-holobase-1`。
- 数据库：`8c2f250b1650` → `nbdb1624-holodb-1`。

以下命令统一使用已确认的容器名；重建应用后 ID 会变化，容器名仍可用于后续验证。Compose 目录、服务名和两个 bind mount 路径均已确认，无需再替换。架构按用户确认使用 ARM64。

## 2. 升级前注意事项

- 测试机已验证核心 1.6.24 可直接升级到 2.2.2，PostgreSQL 17.4 保持不变；本次使用官方 full 镜像。
- 自有 holobase 镜像中的定制代码未逐项比对。若必须保留镜像内定制，应先取得相应 2.2.2 定制镜像，再设置 TARGET_IMAGE。
- 用户已确认本地验证完成。最近一次上传副本测试中，模板打印、专业导出、自定义品牌 3 个插件仍为 1.6.24，日志提示 `License key not found`；本文不把核心升级成功视为商业插件已更新。正式升级后核对实际功能及版本。
- 备份旧镜像不足以完整回退：新版会改动原数据库，数据库和 storage 必须一起备份。
- 本次测试机准备、备份、校验及升级合计约 30 分钟；历史工作流记录更新耗时数分钟。不同数据量和设备速度可能更久，请安排维护窗口。
- 数据盘应能同时容纳新镜像、旧镜像归档、运行快照、数据库归档、storage，以及回退时保留的失败目录。用户此前提供的目标机器空间约 84 GB 可用；仍应在第 3 节读取实际数据量，预留备份与失败副本空间。
- 本手册回退命令经过语法检查，但本次测试未实际执行完整回退演练。生产操作前应验证可恢复性，并将备份另存到可信设备。

## 3. 登录及准备（应用仍可运行）

以下各节按顺序执行，尽量保持同一 root Bash 会话。本手册使用完整 docker compose 命令，不依赖 dc 函数，避免新终端误调用系统 dc。每段应逐条执行；任意命令失败立即停止，不继续执行下面的命令。不要把全文一次性粘贴。交互会话不启用 set -e，避免跟随日志时 Ctrl+C 导致会话退出。重连后重新设置变量，并选择原备份目录，不能创建新目录冒充原备份。

```bash
# 从你当前目标机器的 root 提示符开始；若尚未是 root，先执行 sudo -i
bash
set -uo pipefail
umask 077

cd /home/opt/1panel/docker/compose/nbdb1624
export TARGET_IMAGE='registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full'
export BACKUP="/userdata/nocobase-backups/pre-2.2.2-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
printf '本次备份目录：%s\n' "$BACKUP"

docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml ps
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml config --quiet
df -h /userdata /
docker info --format '{{.DockerRootDir}}'
du -sh /userdata/holo/postgresql /userdata/holo/nb/storage
docker inspect nbdb1624-holobase-1 nbdb1624-holodb-1 \
  --format '{{.Name}} network={{.HostConfig.NetworkMode}} ports={{json .NetworkSettings.Ports}}'
docker exec nbdb1624-holobase-1 node -p \
  'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
docker inspect nbdb1624-holobase-1 nbdb1624-holodb-1 \
  --format '{{.Name}} {{json .Mounts}}'
docker exec nbdb1624-holodb-1 psql -X -U postgres -d holodb \
  -c 'SHOW server_version; SELECT extname,extversion FROM pg_extension; SELECT spcname,pg_tablespace_location(oid) FROM pg_tablespace;'
find /userdata/holo/postgresql -maxdepth 2 -type l -ls
```

核对核心必须为预期的 1.6.24，挂载与第 1 节一致；如已是 2.2.2，停止升级流程，只做验收。数据库若有外置表空间/WAL，先调整完整备份范围。记录实际扩展版本供升级后对比；不将测试机的 TimescaleDB 2.19.3 当作本机已核实事实。

确认当前应用及数据库为 host 网络；本文不改网络设置。网页沿用原端口，下文按原 80 端口检查，升级后仍用目标机器原 IP/域名访问。镜像内 Nginx 直接监听主机端口，无需额外转发。

保留密码、`APP_KEY`、数据库连接和原挂载。不要把完整 Compose 或 inspect 输出贴到公开渠道。备份配置和镜像，避免以后 `latest` 漂移：

```bash
tar --acls --xattrs --numeric-owner -cpf "$BACKUP/compose-project.tar" \
  -C /home/opt/1panel/docker/compose nbdb1624

APP_ID=$(docker inspect -f '{{.Image}}' nbdb1624-holobase-1)
DB_ID=$(docker inspect -f '{{.Image}}' nbdb1624-holodb-1)
printf '%s\n' "$APP_ID" > "$BACKUP/old-app-image-id.txt"
printf '%s\n' "$DB_ID" > "$BACKUP/old-db-image-id.txt"
docker tag "$APP_ID" local/nocobase-rollback:1.6.24
docker tag "$DB_ID" local/holodb-rollback:pre-2.2.2
docker image save -o "$BACKUP/original-images.tar" \
  local/nocobase-rollback:1.6.24 local/holodb-rollback:pre-2.2.2

docker pull --platform linux/arm64 "$TARGET_IMAGE"
docker image inspect "$TARGET_IMAGE" \
  --format '{{.Os}}/{{.Architecture}} {{json .RepoDigests}}' \
  | tee "$BACKUP/target-image.txt"
```

预期架构为 `linux/arm64`。镜像拉取失败或尚有未解决的兼容性问题时，不进入停机阶段。备份目录和故障副本都放在 `/userdata`，不要放入数据库目录；不使用根分区保存这些大文件。

## 4. 维护窗口：停写与完整冷备份

该方案需要同时停止应用和数据库，影响所有访问 `holodb` 的客户端。安排维护窗口，暂停外部采集/API 写入、定时任务及所有数据库写入者，记录恢复清单。现场存在其他业务服务，不要假定停止 NocoBase 就停止了全部写入。

本方案完整备份 PostgreSQL 集群（包括 `postgres` 与 `holodb`），保留 TimescaleDB 数据与角色；仅适用于原数据库镜像和相同架构的恢复。第 3 节需要确认本机没有遗漏外置表空间或 WAL；如有，需把外置数据纳入一致备份。

PostgreSQL 文件级备份要求数据库停止，恢复也必须停止；不能在运行中直接 tar 数据目录作为可靠备份。[PostgreSQL 17 官方说明](https://www.postgresql.org/docs/17/backup-file.html)

```bash
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml stop -t 120 holobase
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml stop -t 120 holodb

test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
docker inspect -f 'exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' nbdb1624-holodb-1
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml logs --tail=40 holodb
```

确认数据库正常关闭，退出码为 0、非 OOM，日志包含正常关闭证据。若超时被强制结束或日志有异常，先处理并完成正常关闭，再备份。

```bash
# 本段作为一个整体执行；任意步骤失败会中止该备份段
(
set -euo pipefail
: "${BACKUP:?先执行第 3 节设置 BACKUP}"
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
test "$(docker inspect -f '{{.State.ExitCode}}' nbdb1624-holodb-1)" = 0
test "$(docker inspect -f '{{.State.OOMKilled}}' nbdb1624-holodb-1)" = false
# 保留旧容器运行后安装的依赖等文件层；挂载目录仍须另外备份
docker commit nbdb1624-holobase-1 local/nocobase-runtime-rollback:1.6.24
docker image save -o "$BACKUP/runtime-image.tar" \
  local/nocobase-runtime-rollback:1.6.24

tar --acls --xattrs --numeric-owner -cpf "$BACKUP/postgresql.tar" \
  -C /userdata/holo postgresql
tar --acls --xattrs --numeric-owner -cpf "$BACKUP/storage.tar" \
  -C /userdata/holo/nb storage

tar -tf "$BACKUP/postgresql.tar" > "$BACKUP/postgresql-files.txt"
tar -tf "$BACKUP/storage.tar" > "$BACKUP/storage-files.txt"
test -s "$BACKUP/postgresql.tar"
test -s "$BACKUP/storage.tar"
(
  cd "$BACKUP"
  sha256sum compose-project.tar original-images.tar runtime-image.tar postgresql.tar storage.tar > SHA256SUMS
  sha256sum -c SHA256SUMS
)
sync
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
printf 'BACKUP_OK\n' > "$BACKUP/backup.ready"
echo 'BACKUP_OK：备份已完成并通过校验'
)
```

必须看到最后的 BACKUP_OK；若出现“在我们读入文件时文件发生了变化”或任何错误，本次备份无效，不进入第 5 节。所有校验必须 OK，并确认数据库归档包含 `postgresql/PG_VERSION`、`base`、`global`、`pg_wal` 等完整内容。将备份复制到独立磁盘或另一台可信主机并重新校验；同盘备份不能覆盖磁盘损坏风险。未完成备份不要启动目标应用。

## 5. 修改应用镜像并启动

只替换 `holobase` 镜像，保留数据库镜像、host 网络、环境变量、`APP_KEY` 及两个数据挂载。下列脚本针对本次核实的原镜像精确替换；找不到或存在多处时会失败，需重新检查配置。

```bash
python3 - <<'PY'
import os
from pathlib import Path
assert (Path(os.environ['BACKUP']) / 'backup.ready').read_text().strip() == 'BACKUP_OK', '备份未成功，停止升级'
p = Path('docker-compose.yml')
s = p.read_text()
old = 'ccr.ccs.tencentyun.com/taidai-holobase-168/holobase:1.6.24'
assert s.count(old) == 1, '原镜像匹配异常，停止并核查配置'
p.write_text(s.replace(old, os.environ['TARGET_IMAGE']))
PY
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml config --quiet
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml config --images

# 启动原数据库容器，不拉取、不升级数据库
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml start holodb
docker exec nbdb1624-holodb-1 pg_isready -U postgres -d holodb
docker exec nbdb1624-holodb-1 psql -X -U postgres -d holodb \
  -v ON_ERROR_STOP=1 -c 'SELECT 1;'

# 数据库就绪后，仅重建应用；不连带重建数据库
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml up -d --no-deps --pull never holobase
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml logs --tail=100 -f holobase
```

`pg_isready` 若尚未成功，等待后重试，成功前不启动应用。官方 Docker 升级流程是替换镜像并重建应用；关注启动迁移日志，不在迁移中反复重启，也不额外并发执行 CLI upgrade。`Ctrl+C` 只退出日志跟随。[官方升级流程](https://docs.nocobase.com/cn/get-started/upgrading/docker)

如果由 1Panel 管理，在面板核对编排文件已与磁盘一致，避免后续面板操作用旧配置覆盖。升级期间不要使用 `docker compose down -v`、`docker system prune` 或拉取 `holodb:latest`。

启动期间，健康接口可能返回 503 / APP_COMMANDING；这不自动表示升级失败。测试机曾在历史工作流字段更新阶段等待数分钟，不要因等待就反复重启。另开 SSH 会话读取日志或健康接口可以帮助判断；主会话 Ctrl+C 退出日志后也可继续以下检查。

## 6. 验收后恢复流量

```bash
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml ps
docker inspect -f '{{.Config.Image}} {{.Image}} restart={{.RestartCount}}' nbdb1624-holobase-1
docker exec nbdb1624-holobase-1 node -p \
  'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
curl --max-time 15 -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1/
# 新版本应用的就绪检查：必须看到 ok 和 HTTP 200
curl --max-time 15 -sS -w '\nHTTP %{http_code}\n' \
  http://127.0.0.1/api/__health_check
docker exec nbdb1624-holodb-1 psql -X -U postgres -d holodb \
  -c 'SHOW server_version; SELECT extname,extversion FROM pg_extension; SELECT value FROM "applicationVersion"; SELECT name,version FROM "applicationPlugins" WHERE enabled=true AND version <> '\''2.2.2'\'';'
tar --compare -f "$BACKUP/storage.tar" -C /userdata/holo/nb storage/uploads
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml logs --since=15m --tail=300 holobase
```

版本应为 `2.2.2`。若目标镜像目录布局有变化，应通过其实际包位置或后台版本页确认，不能只看镜像标签。容器 Up 和 HTTP 200 仅证明部分启动状态，不能代替以下业务验收：

- 管理员和普通角色均可登录；菜单、旧页面、权限范围正确。
- 核对升级前记录的关键表数量及样本；受控测试记录能够新增、修改、查询，再按原业务流程清理。
- 历史附件能打开；测试附件上传、下载及访问权限正常。
- 启用插件无加载错误；商业授权有效；导出、树区块、图表、模板打印、备份功能通过。
- 工作流、JSON 节点、Webhook 在受控目标上通过；恢复定时任务前确认不会补发非预期动作。
- 外部系统调用、原域名/FRP 入口验证通过。

验收期间仍保持外部写入关闭。全部通过后再逐项恢复客户端与任务并观察日志，记录完成时间、最终镜像 digest、插件版本及备份路径。若恢复写入后又回滚，备份之后的新数据会丢失，必须先对新增数据做保全与对账。

## 7. 失败回退：旧镜像 + 升级前数据库 + storage

NocoBase 不支持直接降级。**不能只把镜像改回 1.6.24 连接已经迁移的数据库。** 回退必须恢复同一备份时点的数据库与 storage。[官方回滚说明](https://docs.nocobase.com/cn/get-started/upgrading/docker)

保持维护状态并暂停全部写入者。以下命令会恢复整个 PostgreSQL 集群到备份时间，包含该集群的所有数据库；在执行前确认影响范围及所选备份。当前失败现场通过改名保留，不直接删除。

```bash
# 如已重连：先 sudo -i，再 bash，重新进入配置目录
set -uo pipefail
umask 077
cd /home/opt/1panel/docker/compose/nbdb1624

# 填写第 3 节实际记录的目录，必须是本次升级前的完整备份
export BACKUP='/userdata/nocobase-backups/pre-2.2.2-替换为实际时间'
test -f "$BACKUP/SHA256SUMS"
(cd "$BACKUP" && sha256sum -c SHA256SUMS)

docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml stop -t 120 holobase
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml stop -t 120 holodb
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
docker image load -i "$BACKUP/original-images.tar"
docker image load -i "$BACKUP/runtime-image.tar"

FAILED="/userdata/nocobase-failed-$(date +%Y%m%d-%H%M%S)"
mkdir "$FAILED"
cp -a docker-compose.yml "$FAILED/docker-compose.failed.yml"
mv /userdata/holo/postgresql "$FAILED/postgresql"
mv /userdata/holo/nb/storage "$FAILED/storage"
tar --acls --xattrs --numeric-owner -xpf "$BACKUP/postgresql.tar" -C /userdata/holo
tar --acls --xattrs --numeric-owner -xpf "$BACKUP/storage.tar" -C /userdata/holo/nb
tar --acls --xattrs --numeric-owner -xpf "$BACKUP/compose-project.tar" \
  -C /home/opt/1panel/docker/compose

# 强制使用归档保存的旧镜像，防止 registry 的 latest 已变更
python3 - <<'PY'
from pathlib import Path
p = Path('docker-compose.yml')
s = p.read_text()
replacements = {
 'ccr.ccs.tencentyun.com/taidai-holobase-168/holobase:1.6.24': 'local/nocobase-runtime-rollback:1.6.24',
 'ccr.ccs.tencentyun.com/taidai-holobase-168/holodb:latest': 'local/holodb-rollback:pre-2.2.2',
}
for old, new in replacements.items():
    assert s.count(old) == 1, '配置镜像与现场记录不一致，停止'
    s = s.replace(old, new)
p.write_text(s)
PY
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml config --quiet
# 配置校验失败时不要启动任何容器
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml up -d --no-deps --pull never --force-recreate holodb
docker exec nbdb1624-holodb-1 pg_isready -U postgres -d holodb
docker exec nbdb1624-holodb-1 psql -X -U postgres -d holodb \
  -v ON_ERROR_STOP=1 -c 'SELECT extname, extversion FROM pg_extension;'

# 数据库确认可用后执行
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml up -d --no-deps --pull never --force-recreate holobase
docker compose -p nbdb1624 -f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml logs --tail=200 holobase
docker exec nbdb1624-holobase-1 node -p \
  'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
curl --max-time 15 -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1/
```

数据库 readiness 检查失败时停在该步骤，不能继续启动应用。恢复后应确认核心版本 `1.6.24`、数据库扩展版本与第 3 节记录一致，并核对业务数据、附件、插件和旧入口。完成验收才恢复外部写入。保留原备份、失败数据目录和日志用于排障。

## 8. 原地址、自动启动及其他服务

本方案保留原 Compose 的 `network_mode: host` 和 `restart` 设置。若原配置为 `restart: unless-stopped`，新应用继续沿用；不增加 Python 转发程序、不配置 16060 端口、不创建内部测试网络。

升级命令只针对 holobase，数据库仅按备份需要停止/启动，不拉取 holodb:latest。原 Compose 中若含 holoedge 等其他服务，不通过整栈 up/down 操作它们。备份期间仍需暂停所有外部同步和写入，直到验证完成后恢复。NocoBase 启动时会自动恢复启用的工作流，因此在正式维护窗口提前处理可能触发的业务动作。

## 9. 完成记录与常见情况

完成后记录：实际版本、镜像 digest、备份目录、健康接口结果、插件版本、业务验收结果及完成时间。

| 情况 | 处理 |
|---|---|
| 镜像拉取失败 | 留在准备阶段，不停机；检查网络和镜像地址 |
| 备份/校验失败 | 不启动新版；处理空间或归档错误。尚未改动配置和启动新版时，可启动原数据库、等待就绪后启动原应用恢复服务 |
| 健康接口 503 / APP_COMMANDING | 查看启动日志和数据库活动，先判断迁移是否仍有进展 |
| License key not found | 官方插件没有自动更新；不要通过删除插件或绕过授权来消除提示 |
| 页面 200，但后台仍维护 | 以 `/api/__health_check` 就绪状态和迁移日志为准 |
| 新版已运行后想回退 | 执行第 7 节完整恢复；不能只换旧镜像 |
| dc: Could not open file start | 使用本手册完整 docker compose 命令，不输入 dc start |
| tar 提示文件发生变化 | 备份失败；确认 PostgreSQL 完全停止，再生成新的备份，不能靠忽略错误继续 |
| SSH 断开 | 重连后先检查容器、日志及后台进程；重新设置原 BACKUP，使用完整 docker compose 命令，不盲目重复升级 |

参考：[NocoBase Docker 升级](https://docs.nocobase.com/cn/get-started/upgrading/docker)、[插件升级](https://docs.nocobase.com/cn/get-started/install-upgrade-plugins)、[PostgreSQL 17 文件级备份](https://www.postgresql.org/docs/17/backup-file.html)。资料已在本任务此前核查；镜像标签、插件与环境状态在其他时间执行前仍应复核。
