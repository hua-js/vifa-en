# VIFA EN

场站能源监控、能效分析、负荷与光伏预测、优化调度项目。正式源码按 M1–M4 组织；项目已从旧双层目录迁移到当前仓库根。

| 目录 | 用途 |
| --- | --- |
| `m1/` | 能源监控接口、`web/` 页面和 `tests/` |
| `m2/` | 能效计算、历史查询、`web/`、`node_red/`、`tests/` |
| `m3/` | `worker/` 预测服务、`scripts/` 数据工具、`data/` 输入、部署和测试 |
| `m4/` | `settings/` 参数/API、`optimizer/` 优化器、`orchestrator/` 编排、`selection/` 决策、页面和测试 |
| `shared/styles/` | M1/M2 共用主题 |
| `docs/` | 各模块使用、部署、设计与历史文档 |
| `outputs/<module>/<category>/<run-id>/` | 分析、回测、发布及完整决策证据，默认忽略 Git |
| `runtime/<module>/` | SQLite、Socket、锁、日志等持续状态，默认忽略 Git |
| `.local/` | 本机凭据与配置，默认忽略 Git |
| `scripts/check.py` | 统一离线单元测试入口 |

## 本地环境与测试

依赖由根 `pyproject.toml` 和 `uv.lock` 管理，Python 3.12。已有迁移后的 `.venv` 可直接使用；新环境可使用 `uv sync --frozen`。

```bash
# 从仓库根运行全部现有单元测试，或只选择模块
.venv/bin/python scripts/check.py
.venv/bin/python scripts/check.py m1 m2
.venv/bin/python scripts/check.py m3
.venv/bin/python scripts/check.py m4

# 查看 CLI 帮助，不触发业务操作
.venv/bin/python -m m4.orchestrator --help
.venv/bin/python -m m4.selection --help
.venv/bin/python -m m4.selection.decision_chain --help
```

M3 FastAPI 入口为 `m3.worker.main:app`。M4 应用工厂为 `m4.settings.api:create_app`；启动配置与真实数据权限参照模块部署说明。目录迁移不意味着服务已经部署或运行。

## 页面与文档

- [M1/M4 产品化配置与交付](docs/产品化配置与交付.md)：统一项目配置、站点注册、配套Flow/HTML与版本化发布；不包含MES/EMS指令下发。先使用 `scripts/check_project.py --project <项目JSON>` 离线检查配置。

- [VIFA 界面设计规范](docs/DESIGN.md) · [共用主题维护](shared/styles/README.md)
- [M1 页面](m1/web/dashboard_energy.html) · [M1 时间与历史告警](docs/m1/时间与历史告警说明.md)
- [M2 页面](m2/web/场站三条能效链路能流图.html) · [M2 历史查询](docs/m2/历史查询说明.md)
- [M3 生产模板](m3/node_red/m3_production_gateway_template.html) · [M3 部署说明](docs/m3/部署说明.md)
- [M4 线上页面](m4/web/M4优化调度控制台-线上版.html) · [M4 部署说明](docs/m4/deploy/部署手册.md)
- [目录迁移与验证记录](docs/项目目录与迁移.md)

修改共用主题后同步两个实际页面，不恢复旧副本。历史发布快照保存其原始内容；`outputs/m4/solver-decisions/` 包含页面历史查询所需证据，不能视为随时可删的缓存。

当前验证状态与待办见 [CODEX_HANDOFF.md](CODEX_HANDOFF.md)。M4的 `m4/deploy/backend/` 已恢复，发布入口为 `m4/deploy/build-and-push.sh`；尚有测试失败，不能据此认定已通过发布验收。发布范围、前置条件及命令见 [生产发布与接口缺口](docs/m4/deploy/生产发布与接口缺口.md)。日常开发不默认运行上述测试命令，执行时机以 [AGENTS.md](AGENTS.md) 为准。
