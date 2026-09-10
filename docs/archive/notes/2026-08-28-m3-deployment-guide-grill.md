# M3 部署说明：Grill / Discovery Notes
Date: 2026-08-28 · Goal: 把 M3 部署说明收敛为操作人员可独立执行、可验证、可回滚的部署 Runbook

## Summary / key decisions

- 本轮聚焦 `docs/m3/部署说明.md` 的部署边界、执行顺序、测试/生产差异、验证、故障处理与回滚。
- 首要读者是熟悉 Linux、Docker、Node-RED 和 NocoBase 的运维人员，以 `root`/`sudo`
  在现有服务器执行。
- 文档必须做到开发者不在场也能逐条操作、验证结果；出现异常时立即停止并按说明排查。
- `docs/m3/部署说明.md` 是唯一事实来源，按公共准备、测试环境部署、生产环境部署组织。
- `m3/M3 测试到生产部署操作手册.md` 只保留迁移检查清单和主手册链接，不重复配置与命令。
- 生产部署不允许重启共享 Node-RED；M1/M2 不得因 M3 部署发生短暂中断。
- 生产环境已经存在 Node-RED 和 NocoBase；M3 是对既有共享系统的增量接入，不负责安装
  或替换这两个基础设施。
- 生产环境的 M3 四表已经建立，部署说明不包含建表操作，只复核既有表结构、唯一索引、
  `id` 自动递增和最小权限。
- M3 部署禁止安装、升级、重启或修改生产 Node-RED/NocoBase 的全局配置。
- 生产服务器确认存在 Ubuntu 20.04、ARM64、Docker 27.3.1、Docker Compose 2.29.7；
  M3 不自动安装或升级 Docker/Compose。
- 生产不能依赖 systemd `EnvironmentFile` 为 M3 注入 Node-RED 配置，必须改为仅部署 M3
  修改节点的方式。
- Node-RED 的非秘密配置随 M3 Flow/分组保存；运维只 Deploy 修改的 M3 flows/nodes，
  不安装 systemd drop-in，不维护 `/etc/vifa-m3/nodered.env`，Token 不进入 Flow。
- 生产预测数据源/NocoBase 四表位于 `https://vifa.hlszh.com`，但界面展示在另一个站点；
  数据地址、当前用户鉴权地址和 iframe 父页面 origin 必须分别建模。
- 生产展示站点本身已有 NocoBase 和 Node-RED：展示 NocoBase 负责登录、JS 区块和
  `/api/auth:check`，展示 Node-RED 提供 `/ett` 与 `/energy-forecast-api`；架构与测试环境
  的“远端数据 + 本地展示站点”基本一致。
- 生产展示 NocoBase origin 是 `https://ems.lvkpower.com`；生产展示 Node-RED origin 是
  `https://opdash.lvkpower.com`。iframe 为跨 origin HTTPS，必须使用精确 origin/source/
  nonce 的 `postMessage`，并将 iframe URL 固定为 `https://opdash.lvkpower.com/ett`。
- `https://ems.lvkpower.com/api/auth:check` 尚未现场验证；这是生产启用 `postmessage` 的
  阻断项，不能用固定 Token 绕过。
- 生产凭据由现有系统管理员在部署前按最小权限准备；部署人员只安装，不创建账号、不扩大
  权限。原始源只读、Worker 四表写入、Dashboard 四表只读、Worker 管理接口 Token 分离，
  且只进入 `/etc/vifa-m3` root-only 文件或 Docker secret。
- 代码确认：Worker 启动先拉取两站最近 90 天历史并选择模型，再启动调度器；常规预测在
  每小时 `02/17/32/47` 分运行。若启动恰逢调度分钟，会立即执行并写 M3 表。
- 用户确认生产首次启动后立即启用正式调度；部署必须避开 `02/17/32/47`、`00:30` 和
  `01:02`，先等待恢复和健康检查完成，再观察下一个正常预测时点。
- 生产展示 Node-RED 当前不存在 acceptance-context 与 alerts 接口；普通预测/看板可以
  上线，但连续七天验收尚不具备启用条件，不能宣称完整验收链路已部署。
- 仓库旧 `energy_forecast_flow.json` 含这两个路由的合同实现，但与旧全量接口混在一起；
  直接导入会产生重复 `/energy-forecast-api`，且 acceptance-context 仍缺少真实上游状态，
  不能作为可直接上线的补丁。
- 生产分两阶段：第一阶段只上线普通预测与看板，并明确七天验收未启用；第二阶段提供独立
  acceptance-context/alerts M3 Flow，验证真实上游状态后才开始七天计时。
- 第一阶段新增 `M3_ACCEPTANCE_ENABLED=false`：保留普通预测和模型选择，跳过每日 `01:02`
  验收，不调用不存在的接口、不产生每日伪失败。第二阶段改为 `true` 时只重建 M3 Worker，
  不重启 Node-RED。
- 生产回滚也禁止重启 Node-RED：停止/回退 M3 容器，在编辑器中恢复或删除 M3 节点，只
  Deploy 修改的 flows/nodes，并复核 M1/M2；四表数据和 NocoBase schema 保留。
- 用户结束逐题拷打并转入交付：需要部署架构流程图、人工导入的 Node-RED JSON 和
  NocoBase JSON。第 18 问的不可变镜像标签策略尚未确认。

## Q&A log

### Q1 — 首要读者与可执行性标准
- Asked: 最终生产部署由谁执行，文档是否应当假设开发者在场？
- Captured: 用户确认：由熟悉 Linux、Docker、Node-RED 和 NocoBase 的运维人员，以
  `root`/`sudo` 操作；文档应允许开发者不在场时独立执行、验证和排障。
- Flags: 无。

### Q2 — 文档唯一事实来源
- Asked: 两份现有部署文档如何定位，测试和生产内容是否继续混写？
- Captured: 用户确认：以 `docs/m3/部署说明.md` 为唯一事实来源，内部拆分公共准备、测试环境
  部署和生产环境部署；另一份手册只保留迁移检查清单及链接，不再重复命令和配置。
- Flags: 无。

### Q3 — 共享 Node-RED 的重启边界
- Asked: 生产部署是否允许在维护窗口内短暂重启共享 Node-RED？
- Captured: 用户明确回答“不允许重启”。这是生产部署硬性约束，M3 不能造成共享
  Node-RED 及 M1/M2 的短暂中断。
- Flags: 需确认改用 M3 Flow/分组级非秘密环境变量，并只 Deploy 修改节点 -> 用户确认。

### Q4 — 生产环境现状与增量接入边界
- Asked: 是否把三个非秘密配置放入 M3 Flow，并只部署修改节点？
- Captured: 用户补充原话：“更新信息，生产环境已有node-red、nocobase。”这确认生产是
  既有 Node-RED/NocoBase 上的增量接入，但尚未直接确认配置存放方式。
- Flags: 部署说明是否完全排除 Node-RED/NocoBase 的安装、升级和重启 -> 用户确认；三个
  非秘密配置是否放入 M3 Flow/分组环境变量 -> 用户确认。

### Q5 — 既有基础设施与四表边界
- Asked: 部署说明是否明确排除 Node-RED、NocoBase 的安装和升级？
- Captured: 用户确认，并补充“M3四表已经建立好了，不需要再建立”。因此生产只检查既有
  Node-RED/NocoBase，增量接入 M3；四表只做结构、索引和权限复核，不执行建表。
- Flags: 三个非秘密配置是否放入 M3 Flow/分组环境变量 -> 用户确认。

### Q6 — 生产服务器运行基线
- Asked: 生产服务器的 OS、架构、Docker 和 Compose 版本是否已确认？
- Captured: 用户确认生产确实存在 Ubuntu 20.04、ARM64、Docker 27.3.1、Compose 2.29.7；
  并明确“M3不自动安装或者升级Docker”。
- Flags: 无。

### Q7 — 无重启的 Node-RED 配置与双站点更新
- Asked: 是否把 `M3_AUTH_MODE`、NocoBase 地址和页面 origin 随 M3 Flow 保存，从而不重启
  Node-RED？
- Captured: 用户确认，并更新信息：“数据源用的https://vifa.hlszh.com，界面展示在其他站点。
  与现在测试环境的架构差距不大”。因此 Flow 级配置方向确认，但不能继续把数据地址与展示
  站点地址写成同一个值。
- Flags: 展示站点是否就是当前用户登录所在的 NocoBase，是否支持 JS 区块及
  `/api/auth:check` -> 用户确认；展示站点精确 origin -> 用户提供。

### Q8 — 展示站点组成
- Asked: 生产界面所在的其他站点是否也是 NocoBase，并支持 JS 区块和当前用户 Token？
- Captured: 用户确认：“其他站点也是nocobase以及node-red”。因此生产沿用测试环境的
  远端数据源 + 展示站点 NocoBase/Node-RED 分层，但生产采用当前用户鉴权模式。
- Flags: 展示 NocoBase 和展示 Node-RED 的精确 origin -> 用户提供；展示 NocoBase 是否
  已确认支持 JS 区块和 `/api/auth:check` -> 用户或现场验证。

### Q9 — 展示站点地址
- Asked: 生产展示 NocoBase 与展示 Node-RED 的完整 origin 分别是什么？
- Captured: 用户提供 `https://ems.lvkpower.com`，但未区分它是 NocoBase origin，还是
  NocoBase 与 Node-RED 的共同入口。
- Flags: `https://ems.lvkpower.com` 是否同时代理 `/ett`、`/energy-forecast-api` 和
  NocoBase -> 用户或生产运维确认。

### Q10 — 展示 Node-RED 独立 origin
- Asked: `https://ems.lvkpower.com` 是否同时承载 NocoBase 和 Node-RED？
- Captured: 用户明确 Node-RED 是 `https://opdash.lvkpower.com/`。规范化 origin 为
  `https://opdash.lvkpower.com`；NocoBase 与 Node-RED 不同源。
- Flags: `https://ems.lvkpower.com/api/auth:check` 是否接受该站当前用户 Token -> 现场验证。

### Q11 — 当前用户鉴权前置验证
- Asked: 是否已确认 `GET https://ems.lvkpower.com/api/auth:check` 携带当前用户 Bearer 后
  返回 2xx 且 `data.id` 为正整数？
- Captured: 用户回答“不确认，没验证过”。
- Flags: 上线前由生产 NocoBase/运维使用普通当前用户 Token 完成验证；失败则阻断生产
  `postmessage` 上线，禁止以固定 Token 绕过 -> 生产 NocoBase/运维。

### Q12 — 生产凭据责任与隔离
- Asked: 生产凭据由谁准备，是否按原始源只读、Worker 写入、Dashboard 只读、Worker 管理
  四类用途分离？
- Captured: 用户确认。凭据由现有系统管理员预先准备，部署人员只安装，不自行创建账号或
  扩大权限；凭据不进入 Flow、JS、文档或仓库。
- Flags: 各 Token 的实际创建人、交付渠道与轮换日期 -> 生产系统管理员在上线工单中填写。

### Q13 — 首次启动与调度启用
- Asked: 生产首次启动后是否允许 Worker 立即进入正式调度？
- Captured: 用户确认。Worker 启动后立即启用调度，但部署时间必须避开预测、模型选择和
  七日基线调度分钟；先确认启动恢复及健康状态，再观察下一个自然调度点。
- Flags: 无。

### Q14 — 连续七天验收依赖接口
- Asked: 生产 Node-RED 是否存在 acceptance-context 与 alerts 两个接口？
- Captured: 用户明确回答“目前不存在”。
- Flags: 是否将生产拆为普通预测/看板阶段与七天验收阶段，以及第一阶段是否需要显式关闭
  `01:02` 验收调度 -> 用户确认；两个接口的实现与验证 -> 后续 M3 Node-RED 工作。

### Q15 — 两阶段生产上线
- Asked: 是否接受先上线普通预测/看板、后上线连续七天验收，并禁止直接导入旧全量 Flow？
- Captured: 用户确认。第一阶段不宣称完整验收部署；第二阶段使用独立的两个依赖路由并从
  验证通过后开始七天计时。
- Flags: 第一阶段是否增加显式开关关闭 `01:02` 验收调度 -> 用户确认。

### Q16 — 第一阶段七天验收开关
- Asked: 是否增加 `M3_ACCEPTANCE_ENABLED=false`，让第一阶段跳过 `01:02` 验收而保留普通
  预测与模型选择？
- Captured: 用户确认。第二阶段接口验证完成后改为 `true`，仅重建 M3 Worker 容器。
- Flags: 需要修改 Worker 配置、调度器和部署模板，并补测试 -> 后续实现工作。

### Q17 — 无 Node-RED 重启的回滚
- Asked: 生产回滚是否也必须做到不重启 Node-RED？
- Captured: 用户确认。回滚只处理 M3 Docker 服务与 M3 Flow 节点，不操作 Node-RED
  服务进程，不删除四表数据，不修改 NocoBase schema；回滚后必须验证 M1/M2。
- Flags: Docker 镜像与代码如何保留上一版本以支持真实回滚 -> 用户确认。

### Q18 — 交付请求与未决镜像策略
- Asked: Docker 镜像是否采用不可变版本号并至少保留当前版、上一版？
- Captured: 用户未直接回答，转而要求“给我输出部署架构流程图，设计的node-red以及
  nocobase.json，由人工导入”。访谈在此转入架构设计与交付阶段。
- Flags: 镜像不可变版本策略仍未确认 -> 用户后续确认；NocoBase JSON 的具体导入对象和
  当前版本支持格式 -> 设计阶段核对。

## Open flags (pending input)

- 展示 NocoBase 的 `/api/auth:check` 是否已现场确认接受当前用户 Token -> 生产 NocoBase/运维
- 各生产 Token 的创建人、交付渠道和轮换日期 -> 生产系统管理员
- acceptance-context 与 alerts 路由的实现和验证 -> 后续 M3 Node-RED 工作
- `M3_ACCEPTANCE_ENABLED` 的代码、配置模板与测试 -> 后续 M3 Worker 工作
- Docker 镜像不可变版本与上一版本保留策略 -> 用户确认
- NocoBase JSON 是页面/区块导入还是其他元数据导入 -> 用户确认
