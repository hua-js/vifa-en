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

- `demand_exceed_kw` 按模型约束验证为不低于实际超限量；在仅最小化吞吐量的可行解中该松弛变量可高于下限，强制相等会错误拒绝任务测试中的可行计划。
