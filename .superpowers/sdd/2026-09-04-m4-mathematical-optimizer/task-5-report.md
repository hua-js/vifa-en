# Task 5 Report: 计划解码、指标复算与独立验证

## RED

- 新增 `tests/test_m4_optimizer_validation.py` 后运行
  `.venv/bin/python -m unittest tests/test_m4_optimizer_validation.py -v`。
- 结果：按预期失败，`ModuleNotFoundError: No module named 'm4_optimizer.metrics'`。

## GREEN

- 新增 `m4_optimizer.metrics`：从解向量解码公开计划，基于请求与公开计划独立复算全部指标。
- 新增 `m4_optimizer.validation`：逐点验证功率平衡、SOC 状态和边界、功率/进线/反送/PV/可用性约束、终端 SOC、需量超限下界及购售互斥；成功候选还会比较全部复算指标。
- 新增篡改 SOC、购售同时发生、指标和非成功候选内容的回归测试。
- 验证：
  - `.venv/bin/python -m unittest tests/test_m4_optimizer_validation.py -v`：6 passed。
  - `.venv/bin/python -m unittest tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_lexicographic.py tests/test_m4_optimizer_validation.py -v`：28 passed。
  - `.venv/bin/python -m compileall -q m4_optimizer tests/test_m4_optimizer_validation.py`：passed。
  - `git diff --check`：passed。

## 观察

- 修复前，`demand_exceed_kw` 曾按模型松弛变量下界验证；修复后该内部变量不再公开，公开计划基于购电功率重算精确超限量。

## 修复轮 1：公开需量事实与原始向量防护

### RED

- 新增以下回归测试后运行
  `.venv/bin/python -m unittest tests/test_m4_optimizer_validation.py -v`：
  - 实际无超限但每点高报 `demand_exceed_kw`，同时同步重算指标；
  - 原始解向量 `charge=-10`；
  - 原始解向量同一点充放电均为正。
- 结果：9 项中 3 项按预期失败。高报需量未被验证器拒绝，两个原始向量违例均未被 `materialize_plan()` 拒绝。

### GREEN

- `materialize_plan()` 在解码前拒绝任何显著负内部变量及同时显著充放电，并从公开 `grid_import_kw` 与需量限制重算 `demand_exceed_kw`。
- `validate_candidate()` 要求公开 `demand_exceed_kw` 与 `max(grid_import_kw - demand_limit_kw, 0)` 在容差内精确相等。
- 验证：
  - `.venv/bin/python -m unittest tests/test_m4_optimizer_validation.py -v`：9 passed。
  - `.venv/bin/python -m unittest tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_lexicographic.py tests/test_m4_optimizer_validation.py -v`：31 passed。
  - `git diff --check`：passed。
