# M2 目录重组设计

## 目标

将当前项目根目录中的场站三条能效链路计算、分钟历史、瓶颈事件、看板、测试和专属工程记录统一归档到 `m2/`，同时保持从项目根目录执行测试的能力。

## 迁移边界

移动到 `m2/`：

- `station_energy_backend.py` 与 `station_efficiency_history.py`；
- 三个“场站三条能效链路”Markdown/HTML 文件；
- 四个 M2 Python/JavaScript 测试及两个测试 support 文件；
- M2 后端、数据源、历史瓶颈相关 plan/spec；
- `.superpowers/sdd` 下两个 M2 执行记录目录。

保留在根目录：

- `m3/` 及 M3 plan；
- 通用 `skills/`；
- 项目验收附件；
- 同时涉及 M1/M2 的访谈笔记。

## 兼容策略

- 新建 `m2` 与 `m2.tests` Python 包；测试改用 `m2.*` 绝对导入。
- E2E 使用 `__dirname` 解析同一 M2 目录内的 HTML，不依赖启动命令的当前工作目录。
- M3 当前文档中指向 M2 文件的路径改为 `m2/...`。
- 历史 SDD 报告中的原始命令和绝对路径作为历史证据保留，不批量重写。
- 删除仅由本次移动文件生成的 `.pyc`，不移动缓存。

## 验收

从 `/Users/hua/Documents/LVK/code/vifa` 执行：

```sh
python3 -m unittest m2.tests.test_station_energy_backend m2.tests.test_station_efficiency_history -v
node --check m2/tests/energy_dashboard_e2e.js
node m2/tests/energy_dashboard_e2e.js
python3 -m py_compile m2/station_energy_backend.py m2/station_efficiency_history.py
```

根目录不再保留上述 M2 运行文件或 M2 专属测试。
