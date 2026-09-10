# M4 确定性候选选择实施计划

**Goal:** 将已求解候选按明确配置确定性选择，输出可审计的本地预览和中文模板。
**Architecture:** 独立 `m4.selection` 接受 OptimizationRequest/Result 与可选 SelectionPolicy。重新校验请求、结果绑定和每套计划；按需量峰值、累计超限量、偏好指标逐层取容差内最小组，再按配置顺序选择。CLI 只读取 JSON 并向 stdout 输出预览。
**Tech Stack:** Python 3.12、现有 Pydantic、Fraction、unittest；不增加依赖。
**Spec:** 当前 CODEX_HANDOFF.md 的“程序按显式配置选择＋中文模板解释”，及本文件以下约束。

按项目约定在当前工作区顺序执行，保留现有改动；不自动提交、不创建工作区或实施代理。完成后按 requesting-code-review 技能进行一次独立审查。

## 约束与接口

- `select_candidate(request, result, policy=None) -> SelectionResult`；复制并严格重验传入模型，不信任被 model_copy/原位修改绕过校验的数据。
- 结果必须绑定相同站点、请求、计划起点、观察时间、来源版本，且 balanced/cost/pv 各一次；目标版本与请求一致、计划版本符合当前优化器生成规则。结构或身份不合法抛 ValueError，CLI 退出 2。
- 逐候选调用现有独立校验器，容差保持 1e-6；非 optimal/feasible 或计划复验失败则排除，保留原因。排序用再次计算的指标，不能以客户端 validated 字段为依据。
- 门禁顺序：合法性校验 → 设备不可用 → 无可用候选 → 缺偏好 → 选择。即使仅一套可用，也必须显式配置偏好。
- Policy 必填站点、policy_id、version、三个非负有限容差（kW、kWh、所选指标单位）、metric 与完整无重复 tie_order。metric 仅支持 energy_cost、preferred_soc_deviation、pv_unabsorbed_energy_kwh；全部最小化。无业务默认，无候选 optimal/feasible 额外排序。
- 每层候选与该层真正最小值比较，差值 <= 容差；用 Fraction(str(value)) 表示精确十进制值，避免 float 加容差和外部 Decimal 上下文舍入错误，不能相邻链式合并。
- 输出保留完整策略、规范化请求/结果 SHA256、选中 profile/version/plan_version、逐层指标及留下的候选、排除原因；模板 <=50 字，说明候选范围和容差并列，不称真实收益/全局最优。
- 仅 `usage=preview_only`、`dispatch_status=not_dispatched`，不输出设备动作。历史回放不校验当前墙钟新鲜度；接实时服务前须由服务复核时效和版本。
- 不修改数学核心、B1 契约、UI、客户配置，不调用模型或网络。CLI 仅 stdout，退出 0=选出预览，1=门禁阻断，2=输入/配置错误。

## Task 1：本地选择模块

文件：`m4/selection/contracts.py`、`service.py`、`__init__.py`；测试 `m4/tests/test_m4_selection.py`。

- [x] 编写带真实守恒计划的测试：缺配置、三种偏好、需量两级、负电费/1e-6边界、反序稳定、feasible 可选、候选排除、设备门禁、身份/版本/重复错误、非有限值、不可变输入与结果快照。
- [x] 运行 `.venv/bin/python -m unittest discover -s tests -p test_m4_selection.py`，确认模块缺失导致 RED。
- [x] 实现 SelectionPolicy/SelectionResult 与 select_candidate；每层执行 `minimum=min(values); keep=[id for id in ids if Fraction(str(values[id]))-minimum <= tolerance]`，所有 policy 参数由调用者提供。
- [x] 同命令 GREEN；针对真实计划重算、非法状态和输出一致性审查。

## Task 2：可运行入口与回放

文件：`m4/selection/__main__.py`、`README.md`、`m4/tests/test_m4_selection_cli.py`。

- [x] CLI 测试先 RED：JSON 读入、缺配置退出 1、成功退出 0、无效配置/文件退出 2，stdout 不包含错误或半成品。
- [x] 实现 `python -m m4.selection --request INPUT --result RESULT [--policy POLICY]`；不写输入文件。
- [x] 文档提供明确 evaluation_only 的调用示例，注明不写正式配置、时效由实时接入层负责；用保留 N01/N02/N03 六组偏好回放，另检查未配置返回。
- [x] 独立代码审查，完成本模块及相关优化器/编排回归，更新简短交接与完成状态。

## 完成记录

2026-09-08：新增本地模块和 CLI，未修改数学核心/B1/UI/客户配置，未调用网络、模型或设备。

- 新模块 23 项测试通过（含 CLI 5 项）；先完成缺模块/缺入口 RED，再实现 GREEN。独立审查发现 Decimal 外部精度误选及极端日期错误边界，三项测试先复现后修复，审查复核通过。
- 90 项优化器及 62 项编排回归通过。六个保存工况/偏好组合 CLI 回放符合预期，未配置门禁通过；精确比值修复后六份预览逐字段一致。
- 回放输出：`runtime/m4/deterministic-selection-debug/README.md`；配置均为 evaluation-only，未写入正式参数。
- 接口仅提供历史/本地预览；接入实时服务与页面、复用时效/版本门禁是后续工作。
