# M4 求解器决策实施计划

**Goal:** 将 M4 主链迁移为 Pyomo + HiGHS 的调度及最终决策，退出 AI。

**Architecture:** 原生 Pyomo 调度模型生成三候选，数学决策保留已保存偏好，独立复验及实时门禁约束最终预览。使用独立求解器运行与记录入口。

**Tech Stack:** Python 3.12、Pyomo 6.10.1、highspy 1.15.1、现有 FastAPI/SQLite/HTML。

**Spec:** ../specs/2026-09-08-m4-solver-decision-design.md

## 全局约束

- 保留未提交改动、经营偏好、安全校验，不操作生产/SSH/模型/EMS。
- 三候选业务口径不变，最终界面强调一套方案；历史记录不能重新成为当前有效计划。
- 先运行针对新增行为的失败测试，再实施；回归按相关范围执行。

## 1. 原生模型与后端

- [x] 在 m4/tests/test_m4_optimizer_model.py 增加具名 Pyomo 模型断言；在 solver 测试覆盖新后端 optimal/feasible/timeout/infeasible 及无残留解。
- [x] m4/optimizer/model.py 使用具名 Var/Constraint/Expression 表达现有规则，保留 BuiltModel.index/objectives 供独立解码及目标锁。
- [x] m4/optimizer/solver.py 用 Pyomo HiGHS 接口替换 SciPy 调用，保存剩余时间预算和 gap 判断。
- [x] pyproject.toml/uv.lock 固定新依赖；service.py 报告实际框架和求解器版本。
- [x] 执行 `.venv/bin/python -m unittest discover -s tests -p 'test_m4_optimizer*.py'`。

## 2. 数学决策

- [x] 新增 m4/selection/solver_selection.py，接口 `select_candidate_with_solver(request, result, policy=None) -> SelectionResult`。
- [x] 采用 Pyomo 单选二进制变量及分层顺序，沿用现有精确容差和独立复验；异常或未证实最优时不选择。
- [x] 保留旧 select_candidate 供历史审计及新结果独立复核；LiveSelectionService 生产默认调用新函数。
- [x] 新增 m4/tests/test_m4_solver_selection.py：各偏好、极端数值/容差/并列、坏计划、无偏好、求解失败及与旧基线等价。

## 3. 无 AI 运行链和接口

- [x] 新增 m4/selection/decision_chain.py、m4/settings/decision_runs.py、decision_results.py；运行链只用配置、输入、候选和 selection 接口。
- [x] api.py 新增 POST/GET decision-runs、GET decision-result；旧 AI 接口已在后续清理中删除（404）。
- [x] 原始证据与报告独立保存，状态包含完成/配置阻断/输入阻断/求解阻断/复核阻断，后台锁和请求幂等有覆盖。
- [x] 新增链路、API和结果篡改测试，使用 Mock 上游和临时目录。

## 4. 契约、界面与交接

- [x] m4/orchestrator/contracts.py 新输出 v2/pending_selection，旧 v1 读取兼容已在后续清理中删除。
- [x] 两个 M4 HTML 读取新中性契约；线上控制台用新运行/历史结果，三候选对比保留，主状态退出 AI。
- [x] 浏览器 Mock 验证运行、状态、最终计划、切站/展开/主题及响应布局；旧模型历史已在后续清理中删除。
- [x] 更新优化器/选择/编排说明、M4 概要设计与 CODEX_HANDOFF；审查实际 diff 和相关回归后报告结果。

## 完成验证

2026-09-08：全M4单测410项通过，6组浏览器回归通过，独立审查问题已修复，依赖版本一致，差异空白检查通过。本地8844服务已加载新链；未触发真实上游求解、模型或EMS。该成绩属于迁移时点；后续清理验证以交接文件为准。
