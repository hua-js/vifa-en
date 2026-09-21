# VIFA association read guard

NocoBase 服务端插件。0.1.2 补齐界面兼容检查要求的 dist/externalVersion.js，声明 2.2.x 兼容范围及具体 2.2.2 依赖基线；服务端策略与 0.1.1 相同。本地检查通过，生产界面上传与真实业务验收待完成。

- `server/policy.json`：受限角色及八个集合的普通字段白名单。
- `server/guard.js`：角色确认后拒绝关联读取，保留原始行过滤和 ACL。
- `server/index.js`：通过 NocoBase resourceManager 注册中间件，无核心文件补丁。
- `node --test tests/guard.test.cjs`：本地逻辑测试。

安装、停用、真实 API 证据及未覆盖范围见 [实施任务书](../../docs/NocoBase-AI员工实施任务书.md)第1.7–1.9节。策略中未列出的角色不受本插件限制；生产角色和版本需单独确认。关联列页面可能因本策略返回403，须回归实际使用页面。插件不是全平台权限修复。

`client-entry.js` 提供不修改界面的客户端插件；NocoBase 会在登录前加载插件入口。修改后执行 `node build-client.cjs` 同步两种 dist 入口，再运行测试并打包。`package.json` 的 files 必须包含两套入口及顶层 lane 标记。部署后须检查 pm:listEnabled 中全部动态入口，并单独验收页面，首页 200 不足以证明可用。

打包执行 `npm run build` 和 `npm pack`（prepack 也会构建）。归档必须含 dist/externalVersion.js；它记录实际依赖的具体基线版本，不能填 2.x 等范围，也不能用空对象跳过检查。
