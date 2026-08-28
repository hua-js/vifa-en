# M3 ARM64 同域 Docker 部署手册（已废弃）

> 现行生产架构已改为 AMD64 三域普通 iframe。请使用
> [AMD64三域Docker部署手册.md](./AMD64三域Docker部署手册.md)，不要继续执行本文件。

本手册适用于：

```text
Linux ARM64
Docker 27+
Docker Compose v2
NocoBase 与 Node-RED：https://vifa.hlszh.com
部署用户：root 或具备 sudo 权限的用户
```

本次不建 M3 四表，不修改 M1/M2，不修改反向代理，不重启 Node-RED。

## 1. 确认机器架构

```bash
uname -m
docker --version
docker compose version
```

`uname -m` 必须输出：

```text
aarch64
```

## 2. 上传目录

完整代码放在：

```text
/userdata/holo/pyfiles/vifa-m3
```

至少包含：

```text
Dockerfile
compose.yaml
docker-entrypoint.sh
m3-forecast-api.py
m3_worker/
m3/
```

## 3. 创建运行目录

```bash
sudo mkdir -p /userdata/holo/pyfiles/vifa-m3/run
sudo mkdir -p /etc/vifa-m3
sudo chown 10001:10001 /userdata/holo/pyfiles/vifa-m3/run
sudo chmod 700 /userdata/holo/pyfiles/vifa-m3/run
```

## 4. 配置原始源 Token

创建文件：

```bash
sudo vi /etc/vifa-m3/raw-source.token
```

文件只保存 Token 本身，不添加 `Bearer `、变量名或引号。设置容器用户只读权限：

```bash
sudo chown root:10001 /etc/vifa-m3/raw-source.token
sudo chmod 640 /etc/vifa-m3/raw-source.token
```

## 5. 配置 Worker

复制示例：

```bash
sudo cp m3/deploy/m3.env.example /etc/vifa-m3/m3.env
sudo vi /etc/vifa-m3/m3.env
```

必须确认：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/raw-source.token
M3_SOURCE_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_ACCEPTANCE_ENABLED=false
M3_TIMEZONE=Asia/Shanghai
```

`M3_STATIONS_JSON` 填写两个真实电站 ID。`M3_NOCOBASE_API_KEY` 使用四表写入 Key。
`M3_SOURCE_API_TOKEN` 和 `M3_ADMIN_API_TOKEN` 当前只需使用两个不同的随机值满足启动契约：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

不要把生成值发到聊天或写入部署文档。

## 6. 配置 Dashboard

```bash
sudo cp m3/deploy/dashboard.env.example /etc/vifa-m3/dashboard.env
sudo vi /etc/vifa-m3/dashboard.env
```

必须确认：

```env
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/raw-source.token
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_TIMEZONE=Asia/Shanghai
```

`M3_STATIONS_JSON` 必须与 Worker 完全相同。`M3_DASHBOARD_NOCOBASE_API_KEY` 使用 Dashboard
四表只读 Key。

设置两个配置文件权限：

```bash
sudo chown root:root /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
sudo chmod 600 /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
```

确认没有占位符：

```bash
sudo grep -n 'REPLACE_' /etc/vifa-m3/m3.env /etc/vifa-m3/dashboard.env
```

正常情况下没有输出。

## 7. 构建 ARM64 镜像

```bash
cd /userdata/holo/pyfiles/vifa-m3
sudo docker build -t vifa-m3:0.1.0 .
```

确认架构：

```bash
sudo docker image inspect vifa-m3:0.1.0 --format '{{.Architecture}}'
```

必须输出：

```text
arm64
```

## 8. 启动双容器

```bash
sudo docker compose config -q
sudo docker compose up -d --no-build
sudo docker compose ps
```

初次读取历史和训练可能持续数分钟，Worker 暂时显示 `health: starting` 属于正常现象。

## 9. 检查 Unix Socket

```bash
sudo ls -l run/worker.sock run/dashboard.sock
```

检查 Worker：

```bash
sudo /usr/bin/curl --fail --silent --show-error \
  --unix-socket /userdata/holo/pyfiles/vifa-m3/run/worker.sock \
  http://localhost/health
```

检查 Dashboard：

```bash
sudo /usr/bin/curl --fail --silent --show-error \
  --unix-socket /userdata/holo/pyfiles/vifa-m3/run/dashboard.sock \
  http://localhost/health
```

确认 Node-RED 运行用户能够读取两个 Socket；不要通过放开整个部署目录权限解决问题。

## 10. 导入 Node-RED Flow

在 Node-RED 编辑器中：

1. 备份现有 Flow；
2. 搜索 `/ett` 和 `/energy-forecast-api`；
3. 停用旧的同名 M3 路由，保留回退；
4. 导入 `m3/node_red/m3_production_gateway_flow.json`；
5. 确认 Flow 名称为“M3 负载预测”；
6. 确认三个 Flow 变量均指向 `https://vifa.hlszh.com`；
7. 选择 Deploy Modified Flows；
8. 不重启 Node-RED。

## 11. 配置 NocoBase iframe

按 [M3普通iframe配置说明.md](./nocobase/M3普通iframe配置说明.md) 人工创建普通 iframe。

关键配置：

```text
标题：场站未来能耗预测
URL：https://vifa.hlszh.com/ett
Header：Authorization
Header 值：Bearer + 当前用户 Token 变量
```

如果现场 iframe 区块不能添加 Header，或 `/ett` 没有命中 Node-RED，立即停止，不把 Token
改放 URL，也不修改反向代理。

## 12. 日志

```bash
sudo docker compose logs --tail=100 vifa-m3-worker
sudo docker compose logs --tail=100 vifa-m3-dashboard
```

日志中不得出现 Token、Authorization Header、NocoBase Key 或密码。

## 13. 回退

先停用 NocoBase iframe 和 Node-RED M3 Flow，然后：

```bash
cd /userdata/holo/pyfiles/vifa-m3
sudo docker compose stop
```

需要重新启用时：

```bash
sudo docker compose start
```

不要删除 M3 四表、预测历史、env 或 Token 文件。

## 14. 本次未执行项

按部署要求，本次代码交付不运行自动化或端到端测试。生产上线后仍需人工确认 Socket、
Node-RED 路由、NocoBase 当前用户 Header 和两站页面数据。
