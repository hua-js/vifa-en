# M4 选择预览 API 与页面接入

**Goal:** 通过本地 API 和线上版控制台保存显式站级偏好，对现有真实候选生成有时效/版本边界的选择预览。
**Architecture:** 新增独立 SQLite 偏好表、LiveSelectionService 和三个端点；既有 candidates/B1 不变。页面独立抽屉保存偏好、调用选择并展示模板及审计。不接设备。
**Tech Stack:** 现有 FastAPI/Pydantic/SQLite、原生 HTML/JS、unittest/Playwright。
**Spec:** CODEX_HANDOFF 当前下一步与 m4_selection/README.md。

## 约束

- GET/PUT `/m4-api/stations/{station_id}/selection-policy`，PUT 必带 expected_revision，preferences 为显式指标/三容差/完整并列顺序或 null（清除）。服务器生成 policy_id/version；初始 policy=null，不修改现存 settings 参数或自动配置客户偏好。
- POST `/m4-api/stations/{station_id}/selection` 必带 candidate_run_id 和 policy_revision。只用服务器当前候选，拒绝浏览器传计划或指标。输出独立 `m4-live-selection-v1`，绑定 run/config/revision/有效期；B1 保持 pending_ai/not_dispatched。
- 选择前后复核候选仍同一快照、参数与偏好版本、过期时间、控制/预测/电价、参与柜、SOC 和能力；复用 request_from_inputs 的时效及柜级校验。新采样只有时间/source_version改变而决策内容完全一致时可接受；旧候选原到期时间不延长。其它决策输入变化返回409，读取失败返回502/503，旧选择不回填。
- UI 偏好六项全显式，支持清除与并发冲突恢复；保存偏好不改数学候选。每站独立缓存及请求代次，切站、刷新输入、候选新轮、配置/偏好变化、过期、读取失败均不能让旧选择成为当前结果。
- UI 展示“待配置/待选择/复核中/已选预览/需重选”，已选方案与手动曲线对比不同；显式未下发。含中文模板、所选计划版本和比较记录，安全插入文本。沿用页面主题与响应式布局，账单不变。
- 只操作本地测试服务、临时数据库与 Mock 上游；不读取生产状态或写真实客户配置。核心算法不变。

## 实施与验证

- [x] TDD：偏好存储CAS/清除/站点隔离，选择成功/未配置，过期/输入变化/更换候选/偏好并发/错误隔离，API同源/非法输入。
- [x] 实现 `m4_settings/selection.py`、API 端点，复用 `m4_selection` 与 `CandidateService`。
- [x] 实现 HTML 选择面板、偏好抽屉、按站状态与代次保护，保持旧接口兼容。
- [x] 浏览器验证真实本地API＋Mock上游：配置、计算、选择、清除、冲突/错误/晚到返回、切站；覆盖1440/768/390/320明暗渲染。
- [x] 执行相关unittest及旧页面回归，独立审查，更新短交接和使用说明。

验证记录：相关 unittest 62 项通过；新选择页面与旧候选页面回归通过。已按失败用例修复偏好草稿 revision、逐柜 SOC 抵消变化、读取失败状态码和跨窗口候选复核；独立复审未发现阻断项。全部使用本地临时数据库与 Mock 上游；正式客户偏好及部署不在本次完成范围。

## 电站 1 明确候选顺序（2026-09-08）

用户已明确“均衡”就是 balanced。补充独立偏好 `profile_priority`：两层需量筛选后按显式候选顺序比较，第三层记录顺序排名，排名容差固定为 0；不把 balanced 等同 SOC 指标，不修改数学核心。保留三候选契约，pv 排最后且不启用光伏指标。旧指标及旧结果版本兼容；新选择器版本单独标识。

实施顺序：先测试 balanced 优先、需量可淘汰 balanced、不可用候选回退及非法排名容差；实现契约/选择器/API 与页面；运行相关测试和页面回归并审查；重启已授权本地服务后，使用版本检查保存 station-1（需量两容差 0，profile_priority，排名容差 0，balanced/cost/pv），读回核对。保留 station-2 和原调度参数。

本补充已完成：67 项相关测试与新页面回归通过，覆盖4种宽度明暗截图；独立审查无阻断项。本地 8844 已重启，station-1 CAS 保存为 revision=1 并读回一致；原 settings 文档哈希及 station-2 偏好保持不变。
