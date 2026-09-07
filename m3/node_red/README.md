# M3 Node-RED 页面部署约定

## 永久约束

生产 Node-RED Template 节点必须使用
`m3_production_gateway_template.html`。该文件包含服务端配置注入：

- `{{{m3DashboardAuthModeJson}}}`
- `{{{m3NocobaseParentOriginJson}}}`

直接维护 `m3_production_gateway_template.html`，不再保留 page.html 副本。
只更新页面 HTML 时，不更新 `m3_production_gateway_flow.json`，也不运行旧的
`sync_production_gateway_flow.py` 同步脚本。Flow 文件保留原有版本，不代表最新页面。

## 更新页面

直接编辑生产 Template HTML；在 macOS 本地复制：

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
