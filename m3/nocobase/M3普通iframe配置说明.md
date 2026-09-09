# M3 NocoBase 普通 iframe 配置说明

默认 Flow 使用 `M3_AUTH_MODE=query_token`，iframe URL 填写：

```text
https://opdash.lvkpower.com/ett?token={{ ctx.token }}
```

`{{ ctx.token }}` 由 NocoBase 解析为当前用户 Token，无需配置 iframe Header 或 postMessage。
默认 `M3_AUTH_BASE_URL=https://ems.lvkpower.com`，必须与签发 Token 的 NocoBase 一致。
导入新版 Flow 后，使用当前 `m3_production_gateway_template.html` 覆盖 `m3_prod_page_template` 节点，
再发布 Modified Flows；无需重启 Node-RED。

## 认证与验收

- 缺少、重复、空白或未解析的 Token 参数被页面入口拒绝。
- 页面读取 Token 后清除自身 URL 参数，只在内存中保存，不写浏览器存储；直接刷新 iframe 后需从 NocoBase 重新打开。
- 看板、自定义预测和统计请求通过 `Authorization: Bearer …` 传递当前用户 Token。
- Node-RED 经固定 `/api/auth:check` 校验成功后才调用服务；无效或过期 Token 返回 401，不访问 Worker。
- 当前认证确认登录用户有效，沿用两站白名单，不新增按用户划分场站权限。
- 页面正常展示两站、默认当天预测、历史查询与自定义预测；登录失效时停用提交并提示重新打开。
- Worker 管理令牌与 Dashboard 只读 Key 仅保留在服务端，不能作为 `ctx.token` 写入 URL。
- 首次入口请求含用户 Token，访问日志须隐藏 token 参数；响应使用 no-store 和 no-referrer。

`postmessage` 兼容模式保留；显式改为 `server_token` 会恢复公开访问。仅停用 iframe 区块与 M3 Flow 即可回退，
不删除预测数据或令牌文件。详见 [部署手册](../AMD64三域Docker部署手册.md)。
