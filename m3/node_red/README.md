# M3 Node-RED 页面部署约定

## 永久约束

生产 Node-RED Template 节点必须使用
`m3_production_gateway_template.html`。该文件包含服务端配置注入：

- `{{{m3DashboardAuthModeJson}}}`
- `{{{m3NocobaseParentOriginJson}}}`

不得粘贴 `m3_production_gateway_page.html` 到生产 Template 节点。该文件是无
Mustache 配置注入的页面源文件，仅供本地预览、代码编辑和生成部署产物；直接用于
生产会显示“页面配置不可用”。

也可以导入完整的 `m3_production_gateway_flow.json`，其中的页面节点与上述生产 HTML
完全一致。

## 更新页面

先同步并检查生成文件：

```bash
../../.venv/bin/python m3/node_red/sync_production_gateway_flow.py
../../.venv/bin/python m3/node_red/sync_production_gateway_flow.py --check
```

只替换生产页面 HTML 时，在 macOS 本地复制正确文件：

```bash
pbcopy < m3/node_red/m3_production_gateway_template.html
```

将内容粘贴到 Node-RED 中 ID 为 `m3_prod_page_template` 的 Template 节点，然后选择
`Deploy Modified Flows`，不需要重启 Node-RED。

页面上游配置节点仍需能够读取：

```text
M3_AUTH_MODE=server_token
M3_NOCOBASE_PAGE_ORIGIN=https://ems.lvkpower.com
```
