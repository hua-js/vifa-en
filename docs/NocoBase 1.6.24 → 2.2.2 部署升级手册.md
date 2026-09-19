# NocoBase 1.6.24 → 2.2.2 部署升级手册

> 适用对象：第一次操作 Docker / NocoBase 的现场人员
> 操作方式：登录服务器后，在 Linux 终端中执行命令
> 当前系统：NocoBase 1.6.24
> 目标版本：NocoBase 2.2.2
> 数据库：PostgreSQL 17.4，本次**不升级数据库**

------

# 一、升级前先看这里

这次升级只做一件主要的事情：

**把 NocoBase 1.6.24 的应用镜像替换成 NocoBase 2.2.2。**

以下内容全部保持不变：

- PostgreSQL 数据库不升级
- 原数据库数据继续使用
- 原附件 storage 继续使用
- 原访问地址不变
- 原 host 网络不变
- 原 APP_KEY、数据库账号等配置不变
- 原工作流数据继续保留

## 最重要的原则

### 1. 不要直接升级

必须先备份。

因为 NocoBase 2.2.2 第一次启动时，会自动修改原来的数据库结构。

所以如果升级失败：

**不能只把镜像改回 1.6.24。**

必须同时恢复：

- 原数据库
- 原 storage
- 原 Compose 配置

------

### 2. 看到报错就停止

本手册里的命令请：

**一段一段执行，不要全文一次性复制进去。**

任何一步出现明显报错，都先停止，不要继续往下执行。

------

### 3. 以下命令不要执行

升级过程中不要执行：

```bash
docker compose down -v
```

不要执行：

```bash
docker system prune
```

也不要手动重新拉取：

```bash
holodb:latest
```

因为这些操作可能影响现有数据或原数据库镜像。

------

# 二、本次服务器信息

本手册已经按照当前服务器环境写好，正常情况下不需要修改路径。

| 项目            | 当前值                                                       |
| --------------- | ------------------------------------------------------------ |
| Compose 项目    | `nbdb1624`                                                   |
| Compose 目录    | `/home/opt/1panel/docker/compose/nbdb1624`                   |
| Compose 文件    | `docker-compose.yml`                                         |
| NocoBase 服务   | `holobase`                                                   |
| NocoBase 容器   | `nbdb1624-holobase-1`                                        |
| PostgreSQL 服务 | `holodb`                                                     |
| PostgreSQL 容器 | `nbdb1624-holodb-1`                                          |
| 当前 NocoBase   | `1.6.24`                                                     |
| 目标 NocoBase   | `2.2.2`                                                      |
| 目标镜像        | `registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full` |
| storage         | `/userdata/holo/nb/storage`                                  |
| PostgreSQL 数据 | `/userdata/holo/postgresql`                                  |
| 备份目录        | `/userdata/nocobase-backups`                                 |
| CPU             | ARM64 / aarch64                                              |

------

# 三、开始前准备

## 第 1 步：进入 root

如果当前已经看到类似：

```text
root@ubuntu:~#
```

说明已经是 root，可以跳过。

否则执行：

```bash
sudo -i
```

然后执行：

```bash
bash
```

------

## 第 2 步：设置升级环境

复制执行：

```bash
set -uo pipefail
umask 077

cd /home/opt/1panel/docker/compose/nbdb1624

export TARGET_IMAGE='registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full'

export BACKUP="/userdata/nocobase-backups/pre-2.2.2-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP"

printf '本次备份目录：%s\n' "$BACKUP"
```

最后会显示类似：

```text
本次备份目录：/userdata/nocobase-backups/pre-2.2.2-20260919-160000
```

### 重要

记住这个目录。

后面备份和回退都要使用它。

------

# 四、升级前检查

## 第 3 步：检查 Docker 服务

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml ps
```

正常情况下应该能看到：

```text
holobase
holodb
```

并且都处于运行状态。

------

## 第 4 步：检查 Compose 配置

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml config --quiet
```

### 正常情况

命令执行后**没有输出**。

没有输出一般表示 Compose 配置语法正常。

### 如果出现报错

停止升级。

不要继续。

------

# 五、检查磁盘空间

执行：

```bash
df -h /userdata /
```

再执行：

```bash
docker info --format '{{.DockerRootDir}}'
```

再检查数据库和附件占用：

```bash
du -sh /userdata/holo/postgresql /userdata/holo/nb/storage
```

备份文件全部放在：

```text
/userdata/nocobase-backups
```

不要放到 `/root` 或根分区。

------

# 六、确认当前版本

执行：

```bash
docker exec nbdb1624-holobase-1 node -p \
'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
```

正常应该看到：

```text
1.6.24
```

如果已经显示：

```text
2.2.2
```

说明已经升级过。

**不要继续执行升级，只做后面的验收检查。**

------

# 七、确认数据库版本和扩展

执行：

```bash
docker exec nbdb1624-holodb-1 psql -X -U postgres -d holodb \
-c 'SHOW server_version; SELECT extname,extversion FROM pg_extension; SELECT spcname,pg_tablespace_location(oid) FROM pg_tablespace;'
```

这里主要是留记录。

当前数据库已知为 PostgreSQL 17.4。

同时记录 TimescaleDB 等扩展版本，升级后再进行对比。

------

# 八、确认挂载和网络

执行：

```bash
docker inspect nbdb1624-holobase-1 nbdb1624-holodb-1 \
--format '{{.Name}} network={{.HostConfig.NetworkMode}} ports={{json .NetworkSettings.Ports}}'
```

然后：

```bash
docker inspect nbdb1624-holobase-1 nbdb1624-holodb-1 \
--format '{{.Name}} {{json .Mounts}}'
```

还要检查数据库目录有没有软链接：

```bash
find /userdata/holo/postgresql -maxdepth 2 -type l -ls
```

### 如果发现

数据库 WAL、表空间等被放到了其他目录：

**停止。**

当前备份范围可能不完整，不能直接继续。

------

# 九、先备份配置和旧 Docker 镜像

执行：

```bash
tar --acls --xattrs --numeric-owner -cpf "$BACKUP/compose-project.tar" \
-C /home/opt/1panel/docker/compose nbdb1624
```

保存当前应用和数据库镜像 ID：

```bash
APP_ID=$(docker inspect -f '{{.Image}}' nbdb1624-holobase-1)

DB_ID=$(docker inspect -f '{{.Image}}' nbdb1624-holodb-1)

printf '%s\n' "$APP_ID" > "$BACKUP/old-app-image-id.txt"

printf '%s\n' "$DB_ID" > "$BACKUP/old-db-image-id.txt"
```

给旧镜像增加一个本地回退标签：

```bash
docker tag "$APP_ID" local/nocobase-rollback:1.6.24

docker tag "$DB_ID" local/holodb-rollback:pre-2.2.2
```

保存旧镜像：

```bash
docker image save -o "$BACKUP/original-images.tar" \
local/nocobase-rollback:1.6.24 \
local/holodb-rollback:pre-2.2.2
```

------

# 十、提前下载 NocoBase 2.2.2

执行：

```bash
docker pull --platform linux/arm64 "$TARGET_IMAGE"
```

下载完成后检查：

```bash
docker image inspect "$TARGET_IMAGE" \
--format '{{.Os}}/{{.Architecture}} {{json .RepoDigests}}' \
| tee "$BACKUP/target-image.txt"
```

正常应该看到：

```text
linux/arm64
```

### 如果镜像下载失败

不要停数据库。

不要继续升级。

先解决镜像下载问题。

------

# 十一、进入正式维护窗口

从这里开始会停服务。

在继续之前，请确认：

- 已通知相关人员
- 已暂停外部数据同步
- 已暂停定时任务
- 已暂停其他写 PostgreSQL 的程序
- 已确认当前可以停机维护

------

# 十二、停止 NocoBase

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
stop -t 120 holobase
```

------

# 十三、停止 PostgreSQL

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
stop -t 120 holodb
```

------

# 十四、确认两个容器真的停了

执行：

```bash
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
```

执行：

```bash
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
```

这两条正常情况下都不会输出内容。

然后执行：

```bash
docker inspect -f 'exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' \
nbdb1624-holodb-1
```

理想结果类似：

```text
exit=0 oom=false
```

再查看数据库最后的日志：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
logs --tail=40 holodb
```

### 必须确认

数据库是正常关闭。

如果看到：

- OOM
- 强制 kill
- 异常退出
- 数据库没有正常 shutdown

不要继续备份。

先处理数据库正常关闭问题。

------

# 十五、正式冷备份

下面这一整段可以作为一个整体执行：

```bash
(
set -euo pipefail

: "${BACKUP:?先执行前面的步骤设置 BACKUP}"

test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
test "$(docker inspect -f '{{.State.ExitCode}}' nbdb1624-holodb-1)" = 0
test "$(docker inspect -f '{{.State.OOMKilled}}' nbdb1624-holodb-1)" = false

docker commit \
nbdb1624-holobase-1 \
local/nocobase-runtime-rollback:1.6.24

docker image save \
-o "$BACKUP/runtime-image.tar" \
local/nocobase-runtime-rollback:1.6.24

tar --acls --xattrs --numeric-owner \
-cpf "$BACKUP/postgresql.tar" \
-C /userdata/holo postgresql

tar --acls --xattrs --numeric-owner \
-cpf "$BACKUP/storage.tar" \
-C /userdata/holo/nb storage

tar -tf "$BACKUP/postgresql.tar" > "$BACKUP/postgresql-files.txt"

tar -tf "$BACKUP/storage.tar" > "$BACKUP/storage-files.txt"

test -s "$BACKUP/postgresql.tar"

test -s "$BACKUP/storage.tar"

(
cd "$BACKUP"

sha256sum \
compose-project.tar \
original-images.tar \
runtime-image.tar \
postgresql.tar \
storage.tar \
> SHA256SUMS

sha256sum -c SHA256SUMS
)

sync

test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false

printf 'BACKUP_OK\n' > "$BACKUP/backup.ready"

echo 'BACKUP_OK：备份已完成并通过校验'
)
```

------

# 十六、确认备份成功

最后必须看到：

```text
BACKUP_OK：备份已完成并通过校验
```

同时前面的 SHA256 校验应该全部显示：

```text
OK
```

如果看到：

```text
file changed as we read it
```

或者：

```text
在我们读入文件时文件发生了变化
```

说明备份无效。

### 此时不要升级

重新检查数据库是否真的完全停止，然后重新制作备份。

------

# 十七、修改 NocoBase 镜像到 2.2.2

确认已经看到：

```text
BACKUP_OK
```

后执行：

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
```

检查 Compose：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
config --quiet
```

检查实际使用的镜像：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
config --images
```

应该能够看到：

```text
registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full
```

数据库镜像不要修改。

------

# 十八、先启动数据库

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
start holodb
```

然后检查：

```bash
docker exec nbdb1624-holodb-1 \
pg_isready -U postgres -d holodb
```

正常应该看到类似：

```text
accepting connections
```

再测试数据库：

```bash
docker exec nbdb1624-holodb-1 \
psql -X -U postgres -d holodb \
-v ON_ERROR_STOP=1 \
-c 'SELECT 1;'
```

正常会返回：

```text
1
```

### 如果数据库没有 ready

先停在这里。

不要启动 NocoBase。

等待数据库正常以后重新执行检查。

------

# 十九、启动 NocoBase 2.2.2

数据库正常后执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
up -d --no-deps --pull never holobase
```

然后查看启动日志：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
logs --tail=100 -f holobase
```

NocoBase 此时可能正在进行数据库升级。

### 注意

升级过程中：

- 不要反复重启容器
- 不要再次运行其他 upgrade 命令
- 不要因为暂时 503 就马上回退

如果日志仍然在正常进行数据库迁移，可以继续等待其完成。

如果想退出日志查看：

```text
Ctrl + C
```

这里只会退出日志界面，不会停止容器。

------

# 二十、检查升级结果

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml ps
```

------

## 检查 NocoBase 版本

执行：

```bash
docker exec nbdb1624-holobase-1 node -p \
'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
```

应该看到：

```text
2.2.2
```

------

# 二十一、检查网页

执行：

```bash
curl --max-time 15 \
-sS \
-o /dev/null \
-w 'HTTP %{http_code}\n' \
http://127.0.0.1/
```

正常应返回：

```text
HTTP 200
```

------

# 二十二、检查 NocoBase 健康接口

执行：

```bash
curl --max-time 15 \
-sS \
-w '\nHTTP %{http_code}\n' \
http://127.0.0.1/api/__health_check
```

最终需要看到：

```text
ok
```

以及：

```text
HTTP 200
```

如果看到：

```text
503
```

或者：

```text
APP_COMMANDING
```

先查看 NocoBase 日志。

这可能只是数据库升级还没有完成。

------

# 二十三、再次检查数据库

执行：

```bash
docker exec nbdb1624-holodb-1 \
psql -X -U postgres -d holodb \
-c 'SHOW server_version; SELECT extname,extversion FROM pg_extension; SELECT value FROM "applicationVersion"; SELECT name,version FROM "applicationPlugins" WHERE enabled=true AND version <> '\''2.2.2'\'';'
```

确认：

- PostgreSQL 版本没有被升级
- TimescaleDB 等扩展没有异常变化
- NocoBase applicationVersion 已更新
- 检查是否还有旧版本插件

------

# 二十四、检查附件有没有被影响

执行：

```bash
tar --compare \
-f "$BACKUP/storage.tar" \
-C /userdata/holo/nb \
storage/uploads
```

然后进入 NocoBase 页面实际检查附件。

------

# 二十五、业务验收

技术检查通过以后，不要马上恢复全部外部业务。

还需要人工检查以下内容。

## 登录

测试：

- 管理员能否登录
- 普通用户能否登录

------

## 页面

检查：

- 原菜单是否存在
- 页面是否正常
- 权限范围是否正常

------

## 数据

检查几个升级前已经存在的数据：

- 查询是否正常
- 页面数据是否正常

再做一条可控测试：

- 新增
- 修改
- 查询
- 删除测试数据

------

## 附件

检查：

- 历史附件能不能打开
- 上传新附件是否正常
- 下载是否正常
- 权限是否正常

------

## 插件

重点检查：

- 导出
- 图表
- 树区块
- 模板打印
- 备份
- 商业插件

注意：

以前测试环境发现部分商业插件仍为 1.6.24，并出现：

```text
License key not found
```

所以：

**NocoBase 核心升级成功，不代表所有商业插件都已经成功升级。**

必须实际检查插件。

------

## 工作流

检查：

- 普通工作流
- JSON 节点
- Webhook
- 定时任务

注意：

恢复定时任务以前，要先确认不会因为升级恢复后产生重复执行或非预期业务动作。

------

## 外部系统

最后检查：

- API 调用
- 原 IP
- 原域名
- FRP 访问

------

# 二十六、查看最近日志

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
logs --since=15m --tail=300 holobase
```

确认没有持续出现严重异常。

------

# 二十七、全部正常以后恢复业务

确认前面的检查全部完成后，再逐项恢复：

1. 外部数据采集
2. 外部 API 写入
3. 定时任务
4. 工作流
5. 其他业务客户端

不要一次性全部恢复。

恢复过程中继续观察 NocoBase 日志。

------

# 二十八、升级完成后记录

升级完成以后记录以下信息：

```text
升级前版本：1.6.24
升级后版本：2.2.2

升级时间：
________________

备份目录：
________________

目标镜像：
registry.cn-shanghai.aliyuncs.com/nocobase/nocobase:2.2.2-full

健康接口：
________________

插件检查：
________________

业务验收：
________________
```

备份不要马上删除。

------

# 二十九、升级失败怎么办

## 情况 1：新版还没启动，备份失败

如果：

- 数据库已经停止
- 但是备份失败
- Compose 还没有修改到新版

不要继续升级。

可以重新启动原数据库：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
start holodb
```

数据库 ready 后，再启动原 NocoBase：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
start holobase
```

------

# 三十、新版已经启动，但需要回退

## 警告

NocoBase 不支持直接降级。

所以绝对不要只执行：

```text
2.2.2 → 换回 1.6.24 镜像
```

然后继续连接已经被 2.2.2 修改过的数据库。

正确做法必须同时恢复：

```text
旧 NocoBase 镜像
+
升级前 PostgreSQL
+
升级前 storage
+
升级前 Compose
```

------

# 三十一、完整回退操作

只有升级确实失败并决定回退时才执行本节。

首先：

```bash
sudo -i
bash
```

然后：

```bash
set -uo pipefail
umask 077

cd /home/opt/1panel/docker/compose/nbdb1624
```

找到升级前生成的真实备份目录。

例如：

```text
/userdata/nocobase-backups/pre-2.2.2-20260919-160000
```

设置：

```bash
export BACKUP='/userdata/nocobase-backups/pre-2.2.2-实际时间'
```

不要直接复制“实际时间”四个字。

检查：

```bash
test -f "$BACKUP/SHA256SUMS"
```

验证备份：

```bash
(
cd "$BACKUP"
sha256sum -c SHA256SUMS
)
```

必须全部：

```text
OK
```

------

# 三十二、停止新版

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
stop -t 120 holobase
```

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
stop -t 120 holodb
```

确认：

```bash
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holobase-1)" = false
test "$(docker inspect -f '{{.State.Running}}' nbdb1624-holodb-1)" = false
```

------

# 三十三、加载旧镜像

执行：

```bash
docker image load -i "$BACKUP/original-images.tar"
```

然后：

```bash
docker image load -i "$BACKUP/runtime-image.tar"
```

------

# 三十四、保留失败现场

不要直接删除升级失败后的数据。

执行：

```bash
FAILED="/userdata/nocobase-failed-$(date +%Y%m%d-%H%M%S)"

mkdir "$FAILED"
```

保存失败 Compose：

```bash
cp -a docker-compose.yml \
"$FAILED/docker-compose.failed.yml"
```

移动失败数据库：

```bash
mv /userdata/holo/postgresql \
"$FAILED/postgresql"
```

移动失败 storage：

```bash
mv /userdata/holo/nb/storage \
"$FAILED/storage"
```

------

# 三十五、恢复升级前数据

恢复 PostgreSQL：

```bash
tar --acls --xattrs --numeric-owner \
-xpf "$BACKUP/postgresql.tar" \
-C /userdata/holo
```

恢复 storage：

```bash
tar --acls --xattrs --numeric-owner \
-xpf "$BACKUP/storage.tar" \
-C /userdata/holo/nb
```

恢复 Compose：

```bash
tar --acls --xattrs --numeric-owner \
-xpf "$BACKUP/compose-project.tar" \
-C /home/opt/1panel/docker/compose
```

------

# 三十六、强制使用保存下来的旧镜像

执行：

```bash
python3 - <<'PY'
from pathlib import Path

p = Path('docker-compose.yml')
s = p.read_text()

replacements = {
    'ccr.ccs.tencentyun.com/taidai-holobase-168/holobase:1.6.24':
        'local/nocobase-runtime-rollback:1.6.24',

    'ccr.ccs.tencentyun.com/taidai-holobase-168/holodb:latest':
        'local/holodb-rollback:pre-2.2.2',
}

for old, new in replacements.items():
    assert s.count(old) == 1, '配置镜像与现场记录不一致，停止'
    s = s.replace(old, new)

p.write_text(s)
PY
```

检查：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
config --quiet
```

如果这里报错：

**不要启动任何容器。**

------

# 三十七、先恢复旧数据库

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
up -d --no-deps --pull never --force-recreate holodb
```

检查：

```bash
docker exec nbdb1624-holodb-1 \
pg_isready -U postgres -d holodb
```

然后：

```bash
docker exec nbdb1624-holodb-1 \
psql -X -U postgres -d holodb \
-v ON_ERROR_STOP=1 \
-c 'SELECT extname, extversion FROM pg_extension;'
```

只有数据库正常以后才能继续。

------

# 三十八、恢复旧 NocoBase

执行：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
up -d --no-deps --pull never --force-recreate holobase
```

查看日志：

```bash
docker compose -p nbdb1624 \
-f /home/opt/1panel/docker/compose/nbdb1624/docker-compose.yml \
logs --tail=200 holobase
```

检查版本：

```bash
docker exec nbdb1624-holobase-1 node -p \
'require("/app/nocobase/node_modules/@nocobase/server/package.json").version'
```

应该恢复成：

```text
1.6.24
```

检查网页：

```bash
curl --max-time 15 \
-sS \
-o /dev/null \
-w 'HTTP %{http_code}\n' \
http://127.0.0.1/
```

然后重新进行：

- 登录
- 数据
- 附件
- 插件
- 工作流
- API
- 原访问地址

等业务验收。

确认全部恢复以后，才能重新开放外部写入。

------

# 三十九、常见问题

| 遇到的问题               | 怎么处理                                     |
| ------------------------ | -------------------------------------------- |
| 新镜像下载失败           | 不要停服务，先解决镜像下载                   |
| Compose 校验报错         | 停止，不继续                                 |
| 数据库无法正常停止       | 不做冷备份，先解决数据库问题                 |
| 备份出现文件变化         | 备份无效，重新确认数据库完全停止             |
| SHA256 不是全部 OK       | 不升级                                       |
| 数据库 `pg_isready` 失败 | 不启动 NocoBase                              |
| NocoBase 启动出现 503    | 先看日志，可能仍在迁移                       |
| 出现 `APP_COMMANDING`    | 先确认升级是否仍在执行                       |
| 页面返回 200             | 不代表升级已经完全完成                       |
| `License key not found`  | 检查商业插件授权和插件版本                   |
| 升级后想回退             | 必须数据库 + storage + 镜像一起恢复          |
| SSH 中断                 | 重连以后先检查当前容器状态，不要重复执行升级 |
| 输入 `dc start` 报错     | 使用本手册完整的 `docker compose` 命令       |

------

# 四十、现场人员最简单执行顺序

如果只记住整体流程，可以记下面这条：

```text
1. 登录服务器
      ↓
2. 检查当前 1.6.24
      ↓
3. 检查磁盘、数据库、挂载
      ↓
4. 下载 2.2.2 镜像
      ↓
5. 进入维护窗口
      ↓
6. 停 NocoBase
      ↓
7. 停 PostgreSQL
      ↓
8. 完整备份数据库 + storage + 镜像 + Compose
      ↓
9. 必须看到 BACKUP_OK
      ↓
10. Compose 中把 NocoBase 改成 2.2.2
      ↓
11. 先启动 PostgreSQL
      ↓
12. 数据库正常后启动 NocoBase
      ↓
13. 等数据库迁移完成
      ↓
14. 确认版本 2.2.2
      ↓
15. 健康接口必须 ok + HTTP 200
      ↓
16. 登录、数据、附件、插件、工作流验收
      ↓
17. 最后恢复外部任务和写入
```

## 最重要的三个停止条件

看到下面任何一个情况：

```text
备份失败
```

或者：

```text
SHA256 校验失败
```

或者：

```text
数据库没有正常启动
```

**立即停止，不要继续执行下一步。**

升级最重要的不是“把 2.2.2 跑起来”，而是：

**升级前有完整可恢复的备份，升级后数据、附件、插件和业务都经过实际验证。**