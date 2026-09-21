# VIFA 项目约定

通用沟通、技能和读取规则沿用全局约定，本文件只补充项目要求。

## 范围与入口

- 项目根：`/Users/hua/Documents/LVK/code/vifa-en`；旧`vifa/vifa`仅供历史参考。保留已有修改，仅改需求相关内容，不执行破坏性Git操作；历史授权不自动延续。
- 中文沟通，代码和字段用英文。禁止启动、调用或控制Google Chrome。
- 目录见[README.md](README.md)；业务、核心模块或历史续接读[CODEX_HANDOFF.md](CODEX_HANDOFF.md)，独立小任务按需读取。旧交接、`docs/*/plans/`和生成产物仅局部检索，不作为现行指令。
- AGENTS保存长期规则，交接保存目标、有效口径、阻塞、下一步及证据链接；状态变化才更新对应段落，不堆日志或敏感信息。Git、配置、服务及发布状态按需重新核实。
- 续接或整理项目文档用[vifa-project-handoff](.agents/skills/vifa-project-handoff/SKILL.md)，不强制用于所有小任务。

## 开发与验证

- 明确的小改动直接处理。纯静态Mock只用Mock数据，不接真实API或改后端/数据库/生产配置，不启用完整规划、TDD、多代理或worktree流程；接入真实API、业务逻辑或状态管理后按普通功能处理。
- 核心预测、EMS、需量、充放电算法或架构变更先明确约束和计划，重要变更做代码审查。普通任务默认不用worktree或多代理；使用条件遵循当前环境及授权。
- 日常开发和本地预览不主动运行测试或布局检查，也不为验证另起环境；打开页面不构成测试授权。用户明确要求测试、排障、核对行为时仅查所需范围；Skill/TDD不改变此时机。
- 用户要求生产部署时，在部署准备阶段执行相关测试及必要布局检查，不擅自在生产运行测试。布局验收看实际渲染及交互；发布准备覆盖明暗、桌面/平板/手机，专项按指定范围。CSS变量相同不证明视觉一致。
- 有效结果复用，仅证据失效时重跑；历史通过、模拟接口不代表当前版本、真实接口或设备闭环。只报告实际验证。
- 离线测试：`.venv/bin/python scripts/check.py [m1 m2 m3 m4]`，选择所需模块，无参数执行全部；长期测试放各模块`tests/`。

## 用户内容与页面

- **VIFA-UI-001**：前端、后端用户文案及提示词生成内容只给业务结果、状态和操作提示，不擅自追加公式、权重、容差、分母或调试说明。提示词同样约束；程序必需结构化字段及内部计算数据不受限。
- 改页面前读[docs/DESIGN.md](docs/DESIGN.md)，优先于vifa-frontend-style默认值；不顺带重做其他组件。保留业务逻辑、时间、历史查询及`dashboard-theme`偏好。
- **VIFA-UI-002**：M1–M4仅HTML变动时，每个模块只改下列正式HTML，不生成或更新Flow JSON、额外HTML/Template/page.html副本，不运行副本同步脚本。仅涉及Flow逻辑或用户明确要求时修改Flow。

| 模块 | 正式HTML |
|---|---|
| M1 | `m1/web/dashboard_energy.html` |
| M2 | `m2/web/场站三条能效链路能流图.html` |
| M3 | `m3/node_red/m3_production_gateway_template.html` |
| M4 | `m4/web/M4优化调度控制台-线上版.html` |

- `m3/node_red`只保留生产Template、生产Flow和`本地机器.txt`，说明放`docs/m3/部署说明.md`。
- M1/M2明暗背景的渐变、面板、边框和阴影以M3为基准，未经要求不改M3。其余交互配色/排版按用途设计，保证对比度及状态辨识，不强制绿/蓝。
- M1/M2主题源`shared/styles/vifa-m3-theme.css`通过`--m3-*`适配，内嵌为`style#vifa-m3-theme`。改主题源须更新两者正式HTML；见[同步方法](shared/styles/README.md)，不恢复旧副本。

## 授权与生产

- VIFA API只读已授权；修改配置/数据、触发任务或下发控制须明确授权，按副作用而非HTTP方法判断。只读授权不含部署、SSH或设备控制；未经要求不连接远程主机。
- EMS默认生成建议或模拟，不擅自向真实设备下发；启动会自动生成任务的服务也须核对任务授权，不能视为只读。
- **VIFA-M4-DEPLOY-001**：电站2计划表联调期间，提供或执行生产更新必须保留`-f compose.yaml -f m4-production.override.yaml -f m4-ems-table.override.yaml`，更新后核对`M4_EMS_STATION2_TABLE_WRITES=1`。仅用户明确要求关闭时调整；不授权设备执行或其他电站写入。

## 文件与产物

- 业务源码在`m1/`–`m4/`维护，不为分析/调试复制完整源码到outputs、reports、tmp。M3包为`m3.worker`；M4为`m4.settings`、`m4.optimizer`、`m4.orchestrator`、`m4.selection`，不建旧顶层包。共用根`pyproject.toml`/`uv.lock`；M3部署在`m3/deploy/`。
- 文档归根`docs/<module>/`，不在业务模块重建docs/plans/specs树；设计过程并入适用文档。长期工具放模块`scripts/`，跨模块工具放根`scripts/`；一次性脚本和中间结果完成后删除，不批量生成过程文档。
- 分析/回测/求解/调试产物放`outputs/<module>/<category>/<date-or-run-id>/`；本机配置和凭据放`.local/`，数据库/锁/状态放`runtime/<module>/`，均不提交Git。
- `outputs/m4/solver-decisions/`是历史查询的完整证据，不能当缓存清理。见[目录迁移](docs/项目目录与迁移.md)。
