# M1 / M2 主题与 M3 统一

跨模块的颜色语义、排版、组件状态、响应式和验收要求见 [VIFA 界面设计规范](../../docs/DESIGN.md)。本文件只维护主题适配与同步方法；实际 token 值以主题 CSS 和 M3 模板为准。

背景基准为 `m3/node_red/m3_production_gateway_template.html`：复用其明暗底色、两层径向渐变、面板叠层、边框和阴影。交互配色、排版和组件细节可按页面用途自行设计，不强制绿色或 M3 蓝色；须保持可读性、业务语义及功能完整。

当前 M1 使用蓝紫交互主色（浅色 `#5b5bd6`，深色 `#a5b4fc`），SOC 进度条采用天蓝→靛蓝→淡紫渐变；M2 使用 M3 的 `--blue`（浅色 `#477df0`，深色 `#74a7ff`）。绿色用于在线/正常及能流。这是现有设计，不是后续设计必须遵守的配色限制。

M1 使用与 M3 相同的整页滚动背景机制，渐变随内容高度展开；内容长度不同仍会影响渐变覆盖范围，不保证逐像素一致。桌面告警栏采用 sticky 定位，平板与手机恢复普通文档流。手机页头透明且随页面滚动；场站承载主面板表面，系统分组用分隔线，设备使用柔和底色。相关规则在主题源内仅作用于 `data-vifa-page="m1"`。

`vifa-m3-theme.css` 为主题适配源，使用 `--m3-*` 前缀避免覆盖 M2 同名但含义不同的变量。两个 HTML 入口内嵌同一份 CSS（`style#vifa-m3-theme`），保留原有单文件部署，不需要额外公开 CSS 路由。M1、M2 均支持明暗切换，并沿用同一个 `dashboard-theme` 本地偏好。

修改主题后，在仓库根目录同步内嵌样式：

```python
from pathlib import Path

css = Path('shared/styles/vifa-m3-theme.css').read_text()
for name in ['m1/web/dashboard_energy.html',
             'm2/web/场站三条能效链路能流图.html']:
    file = Path(name)
    before, rest = file.read_text().split('<style id="vifa-m3-theme">\n', 1)
    _, after = rest.split('</style>', 1)
    file.write_text(before + '<style id="vifa-m3-theme">\n' + css + '</style>' + after)
```

部署时替换实际使用的 HTML 入口；本次主题修改不需要更新后端或数据库。
