# M3 当前用户鉴权与服务端 Token 隔离设计

日期：2026-08-27\
状态：用户已确认，实施中

## 1. 目标

M3 看板沿用 M2 已验证的“服务凭据只在服务端使用”原则，同时增加 NocoBase 当前用户
鉴权。打开 NocoBase 页面并已登录的用户可以查看 M3；直接访问 Node-RED API、缺少登录
凭据或凭据失效的请求不能读取预测结果。

本设计只调整 iframe 鉴权链路，不修改 StatsForecast、两电站独立预测、NocoBase 四表、
Worker 写入流程或 M1/M2。

## 2. 两类 Token 的职责

| Token | 所属主体 | 存放位置 | 用途 |
|---|---|---|---|
| 当前用户 Token | 浏览器中的已登录 NocoBase 用户 | NocoBase 页面运行时和 iframe 闭包 | Node-RED 调用 `/api/auth:check` 确认当前用户 |
| Dashboard 只读 Key | M3 服务账号 | `/etc/vifa-m3/dashboard.env` | Dashboard 容器通过 HTTP 读取四张预测表 |

两类 Token 不混用。当前用户 Token 不写入 Node-RED 环境、日志、URL、DOM、
`localStorage` 或 `sessionStorage`；Dashboard 只读 Key 不进入 Node-RED、HTML 或浏览器。

## 3. 请求流程

```text
NocoBase JS 区块
  ├─ ctx.getVar("ctx.token") 读取当前用户 Token
  └─ 创建 iframe: http://192.168.1.53:1880/ett
       │
       ├─ child -> parent: m3-auth-ready + 一次性 nonce
       ├─ parent 校验 event.source 和 Node-RED 精确 origin
       └─ parent -> child: m3-auth-token + 同一 nonce + 当前用户 Token
            │
            └─ iframe GET /energy-forecast-api
                 Authorization: Bearer <当前用户 Token>
                 │
                 ├─ Node-RED -> 测试 NocoBase /api/auth:check
                 ├─ 删除 Authorization 和当前用户 Token
                 └─ curl --unix-socket dashboard.sock /dashboard
                      └─ Dashboard 使用服务端只读 Key 读取预测表
```

测试环境的用户认证地址使用本机 NocoBase `http://127.0.0.1:13000`；预测表数据源由
Dashboard 容器的独立配置决定，二者可以不同。生产环境的页面 origin 和用户认证地址均为
`https://vifa.hlszh.com`。

## 4. iframe 握手协议

Node-RED 的 `/ett` 只注入非敏感配置 `M3_NOCOBASE_PAGE_ORIGIN`，值必须是完整且精确的
HTTP(S) origin，不得包含路径、query、fragment、用户名或密码。

子页面生成密码学随机 nonce，并只向配置的精确父 origin 发送：

```json
{"type":"vifa-m3-auth-ready","nonce":"<一次性随机值>"}
```

父页面必须同时校验：

- `event.source === iframe.contentWindow`；
- `event.origin === new URL(iframe.src).origin`；
- 消息只包含 `type` 和 `nonce`，且字段类型和长度符合合同。

父页面只向精确 iframe origin 回应：

```json
{"type":"vifa-m3-auth-token","nonce":"<同一随机值>","token":"<当前用户 Token>"}
```

子页面必须同时校验 `event.source === window.parent`、精确父 origin、一次性 nonce、精确字段
集合和 Token 字符范围。nonce 使用一次后立即失效，Token 只保存在闭包中。双方都禁止使用
`postMessage(..., "*")`。

## 5. Node-RED 边界

`GET /energy-forecast-api` 继续执行现有防护：

- 拒绝 query、body 和不符合合同的 Authorization；
- 只向固定 `M3_NOCOBASE_BASE_URL/api/auth:check` 发起认证；
- 只接受成功响应中的正整数当前用户 ID；
- 认证成功后删除浏览器 Authorization，再执行固定 Unix Socket curl；
- 不把 stderr、上游正文、凭据或堆栈返回浏览器；
- Dashboard Socket、URL 和命令均不能由浏览器输入改变。

`/ett` 返回 `Cache-Control: no-store`。Node-RED 环境只包含：

- `M3_NOCOBASE_BASE_URL`：当前用户所属 NocoBase 的固定 origin；
- `M3_NOCOBASE_PAGE_ORIGIN`：承载 iframe 的 NocoBase 页面精确 origin。

删除临时的 `M3_NOCOBASE_IFRAME_TOKEN`，HTML 中不得出现 JWT 或固定 Token。

## 6. NocoBase 区块

采用 NocoBase JS 区块，不把 Token 写进普通 iframe URL 或 HTML 字符串。区块通过
`ctx.getVar("ctx.token")` 读取当前用户 Token，动态创建带
`sandbox="allow-scripts allow-same-origin"` 和 `referrerpolicy="no-referrer"` 的 iframe。

区块重跑时先移除旧消息监听器、清空旧闭包中的 Token，再创建新 iframe。页面销毁或替换
后旧 iframe 的消息因 `event.source` 不匹配而不能取得新 Token。

## 7. 部署配置

测试环境：

```text
M3_NOCOBASE_BASE_URL=http://127.0.0.1:13000
M3_NOCOBASE_PAGE_ORIGIN=http://192.168.1.53
```

生产环境：

```text
M3_NOCOBASE_BASE_URL=https://vifa.hlszh.com
M3_NOCOBASE_PAGE_ORIGIN=https://vifa.hlszh.com
```

若生产 Node-RED iframe 地址不是当前测试地址，只修改 NocoBase JS 区块中的固定 iframe
URL；不能把它改成用户输入或页面 query。

## 8. 验证与回滚

验证必须覆盖：未握手不请求 API；错误 origin/source/nonce 被忽略；正确握手后只发起同源
固定 GET；请求带当前用户 Bearer 且不带 cookie、referer、query 或 body；Node-RED 无 Token
返回 401；有效当前用户返回两站结果；`/ett` 和 Node-RED 环境中不存在固定 Token。

服务器变更前备份 `/root/.node-red/flows.json` 和 `/etc/vifa-m3/nodered.env`。回滚时恢复这
两个文件并重启 `nodered.service`，不修改 Docker 容器、预测表或 M1/M2。
