# Task 4 分层目标求解报告

## 结果

- 新增 `m4_optimizer/lexicographic.py`：按 `ObjectiveProfile.objective_order` 逐层求解，并用绝对/相对容差锁定已完成目标。
- 新增 `tests/test_m4_optimizer_lexicographic.py`：覆盖零容差保持主目标最优，以及绝对容差允许的主目标权衡。
- 总时限从统一起始时间计算，每层只使用扣除已耗时后的剩余秒数。
- 某层没有可行 incumbent 时立即返回该层状态；任一层仅为 `feasible` 时最终状态保持 `feasible`。

## TDD 证据

### RED

命令：

```text
.venv/bin/python -m unittest tests/test_m4_optimizer_lexicographic.py -v
```

结果：失败，`ModuleNotFoundError: No module named 'm4_optimizer.lexicographic'`。

### GREEN

命令：

```text
.venv/bin/python -m unittest tests/test_m4_optimizer_lexicographic.py -v
```

结果：2 tests passed。

## 回归验证

命令：

```text
.venv/bin/python -m unittest tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_lexicographic.py -v
```

结果：21 tests passed。

## 关注点

- 当前 `ProfileSolveResult` 在后续层无 incumbent 时返回 `x=None`，由上层按失败/超时状态处理，不复用不完整的前层解。
- `uv run` 因受限环境无法初始化 `/Users/hua/.cache/uv`；按任务要求使用 `.venv/bin/python` 完成验证。

## 修复轮 1：错误状态 incumbent 门禁

### RED

新增回归测试 `test_error_with_finite_incumbent_does_not_continue_to_next_layer`，模拟第一层返回 `status="error"` 但带有限 `x`，第二层返回 `optimal`，要求第一层立即返回且只调用一次 solver。

命令：

```text
.venv/bin/python -m unittest tests/test_m4_optimizer_lexicographic.py -v
```

结果：预期失败；修复前结果状态为 `optimal` 而非 `error`，证明错误状态解被误继续。

### GREEN

最小修复：仅当层状态为 `optimal`/`feasible` 且 `x` 全部有限时继续；其他状态立即按原状态返回并置 `x=None`。

命令：

```text
.venv/bin/python -m unittest tests/test_m4_optimizer_lexicographic.py -v
.venv/bin/python -m unittest tests/test_m4_optimizer_contracts.py tests/test_m4_optimizer_model.py tests/test_m4_optimizer_solver.py tests/test_m4_optimizer_lexicographic.py
```

结果：分层测试 3 tests passed；累计 M4 测试 22 tests passed。
