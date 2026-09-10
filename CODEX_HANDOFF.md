## 2026-09-10 项目已迁移至 vifa-en 并完成模块目录整理

- 用户要求先迁移Git再整理；新项目根 `/Users/hua/Documents/LVK/code/vifa-en`，后续正式开发在新仓库完成。原 `/Users/hua/Documents/LVK/code/vifa/vifa` 保留，未修改其Git tracked工作区。
- 5个本地分支、2个标签与7个旧远程记录保留；当前提交cb512d9，分支feat/m4-offline-orchestration；新origin保持hua-js/vifa-en，未推送。目录调整尚未提交，可直接审查diff。
- 源码按m1–m4归整，Python包改为m3.worker与m4.settings/optimizer/orchestrator/selection；测试在模块tests，说明集中docs，产物outputs，持续状态runtime，本机配置.local。
- 1169项单元测试与旧基线相比无新增失败/错误（1142通过、14失败用例、10错误用例、3跳过）；M1/M2浏览器、M4静态/离线浏览器和网关通过。原有依赖锁、M4fixture与缺失backend问题保留。
- 391个原tracked文件初始迁移SHA一致，698个归档文件内容未变。详见 `docs/项目目录与迁移.md`、根README及outputs/project/migration/20260910/manifest.json。
- 未切换或重启旧服务，未生产部署、发布镜像或设备下发。

## 2026-09-10 当前任务MAPE版M4镜像已发布CCR

- 用户要求更新M4镜像。已构建并推送 `ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m4-current-mape-b474eb527596-amd64`，远端摘要 `sha256:f3d23f4ad0bea17d1e97d1cecd202c77764328863d64bc00c1d9d080dde402b9` 与本地匹配，linux/amd64已回读确认；保留旧镜像。
- 基于上版pv-auto镜像，白名单复制55个M4四包Python文件；未复制新HTML/真实凭据。冻结上下文 outputs/m4/releases/m4-current-mape-20260910，source SHA b474eb5275968ba03e791c322c2602b41d5017243972bec9b9609395cd1030f2，release/manifest与覆盖文件齐全。
- 离线无网络amd64容器模块导入与POLICY/30%常量检查通过。首次检查误导入不存在的api.app，改用真实模块入口后通过，无源码修改；沿用此前37项测试通过与25项live_inputs中3项旧PV预期失败记录。
- m4/deploy/m4-m3-mape.override.yaml现固定新摘要，须最后应用以覆盖旧PV镜像值；含M3 run和独立管理Token副本只读挂载。用户按docs/m4/负荷MAPE门槛说明.md准备Token，将覆盖文件放backend，然后compose原文件+PV覆盖+M3-MAPE覆盖 up -d --no-build --pull always m4-api。HTML独立替换。
- 未生产部署/SSH/设备下发，未验证生产M3连通性/当前真实MAPE，未重启本地8846。

## 2026-09-10 用户纠正MAPE口径：改为M3当前任务实测对比，未发布

- 本节替代下方七日MAPE方案。用户确认使用M3当前预测MAPE；阈值>30%阻断、=30允许、缺失阻断，光伏误差不加入，SOC规则保留。
- 核实M3页面默认900秒/1天/weekly_load_v2，当前任务为覆盖今日北京时间零点的最新可用任务；页面取result时overlay_actuals实时叠加实测。数据库manual_points实际值为空不证明M3页面无MAPE，不能再用七日评估或CV替代。
- 新m4/settings/m3_current_result.py通过M3 Worker UDS GET latest/result，专用管理Token，校验任务/站点/负荷kW序列；load_accuracy.py与M3同公式，valid/有限/非零实际配对，逐点绝对百分比误差取平均。至少1点可暂估，不要求7日605点，不增加原24小时验收有效期。两种求解入口仍复核证据。
- HTML/模板/Flow同步当前MAPE、有效点数、暂估、短任务ID。新增m4/deploy/m4-m3-mape.override.yaml：M3 run只读挂载，专用管理Token副本只读挂载。详见docs/m4/负荷MAPE门槛说明.md；不能假定M3管理Token与NocoBase/PV密钥相同。文件不含镜像，须配合新镜像。
- 37项门槛/适配/候选/PV测试通过；额外25项live_inputs测试22通过、3失败（旧历史PV来源预期，检查station2阻断源是PV，负荷及MAPE ready）。Python/JS语法、HTML/Flow一致性通过；初始JS检查误包含application/json，排除数据块后通过。页面未实际渲染检查。
- 未读真实生产Worker，真实当前MAPE未知；未发布镜像、生产操作或重启本地8846（仍旧逻辑）。最新发布7c8f镜像不含MAPE门槛。下一步构建发布新版M4，用户配置M3只读socket/admin token与独立HTML；不要声称现在生产已生效。

## 2026-09-10 负荷MAPE>30%阻断求解：代码与本地验证完成

- 用户确认只加入负荷MAPE门槛，暂不加入光伏误差门槛；光伏功率输入保留，SOC现有准入/边界保留。
- 新增m4/settings/load_accuracy.py及m4/tests/test_m4_load_accuracy.py。读取energy_forecast_evaluations最新station_total_load，复用M3七日672点/有效至少605非零点验收MAPE；新增窗口结束24小时有效期。大于30%阻断，等于30%允许；无效/缺失/样本不足/过期/读失败不放行。阈值Decimal，证据和政策绑定来源版本。
- forecast_source附accuracy_gate和阻断原因；滚动LiveInputService结束/请求构建复核，自然日DailyPlanService调用优化器前复核，daily_baseline历史请求重建用原fetched_at核验。HTML/Flow独立更新MAPE卡片与全天检查文案。未添加光伏MAPE/SOC MAPE门槛。
- 本地32项检查通过（含阈值、缺失/过期、两种入口优化器不调用与候选/PV回归）；修复开发中manual fallback误引用gate的NameError，回归已通过。Python/JS语法通过。只读检查两站评估unavailable，未获取有效MAPE，不假设为0。
- 本地8846旧PID99338已停，新服务工具会话60632加载门槛；实际GET station-2/inputs blocked，显示负荷MAPE无法评估，同时当时负荷/光伏窗口也缺覆盖。未触发求解/生产写入；未构建推送本次镜像。最新已发布7c8f镜像仍不含MAPE门槛。
- 文档docs/m4/负荷MAPE门槛说明.md。下一步可发布镜像与独立页面；启用前需M3提供有效评估，否则按用户已确认的规则阻断。不能擅自改用CV分数或关闭门槛。

## 2026-09-10 M4按需光伏版镜像已推送CCR

- 用户要求更新M4镜像。已构建并推送 ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m4-pv-auto-7c8f7b8fd258-amd64，远端digest sha256:05a95a2fed2237cd743e5483132b20d0510018d5f1147d7ef7675635754d2921、linux/amd64已核验。基础为上版vifa-m4:pv-20260910；本次只复制M4四包Python源码，未复制/打包新HTML（基础旧HTML层保留）。
- 冻结构建上下文outputs/m4/releases/m4-pv-auto-20260910，source SHA 7c8f7b8fd258f4a0da21d4065537d12ba8bfa08c5c07e50cc3ce48be545d76ac，含source-manifest/release.json，无真实凭据。保留旧镜像标签。
- m4/deploy/m4-pv.override.yaml已固定新digest，并挂载PV run只读及1panel-network；用户放服务器backend目录后，docker compose -f m3/deploy/compose.yaml -f m4-pv.override.yaml up -d --no-build --pull always m4-api。无需改.env，后续必须带覆盖文件避免退回旧镜像/网络/挂载。
- 独立HTML及m4_prepare_proxy.js仍需替换NodeRED对应节点，新增candidate-jobs路由才能支持页面后台任务。此前22本地检查及模拟Socket通过；本轮未重复测试/启动容器验证/部署生产。

## 2026-09-10 用户授权本地检查：22项相关检查与模拟Socket通过

- 用户问本地是否可以检查，已按新授权运行本地验证，覆盖此前不测试要求的本轮检查范围。新增m4/tests/test_m4_pv_on_demand.py；初次发现PreparedInputs第二次取数未复核其他来源ready，已有失败用例后修复。unittest test_m4_pv_on_demand + test_m4_candidates 共22项通过（pytest未安装，未安装新依赖）。
- 本地临时UnixSocket模拟服务验证Bearer请求、单POST、GET轮询及同job完成通过，无真实凭据/外部写入。FastAPI测试验证后台任务进度、幂等与站点隔离；已有候选测试包含模拟输入的本地优化器回归。
- 原本任务PID98429已TERM，当前源码在127.0.0.1:8846重启，工具会话77715。页面200，新candidate-jobs接口不存在任务返回预期404。GET station-2/inputs实际只读取数status ready，光伏/负荷/电价/控制/实时均ready；PV run_id a7995e69-254f-48cd-aae4-8497e5af6cf5。
- 未触发真实预测入库或设备下发，未构建发布新版镜像。本机尚未接真实PV自动更新Socket；有效预测可复用，真实按需更新仍需配置Socket。原8850只是只读查询服务。

## 2026-09-10 M4按需光伏更新与后台候选：源码完成，未发布

- 用户确认生成策略时按需更新光伏并追踪精度。新增pv_on_demand.py，只在显式策略入口检查可恢复的空/过期/覆盖不足，并要求其他来源ready；PV共享UnixSocket单次POST、固定任务ID轮询600秒，失败/超时/身份变化阻断，不自动重发，完成后回读必须匹配run_id。
- CandidateService支持准备钩子和进度；普通GET inputs仍只读，内部滚动decision chain调用准备入口。新增candidate_jobs.py、POST/GET candidate-jobs，站点隔离/请求幂等/最近40任务，页面轮询避免长请求。服务重启不重放。自然日口径未改。
- ForecastRefreshRequired区分可更新原因；前端允许仅光伏缺口时启动后台候选，显示阶段。独立HTML、Flow和新增node-red/m4_prepare_proxy.js同步两新路由；无需打包HTML。
- 新增m4/settings/pv_accuracy.py只读成熟预测/实测报告工具，按需输出本地配对及MAE/RMSE/覆盖/完整窗口电量/完整自然日电量。白天固定06–18代理，非太阳高度。未运行报告或启用定时追踪。
- m4/deploy/m4-pv.override.yaml挂载PV run只读至/run/vifa-pv，复用M4 Token，接入1panel-network；部署说明docs/m4/按需光伏更新说明.md。此前用户生产日志确认M4健康但与NodeRED无共同网络，已提供docker network connect修复命令；是否恢复未收到新结果。
- 7个Python文件AST、页面JS语法、HTML/Flow一致性检查已完成；未运行测试、真实预测/求解或生产操作。未构建/推送本次镜像，旧f188版本仅能读PV；本地8846/8850仍是原进程（8850为只读）。下一步发布新版M4并应用挂载及网关Function；本地演示需重启M4并配置真实PV Socket。

## 2026-09-10 当前源码M4本地服务已启动

- 用户要求本地运行。原8844已有项目Python进程42735，保留；新增当前源码服务127.0.0.1:8846，入口/m4，PID98429，工具会话39908。沿用默认本地settings数据库与已有只读上游凭据加载规则。
- 启动完成，GET /m4 返回200；未执行测试、触发求解或生产写入。此次仅确认页面可达，尚未验证实时光伏读取。

## 2026-09-10 M4光伏接入镜像已构建并推送CCR，待用户部署

- 用户确认继续发布。新版 ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m4-pv-f188d7dcd46f-amd64，远端digest sha256:d61c736165521033378ec5aa369ae98233a5a7d2e8918556c418771e888cd222、linux/amd64已回读匹配。旧m4-0.1.0/M3/PV标签保留。
- 旧发布脚本依赖的m4/deploy/backend目录在当前工作区缺失；未伪造恢复。改为基于已发布本机vifa-m4:m4-0.1.0（ID sha256:9d19c5926ac631ab862e61673f94b61ddee0fc8e8a97ac37633e9c20dc05245c），复制当前M4四包源码与线上HTML，保留原依赖和入口。源码白名单冻结及凭据样式扫描完成，标签按source SHA f188d7dcd46febe24fb48bed9532304b32b852d3cf08e45d2de3815989c6b5f9生成，不冒用Git提交号。
- 交付outputs/m4/releases/m4-pv-forecast-20260910/及ZIP，含源码构建上下文、HTML/Flow、source-manifest、release.json、SHA256SUMS、更新步骤。是增量交付，不含完整Compose/真实凭据。
- 下一步用户修改现有 /userdata/holo/pyfiles/m4/backend/.env 的M4_IMAGE，compose pull/up m4-api；只替换现有M4 HTML模板保留网关配置。手动生成PV完成后刷新M4电站2输入并预览候选。
- 未运行测试或容器行为验证，未生产部署/SSH、未求解或下发、未开启定时任务；仅实际完成打包/build/push/远端发布回读。

## 2026-09-10 M4滚动优化接入光伏预测：代码完成，待M4发布

- 用户反馈PV新版“OK了”，随后确认将光伏预测接入M4滚动优化；本轮未自行验证生产任务结果。
- 新增 m4/settings/pv_forecast_source.py，复用现有NocoBase客户端，只读两张energy_pv_forecast_*表。最新ES02/operational/completed批次按as_of/id选取；源96点完整性、时间/关联/步数、非负有限功率与版本格式检查，按负荷95/96点窗口取值。as_of达到7200秒或覆盖不足则阻断，不补零/拼批次/历史回退。完整训练与天气复验仍由PV发布服务负责。
- live_inputs仅滚动入口替换；读取结束及request_from_inputs再查时效/覆盖。电站1零光伏、daily_inputs调用原_pv历史参考均保留，优化器和设备控制未改。
- 线上HTML、Node-RED模板及Flow HTML节点同步显示光伏预测模型/更新时间/覆盖点数。新增 docs/m4/光伏预测接入说明.md 与实施计划。
- 已执行AST语法检查、HTML副本/Flow一致性检查；按既有用户要求未运行测试、真实API、求解、容器或生产操作。未构建/推送本次M4镜像。下一步需发布M4后端（不是重发PV镜像），同步M4页面；M4现有凭据需有两预测表list权限。手动生成PV完成后及时刷新M4输入并预览候选，无自动调度/设备下发。

## 2026-09-10 运行时集合管理403：移除管理API依赖，修复镜像已推送

- 用户确认Node-RED请求退出0、forecast已accepted，连接问题已恢复；任务bc014b70-711a-4f69-9d8c-1769bd8a5280在天气入库预检失败，0行写入。探测auth:check200/用户1、天气结构403、天气读取200；证明Token有效和数据可读，尚未证明写权限。
- import-weather-history.py与publish-pv-forecast.py改为业务表list指定字段预检，运行时不调用collections:get。空表允许首写，仍执行完整回读/值/哈希/96点验证；物理结构检查保留在独立建表工具。业务API错误补充资源名和HTTP状态，避免误报为Schema权限问题。
- 本机已构建并推送 m3-pv-b254b4941c43-amd64，digest sha256:cf83bc19ac27664ce79c2745fd93c8d7bbddcb8003eeccc0ab9ccf95ccdab799，远端linux/amd64摘要回读一致。compose.registry.yaml与m3-image.override.yaml已更新固定digest；包SHA b254b4941c432b23741052a97ab0f65671e4e6ba30a507d25637816e94350b4c。Flow沿用共享目录Token版本。
- 按用户要求不测试：本轮未运行测试/容器行为验证，无生产部署/API写入，无定时任务。下一步用户更新生产PV Compose及镜像，提交新的手动预测任务，核验入库/预测结果；旧失败任务不会自动重试。

## 2026-09-10 已定位Node-RED缺少/etc凭据路径；Flow改用现有共享目录

- 用户诊断显示cat `/etc/vifa-m3/raw-source.token` 不存在，退出1，请求未到PV服务。随后生产inspect确认 `nodered` 是root，挂载 `/opt/1panel/apps/node-red/node-red/data -> /data` 和整个 `/userdata/holo/pyfiles -> 同路径`；M3两Socket和PV Socket均可见，PV run为10001:10001/2770、Socket0666。因此无需新挂载或HTTP改造。
- 已将生产Flow生成器、生产JSON、诊断JSON的固定命令改读 `/userdata/holo/pyfiles/vifa-pv/run/.raw-source.token`。提供宿主 `install -o root -g root -m 600 /etc/vifa-m3/raw-source.token /userdata/holo/pyfiles/vifa-pv/run/.raw-source.token`，放置原Token同值副本；Python仍读取原/etc文件，不改Token值/权限或镜像/Compose。Token轮换需同步副本。此条覆盖之前“无需复制、Node-RED新增/etc挂载”的说明。
- 用户明确不测试，本轮仅生成Flow/更新说明，未运行测试或连接生产。待用户执行复制、导入更新Flow后验证；不能声称连接已恢复。当前镜像仍m3-pv-d0b0cba26165-amd64。

## 2026-09-10 生产连接待诊断；按用户要求直接输出诊断Flow，不测试

- 用户已在生产启动PV服务；此前 `/run/vifa-pv/manual` 权限报错，经提供宿主run目录10001:10001/2770修复命令后，用户日志确认 Application startup complete、监听pv.sock。
- Node-RED查询返回通用 `pv_service_unavailable`，目前缺少具体stderr/退出码/HTTP状态，不能断言Token、Socket权限或上游查询哪项失败。用户不熟悉手动接Debug，明确要求“输出flow就好了，不需要测试”。
- 已新增 `m3/pv/pv_connection_diagnostic_flow.json`：独立“光伏连接诊断”标签，仅一个手动GET latest按钮、原相同固定curl命令、三个payload-only Debug（响应/错误/退出码）；无定时、写入或HTTP入口，与原Flow并存。未修改现有Flow/镜像、未部署、未运行测试。下一步用户导入后点击诊断并提供输出，再定位实际原因。

## 2026-09-09 最新：全部复用原M3 Token，已重建并推送共享凭据版

- 用户明确“token都是一样的”，不再要求另配PV凭据。天气/预测入库、查询及Node-RED调用PV服务均统一复用宿主 `/etc/vifa-m3/raw-source.token`，Python内只读挂载 `/run/secrets/raw-source.token`；保留现有 `root:10001 / 0640`。不需要 `/etc/vifa-pv/pv-nocobase.token`、`pv-service.token` 或 `.pv-service.token` 副本。本条覆盖下方旧独立凭据说明。
- 新增 `m3/worker/pv_credentials.py`，三个读取入口共用：允许0400/0600/0440/0640、属主root或当前进程、组读需属于进程有效组，拒绝其他用户权限、组写、符号链接和无效内容；不修改原M3读取器。新增3项测试，包含导入/查询/服务共用同一0640文件。
- Flow生成器及JSON中4个固定curl命令均从 `/etc/vifa-m3/raw-source.token` 读取，仍经stdin发送。Node-RED如果容器运行，部署时需把同一文件只读映射到这个绝对路径并给运行账户对应组读权限；本轮没连接生产或自动改文件权限。Flow也必须更新，不能只拉镜像保留旧Token路径。
- 已推送新版 `ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m3-pv-d0b0cba26165-amd64`，远端回读摘要 `sha256:d1ad9d40364f731bc168d88b39a3d5138aae208197e63ba9ec2cb94f86474208`、linux/amd64。继续worker/dashboard/pv三种模式，旧标签保留；本地 `vifa-m3-pv:shared-token-20260909`。当前compose.registry.yaml/m3-image.override.yaml已固定新版digest。
- 冻结包 `outputs/m3/releases/m3_pv_shared_token_20260909.tar.gz`，80文件，SHA `d0b0cba2616502d5a6a19e5bbcd6bdbed68045bd8e5e76723cab26d85f9ba780`；未打包实际Token。生产Token真实内容/权限未远程读取，使用用户确认的相同凭据口径；容器测试使用测试Token。
- 镜像内57项光伏+7项天气测试通过，5项Flow行为测试通过。实际容器创建root:10001/0640测试文件后降权UID10001，PV通过同一文件启动成功，UDS健康200、未认证401、初始任务为空；禁网且无业务数据写入。新增发布证据 `outputs/m3/releases/m3_pv_shared_token_ccr_release_20260909.json`，部署文档 `docs/m3/pv/镜像发布说明.md` 与README已同步。尚未部署生产、启用自动任务或提交Git。

## 2026-09-09 M3 与光伏合并镜像已本机构建并推送 CCR

- 用户要求本机构建推送CRR，随后确认“vifa-3.0”指既有 `vifa-m3:0.1.0`，要求光伏一起打包。本轮已合并并推送，没有部署生产或启用定时任务。
- `m3/pv/entrypoint.sh` 保留原M3 worker/dashboard转交，增加pv模式（默认）；`m3/pv/Dockerfile` 仍基于本机AMD64 `vifa-m3:0.1.0`，基础ID `9cae460a8f508051df30c43a4626b8b34098ab1d461525719c9cf64677fda67c`。既有M3代码相对基础镜像对应提交c047783无已提交差异，没有改旧入口/Compose。
- 已推送 `ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa:m3-pv-bb3deebe5238-amd64`；远端index摘要 `sha256:995666a3ce4dea6f158e45a099edd90fcb54a78c88b7081c238c7681cac0d53c` 与本机一致，实际运行清单 linux/amd64。旧0.1.0及M4标签未覆盖。本地另有 `vifa-m3-pv:2026-09-09`。
- 新冻结构建包 `outputs/m3/releases/m3_pv_combined_20260909.tar.gz`，SHA `bb3deebe5238e9162df4c19a6601048ad47b9f77b444966957ac7916c89421e6`，沿用2,136点训练源，无凭据。旧独立手动包保留作历史；当前部署用合并镜像。
- 默认docker-container builder无法读取本地基础镜像，首次独立构建因尝试从Docker Hub拉取vifa-m3失败、未推送；随后指定 `--builder orbstack`（docker驱动）构建合并镜像成功。无需新登录，使用用户已有Docker凭据；未输出认证内容。
- 镜像内54项光伏测试通过；3个应用模块导入、训练源SHA/2,136点校验通过；worker/dashboard经隔离测试桩验证入口转交；pv实际在禁网/只读容器中启动，UDS健康200、未认证查询401、初始任务为空。没有以生产配置完整运行旧M3 Worker/Dashboard。测试容器均 --rm 自动清理。
- 文档 `docs/m3/pv/镜像发布说明.md`；新增固定digest的 `compose.registry.yaml`（光伏服务）和 `m3-image.override.yaml`（原M3两服务仅换镜像），供后续部署使用，尚未应用到生产。发布记录 `outputs/m3/releases/m3_pv_ccr_release_20260909.json`。没有提交Git或自动覆盖原交付包。

## 2026-09-09 ES02 生产手动 Flow 已输出；按用户要求暂不启用小时调度

- 用户原确认补9月天气、本机小时更新、最新预测查询，随后改为“输出生产环境 Flow，目前不需要完整小时更新流程”，并明确选定“手动采集天气、生成一次光伏预测、查询结果”。本条为当前范围：交付文件，不部署生产；没有创建 Codex automation、cron 或 Node-RED 定时任务。既有按小时 CLI 保留，不自动执行。
- 已完成215小时历史天气补采/入库：09-01 00:00至09-09 22:00，fetched_at=09-09 22:45:16.538，批次 `1b468801528555ff98402445142af149f8eaffd9595724440446a735ac102b7a`；全字段/哈希回读通过，原2,952历史行保持。工件 `outputs/m3/forecasts/weather_history_es02_20260909_refresh/`。
- 实测固定上界回查最近48小时，保留更长历史；真实 API 分页使用 hasNext、无 count，已复现后修复并加回归。原始实测61,189行，训练合格2,136点，08-18 15:00至09-09 22:00（右端不含）。完整输入SHA与历史天气来源在 `outputs/m3/training/pv_training_es02_20260909_refresh/`，未把缺失值补零。新增 `fetch-open-meteo-history.py`、`refresh-pv-training.py`，生成器增加 `--training-source`。
- 本机按次流程已经实际新增 run_id `dba09dce-7f2a-49c1-9358-f91fe7b8b430`、run_pk=2，窗口09-09 23:00至09-10 23:00，96点与50天气行核验完成；电量1679.34897975kWh、峰值247.452967kW。发布22:58:46完成，早于目标开始；同小时重复返回 already_completed，没有重复新增。状态 `runtime/m3/pv-hourly/state.json`，运行证据 `runtime/m3/pv-hourly/20260909-22/`；本次已核验事实不代表启用了定时任务。
- 新查询 `pv_query_app.py`、`services/pv_query.py` 只读最新完整 ES02 operational completed，并复算模型/96点/哈希；响应过滤训练快照，明确 fresh/stale/expired、训练截止和剩余完整点。本机8850查询服务在工具会话9225/PID92528启动，续接先确认存活；它不是生产常驻部署。已有M4端口不变。
- 生产独立 Flow：`m3/pv/pv_manual_production_flow.json`，26节点；四个手动按钮（天气、预测、任务状态、最新结果）及 GET `/pv-forecast-api/ES02/latest`。所有 inject 均无 repeat/crontab/once。公网只读入口校验 EMS 用户 Bearer；写入仅 Node-RED 编辑器手动按钮。沿用 Unix Socket + curl，不从 Node-RED 执行 Python/Docker；固定命令不追加 payload，服务令牌经 stdin，stdout/退出码按请求ID隔离。未改既有M3/M4 Flow 或页面。
- 配套 `pv_service_app.py`、`services/pv_manual_jobs.py`、`run-pv-manual.py`：固定 weather/forecast 操作、私有服务认证、后台任务与文件锁；同一时间一个任务，重复提交返回当前任务；重启把未完成任务标 interrupted，不自动重放。预测按钮自动取新天气，复用上述固定训练快照；没有在生产版加入历史自动刷新、小时调度、模型升级或EMS。
- 部署说明与打包工具在 `m3/pv/`。包 `outputs/m3/releases/pv_manual_production_20260909.tar.gz`，79文件（78文件SHA+清单），SHA256 `d899c954548465e1b8750f3cee450249791d79645b7da7ec23888c00f5958e4e`；含完整固定训练源、服务源码、独立 Compose、Flow，无凭据文件/明文JWT/私钥。基于已有 AMD64 `vifa-m3:0.1.0` 构建独立服务，生产 Socket 约定 `/userdata/holo/pyfiles/vifa-pv/run/pv.sock`；凭据/账户权限按 README 由部署者配置。
- 实际验证：54项光伏Python、7项天气、5项Node-RED Function测试通过；Compose配置解析、shell语法和包内SHA一致。包内源码在本机临时UDS服务启动，使用Flow原固定命令仅替换本地路径后真实回读run_pk2/96点，未认证401；任务初始为空。临时服务验证后停止。包内训练源离线生成/复算96点通过（run_id `3e528a66-dbe2-462c-8edc-e81325df7ff4`，未入库）；工件 `outputs/m3/verification/pv_manual_offline_replay_20260909/`。核验报告 `outputs/m3/releases/pv_manual_production_verification_20260909.json`。
- 本地 Docker Socket 对沙盒不可用，未执行镜像构建；未在真实 Node-RED runtime 导入、未连接生产主机、未SSH或下发设备。生产镜像构建、Socket账户权限和实际导入是部署阶段待验项；不要把本地Function测试描述为生产验收。真实天气预报精度仍未评估。
- 用户授权的本机 API 凭据现按0600保存在仓库外 `/Users/hua/.config/vifa/pv-nocobase-api.token`，用于已授权本机查询/工具，未打包；不要打印其内容。当前不需要再次索要凭据，也不应因原小时计划自动创建调度。计划 `docs/m3/plans/2026-09-09-pv-hourly-refresh-query.md` 已按最新范围更新。

## 2026-09-09 M4 下午六点阶段已本地提交

- 用户明确提交下午六点左右的版本；已在 `feat/m4-offline-orchestration` 提交 `0254c63`（`feat(m4): add daily EMS comparison and peak energy reserve`），包含下午阶段的30个M4文件及收尾说明。
- 已逐项核对提交内容与审阅时的文件摘要一致；暂存区为空，M4无剩余改动，其他32个工作区状态项保留。后续光伏、天气相关改动未纳入；未推送。
- 本次仅提交已有代码，未新增测试或求解。电站2零点SOC 1.933%低于最新2%安全下限的阻断仍未解决，相关业务口径问题尚待用户答复，不自动修改设置或恢复规则。

## 2026-09-09 ES02 首批未来24小时光伏预测已入库

- 用户确认继续生成未来24小时光伏预测并通过NocoBase入库。复用WeatherRidge固定9特征、训练集RMS、无截距、lambda=0.01；抽取 `pv_training.align_weather_grid` 使训练与未来使用相同小时结束辐照/中点瞬时字段对齐，新增 `pv_operational.py`、`services/pv_publication.py` 和 `generate-pv-forecast.py`、`publish-pv-forecast.py`、`plot-pv-forecast.py`。没有改旧负荷/SOC预测入口或M4。
- 真实获取新天气于北京时间2026-09-09 22:28:53.684，50个小时标签21:00至09-11 22:00，全部入库并逐字段/哈希回读通过；新批次 `42d75bd4f1626a50eefa73da5ab019884540e7515cdfc8c3d797dc719e98520f`。原始响应SHA `be9dfbea7bcc837b5aff51cc251cc1e9564de552a3c2878d1c833d82438f4d12`；快照 `outputs/m3/forecasts/weather_forecast_es02_20260909_pv01/`。旧两个来源批次保持。
- 预测run_id `b09c51c7-5d47-4ad1-a894-9afbf64f8a54`，数据库批次主键id=1（点表run_pk=1），as_of为09-09 22:30:24.912，窗口09-09 22:45至09-10 22:45，右端不含，96个15分钟平均交流功率点。预计积分电量1711.55004275kWh，峰值242.171483kW在09-10 12:45–13:00，无负值截断。
- 首次新增1批次/96点，先running、完整回读点后completed，最终回读在22:31:14完成，早于目标开始。再次执行同工件，新增批次0/点0，所有字段一致；同批天气50行再次只读核验。content_hash `f1075c70069ffd1c950cc0dab7f8c2487ec234470027e7c5b8625984ce22ff1c`。工具只补缺失点、不覆盖冲突、不自动重试POST，未完成批次留running；窗口已开始拒绝首次发布/恢复。多API请求不是数据库事务，完成门槛只由此写入服务执行，不是表内物理CHECK。
- 训练仍使用永久实测快照60938原始行与历史CSV重算交集，1276合格点，08-18 15:00至08-31 23:00；未补9月历史天气或刷新实际功率。训练聚合快照、全部50天气行、来源路径/SHA、读取时间和环境内嵌source_manifest；model_config存全部缩放和系数。浮点输入/系数以17位可还原字符串保存，点保留numeric(14,6)；原始预测/非负输出与is_clipped一致。七日参考baseline_kw与预测区间上下界为NULL，不把回测昨日基线写入七日字段。
- 工件 `outputs/m3/forecasts/pv_forecast_es02_20260909_01/`：中文首批报告、run/points、三个CSV、输入/源码、首次及幂等核验、verification/delivery_manifest与forecast_curve.png；实际图像已查看。27项光伏相关测试（含原12项）与7项天气测试通过，独立NumPy拟合及天气时间对齐/预测/日电量复算通过，源码自行审查。绘图缺少PingFang时已采用本机Arial Unicode字体，未修改生产依赖。
- 本次NocoBase权限沿用用户明确授权，凭据通过0600临时文件使用后已删除；无凭据写入项目/交接/报告。计划 `docs/m3/plans/2026-09-09-pv-operational-forecast.md` 已完成；用法/查询 `docs/m3/光伏运行预测说明.md`。本地run.json标completed是待写入目标状态；生成时summary保持database_written=false作为原始证据，实际数据库完成事实以publication_verification.json为准。
- 下一阶段可配置按次流程的周期更新、查询/前端展示和到期实测评估；本轮没有自动定时任务、前端接线、M4接入或设备下发。训练截止8月底且真实未来实测尚未到齐，不能宣称已验证在线预测精度或使用离线事后天气回测指标替代。

## 2026-09-09 ES02 实时与未来天气统一 Forecast API，首批已入库

- 用户指定实时与未来预报统一 `https://api.open-meteo.com/v1/forecast`。新增 `m3/scripts/fetch-open-meteo-weather.py`，一次请求current（原11天气字段+is_day/weather_code）和hourly（原11字段），坐标23/113、Asia/Shanghai、past_hours=1、forecast_hours=49，共50个小时标签。保留完整原始响应、请求、真实获取时间、SHA和稳定批次。
- 表结构保持原小时契约：hourly逐行写入 `energy_weather_points`、source_kind=forecast；current保存为同批次每行相同 `source_metadata.current_conditions`（valid_at、实际interval_seconds、数值/单位、model_based=true）。current是模型当前值，900秒统计不能冒充小时累计或站内实测。JSON天气数值为规范化十进制字符串，避免JS/JSONB浮点格式变化导致哈希不一致。
- fetched_at按完整接收后的真实时间向上取毫秒，微秒原接收时间留元数据；issued_at未知保持NULL。默认best-match最终具体模型未返回，resolved_model为NULL；响应网格坐标/海拔记录在元数据中。补齐shared importer对issued_at/fetched_at的UTC回读归一化，核验报告的read_query使用实际source_kind。
- 首批于北京时间2026-09-09 22:06:43.228获取，current有效22:00，气温26.6℃、湿度80%、总云量15%、风速6.9km/h、夜间辐照0。小时标签09-09 21:00至09-11 22:00，50行字段完整；获取当时22:15起未来24小时天气覆盖完整。摘要窗口是获取时刻的覆盖检查，较晚预测须重新确定未来窗口与覆盖。
- 已通过NocoBase新增50行并整批逐字段/哈希回读通过；再次从同一快照执行新增0、已有一致50。批次 `4f0142af09d1e7fd45fa83b85836f0d0b3849bdba7be2b990b23fed05ba93425`，原响应SHA `e72c904106549f6f4edd9104e15d77c3ea81110bc1baa9a5ab98e14dfecbf75e`。原响应、envelope、summary、weather_rows、首次/幂等核验与manifest位于 `outputs/m3/forecasts/weather_forecast_es02_20260909/`。
- 7项相关单元测试通过，覆盖当前/小时语义分开、单位/缺口/重复/NaN/未来current拒绝、缺失保留、批次重放、获取时间毫秒上取整、UTC日期回读。真实快照重放与已保存行/摘要完全一致；原历史天气2,952行离线重算批次与哈希仍一致。未跑无关测试套件。
- 复用用户已有授权凭据，通过0600临时文件只向NocoBase使用，完成后已删除；Open-Meteo请求没有发送该凭据。用法与查询说明 `docs/m3/实时与预报天气采集.md`。没有改表结构/旧天气批次、生产配置或设备指令。
- 本轮完成按次采集与入库，未配置周期调度、更新前端、补9月历史再分析或生成未来光伏96点；后续预测任务可消费完整forecast批次。真实预报模型验证仍未开展，不能把上一轮事后天气回测当作上线精度。

## 2026-09-09 ES02 光伏功率第一轮离线模型与回测完成

- 用户确认继续“训练数据集、对比曲线与误差报告”，中途两次询问模型；已明确主候选采用岭回归WeatherRidge，输入辐照/温度/云量/湿度/风速/时刻，预测十五分钟平均交流功率。同步比较SeasonalNaive昨日同刻和IrradianceGain非负辐照比例；未在测试集上调参或追加模型。
- 新增 `m3/worker/domain/pv_training.py`、`pv_backtest.py`，CLI `m3/scripts/backtest-pv-history.py`、`plot-pv-backtest.py`。仅依赖现有NumPy/Pandas，Matplotlib在临时分析依赖目录使用，未改生产依赖、API入口、M4或数据库。计划 `docs/m3/plans/2026-09-09-pv-weather-backtest.md`，用法 `docs/m3/光伏离线回测说明.md`。
- 实测使用此前授权查询的60,938行固定快照；天气使用已入库的相同CSV文件，核对成功入库报告SHA一致，本轮没有重新API查询。输入与源码已复制进报告工件，无凭据。实测先等权分钟均值再十五分钟平均，每点至少12有效分钟、四点合格才纳入小时；空值不补零。辐照按小时结束标签，瞬时字段分别插值到区间中点，降水均分。
- 样本池1,276点=319小时，08-18 15:00至08-31 23:00（右端为结束）；首折708点含7个完整日，08-26至08-31逐日滚动。共576预期点，天气/功率合格568，昨日同刻再缺1点，三模型共同567点；GHI>20W/m²有辐照共同279点。只有08-26、08-29、08-30三个日期共同完整96点，可以比较全天能量。
- 有辐照MAE/RMSE（kW）：昨日同刻103.9037/143.5584，辐照比例52.2473/75.1942，Ridge53.2040/72.1655。Ridge MAE相对昨日降低约48.8%，但比辐照比例略高；完整日电量MAE分别1340.5454/204.0889/300.3371kWh。不能声称Ridge全面最优；没有自动上线选模。
- Ridge无截距，9个固定天气交互特征；每折仅训练集RMS缩放，平均平方误差加0.01倍系数平方和，非负截断，不编造交流额定容量上限。每折功率标签结束时间<=该日零点；再分析天气为事后已知，实验固定标记 `retrospective_known_weather`，不是实际可用预报或上线精度。尚未接入限发/停机/额定容量，日电量仅采样功率积分估计。
- 工件目录 `outputs/m3/backtests/pv_backtest_es02_20260909/`：中文报告、training.csv、aligned_all.csv、backtest_points.csv、metrics/folds/quality/manifest.json及两张PNG。已看实际图像；12项相关测试通过（含时间语义、缺失、Ridge手算解、目标与特征训练泄漏边界），独立CSV指标/日电量复算通过，源码自行审查；用保存快照重复运行，六个数据/结果文件SHA完全相同。已有目录拒绝覆盖、天气SHA篡改在写前拒绝；未跑无关测试套件。
- 后续阶段：接入并按批次保存真实天气预报，按真实获取时间/预测提前量评估；可补9月同源天气扩大样本。当前没有写光伏预测批次/结果表，没有运行未来24小时预测、接M4或下发设备指令。历史天气入库状态保持。

## 2026-09-09 ES02 历史天气已入库并验证

- 用户明确要求“那你入库吧”。已通过 `https://vifa.hlszh.com/api/energy_weather_points:create` 分30批写入现有 `vifa/历史天气数据.csv` 全部2,952行，电站ES02，时间2026-05-01 00:00至08-31 23:00（Asia/Shanghai），纬度23、经度113。多出的天气历史完整保留，后续训练再按光伏有效范围取交集。
- 保存为 `historical_reanalysis/open_meteo`，保留原单位及逐字段时间语义；原文件未知的 `issued_at/fetched_at` 为NULL，入库时间由NocoBase系统字段记录。CSV天气值无空缺，`quality_status=valid` 代表格式/连续性/有限数值/范围/精度检查通过，不代表站内观测精度。
- 新增 `m3/scripts/import-weather-history.py`，默认离线校验；执行前验证集合结构，读取并逐字段核对已有同批次记录，仅创建缺失行，每批100条，不覆盖/删除或自动重试POST。固定批次 `5b01ef004c928602219d0ca1f4a1f0f9a311c32d3e289245d7a7abc83fd05951`；原CSV SHA-256 `2b320898fbd4d1df87ecb0766bcf3913401cdbddb9cb1d503416ed407af51d46`。
- 首次执行新增2,952行；完整分页回读全部字段及重新计算内容哈希一致，时间无重复/缺失。再次执行识别全部2,952条已有一致记录，新增/更新/删除均0。证据 `outputs/m3/contracts/20260909/weather_history_import_verification.json` 和 `weather_history_idempotence_verification.json`；离线另外验证UTC时间/数值归一化、非法值/哈希冲突/重复时间拒绝。未运行无关测试套件。
- 设计文档同步了导入用法和实际状态。复用用户提供的本次授权凭据，经0600临时文件使用后已删除，未写入项目或现有业务配置。44条SQL CHECK的物理落库状态未变；新增校验只在CSV导入工具中执行，不约束其他写入方。
- 下一步可按站点、来源类型、固定批次及天气时间从API回查历史。尚未补下载9月天气、采集未来预报、训练模型、生成预测结果或接入M4；本次未修改已有实测表或调度/设备配置。

## 2026-09-09 光伏预测三张表已通过 NocoBase 创建

- 用户要求输出PostgreSQL设计并通过NocoBase接口建表，确认三表范围；天气要同时支持历史回查和后续预报批次保存。已新建 `energy_weather_points`、`energy_pv_forecast_runs`、`energy_pv_forecast_points`，实测继续复用 `t_es_data.ac_solar_power`，没有新建实测副本。
- 天气以 `es_sn/source_kind/source_batch_id/weather_time` 唯一，source_kind区分historical_reanalysis/forecast；小时天气保留原单位和逐字段时间语义。预报以实际fetched_at记录可用性，issued_at未知保持NULL。预测运行保留as_of、运行用途、输入快照清单和天气批次；结果以run_pk关联，目标时间与步数分别唯一，输出15分钟平均交流功率。
- 已交付 `m3/contracts/pv_forecast_schema.py`（统一结构源）、`pv_forecast_schema.sql`（完整PG目标DDL）、`pv_forecast_nocobase_payloads.json`（三次POST请求）、`m3/scripts/create-pv-forecast-collections.py`（离线生成/创建/回读）和 `docs/m3/光伏预测数据库设计.md`。
- 业务Key初次GET集合结构403；用户随后提供本次建表凭据，三表预检不存在后已逐表POST创建并验证字段/默认值/关系/索引元数据。三个数据:list接口也已实际查询全部声明列成功，创建后为空。证据 `outputs/m3/contracts/20260909/pv_forecast_api_verification.json`。临时凭据文件已删除，未写进项目/现有配置；后续管理操作需重新配置具备权限的凭据。
- 本地离线生成和重复字段/索引引用/标识长度检查通过，并经独立静态审查；未直接运行PGSQL或检查数据库系统目录。完整SQL中的44条CHECK不会由当前collections:create接口落库；跨表完整性/天气可用性校验还需实现写入服务，不能声称已强制生效。
- 建表完成时尚未导入天气CSV；随后已入库，见上方最新记录。尚未补下载9月天气、训练模型、实现预测发布/查询服务或接入M4。没有改现有业务表、用户角色/权限、调度算法或设备配置，未SSH或下发设备指令。

## 2026-09-09 光伏预测前置数据核验（尚未实现预测）

- 用户希望增加光伏功率预测，确认 `vifa/历史天气数据.csv` 的北纬23/东经113对应电站2，要求按电站2最长可用历史取天气交集，不要求光伏覆盖整份天气CSV。用户明确授权本轮API查询。
- 已通过现有同源API只读获取ES02的 `timestamp,es_sn,ac_solar_power`，固定最新时间分页31页，共60,938条。北京时间最早站级记录2026-08-06 08:00，最新2026-09-09 20:44:30；真正有效光伏从2026-08-18 14:18开始，首次正功率14:26。有效43,461条、空值17,477条，无非空非法功率、重复或冲突时间戳；此前空值不能补成零发电。
- 天气CSV为2026-05-01 00:00至08-31 23:00的2,952条完整小时记录。辐照是时间戳前一小时均值；温度/云量等字段应按各自时间语义对齐，不能统一平移。按每15分钟至少12个有效分钟、每小时四个合格区间做覆盖核验，天气与光伏可配对319小时，区间从08-18 15:00至08-31 23:00，其中172小时光伏均值大于零。08-27有一小时未通过该覆盖门槛；08-31最后一小时需09-01 00:00天气记录，目前CSV不含。
- 暂存证据目录 `/tmp/vifa-pv-history-20260909/`：`bounds.json`、`station2_pv_history.jsonl`、`audit.json`。核验的是数据覆盖，不代表已训练或验证预测精度。09月光伏已取得，但当前天气CSV只到08月底，尚未补下载未来/历史天气。未修改预测、M4算法或配置，未运行求解、部署或下发设备指令。

## 2026-09-09 峰段保电已实现；新计划被最新2%安全下限阻断

- 用户确认上午平段电量留给14–19，并补充23:30–24也应保留给尖峰。本轮已实现仅电站2全天计划`m4-daily-peak-reserve-v1`，电站1和滚动规则不变：非feng段仅需量/购电上限必要放电，需量两层后优先减少首峰相对推荐SOC上限的储能缺口，日末SOC>=max(EMS末SOC,推荐下限)。保留现有功率/SOC/PV硬约束，不硬编码具体峰时段或新增备用百分比。
- 新请求可选`peak_reserve_policy`（None序列化省略保留旧请求形状），含version与terminal_soc_min_pct；Pyomo增加末尾辅助变量，独立验证同步新功率和末SOC边界；峰前准备使用零MIPgap，未完成风险`PEAK_RESERVE_PREFERENCE_INCOMPLETE`在服务选择和比较两处拦截。模型后缀`/peak-reserve-v1`，日候选用途版本独立。
- EMS仍按原配置/PV策略模拟，验证副本清除新保电策略；仅新策略允许优化日末电量不低于EMS且费用更低才推荐。comparison增加daily_policy_version/terminal_energy_rule/baseline_terminal_soc_pct/optimized_terminal_soc_pct/retained_energy_kwh，selector为daily-peak-reserve-cost-gate-v1。不能再把新策略称为同末电量比较；沿用EMS时明确保电优化未被采用。
- latest内存completed与磁盘统一复验并核对当前日策略/目标；前端增加本站策略和末电量数值门槛，empty清旧记录并阻止晚到历史回填。主HTML、Node-RED HTML/Flow模板同步；仅文案/门槛，无布局改动。配置说明及`docs/m4/plans/2026-09-09-m4-peak-reserve.md`已更新。
- 两轮实现阅读及独立源码审查未发现阻断，遵从此前用户“不测试、直接实现”：未跑测试套件、语法命令或浏览器；不声称模型运行通过或旧hash已实测。未提交/打包/部署/SSH/EMS下发。
- 重启前确认8845两站daily均completed、decision jobs均finished。停止PID78141后，新服务PID81839、会话65284运行同目录/原SQLite；重启前后两站settings完整JSON一致。station2旧002e计划已返回empty，未当成新规则结果。
- 最新用户参数已是v8（非此前v7），安全SOC下限2%、推荐2%–98%、效率0.98、充600/放540、光伏上网优先True。本轮一次业务生成run `f065c490-41ad-47b6-9032-64496a013104`于18:11发起，在求解前报“零点SOC位于当前安全范围之外”，没有新优化计划。今日零点SOC此前及当前记录1.933%，不能自动降下限或夹高采样。
- 已向用户异步询问：保留2%并加入低SOC只充电恢复规则（推荐），或将安全下限恢复1%再生成。**这是尚未获答的业务边界，不能把沉默当批准；不自动修改安全配置或开始恢复规则。** 当前可继续阅读/文档等独立工作。证据`/tmp/m4-peak-reserve-before-restart.json`、`/tmp/m4-peak-reserve-started.json`；失败/当前全天输入已保存到`/tmp/m4-s2-peak-reserve-job.json`、`/tmp/m4-s2-peak-reserve-blocked-inputs.json`，再次只读确认initial_soc=1.933、唯一missing为ems_baseline。

## 2026-09-09 用户纠正：上午平段电量应留给下午预测偏差

- 用户认可上午待机，但明确08:30–08:45的108.208029kWh应留给14–19，不能假定预测兑现后必然有余量到2%。此前“高价段用不完所以可提前放”的解释仅在确定性预测下成立，不应当作实际可放心释放的备用电量。
- 已核对保存输入：10–12及14–19的计划放电均达到每点min(预测净负荷,540kW)，原预测下高价段已排满；这不等于实际电量有保证。保留上午108.208029kWh、下午仍按原预测放电，19点预计SOC为5.525426%，可提供相对原2%约3.525426个百分点的储备。该条件算术不是新生成或采用计划，也不保证任意预测偏差下均够用。
- 现模型没有显式预测误差备用量，全天仍为零点起算回算、无实时SOC闭环。最小候选方向为在不恶化既有费用/需量/SOC/PV/吞吐目标时，优先把同价平段放电移至后续峰段结束后；不擅自增加固定SOC比例、不取消同末SOC费用比较。当前只完成核对，尚未改求解规则。
- 若继续实现：仅全天用途应有独立目标配置/版本，避免修改global ORDERS波及两站滚动；新重排层应在早谷充层之前，临时固定充电的锁不得遗留并锁死后续谷段重排；需处理feasible未完成末层仍被费用门槛选中的情况，以及旧日计划仅按EMS基线版本判断有效、不会自动因目标版本变化失效的问题。不能仅新增目标项就承诺保电已生效。

## 2026-09-09 电站2 08:45–10:00待机原因（只读排查）

- 本机8845最新已保存日计划已由用户侧更新为run `002e709c-377c-423a-aff6-1e6604b9d71e`，北京时间17:38:33完成，配置v7/service，`load_first_export_priority`（光伏余电优先上网）。本段覆盖下方v6旧计划的当前结果描述；本轮没有发起求解或修改参数。
- 选中balanced/optimal，费用2156.6532197元，EMS基线2534.1488695元，预计差377.4956498元；cost/pv候选为feasible。全天入口按有效候选净电费最小选取，不能将其他候选最后一层HiGHS optimal消息当成全层最优。
- 真实保存输入中08–10为平价0.65856875元/kWh，10–12和14–19为峰价1.10016875。08:30–08:45放432.8321159kW、108.208029kWh，SOC从约98%降至94.474488%；08:45–10待机保持该SOC，非电量耗尽。10–12放714.070267kWh至71.210020%，14–19放2124.304674kWh至2%。上午少放是保留高价段电量；当前需量上限1062.5kW，这段净负荷448.8–532.5kW，无需强制削峰。
- 当前光伏上网优先使12–14余电232.550561kWh全部上网，该段电池不充电。不要沿用v6“中午入储”的解释。售电价仍0，属于用户当前策略边界。
- 同价时段没有连续放电/减少启停/早放目标，仅末层凌晨谷段早充。因此08:30孤立放15分钟不是唯一经济排程；算术上108.208029kWh可在08–10均摊为54.104014kW并保持10点SOC、总费用和既有约束（未生成或采用替代计划）。应区分“保留峰段电量合理”与“具体碎片时序仍可改善”，不能声称08:45–10必须每点为零。
- 用户继续追问08:30和23:15两个放电段。08点约98%到推荐下限2%可供交流2946.582970kWh，高价段共放2838.374941kWh，剩余108.208029kWh用于平价段；具体落在08:30没有唯一时序依据。晚19点已到2%，为与EMS基线保持24点同末SOC1%，还需释放31.32kWh电池侧/30.6936kWh交流侧。balanced优先最小推荐SOC偏离，使这1个百分点尽量晚释放；禁止储能外送下，23:30–24点负荷仅能吸收29.379258kWh，所以需23:15–23:30先放1.314342kWh（5.257367kW），随后58.161533/59.3555kW供最后两个区间负荷，午夜恰到1%。该晚段主要是单日末SOC对齐和SOC推荐偏离目标共同造成，不是新增峰价或实际调度记录。
- 仅GET当前本地日结果、源码阅读和保存数据算术核对；无新求解、测试套件、源码/配置修改、重启、远程连接或EMS下发。原始证据 `/tmp/m4-s2-idle-current.json`。当前仍为零点SOC起算的同日回算预览，非现场实际执行记录。

## 2026-09-09 最新：取消原始EMS时段冒充有效计划，按输入重新生成电站2日计划

- 用户指出应根据负载和SOC生成，电量不支持一直放到21点。代码定位customerEmsSchedule/renderCustomerEmsSchedule会在无匹配有效日结果时直接将原配置时段填入主策略、功率和时间线；已删除该回退。首页仅展示校验通过的优化或EMS模拟；缺数据/过期/失败时显示有效计划待计算，原始配置仍在设置来源区。主HTML、Node-RED HTML/Flow同步，未浏览器验收。
- 本机原8845服务PID77255已不存在，第一次urllib通过代理返回空502，绕过代理直连后确认Connection refused。已恢复本地服务PID78141、会话17260。后续本机只读调用使用ProxyHandler({})避免本地代理影响。
- 读取station2参数为用户v6/service：效率0.98/0.98、安全SOC1%–98%、推荐2%–98%、容量3132、整站600/540kW、光伏优先入储False。今日00点SOC1.933%。最新EMS基线模拟05:07:01充满98%，17:03:40降到1%后停放，费用2288.435466元，说明不能按原19–21时段画固定放电。旧日比较因v6PV策略返回empty。
- 按用户“根据负载和SOC去生成计划”在本地发起一次日计划业务求解（不是测试），run_id c55c79b9-a5f4-49d9-af53-681833c24b60，completed/optimized。基线2288.435466、优化2009.566450、预计差278.869017元。需量峰值677.777722kW，超限0，末SOC1%，服务内独立复核通过后推荐。
- 优化放电段：08:15–09:00、10:00–12:00、14:00–19:00（148.716–540kW可变）、23:15–24:00（5.257–59.356kW）。09–10待机减少上午消耗，中午12–14光伏充电；19点SOC2%后待机至23:15。末段释放约30.6936kWh，将推荐下限2%降到与基线同末1%，未擅自取消这段。不要声称优化电量17点耗尽；17:03是EMS模拟，优化通过重新分配延到19点。
- 当前仍是从零点SOC和全天预测形成的同日回算，非以当前实测SOC为起点的剩余时段滚动重算；页面摘要已明确。没有编造或修改当前SOC、设备状态、功率约束或真实EMS指令。
- 原始只读/业务结果/tmp/m4-current-s2-daily-inputs.json、/tmp/m4-current-s2-generated.json。未运行测试套件/语法命令/浏览器测试、未打包部署提交或EMS下发；本轮运行一次用户要求的本地优化任务。

## 2026-09-09 最新：光伏余电优先上网单开关已接入（未测试）

- 用户确认简化为一个按站开关，无上网电价配置和电量去向展示。pv_export_priority：开启优先满足负荷再上网，上网受限部分入储；关闭优先负荷再入储，充满/达到功率上限后再上网。默认关闭。需量/SOC/功率硬约束不变，电池不能外送，有光伏余电时不能同时电网购电充电。当前后端沿用外送边界（并非新增外送权限设置），售电价仍0。
- StationParameters新增可选bool以保持旧证据；serializer在None时省略新字段，不改变旧参数重建。有效配置读取将旧缺省转False，RUNTIME_POLICY升m4-runtime-v2-pv-priority，含开关的内容哈希使旧输入失效；save由页面布尔开关提交，未自动写SQLite。
- OptimizationRequest增加load_first_export_priority/load_first_storage_priority两策略。model对新策略启用负荷优先/余电禁购电/禁止储能外送，复用storage_full二进制变量施加先上网或先充电硬约束。export模式先固定min(余电,允许外送功率)，余量尽量入储；storage模式强制按min(余电,可充功率,SOC容量余量)吸收，剩余再上网。validator独立复算对应充电和外送值；价格与候选目标不能改变顺序。旧legacy/load_first_economic解释保留，service模型版本新增策略后缀。
- adapter滚动请求、daily_baseline全天请求均显式带策略。EMS模拟有光伏余电时也按开关安排，其他时段沿用EMS计划和需量/SOC控制；到限后剩余时间待机，必要时限制外送并弃光。比较重放传同一策略，两者同末SOC且仍更便宜才采用。基线策略升ems-demand-soc-duration-v6-pv-priority，旧日费用失效；开关下EMS基线属于配置后模拟，未修改现场EMS。
- HTML调度设置增加单个原生checkbox/role=switch，按站保留草稿，晚到配置不被空草稿覆盖。未增加流向展示、电价或其他开关。主HTML/Node-RED HTML/Flow同步，配置说明及实施计划更新。
- 按用户要求未测试、未运行语法命令或浏览器验收、未打包/部署/提交/新求解/下发，仅源代码实现和阅读。旧本机8845 PID75874确认两站均无运行任务后停止，新服务会话41134启动；8844与生产未动。用户之前自行生成的station2日计划completed，本轮没有重算它，新版本会要求重新生成。

## 2026-09-09 最新：调度设置恢复单柜最大功率编辑

- 用户说“取消电池充放电的界面，我需要修改电池充放电的最大功率”，异步澄清回答“调度设置界面”。按在该页面恢复编辑实现，未擅自删除效率字段或取消既有接口安全上限。
- 表单新增单柜最大充电/放电功率，允许0。初始化从已存整站值除接口柜数，新站用对应接口限值；保存乘本站柜数回现有max_charge_kw/max_discharge_kw。界面校验不得高于t_model对应模式最大kw，后端继续人工/接口取小；没有放宽功率规则。现共8项（功率2、效率2、安全SOC2、推荐SOC2）。
- 控制只读卡明确接口上限及已存人工整站值；等待接口时禁用功率框，来源迟到才初始化，草稿按站保留并绑定柜数。容量仍自动读取；成本/时效仍服务端配置。没有自动写任何电站参数、启动求解或下发。
- 主HTML、Node-RED HTML/Flow同步，配置说明更新。按用户要求未测试、浏览器验收或打包；仅实现及源码阅读。后端逻辑未变，无需重启，本地刷新即可查看。

## 2026-09-09 电站2用户调整后EMS基线已就绪（只读）

- 用户“继续，我调整了”。重新只读8845：station2设置已由用户改为v5/service，SOC安全1%–98%、推荐2%–98%、效率0.95/0.95，接口容量3132kWh，汇总充600/放540kW。助手未写参数。
- 今日2026-09-09 daily-inputs ready/can_compare=true，missing为空；零点SOC1.933%符合调整后范围。EMS全天模拟基线2659.968829元，包含光伏参考1729.886667kWh，其中自用1497.336106kWh（86.55689%），外送232.550561kWh、外送收益按0。最大购电677.777722kW，模拟需量超限0；期末SOC1%。充电3167.177305kWh、放电2886.138kWh。均为同输入预测模拟，非实际账单/实测发电。
- 日计划查询仍empty，本轮未启动新优化、测试或EMS下发；只读全天准备接口已生成基线。结果更新/tmp/m4-station2-daily-inspect.json；页面重新读取全天数据可加载，不需代码修改。

## 2026-09-09 电站2基线缺失定位（只读，未修改规则）

- 用户问电站2没有基线及其有光伏。只读本地8845 daily-inputs/plan/settings：首次取数期间参数变化返回409，重读成功；station2日计划empty，全天输入blocked，唯一missing为ems_baseline。负荷/光伏/电价均96点、时段有效、容量3132kWh、零点SOC1.933%。当前station2配置v4/service版本，SOC安全2%–98%。ems_simulation.py对日初SOC越界直接抛ValueError，因此不是缺光伏或EMS时段。
- 光伏来源为2026-09-02至09-09零点前七日同一时段中位数，当前参考日电量1729.8867kWh。基线已用load-pv+charge-discharge计算净电网功率；售电单价当前0，此值不是实测当日电量或已求得费用。
- 未修改安全SOC、初始采样或模拟/求解边界，未自动将1.933夹到2%；若后续修复，需同时考虑EMS和优化日初低于下限时的恢复规则及独立验证，不可只放宽EMS一侧。原始只读结果保存在/tmp/m4-station2-daily-inspect.json。
- 未运行测试、新求解、写配置或下发EMS。服务仍为8845 PID75874会话65131。

## 2026-09-09 最新：成本时效归服务端、购电硬上限取need_kw

- 用户要求成本和数据有效期移到配置、购电边界来自t_need.need_kw，reserved_kw是免调功率；并要求核对其余参数。页面现仅效率2项、安全SOC2项、推荐SOC2项。期末SOC偏差当前全天比较不用，移出表单但保存时保留旧滚动参数，新站默认0。当前适配器尚未接入效率/安全SOC/推荐SOC的上游配置来源，不声称已证明上游无这些字段。
- 新增m4/settings/runtime_config.py：GLOBAL_RUNTIME_DEFAULTS成本0、时效86400秒（只读SQLite核对当前两站均为这两个值，保持数值不变，非生产时效推荐）；支持按站部分覆盖。SettingsStore.get读取时形成有效参数及/service/内容哈希版本；旧手填grid_import_limit_kw清为None，客户端成本/时效/旧购电字段不能覆盖服务端。get不自动改写SQLite，save继续事务和revision比较，返回有效版本。旧归档配置不追溯改写。
- ResolvedControlLimits新增可选grid_import_limit_kw，新实时输入显式need_kw为硬上限；adapter对旧历史未提供该值时保留旧解释。全天基线和优化共同直接need_kw，不再min旧手填上限。reserved_kw沿用已确认只读展示，不扣need_kw、不扩充超限，也未新建现场免调死区算法。
- 控制策略m4-control-policy-v5-need-import-limit，日比较EMS策略ems-demand-soc-duration-v5-need-import-limit，HTML门槛同步；旧日比较须按新参数版本重新生成。未启动新求解或计算新费用。之前仍保留的人工功率收紧逻辑本轮未变。
- 主HTML、Node-RED HTML/Flow模板同步；配置说明首段和实施计划追加新归属说明。按用户要求未运行测试、语法检查、浏览器验收或打包；仅源代码阅读与修改、只读当前SQLite参数以及重启所需本地任务状态读取。
- 本机8845确认station1日任务completed、station2empty，两站旧decision job均finished后停止PID75159并启动新工具会话65131；续接查当前PID。8844及生产未动，未提交、未自动写参数或下发EMS。

## 2026-09-09 最新：调度设置去除已有接口值的手填项

- 用户要求去除已有值并罗列剩余设置。仅调整HTML：移除容量、最大充电功率、最大放电功率三个表单项，在接口只读区显示总容量、单柜和整站充放上限。保存时从当前同站接口结果自动带入这三项以兼容既有后端参数契约；缺有效容量/功率来源时明确阻止保存，不用旧手填值兜底。
- 剩余10项：充放电效率2项、安全SOC上下限2项、推荐SOC上下限2项、期末SOC允许偏差、可选购电功率上限、循环吞吐成本、实时数据有效期。期末偏差明确仅用于滚动计划，全天费用比较固定EMS期末SOC。
- 本轮未改变后端保留的人工功率收紧逻辑或自动写SQLite；若旧已存功率低于接口整站上限，设置页明确显示旧上限仍有效及保存后更新。不要声称已完全移除后端历史功率约束。保存后对应配置版本变化，已有方案继续按历史输入解释。
- 主HTML、Node-RED HTML与Flow内m4-page-template已同步。按用户要求未执行测试、语法检查、浏览器验收、新求解或打包；未重启、部署、提交或下发EMS。仅修改与阅读源代码；本地刷新HTML即可查看。

## 2026-09-09 最新：两站单柜功率与电站容量均由接口提供

**用户明确电站2也按单柜功率解释，电站总容量来自 `api/t_es:list.es_power_storage`。本段覆盖下方“station2保留整站功率”和使用人工容量的旧说明。** 继续不测试、直接实现。

- 只读核对电站表字段：站点编号是`sn`，不是`es_sn`；ES01/id5容量1044kWh，ES02/id6容量3132kWh。首次带错误编号字段的只读查询返回HTTPError，随后只输出字段名及选定容量值确认了真实字段；未输出凭据或其他敏感配置。
- `control_sources.py` 增加必需t_es读取（id,sn,es_power_storage,updatedAt），按sn精确唯一匹配。storage_capacity保存整站范围、表/字段、来源站点及正数kWh；缺失、重复、非正数或读失败阻断新计算，不回退人工值。两站power_scope均cabinet，柜数来自既有roster：电站1两柜、电站2六储能柜（emu27光伏不计）。
- `schedule_power.py`新增station_energy_capacity：新控制策略 `m4-control-policy-v4-cabinet-power-api-capacity` 必须用接口容量；旧已保存证据允许保持原manual语义。实时快照通过临时配置副本用接口总容量计算单柜及可用容量，保存capacity_source/version；复验按同一接口和参与柜数量核对。`ResolvedControlLimits`携带总容量，adapter构建请求按参与比例折算。持久化人工参数未自动改写。
- 日基线和优化请求同样使用接口全站容量，单柜模式功率×柜数后再模拟SOC/需量。EMS策略升级为 `ems-demand-soc-duration-v4-api-capacity`，旧日比较失效需重新生成；未计算新基线金额或启动新求解。旧人工功率限值仍可收紧接口汇总功率；容量不与人工值取小，直接以接口值为准。
- 参数页容量改为只读接口值；只读控制卡和全天准备显示接口总容量，设备可用容量来自相同来源。HTML版本门槛同步v4，Node-RED HTML/Flow模板同步。未打包。
- 用户不测试要求生效：本轮未运行测试套件、语法命令、浏览器验收或新求解；仅源代码实现阅读、授权只读电站表取数，以及避免中断任务的本地状态读取。两站任务结束后停止8845 PID73834，启动最新预览工具会话82961（续接查PID）；旧8844未动。未部署生产、修改配置、提交或下发EMS。

## 2026-09-09 最新纠正：电站1单柜功率从充放模式表读取

**用户明确电站1每柜充电100kW、放电60kW，并指出接口充放模式表已有值；不得将这些数值硬编码。用户再次要求“不需要测试，直接实现”。** 本段覆盖此前将t_model的100/60解释为整站及由此计算的全部基线/节省数字。

- 只读已查接口：t_model充电行id13，00–08、kw100；放电行14/15/16，08–12/14–18/19–21、kw60。此前 `ControlSourceReader` 固定 `power_scope=station` 是适配器自身标记，不是上游对范围的证明。结合用户单柜确认，新station1明确cabinet；station2保留station范围。
- 新增 `schedule_power.py`：按表内对应模式最大kw读取单柜充/放上限，缺任一模式或无效值阻断新输入；不填默认100/60。按参与柜数汇总，并与原人工总功率的按柜份额取较小值。刚加的roster固定上限、store/API拒绝保存硬编码值及HTML固定max均已撤回；人工参数未修改。
- `control_sources.py` 返回power_scope、configured_cabinet_count，策略版本 `m4-control-policy-v3-station1-cabinet-power`。`live_inputs.py` 快照/独立复核及 `ResolvedControlLimits`/adapter都携带接口单柜限值，单柜参与不会继承退出柜功率。旧无cabinet范围的历史证据保持其原语义。
- `daily_baseline.py` 按全站柜数将原时段功率汇总后再做SOC/需量模拟，并保留source_schedule及source_power_scope；能力同样与接口汇总上限取较小值。EMS策略升级 `ems-demand-soc-duration-v3-cabinet-power`，旧v2日比较失效，不继续显示旧184.92元或更早273.25元结论；未按新口径重新求解或读取新费用。
- HTML来源表同时显示kw/柜及整站合计；参数说明动态来自接口，旧固定输入上限撤回。备用EMS时段表及时间线也按柜数汇总，首页版本门槛同步v3。线上HTML、Node-RED HTML和Flow模板已同步；未打包。
- 按用户最新要求，最终动态取值实现后没有运行测试、语法命令、浏览器验收或新求解。只阅读实现并检查本地任务状态（两站结束）以便加载代码。旧8845 PID72550已停止；新服务启动命令会话99488，待续接查PID；8844未动。未写生产/配置、部署、提交或EMS下发。此前用户打断前针对已撤回固定上限所做语法检查不能代表此最终实现通过验证。
- 当前仍为站级汇总规划；柜级SOC差异和真实指令分配/回读尚未接入，不能宣称已实现真实单柜下发。

## 2026-09-09 方案来源展示加强（仅 HTML）

- 用户表示“现在采用求解器但界面难以分辨”。首页新增醒目的方案来源区，直接显示“采用求解器优化方案”或“沿用 EMS 原计划”；蓝色/琥珀色辅以明确文字，不仅依赖颜色。求解器采用状态列出同日末电量下的预计费用差，保持“页面采用方案 / 计划预览 / 尚未下发 EMS”。加载或不匹配时不冒称采用。
- 状态来源沿用已校验的同站、同日、同配置、当前基线策略的 daily_comparison，不根据存在某个求解候选推断已采用。同步主策略小标签、计划摘要、底部策略状态与调度计划上下文的来源称呼。未修改后端/算法/费用或生产配置。
- 只读本地当前结果：completed / optimized，预计差184.91903489724336元；这轮由用户侧生成，助手未触发。实际页面显示15:58生成的方案、顶部“采用求解器优化方案”和184.92元/天；尚未下发。全天输入准备可能随新预测更新，方案费用仍绑定保存的原输入，不能混用二者。
- 线上HTML、Node-RED HTML/Flow模板同步，仍5分组。JS语法、内嵌JSON、三份一致性、diff空白检查完成；已看本地DOM和当前桌面截图，浏览器错误为空。未运行测试套件或全尺寸主题回归，未打包/部署/重启/新求解/提交。HTML即时读取，刷新8845页面即可查看。

## 2026-09-09 求解器计费口径核对（只读）

- 用户补充“求解器计费的时候也是一样”：优化与EMS基线都应只计有效电量，SOC到限后的待机不可继续计充放电。
- 已核对 model.py / metrics.py / validation.py / service.py：求解器每15分钟决定连续可调功率，能量按效率递推且受SOC上下限、充放互斥限制；电费按电网购电/外送的计划电量积分，输出再次独立复算。其已有逻辑不同于上一轮EMS固定配置功率展开的缺陷，本轮无需更改算法。
- 当前求解器不优化区间内分钟级启停；剩余电量不足可降低该15分钟计划功率。恒定区间电价下与相同有效电量计费一致，但不能据平均功率宣称实际停机分钟，也不能用满功率短时运行替代计划而忽略需量差异。待机期间客户负荷的电网购电仍计费。
- 本轮只读核对，未运行测试、生成新求解、写参数、部署或下发。此前旧基线比较失效状态不变。

## 2026-09-09 最新修正：EMS 基线必须计入 SOC 到限及有效充放电时长

**本段覆盖下方“原时段按固定功率直接展开”的判断。** 用户指出可能提前充满转待机、下午五六点到 SOC 下限停止放电；这不是要求再次提供 EMS 控制 Flow。上一轮273.25元/天差额基于错误的固定功率基线，已失效，不再作为新结论。

- 新增 `m4/settings/ems_simulation.py`，策略 `ems-demand-soc-duration-v2`：先按需量优先限充/削峰，再限设备功率、禁止储能外送、按配置 SOC 上下限及效率计算可用时长；15分钟内提前到限，剩余时间待机。公开计划采用区间平均功率，另存实际模拟活动分钟、待机分钟、控制原因与到限时间。初始 SOC 不在安全范围仍明确报口径不一致。
- 削峰模拟在净负荷超过阈值时优先放电（包括配置充电/待机时段），功率需求为max(需量缺口,配置放电功率)，受设备和SOC限制；无超限时按配置运行，充电受需量余量限制。使用配置 SOC 安全上下限，不擅自换成推荐区间。此为确认原则下的预测模拟，不声称复刻现场固件迟滞/延时或实测执行。
- `daily_baseline.py` 接入模拟基线；`daily_comparison.py` 重放模拟并核对保存结果，版本绑定后才比较。新优化继续固定为模拟基线期末电量并满足需量硬约束。`daily_plans.latest` 对旧固定功率基线返回empty/需重新生成，前端亦拒绝旧基线策略。旧文件保留，未删除。
- 页面已显示模拟充/放/待机时长、到限事件；EMS主策略和时间轴按实际模拟停止时刻拆段（如17:47转待机），曲线保留15分钟平均值并注明。3份HTML/Flow同步、5分组及网关配置保留。没有声称已获取现场实际执行功率。
- 本地当前输入回算：基线5305.689067元，充800kWh/8h，放725.71925kWh/7.795217h，待机8.204783h；预计17:47:42达到SOC下限2%，其后待机。凌晨按目前100kW及零点SOC2.375%充到08:00仅75.171935%，未强行套用用户举例的06:00充满。仍用用户v5配置1044kWh、效率0.95/0.95、200/120kW。
- 预测下能量耗尽后有8点需量缺口，最大预测购电550.8013kW；已在数据口径披露，不冒称现场EMS实际超限，也不凭空造电使基线满足需量。新优化仍需满足504kW，尚未按新基线启动求解，旧优化费用和节省留空。
- 已确认两站本地任务结束后，仅重启本任务8845服务，当前PID72550、工具会话22800；8844未动。只读实际新API及本地DOM/截图，页面呈现14:00–17:47放电、17:47–24:00待机，错误日志空。5个Python文件AST、可执行JS、JSON/三份同步、5组及diff空白检查通过。未运行测试套件/TDD（沿用用户不测试要求），未新求解、打包、部署、修改参数、提交或下发。
- 当前服务已包含上一轮的逐次归档代码；新日任务尚未接入旧运行记录分页。完整计划文档首段已同步本次口径，后文保留历史不作为当前实现说明。

## 2026-09-09 最新纠正：EMS 原计划已含控制规则，全天基线与本地比较已接通

**本段覆盖下方旧记录中的“仍缺有效 EMS 计划/控制 Flow”。用户明确原计划已考虑需量限充、削峰及 SOC 到限规则；不得继续要求另行提供控制算法。**

- 新增 `daily_baseline.py` 直接展开用户确认的 EMS 时段及功率，按同一自然日96点负荷/光伏/电价、准确零点全站 SOC 和已保存配置计算基线，不额外添加一套 EMS 控制。`daily_inputs.py` 不再硬编码 effective_ems_plan 缺项，已具备有效基线生产者。
- 求解器模型/服务/独立校验新增可选期末 SOC 目标。`daily_plans.py` + GET/POST daily-plan 提供本地后台全天比较；固定期末电量与 EMS 一致，新方案必须满足需量及安全约束，全天严格更便宜才推荐，否则沿用原计划。旧滚动请求契约保持。当前日期数据是同输入回算，部分预测晚于零点生成，不是实际账单节省或零点日前已知方案。
- HTML/Node-RED HTML/Flow 已同步。首页已展示基线和完整日方案，“生成全天优化计划”启动比较；被拒候选详情标记未采用。原5分组与认证/网络保持，新 API 已列网关白名单。
- 本轮没有主动点击求解，续接只读检查时发现本地已有运行中的任务，保留它直至完成。run_id `6d4a2b59-e7a7-4ef5-8d45-46f9970527e6`：基线5480.370733元、优化5207.119847元、预计少273.250886元，最大购电504kW，两者期末 SOC 同为14.675867110304527%。服务返回三候选 optimal，独立比较推荐 optimized；不将其扩大解释为测试套件通过或实际节费。
- **该轮参数已由用户侧更新为本地 v5 `station-1-settings-v5-5b2f80c302f6f6c7`：1044kWh、充200/放120kW、效率0.95/0.95、零点SOC2.375%。** 助手未写配置。更早手工回算用旧本地效率1.0得到期末21.532%，不得与此轮混用，也不要覆写用户新参数。
- 本地预览 `http://127.0.0.1:8845/m4`，当前PID71442/工具会话88184；8844旧服务未动。已实际读完成结果、查看DOM与截图，首页显示优化日计划/预测需量裕度，浏览器错误日志空。Python10文件AST、可执行JS、内嵌JSON、三份页面同步、Flow5分组、diff空白检查通过；未跑测试套件、打包、部署、提交或EMS下发。
- `daily_plans.py` 最新增加逐次 JSON 归档是在此服务启动后写入，需下次无运行任务时重启8845加载；当前最新结果持久化已有。新日任务证据尚未并入旧“运行记录”分页，勿宣称此入口已覆盖新任务。实施现状以 `docs/m4/plans/2026-09-09-m4-daily-ems-baseline.md` 为准。

## 2026-09-09 全天输入已接通；电站1数据侧仅缺有效EMS日计划

- 用户“下一步”后新增 `m4/settings/daily_inputs.py` 与API `GET /m4-api/stations/{station_id}/daily-inputs`，今天00:00–24:00严格96点。`load_forecast`增加可选require_full_day，旧滚动默认行为保持；独立取零点站级SOC，不替换滚动求解的当前SOC。
- 已用授权本地配置只读核实电站1：全天负荷/光伏参考/电价均96/96，EMS时段已读，2026-09-09零点`emus_soc`=2.375%。因此“缺完整自然日预测、缺零点SOC”不再是当前事实。零点站级字段为whole_station_reference，不自动用于部分参与柜。当前预测含当日12:05完成的手动任务及15点后的滚动数据，明确仅为同输入回算，不是零点已知的日前计划。
- 数据侧missing仅effective_ems_plan：EMS需量限充/削峰/SOC到限的实际控制规则或有效日计划仍无来源；日基线构建及与其相同期末电量的自然日求解尚未完成。禁止把现有可读数据说成全链路可比较/已算节省。
- 首页新“全天比较准备”逐项列覆盖/零点SOC/缺项，重新读取按钮只读；当日负荷预测、电价可独立显示，基线费用/有效购电仍空。三份HTML/Flow同步；Flow m4-api-prepare仅增加GET daily-inputs白名单，认证/内网目标/5分组不改。
- 本地8845确认无运行中任务后重启，最新PID70577、工具会话18440；旧8844保留。已只读访问新接口、看实际DOM和截图，页面确显示5项已读及EMS有效日计划待接入，浏览器error日志为空。最后补独立负荷展示已语法检查；未跑测试套件、真实求解、写配置/生产、打包、部署或提交。

## 2026-09-09 最新 M4 已启动本地预览

- 用户要求“先本地运行查看”。旧8844服务PID42735仍保留；两站旧服务任务状态finished，没有触发新求解。
- 使用 `.venv/bin/python -m uvicorn m4.settings.api:create_app --factory --host 127.0.0.1 --port 8845` 启动最新代码，PID70049，工具会话77369（续接需查存活）。地址 `http://127.0.0.1:8845/m4`，沿用现有本地参数/历史文件。
- 已在Codex内嵌浏览器打开并保留新版页面。实际DOM和截图显示电站1 EMS全天原时段：00–08充100kW，08–12/14–18/19–21放60kW，其余待机；当前显示原计划放电60kW、2/2柜和实时SOC采样。旧跨日记录返回新门槛提示，费用为空。浏览器错误日志读取为空。
- 仅本地运行查看、页面自动只读取数；未运行测试套件、未点击求解/保存/下发、未打包或部署。此次用户授权页面查看覆盖上一轮暂不做浏览器验收的范围，但只看当前视口，不代表全尺寸功能验收。

## 2026-09-09 用户授权修改：全天推荐门槛及 EMS 首页回退已写入

- 已新增 `m4/settings/daily_comparison.py`，接入 `decision_results.py` 的 `record.daily_comparison`：仅北京时间00:00起96点可比较；有效EMS基线与请求/控制版本/日初状态绑定；复核物理约束、需量、相同期末电量及全天费用，只有严格更省才返回optimized，否则ems或unavailable。
- 重要限制：当前输入服务仍生成滚动输入；完整EMS控制规则、有效日计划及日初状态来源仍未提供。本轮没有生成它们、没有改求解器终点条件。新增有效基线字段 `inputs.daily_baseline` 尚无生产者，不能宣称真实全天费用已接通或全链路完成。当前旧/滚动记录返回暂无法比较。
- 首页不再直接推荐未经全天门槛验证的跨日候选。改为展示从control-sources读取的EMS全天原时段配置，功率标注配置值，缺有效基线时不展示假SOC/购电/节省；候选详情仍可查看。未来通过门槛结果需同日/同站/同配置才能整套展示。兼容旧后端缺字段。已同步线上HTML、Node-RED HTML和Flow模板；原5分组及非模板网关配置与HEAD一致。
- 仅做Python/可执行JS语法、JSON、页面同步及diff空白检查。首次检查误将application/json当JS，修正提取范围后通过；未跑功能测试、浏览器验收、真实求解、打包、部署或提交。保留此前所有修改与未跟踪密钥，不输出/提交凭据。
- 实施现状和后续依赖详见 `docs/m4/plans/2026-09-09-m4-daily-ems-baseline.md`。继续工作要优先取得EMS规则及日初状态来源，不能重复要求用户确认已经明确的全天比较规则。

## 2026-09-09 已确认按完整自然日比较 EMS 基线

- 用户进一步明确周期是一天：00–08充电，08–12/14–18/19–21放电，其余待机；已确认00:00–24:00全天费用整套比较，15分钟仅计算粒度。优化必须同样不超需量且全天更便宜，否则沿用EMS完整日计划。
- 已核对当前输入仍从下一15分钟起滚动95/96点，不是自然日；日初SOC不能复用日内当前SOC，需同日边界数据及相同期末电量。
- 已写实施计划 `docs/m4/plans/2026-09-09-m4-daily-ems-baseline.md`。通过异步问题询问原EMS需量控制Flow/代码的本地路径，尚未收到；这是完整基线依赖，不重复业务确认。缺少规则时不能猜测限充/削峰，不能用裸时段表计算节省。
- 本轮仅核对代码、记录口径与计划；没有修改运行逻辑/HTML，没有执行测试、打包、访问或修改生产。后续需拿到控制规则与日初状态来源后继续实现，不能宣称基线已接入。

## 2026-09-09 用户明确 EMS 基线不超需量，优化必须比基线便宜才采用

- 用户提议先计算EMS原充放电计划的费用，以其为基线；优化费用更低才显示为采用方案，否则沿用EMS计划。针对“需量改善但费用更高”的澄清，用户回答“原计划是不会存在需量超限的”。应以此作为业务前提，不再要求在更低需量与更低费用间二选一，也不主张为了降电费允许超需量。
- 预期门槛：原EMS完整控制方案作为基线；优化需满足同样需量与安全条件、费用严格更低才采用；无改善则首页保留EMS原计划。仍仅展示、不下发。费用比较必须统一周期、预测、电价、初始SOC并处理期末存量电量，不能把少留电冒充收益。
- 本地代码只读取t_model固定时段计划与t_need参数，没有EMS实时限充/削峰执行算法。因此此前按原始时段功率粗算的562.348kW及期末60.19%不代表完整EMS实际控制基线，不能据此反驳用户。建立可信基线仍需完整EMS控制规则/实际可执行计划；不得猜测保护行为或拿未校验计划直接做自动回退。
- 本轮只读检查本地控制适配代码并记录新口径，未实现基线、改变算法、运行测试或写生产。

## 2026-09-09 23:15 放电追加核查：存在可消除同价循环

- 用户追问23:15放电。只读刷新线上仍为14:29记录1ac304cd；23:15负荷14.3649、放电2.7345、购电11.6303kW，非需量需要。平段0.65856875，午夜谷段0.26736875元/kWh；SOC约3%降至23:45的2.213142%。
- 发现21:00/21:15同价充8.408116kWh，23:15–24:00放7.886201kWh。局部算术构造：取消前两次充电及23:15/23:30放电，23:45仅放0.549124kW，其余计划不动；SOC末值/安全下限、最大购电及需量目标不变，可少损耗0.659196kWh、少电费0.434126元。推荐3%SOC偏离增加，但在cost方案中其优先级低于电费。这是可改进的同价循环，不可解释为全部必要。
- 候选为feasible，30秒/1% MIP gap；末层message optimal不能冒充全层最优。未重新求解、未运行测试、未修算法/线上配置。已追加 `docs/m4/电站1峰段充电核查.md`。若用户继续要求修复，应保持需量最高优先，围绕经济层精度/可行解处理及无效循环做有针对性的复现与修复，不应固定禁止23:15放电。

## 2026-09-09 用户确认需量最高优先与双柜1044 kWh；线上已有修正计划

- 用户明确：所有电站需量目标优先级最高；电站1两柜总容量1044 kWh。不应把此前峰段充电视作授权增加“充电禁止新增超需量”的硬约束；保持需量峰值→超限积分→后续候选目标。
- 只读复查线上发现参数已更新v3/1044 kWh，最新14:29:33记录1ac304cd已使用新容量，初始SOC45%、放电上限120kW、充电200kW、需量504kW、95点，cost候选。16:00放电120kW/购电380.6088；18点整小时待机；全窗最大购电504.000001kW，为1e-6容差，未见充电造成实质超限。候选状态feasible，三候选前两层均为需量目标。已与同批次请求匹配。
- 参数与新计划由用户侧在此前读取后更新，助手没有写生产或触发求解；也未执行测试/下发。已更新 `docs/m4/电站1峰段充电核查.md`，保留522 kWh旧计划分析并注明被当前事实覆盖。不要按旧v2容量再写请求，也不要把不同预测窗口的费用差当实际节费。

## 2026-09-09 线上电站 1 峰段充电只读核查

- 用户明确授权“去线上查询 / 用 API / 读取本地密钥”，指定本地密钥文件；已使用有效凭据只读访问 M4 HTTPS API，未输出凭据、未写线上配置、未求解或下发。浏览器此前因权限不可读，外网 8844 超时；HTTPS 网关 API 现通过有效认证读取成功。
- 旧 11:34 记录 87165cd4 对应 813.0884/754.3535 kWh、527.0209 kW，16:00/18:00 实为待机。最新 14:10 记录 3449b81b 为 cost、778.5848/722.5550 kWh、531.9122 kW，确有高峰补电。已匹配候选批次。需量均504 kW；两轮候选均feasible，不能宣称整个分层全局最优。
- 最新16:00负荷500.6088、充31.3034、购531.9122：主动超线27.9122。模型先锁需量峰值、积分后才优化电费；14:15初始48.8%/522kWh、17:30到2%安全下限，16点补电用于分摊高负荷区间削峰电量。18点补能56.4477kWh经双96%效率供19:00–20:45放电52.0222kWh，避免后续超需量。未做反事实求解；只有电量平衡和局部峰值下界核算。
- 待明确的实际业务问题：是否给充电增加“不得主动制造/加重超限”的独立约束；两柜合计522kWh是否误填单柜容量（此前本地双柜1044kWh）。目前没有修改算法或生产配置。详情 `docs/m4/电站1峰段充电核查.md`；脱敏凭据以外的API响应留在本机 /tmp/m4-online-station1-*.json，文件模式0600。保持此前用户不运行测试的要求。

## 2026-09-09 M4 大屏自适应宽度

- 用户指出大屏右侧留空，已取消 M4 主容器 1480px 上限，改为 100% 宽度、左对齐；保留桌面 28px、720px 以下 15px 边距。总览右栏随屏幕按 25% 缩放，限制 290–440px，主图区使用剩余空间；原平板/手机断点保留。
- 线上 HTML、Node-RED Template 和 Flow 内嵌页面同步；只改样式，未运行测试、打包或部署。此条覆盖上一轮沿用 M3 最大宽度的选择。

## 2026-09-09 M4 按客户运行总览重设计稿接入数据

- 用户提供 `/Users/hua/Downloads/m4_优化调度_运行总览_重设计.html`，要求据此重构 HTML 并接现有数据。本轮以该参考稿为布局：左侧当前时段计划和调度曲线、右侧计划摘要/设备数据状态、底部合并时段；保留调度计划、数据与约束、运行记录、账单统计五个 Tab。旧求解详情移入计划页，运行准备与执行状态移入数据页。独立 Mock 文件仍仅为先前演示，不作为上线入口。
- 复用 decision-result、candidates、inputs、settings、control-sources 及原账单/历史接口；新保存计划可只读补取最近候选快照。充放电、SOC、需量线和费用绑定最终方案，负荷/电价仅与同批次或同时间窗口+来源版本输入拼接；未匹配时缺项不填演示值。95/96点按真实窗口展示，SOC 单列右轴；计划裕度允许负值，实测参与柜 SOC 有采样时效限制。历史/过期/未下发状态保留，不声称实际运行或节费。
- 已同步 `m4/web/M4优化调度控制台-线上版.html`、`m4/node_red/m4_customer_template.html` 与 Flow 的 m4-page-template 内容；网关函数、分组、Token、后端地址保持现有配置。本轮没有改后端、重打镜像、打包、部署、连接生产或触发求解。
- 用户此前连续明确“不需要测试”，本轮遵循该要求，未运行测试、浏览器视觉验收或生产联调；当前交付为已接线的界面，运行效果尚未验证。先前测试结果不能代表本次重构已通过。所有其他任务改动保留。

## 2026-09-09 M3 iframe 当前用户 Token 接入

- 用户要求 M3 Flow 使用 `{{ ctx.token }}`。默认 `M3_AUTH_MODE=query_token`，NocoBase iframe URL 为 `https://opdash.lvkpower.com/ett?token={{ ctx.token }}`；Node-RED 接收参数，页面在内存中保存并清除自身 URL 参数，所有预测 API 携带 Bearer Header，经现有 EMS `auth:check` 校验后访问 Socket。
- Flow 内嵌页面和独立 Template 均只修改认证逻辑，保留各自现有页面内容；导入 Flow 后仍按部署说明使用最新独立 HTML 覆盖 Template。保留 postmessage/server_token 兼容模式，Worker 管理令牌路径和两站白名单不变。Token 签发方须匹配 M3_AUTH_BASE_URL；默认 EMS。失效或刷新已清除参数的 iframe 后需从 NocoBase 重新打开。
- 网关离线认证测试、Flow/内嵌脚本语法、query_token 浏览器回归通过，覆盖查询/提交 Bearer Header、URL 清理、过期禁用、日期/切站、1440/768/390/320明暗无溢出；已看桌面亮色和手机暗色截图。浏览器首次被沙箱阻止，经自动审批允许在沙箱外运行本地 Mock 后通过。
- 部署制品16项检查15通过、1失败：已有 m3/deploy/requirements.lock.txt 缺少 pyproject.toml 声明的 highspy==1.15.1，两文件本轮未改且与 HEAD 一致。本轮未处理该依赖问题。未访问或导入线上、未部署、未提交；保留 M4 其他任务修改。

## 2026-09-09 M4 账单页左对齐

- 用户追加“左边对齐”。线上HTML账单页的表头、数据列统一左对齐，手机指标改为标签/数值上下排列且左对齐；趋势图在绘图区靠左，点选日期按SVG实际坐标换算，保留响应式滚动。
- 账单Mock浏览器回归通过，1440/768/390/320明暗布局无溢出，已看桌面亮色与手机暗色实际截图。保留其他任务的所有修改；没有改数据或部署。

## 2026-09-09 M4 客户页面文字精简

- 用户要求去除HTML无关信息和文字。本轮精简线上HTML：账单页删除重复导语、长公式、数据库表/字段说明、非核心金额，收益名称简写；只保留简短折叠数据说明、金额单位、时间/覆盖及真实异常提示。成功查询状态文案留空，状态属性和交互保留。
- 计划/设备/历史页缩短或移除重复导语，执行卡删除重复解释；客户模式隐藏重复页脚，保留预测、未下发及执行状态。未改变数据计算、API、导出或错误处理，未修改其他任务的Node-RED迁移/发布文件。
- 8项收益计算测试、账单Mock浏览器回归和既有调度结果浏览器回归通过；明暗主题、1440/768/390/320布局检查通过，已看账单桌面/手机及其他页签截图。diff空白检查通过。详细原始口径仍保留在 `docs/m4/账单统计说明.md`。
- 本轮未提交、打包、部署、重启或访问生产。只修改线上HTML、账单说明及本交接；保留所有已有未提交改动。

## 2026-09-09 M4 收益趋势与月份对比已实现

- 用户明确“下一步”指功能，并确认优先做收益趋势和月份对比；本轮仅实现这两项，未继续打包/发布。线上HTML复用原账单GET接口，并行查询所选月份和上月；保留工作区及其他任务提交。
- 三条曲线：充电成本、放电收益、净收益（day_earnings来源值），图例开关、点选/日期滑块；保留负值，缺日期/字段处断开。月份对比统一用日统计汇总：当前月截至今天与上月同期；若上月较短，两期都截至较短日。历史月与上月各自整月，显示日期范围与覆盖。当天未结束的非同一时刻比较明确提示；缺日/来源失败/缺值不给出对应差额，零/负基期不给百分比，不认定节费。
- 主月结果先显示，上月失败只影响对比区；两请求共用取消与版本校验，迟到对比不能覆盖切站后的页面。没有新增API、网关、后端或轮询；2000-01不查询支持范围外的上月。
- 8项纯计算契约测试通过；增强账单Mock浏览器通过，含图例/日期交互、上月失败隔离、主月及对比迟到响应隔离、旧导出/错误/转义回归。1440/768/390/320明暗布局无页面/导航溢出，已看桌面亮色/手机暗色实际截图，`/tmp/m4-billing-screens/`。diff空白检查通过；Python Mock宿主退出有既有非阻断semaphore提示。
- 本轮改动为HTML、`m4/tests/m4_billing_e2e.js`、新增`m4/tests/m4_billing_analytics_test.js`、账单说明及本交接。本轮没有提交、部署、构建镜像、再取生产数据或触发求解。工作期间其他任务/用户已提交到 `e43784b`（暂存）；不能把该提交视为本轮趋势增强已提交。

## 2026-09-09 最新发布流程：本地构建 AMD64，经 CCR 发布

- 用户要求参照 M3 发布脚本，由本地构建 Docker 并经 CRR 推送。新增 `m4/deploy/build-and-push.sh`，沿用 M3 的腾讯云 CCR 仓库 `ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa` 与变量名 CRR_IMAGE；M4 标签为 `m4-0.1.0`、`m4-<sha12>-amd64`，避免覆盖 M3。
- 发布脚本要求干净 Git（与 M3 一致），从 HEAD 导出白名单 Git 快照，生成配套部署目录/ZIP，再本地 buildx --load、核对 AMD64、tag/push 双标签、读取 manifest。生成包的 `release.json` 和 `.env.example` 自动固定对应提交镜像。失败退出非零，脚本不连接/启动生产。
- 生产 Compose 改为只拉取镜像、没有 build 字段；安装目录仍 `/userdata/holo/pyfiles/m4`，端口/iframe Token/Node-RED 方式不变。手册更新本地发布、服务器 pull/up --no-build、更新回滚。build_package 新增 --image/--revision，修复版本号含小数点时 ZIP 文件名截断的问题。
- 验证：6项发布脚本测试通过（真实临时 Git + 模拟 Docker，覆盖快照/包/镜像一致性和失败阻断）；当前16项网关离线测试通过；Compose 只拉取/AMD64/默认8844与替代8845、Bash/Python语法、diff空白检查通过。
- 本轮未实际构建或推送镜像，未连接仓库或生产、未导入 Flow。工作区待提交修改仍在（含另一个任务已完成的账单功能），本任务未自动提交。正式发布要提交预定内容后运行脚本；旧 outputs ZIP 保留为历史材料，本轮未重新打包或宣称旧 ZIP 是新镜像配套包。

## 2026-09-09 最新部署决定：恢复 TCP 端口（覆盖此前 Socket 方案）

- 用户最新要求“还是使用端口吧”。安装目录继续 `/userdata/holo/pyfiles/m4`；默认 `127.0.0.1:${M4_BIND_PORT:-8844}:8844`，Node-RED 使用 `M4_BACKEND_URL`，宿主机用 localhost，容器用共享网络中的 `http://m4-api:8844`。继续 iframe 专用 Token，不新增 Nginx，不操作生产或导入 Flow。
- 已恢复 TCP Docker 启动/健康/preflight、HTTP Request 网关、配置/手册/架构图，删除 Socket 专用脚本及测试；保留网关中其他任务的账单路由和追加测试。当前 TCP 调整未提交；最近用户要求的提交仍为 `620ef58`。
- 最新交付：`outputs/m4/releases/m4-deployment-20260909-tcp-userdata/` 及同名 ZIP，共 61 文件。业务源码与 HTML 固定为 `620ef58`，叠加本次部署调整；不包含其他任务正在修改的账单 HTML/API/测试。旧 Socket 包仅作历史材料。
- 验证：工作区网关 16 项通过；隔离发布基线网关 15 项通过（临时目录首次缺 .venv，显式 M4_PYTHON=python3 后通过）；Compose 默认8844/替代8845的 loopback/内部端口映射通过；Python语法、git diff --check、SHA256/ZIP/HTML-Flow一致性及业务基线隔离检查通过。运行脚本与此前已验证 TCP 包逐字节相同，本次未重建 Docker 镜像或重复运行容器，也未做机器/Flow 导入验证。

# 项目状态

更新：2026-09-09。仅保留续接所需状态；长期规范见 [AGENTS.md](AGENTS.md)。实际 Git/Python 项目位于 `vifa/`，已有未提交改动须保留。

## 已实现：M4 账单统计（2026-09-09）

- 用户确认允许只读查询 `t_es_stat:list` 与 `t_es_count_stat:list`。已读取样例及用新适配器验证 ES01/2026-09、ES02/2026-08，分别返回9/31天、无缺日；仅读取这两表，没有生产写入、求解或设备下发。
- 新增 `m4/settings/billing.py`、`GET /m4-api/stations/{station_id}/bills?month=YYYY-MM`；沿用固定域名GET和分页完整性检查，按 `es_sn` 与北京时间 `createdAt` 月范围查询，复核错站错月、重复周期、数值、缺值。单来源失败保留另一来源；缺值不填零；日/月独立汇总并输出差额。没有独立账期字段，创建日期归属方式已明确写入页面及说明。
- 线上HTML客户界面增加“账单统计”第五页签：按站/月查询、月收益及充放电量、每日收支、日月差额、缺日/错误、CSV导出、各站月份记忆、请求取消与旧响应隔离。现有Mock账单仅留在独立演示模式。月表其他金额为独立原值，不把历史统计当正式结算，不计算缺少基准的节费率/达标结论。
- 真实样本ES02/8月月收益46778元，日月收益差额0.96元、充电量差额-0.5kWh；本月持续更新，差额来源未臆断。临时核对结果在 `/tmp/m4-billing-station-1-2026-09.json`、`/tmp/m4-billing-station-2-2026-08.json`。
- 10项账单单元/API测试、15项上游客户端、5项原输入API回归通过。新账单Mock浏览器及既有调度结果回归通过；账单覆盖1440/768/390/320明暗布局、负收益、CSV、缺失/错误、转义、错站/迟到响应、月份记忆、iframe Bearer。已查看桌面/平板/手机截图；窄屏导航间距和长表滚动已优化。截图 `/tmp/m4-billing-screens/`。Python测试宿主退出有既有非阻断semaphore清理提示。
- `prepare_proxy.js` 仅新增 `bills` GET白名单和月份过滤，账单专用网关测试在当前共享工作区通过。另一个任务正在将部署从Socket恢复TCP，保留它的修改；原16组网关在其变更前曾通过，不能据此宣称最新通信方案全量验证通过。部署手册已补充账单发布提示。
- 本次未提交、未重启现有业务服务、未重打部署包或镜像，未导入现场Flow。上线须同时同步HTML、后端、网关/Flow；旧发布包不自动包含账单。详细口径与验证见 `docs/m4/账单统计说明.md`。其他部署任务的包/提交范围见其交接条目。

## 最新：生产安装目录已统一（2026-09-09）

- 用户指定生产安装到 `/userdata/holo/pyfiles/m4`。已更新部署 Compose 默认 Socket 目录、backend/.env.example、Node-RED env 示例及手册；宿主 Socket 为 `socket/api.sock`，Node-RED 环境文件为 `secrets/node-red.env`，容器内部仍 `/run/vifa-m4/api.sock`。业务命名卷不变；没有连接或创建生产目录。
- 新交付 `outputs/m4/releases/m4-deployment-20260909-socket-userdata/` 及ZIP，基于已提交 `620ef58` 加本次5个部署配置/文档文件生成；63文件、SHA256/ZIP/路径及HTML-Flow一致性检查通过。未重做Flow导入或Docker构建，协议/容器路径未变。
- 工作区另有未提交的账单接口开发（billing.py、api.py、upstream.py、prepare_proxy.js等），已保留，**没有纳入此次发布包**。此次路径调整尚未提交；再次提交时需与账单开发区分范围。

## 最新：M4 已改 Unix Socket 部署（2026-09-09）

- 已按用户要求本地提交 `620ef58`（`feat(m4): add customer console and Unix Socket deployment`），分支 `feat/m4-offline-orchestration`。共53个M4源码/文档/测试文件，包含界面及其运行历史、95/96点、峰前提示等配套改动；提交后实际Git项目工作区干净，未推送。外层outputs部署ZIP与本交接文件不属于内层Git项目。

- 用户确认取消后端 TCP 端口，采用同机 Unix Socket。已生成 `outputs/m4/releases/m4-deployment-20260909-socket/` 及 ZIP（63个文件，SHA256/ZIP/HTML与Template一致性检查通过）；旧端口包保留为版本记录，新部署使用 `-socket` 包。
- 后端 Docker AMD64、单 worker、无 ports/EXPOSE，监听 `/run/vifa-m4/api.sock`；宿主 `M4_SOCKET_DIR` 默认 `/opt/vifa/m4/socket`，数据卷仍 `vifa-m4-data`。应用UID/GID10001，目录2750、Socket0660；运行锁及活动Socket/非Socket文件保护，健康检查改走Socket。
- Node-RED 同机挂载共享目录（容器可只读），补充组10001；环境改为 `M4_SOCKET_PATH`，不再用M4_BACKEND_URL/M4_BIND_PORT。现有settings.js合并 `functionGlobalContext.m4Http=require('http')`，使用原生http.request的socketPath转发，无shell命令、TCP回退、重试或跟随重定向。iframe URL与专用Token、前端HTML保持不变。
- 本地5项Socket生命周期测试、15组网关离线测试、原生Node.js Socket代理测试通过。AMD64镜像构建成功；无网络独立测试容器中验证无TCP/TCP6监听、UID/GID10001且有效capabilities为0、Socket0660；独立UID1000/GID10001客户端通过只读Socket挂载完成GET/本地偏好PUT/无效POST422；缺组权限被拒；preflight/health与重启后偏好revision保留均通过。已查看更新后部署图，临时容器和专用卷已清理。
- 按用户要求，未启动Node-RED导入Flow、未连接生产、未真实取数/求解/设备下发。现场settings/组权限/目录挂载/HTTPS及数据连通仍待部署人员有机器后核对。后端业务源码及优化规则未改。

## 先前：M4 端口版部署材料（2026-09-09，已由 Socket 包替代）

- 用户要求 HTML 放 Node-RED，NocoBase iframe 访问 URL，后端服务器 Docker；明确不要新增 Nginx，iframe URL 携带 Token，生产为 AMD64。又明确没有机器，不需要 Flow 导入验证。未 SSH、未部署生产、未触发真实求解或设备下发。
- 部署源位于 `m4/deploy/`，含生成脚本、Node-RED Token 校验/白名单转发、Docker 单 worker/持久化卷/文件 secret/降权运行、部署手册和图。已生成并校验 `outputs/m4/releases/m4-deployment-20260909/` 及同名 ZIP（61个文件，HTML与Flow模板一致，SHA256和ZIP内容校验通过）；这是源码包，不含镜像 tar、凭据和现场数据。
- 默认使用专用共享 `M4_IFRAME_TOKEN`：页面 query token 由 Node-RED 校验；canonical HTML 内存读取 token 后仅向同源 `/m4-api/` 加 Bearer，禁止凭据重定向及 Referer，401/403 显示客户提示；没有改后端算法/API。Node-RED 单独配置页面 Origin、iframe 父域、内部后端地址；后端数据源 Token 独立保存在 Docker secret。此方案不等于 NocoBase 当前用户身份/角色隔离；可选 Token 类型问询未得到具体选择，按专用访问 Token 落地。
- `m4_iframe_auth_e2e.js` Mock 浏览器测试通过；`m4_node_red_gateway_test.js` 15 组离线测试通过；部署 Python 语法和差异空白检查通过。Linux AMD64 镜像实构建成功（Python3.12.14/Pyomo6.10.1/HiGHS1.15.1，22个锁包）；独立空卷+假 Token 容器 preflight 通过。已实际查看部署图。
- 未完成 Node-RED HTTP 导入联调，按用户要求不再执行；临时测试容器/专用空卷已清理，未改既有服务。现场 HTTPS/iframe、真实 Token 权限、数据连通、迁移/恢复及生产性能待部署人员有机器后验证。详见随包 `验收记录.md`。

## 最新：M4 客户运行界面增加页签（2026-09-08）

- 后续客户化修正：运行记录列表、A/B选择和详情摘要不再直接展示短ID，改为生成时间与方案名称；同秒记录按当前列表加“记录1/2”区分。完整ID、原始来源/参数/偏好版本保留在默认折叠的“技术追溯信息”。查询、去重及选择绑定仍使用原ID；默认对比仍展示实际数值差异，版本差异只说明两轮记录不同。
- 用户要求 M4 从技术页面调整为客户运行界面。仅修改 `m4/web/M4优化调度控制台-线上版.html` 的 live 模式及相关浏览器回归/使用说明；保留既有未提交工作，不改后端、优化规则或真实设备状态。
- 新增“运行总览 / 调度计划 / 设备与数据 / 运行记录”四个独立 tabpanel。总览精简为四项实测/预测 KPI、最近计划、峰前提示、运行准备与执行状态；计划曲线/95–96点/候选比较在计划页，分步试算和技术详情默认折叠；柜级表在设备页默认展开；原历史抽屉内容成为记录页并保留分页/对比。
- 全局保留调度设置、刷新计划和生成计划；提交进度/错误在所有 Tab 可见。支持键盘方向/Home/End，后台轮询不改变当前页签；单站切换保留页签，全场→单站回到总览。历史异步请求继续按站隔离。界面仍明确“计划预览、未下发”。
- 最终 `m4_decision_results_e2e.js`、`m4_live_inputs_e2e.js`、`m4_decision_history_e2e.js` 全部退出0；原 `m4_online_static_console_e2e.js` 也通过。四页覆盖1440/768/390/320明暗布局，实际查看桌面/平板/手机截图；修正移动端按钮换行。截图位于 `/tmp/m4-peak-preparation-screens/`、`/tmp/m4-customer-devices/`、`/tmp/m4-history-screens/`。Mock Python退出仍有非阻断信号量清理提示。
- 本次未调用真实求解/下发、未重启服务、未改 M3。`/m4` 以 FileResponse 读取 HTML，已运行的本机页面刷新即可读取源码变更；此前上游输入失败和自动刷新问题未在此次界面任务中处理。

## 电站2无法决策原因核对（22:47，仅诊断）

- 用户贴出“输入快照已过期”及95点/warming_up/PV基线/emu22、24、26低SOC提示，询问为何无法决策。本轮仅GET核对，没有改规则或触发真实求解。
- 最新实际决策`0cbc9d67-afac-4c43-8735-383cb1a1b567`，22:44:21启动、22:44:34结束，`blocked_inputs`：光伏历史、峰谷时段、需量接口读取失败。配置阶段通过，电站2偏好已为revision2、`profile_priority`、pv→balanced→cost；不再是缺配置。
- 页面过期与这条决策失败要区分：前端Date.now超过缓存plan_start_at即标记过期；10秒轮询仅重绘输入状态，loadLiveInputs只在初始化/手动刷新/配置变化调用，不自动续接缓存。正式“生成调度计划”会重新取数，旧页面快照不是该POST直接使用的输入。
- 22:46:41 GET：load仅94点（新批次发布前），其他来源ready；emu21/23/25 SOC2%可参与，emu22=1.95%、24=1.8%、26=1.85%低于配置硬下限2%被逐柜排除，剩余1566kWh/300kW。低SOC提示不阻断整站，只减少参与柜。
- 22:47:37 GET再次出现不同来源读取失败：M3滚动接口与电价失败，PV/controls/realtime ready；当时仅emu25满足准入、522kWh。表明当前反复出现上游读取失败，不能承诺刷新必定可生成；异常究竟超时/限流/网络等尚未定位，错误消息被适配层统一脱敏。没有放宽SOC、伪造缺点或把提醒当硬阻断。
- 下一步建议处理M4输入时效展示/刷新与只读接口失败原因；本轮未擅自新增自动刷新行为。95点功能已验证，持续在线稳定性仍有上游读取问题。

## 最新：用户确认95点起算，已本机生效（22:43）

- 用户确认“95点起上”，随后要求继续。已实现M4连续95/96点窗口：滚动源已覆盖起点后的95点时直接采用，不为凑96依赖手动尾点；滚动不足95时保留旧手动来源兼容。最终只能裁掉末尾缺失的一格，中间缺口、缺首点或不足95仍阻断。不改M3，不补造预测、不下发。
- `forecast_source.py`输出实际horizon，策略`rolling-min95-v2`；`live_inputs.py`对齐负荷、PV、电价及period_types长度、plan_end_at，请求二次复核95/96长度与时间。`adapter.py`/优化契约使用实际点数，Pyomo所有时段变量/能量状态/终端SOC约束动态化；95点模型追加`/horizon-95-v1`，96点旧模型版本保留。午夜谷段标签不足时不推断，完整已知谷段仍能启用既有偏好。
- M4候选/最终/历史页面接受95/96并按同轮点数校验；时长、来源分母、末点索引、滑块、图轴、明细、历史结束时间与峰前估算窗口同步。95点为23小时45分钟。旧96点记录继续独立复验，不重写或冒充新计划。
- 新增`m4/tests/test_m4_variable_horizon.py`及95点决策链/浏览器回归；先红后绿，**全M4 403项unittest通过（111.611s）**。95与96候选、95/96最终结果、历史对比、输入、旧离线/演示共六次Mock浏览器运行最终通过；浏览器验证期间修复一处旧演示标题引用错误，测试等待与闭包参数问题亦已纠正。已查看95点桌面亮色和手机暗色截图，`/tmp/m4-95-screens/`与`/tmp/m4-95-candidate-screens/`。Starlette弃用、Mock退出信号量提示非阻断。`git diff --check`通过，业务修改未提交。
- 确认两站无运行中任务后，本机8844重启PID42735（续接重查）；前后两站参数/偏好JSON相同，历史独立复验通过。此时电站1三条completed；电站2已有两条blocked_inputs/blocked_configuration（不是本轮工具创建，保留用户/其他操作产生的记录）。本轮没有通过真实POST重新求解。
- 实测22:41电站1输入ready：95点滚动、手动0点，负荷/PV/电价各95；计划22:45至次日22:30。**电站2负荷95点已ready，但整组输入仍blocked，另因光伏历史接口读取失败。** 22:41:46和22:43:16两次GET均如此，tariff/controls/realtime都ready；未用零光伏绕过、未改历史接口读取逻辑。临时核对快照`/tmp/m4-95-station-2-inputs.json`。不能对用户声称电站2整轮可生成计划；剩余阻断与原尾点问题分开说明，PV接口原因尚未进一步定位。
- 实施计划`docs/m4/plans/2026-09-08-m4-95-point-window.md`，说明已更新`docs/m4/求解器决策说明.md`及优化器README。未改任何M3文件、未SSH、未创建自动化、未下发设备。

## 电站 2 缺点核对（21:48，只读诊断）

- 用户指出电站2通常缺1点并要求查看。本轮只读实际M4输入与两站M3现有结果，未修改业务代码、参数、预测/调度任务，也未下发。
- 21:46:25实际M4 station-2输入：计划9月8日22:00至9月9日22:00；滚动仍是21:32发布、从21:30至次日21:30，匹配94点，末尾缺2点。PV/电价/控制/实时来源均ready，仅负荷incomplete阻断。
- 21:47:56再次核对：新批次21:47:11发布、从21:45至次日21:45，M4匹配95点、手动补缺0点；唯一缺失是9月9日21:45–22:00。原始M3序列完整96点，当前时段被M4下一时段起点排除，是窗口错位而非随机丢点。旧批次尚未刷新时会暂缺更多点；本轮实测94→95。
- 两站差别在手动兜底：ES02已完成手动预测只覆盖9月8日00:00至9月9日00:00，不能补明晚；ES01有用户已跑的192点/两天结果，覆盖至9月10日00:00，所以此前能够补齐。上轮“接入完成”仅指读取接通，未解决每站独立滚动96点完整覆盖，不能声称电站2可无人值守运行。
- 用户此前仍限定M3暂不处理，本轮未擅自扩展M3或用尾点复制。后续若要只改M4并取消手动依赖，需要讨论/实现按实际可用预测窗口求解（通常95点，更新迟延可能更少）及相应边界；尚未授权更改固定96点/24小时契约，不把建议当作已实现。

## 最新：仅 M4 接入 M3 现有滚动来源（21:39 本机已生效）

- 用户限定“只需要接M4数据源，M3部分暂时不用处理”。本轮未修改任何M3文件、任务、算法或部署。只改M4负荷适配、只读表白名单、来源提示及相关测试/说明；未提交、未自动求解、未EMS下发。
- `forecast_source.py`优先读取本站`energy_forecast_latest.series_payload`的`station_total_load`。滚动点`data_time`是区间起点，`target_time`是终点；与手动表原有起点契约分别适配。校验站点、唯一快照、时间顺序、96点连续性、单位/状态、数值与版本。损坏/错站/读取失败报错，不静默改用手动。
- 已告知用户并按默认推荐实现：滚动优先，只有未覆盖时段使用既有有效手动预测补缺；不触发M3、不复制尾点。曾用异步工具询问“手动补缺/仅滚动缺点阻断”，未收到明确回复；等待合理时间后已声明采用前者。若用户后续选择纯滚动，需收窄适配并保留96点门禁。不存在滚动覆盖时明确显示手动来源；两者仍不足96点时阻断新计划。
- `sources.load`新增来源类型、滚动/手动点数、生成时间与覆盖范围、逐点来源；版本绑定`rolling-first-manual-gaps-v1`、实际值与批次。M4页面显示点数/更新时间，`warming_up/degraded`提示传至输入告知；M3预测SOC不参与M4初始SOC，后者仍取实时参与柜。
- 先红后绿：86项相关unittest通过（滚动来源、旧手动来源、只读客户端、输入/请求、候选、决策链、历史）；输入Mock浏览器回归通过，覆盖1440/768/390/320明暗主题、来源信息与原柜级交互，全程GET；已查看桌面亮色和手机暗色截图，目录`/tmp/m4-rolling-screens/`。`git diff --check`通过。未重跑全M4；Starlette弃用提示非阻断。
- 确认两站无运行中任务后，本机8844重启PID40857（续接重查）。前后两站参数/偏好JSON相同，电站1三条completed历史独立复验，电站2历史为空。21:39:09 GET输入ready，计划起点21:45，95点滚动+1点手动，滚动21:32:11生成、覆盖21:30至次日21:30，warming_up/SeasonalNaive；emu11+emu12参与。当前预览计划仍是21:18生成的d20a6bda，不应声称已按滚动来源重算。
- CUA报告电脑锁屏，未刷新用户已打开M4标签；已告知解锁后刷新即可。本机服务与HTML已加载新版本。仅接来源尚不能保证全天无人值守96点覆盖，通常尾1点仍依赖既有手动预测；M3扩展预测窗不在本轮范围。

## 双柜正式预览已补齐（21:18 完成）

- 用户确认“按新规则生成双柜预览计划”后，已通过本机`POST /m4-api/stations/station-1/decision-runs`仅启动一次预览，request/run_id `d20a6bda-fd8a-437c-8ccb-9ae97915c787`。21:18:17 completed，三候选optimal，最终balanced，独立证据复验通过；`dispatch_status=not_dispatched`，未调用EMS下发、未创建自动化。
- 新计划从9月8日21:30起96点/24小时，emu11+emu12、排除无，1044kWh、充放电上限200/200kW、初始SOC2.325%，需量504kW。预测最大购电504.000001kW（1e-6数值容差量级），净电费4910.38元；不是实测节费，也不能与旧周期费用直接归因比较。
- 新记录来源含`emu11-alert-advisory-v1`；参数与运营偏好运行前后JSON一致，其他设备状态/SOC/时效规则未改。旧18:34单柜记录保留为历史，新双柜记录已成为首页最新结果。
- 已在当前任务M4标签核对首页完成时间21:18:17、均衡方案、峰值504kW、21:30起96点；随后仅刷新输入，21:19页面显示2/2柜、1044kWh、排除无，emu11告警仅提示。浏览器没有点击重新生成或设备动作。
- 本轮没有修改业务源码或Git提交。核对报告：`outputs/m4/solver-decisions/station-1/d20a6bda-fd8a-437c-8ccb-9ae97915c787/展示核对.md`。下文遗漏清单的“双柜展示计划未更新”现已关闭；在线判定口径和版本提交是独立事项，不声称一并完成。

## 用户要求遗漏核对（告警修正后）

- 用户询问“还有什么是你遗漏的？我提过的”。本轮只核对代码及已有记录，未追加求解或改规则。架构要求已落实：主建模与最终选择为Pyomo/HiGHS，无AI参与；日常页面已改；EMS未接入、未下发。
- 未闭环：最新正式展示结果仍是18:34的`53243a88...`，仅emu12/522kWh；21:12已修复的当前输入为双柜/1044kWh，两者不是同一轮。此前助手将“只展示、不下发”额外解释成“不得新求解”，这是助手增加的限制，不是用户明确要求。若继续完善展示，应基于新准入生成仅预览结果，而不是把旧记录冒充双柜新结果；本轮审核没有启动求解。
- 口径差异：“在线即可”当前仅落实为取消emu11的alert_status阻断，EMU/BCU/PCS允许运行状态及SOC/时效等检查仍在。不得笼统声称已实现字面上的“仅看在线”，也不得凭此审核自动删除其他校验。后续需清楚区分在线显示与优化输入约束。
- 当前多数页面/历史/告警修正仍未Git提交；先前“固化版本”只保存了源码校验清单，且清单已被之后修正超出。未提交是交付状态，不冒充用户额外提出的Git提交要求。

## 最新生效口径：emu11 告警仅提示（21:12 已落地）

- 用户指出“不是取消了？”明确要求正式页面同步此前emu11告警豁免。此前仅独立试算生效、正式准入未同步是本任务遗漏；本轮已修正。**本段优先于后文“正式仍严格排除emu11”的历史描述。** 范围仅station-1/emu11的alert_status，其他设备状态、数据时效、SOC、记录完整性和其他柜告警规则保留。继续只展示、不下发。
- `m4/settings/realtime.py`新增`emu11-alert-advisory-v1`及精确站点/柜匹配，告警进入advisories而非issues，原始上报保留。`live_inputs.py`请求复核同步规则，缺alarm_policy的旧快照保持严格校验，新快照及request.source_versions记录版本，未知版本拒绝。旧两条正式记录仍独立复验通过，不重写历史。
- 页面emu11告警显示“仅提示，不影响输入准入”，不再仅因alert排除。先红后绿：78项实时/输入/候选/决策链/历史unittest通过，柜级Mock浏览器回归通过（告警仍展示但可参与、缺/未知标志、过期、其他故障及明暗布局）。原严格告警测试移至emu12，部分能力测试使用真实故障条件，未删除隔离检查。差异空白检查通过。
- 本机8844确认两站无运行中任务后重启，当前PID39574（续接重查）；重启前后两站参数/偏好JSON相同。GET输入21:12:23为ready，emu11 wait/alert且available=true，emu12 wait/null且available=true；1044kWh、200/200kW，未下发。已刷新当前任务内M4标签并展开柜级表，21:12:59页面确认2/2柜、排除无，emu11可参与新计划。
- 本轮只读刷新实际输入，没有触发新求解、自动化或EMS下发。此前自动化审批拒绝不代表已授权创建自动化，此次未申请。使用/验收说明已追加更正，旧验收SHA清单对应更正前版本；未覆盖该历史清单。全部代码仍未提交，保留其他任务既有改动。

## 展示版收口验收（2026-09-08 已完成，未提交）

- 用户在“展示版验收与收口”建议后要求下一步，本轮完成日常查看验收。发现柜级表未单列告警，现分开显示EMU状态/采样时效、告警上报、新计划输入准入，明确它不是历史计划参与名单或执行状态。缺字段/未知告警不显示无告警；仅改页面表达，不改emu11告警准入、后端、求解器或参数。
- 三组浏览器回归通过：`m4_live_inputs_e2e.js`、`m4_decision_results_e2e.js`、`m4_decision_history_e2e.js`，各覆盖4宽度×明暗。原输入测试因改版后面板折叠超时，已改为点击实际日常入口；新增告警断言先红后绿，补充未知字段及过期场景，隐藏分析图使用textContent断言。差异空白检查通过。本轮未重复全M4单元测试，既往30项结果见后文；Mock服务退出信号量提示仍非阻断。
- 已实际查看柜级桌面/平板/手机截图。新增`docs/m4/展示版使用与验收.md`，验收截图与15份相关源码SHA-256清单位于`outputs/m4/evaluations/2026-09-08-display-acceptance/`。清单固定本次验收内容，不等于Git提交或部署；此前未提交工作全部保留。
- 只读核对已有正式证据：18:12、18:34两轮均仅emu12参与，emu11当时wait/alert；不代表当前在线状态。没有再次取现场数据、真实求解、告警豁免、自动化或EMS下发。HTML经FileResponse按请求读取，本轮无需重启本机服务。

## 当前展示阶段：历史计划与两轮对比（已完成，未提交）

- 用户最新边界为“目前只做展示 不下发”。本轮“下一步”已实现顶部“历史计划”抽屉：按电站分页浏览、指定轮次读取、96点明细、两轮共同时间点的功率/SOC差异、输入/参数/偏好版本对照。历史视图独立于首页最新结果轮询；费用按各自周期显示，不能解释为实际节费或因果结论。
- 新增只读GET `decision-history?limit=10&offset=0` 与 `decision-results/{run_id}`；记录继续完整独立复验，损坏条目明确标记，指定轮次失败不替换。新增可选 `record.input_summary` 供对照。页码按保存时间倒序，分页中有新记录时需刷新；前端去重。未改变求解器、准入、客户参数或EMS接口，未导入emu11独立告警例外试算。
- 30项相关unittest通过（历史5、决策链15、峰前8、运行2）；历史和现有决策页两组Mock浏览器回归通过，覆盖分页/96点/95个共同点/无重叠/损坏及阻断/错站/晚响应与1440、768、390、320明暗布局。历史浏览全程GET，未触发真实求解。已查看桌面、平板、手机截图；截图在`/tmp/m4-history-screens/`，差异空白检查通过。未重复全M4测试。沿用的Starlette弃用与Mock退出信号量提示均非阻断，进程退出0。
- 本机8844确认两站无运行中任务后重启至PID36691（续接需重查），页面与两条历史GET可用：电站1有2条completed正式记录，电站2为空；重启前后两站参数/偏好JSON一致。未部署生产、未SSH、未创建自动化、未下发。修改与此前峰前提示一起保留未提交。

## 当前任务：M4 AI 清理（已完成）

- 已按用户要求本地提交：`7a46fdc`（`refactor(m4): replace AI selection with solver decisions`），分支 `feat/m4-offline-orchestration`；包含此前求解器迁移与本次清理，未推送。历史评测/run 目录原属 Git 忽略，本地删除不形成提交记录。

- 用户要求清除 M4 AI 相关内容，并明确连同历史评测和原始运行记录全部删除。已删除模型适配器、配置、旧 CLI/API、专属测试及 12 个历史目录（含旧模型工况衍生的偏好对照）；保留数学优化器、经营偏好、SQLite、求解器决策及纯数学光伏验证。
- 旧 ai-runs / ai-selection 路径已移除并实测404；编排仅接受 v2/pending_selection。离线示例已由当前 Pyomo/HiGHS 重新求解并同步页面。S03 旧数学回归输入/结果迁至 m4/tests/fixtures/m4_pv_legacy_midday，迁移内容一致，不保留模型请求或回答。
- 本次验证：全M4 **371项 unittest 通过（138.347秒）**；离线控制台、决策结果两组浏览器回归通过，覆盖1440/768/390/320明暗、导入拒绝旧契约、切站/对比/运行锁/失败和晚响应。已查看桌面亮色与手机暗色实际截图。嵌入数据一致性和差异空白检查通过。
- 本地8844已在无运行中决策时重启（PID32393，续接需重查）；新页面及两站决策GET接口正常，旧AI路由404，SQLite文件哈希未变。未连接模型或生产、未触发真实输入计算或EMS。浏览器沙箱启动失败后经批准在沙箱外运行通过；退出时有非阻断的Python semaphore清理提示。

## M4 求解器决策架构

- 2026-09-08 用户明确取消 M4 的 AI 决策参与，并授权选择新建模框架。现已迁移为 **Pyomo 6.10.1 + HiGHS 1.15.1**：调度模型使用具名 Var/Constraint/Expression，最终方案使用单选 MILP；原确定性比较器仅作独立审计。主模型不再手工组装稀疏矩阵或调用 SciPy milp。新模型版本追加 `/pyomo-v1`，新选择版本 `pyomo-highs-selection-v2`。
- 保留 balanced/cost/pv 三候选及既有跨方案需量优先比较，界面突出一套最终计划，其余展开对比。直接只算 balanced 会改变原有业务语义，本轮未这样替换。SOC、功率、设备状态、期末SOC、PV及谷段早充规则未放宽。
- 新增 `m4.selection.decision_chain`、`m4.settings.decision_runs`、`decision_results`；页面使用 `decision-runs` / `decision-result`。新链不导入 AI 配置/传输、不调用模型、不受模型门禁影响；旧 AI 运行/历史接口及 CLI、实验函数、原始证据已在本次清理中删除。其余既有未提交改动保留。
- 后台按站互斥、请求幂等、中断可识别。证据目录 `outputs/m4/solver-decisions/<station_id>/<run_id>/`，记录输入、配置/偏好、候选、选择及哈希；读取时独立重建校验。审查发现的初次输入/阶段状态可与completed矛盾问题已修复并补回归。
- 电站1偏好保持 revision=2 / `station-1-selection-v2-30588b1cf5965578`，`profile_priority`，顺序 balanced/cost/pv，需量容差1e-6、排名容差0。电站2参数/偏好未自动配置；不修改SQLite客户口径。
- 新编排输出 `m4-orchestration-v2 / pending_selection`；本次清理已移除旧 v1 兼容。最终选择独立返回，不回写候选快照。
- 入口：[求解器决策说明](docs/m4/求解器决策说明.md)、[当前概要设计与架构图](docs/m4/M4优化调度控制台概要设计.md)、[实施设计](docs/m4/specs/2026-09-08-m4-solver-decision-design.md)。已随 `7a46fdc` 本地提交，未推送。

## M4 已完成与有效口径

- 本地真实输入、参数、按柜隔离、候选计算和页面预览已实现；阶段提交 `11ff466` 位于 `feat/m4-offline-orchestration`。后续新光伏规则与评测仍有未提交改动。运行入口见[调度参数配置说明](docs/m4/调度参数配置说明.md)，不沿用历史 PID 或假定服务仍在运行。
- 两电站独立：ES01 为 emu11/12、无光伏；ES02 为 emu21～26，emu27 仅光伏；共用电价，末段延续至 24:00。完整 96 点负荷/光伏/电价及来源时效必须校验，核心不猜测或插补缺点。光伏目前采用七日历史参考，未接天气预测。
- 固定柜名单及总容量按柜等分；异常、缺失或过期柜逐柜排除，其余柜可参与。可用容量/功率按参与数量折算、SOC 按参与柜加权，完整全站 SOC 仅作对照。全部不可用或必需站级输入缺失才阻断。`alert_status` 的现场故障语义仍待核实，不能用 M1 显示正常绕过现行校验。
- 计划为参与柜适用的站级汇总，单柜分配由 EMS 负责。SOC、功率、设备状态与期末 SOC 为硬约束；需量为高优先级软目标，允许记录不可避免超限，不突破安全边界或切负载。正式 EMS 聚合能力及下发/回读接口尚未接入。
- 站1 **504 kW 仅为用户指定的离线测试阈值**，按整站负载计算，不除以柜数；未改上游 `need_kw` 或真实服务。真实适配使用上游需量，不扣免调功率；`re_flow=40` 已确认当前不生效，不能直接当作允许外送上限。
- 新请求 `pv_dispatch_policy=load_first_economic`：PV 先供负载，富余不购电；禁逆流时尽量入储，安全能力无法吸收时允许限发；允许逆流时在既定优先级内经济优化。合法外送不计入未吸收光伏。缺省 `legacy` 用于历史兼容，**真实适配器未自动启用新规则**；客户逆流权限、上网电价和上限入口待接入。见[新光伏验证](outputs/m4/evaluations/2026-09-07-pv-policy-v2/验证说明.md)。
- 分层目标优先锁定需量峰值/超限量，再按候选配置处理 SOC 推荐偏离、费用、光伏与吞吐量；配置及容差须版本化。`energy_cost` 为净电费，不含循环成本；推荐 SOC 偏离不是安全越限。详细定义以[优化器说明](docs/m4/optimizer/README.md)和契约为准。
- 末层可选 `valley_charge_delay` 仅重排凌晨同价谷段已有充电，不能降低前序目标、跨日/跨价格搬移或新增循环；保留中午平段经济补电，不新增“仅谷段充电”硬约束。`feasible` 与 `optimal` 分开报告，不能把可行或重复稳定等同全局最优。
- 新 B1 契约为 `m4-orchestration-v2 / selection_status=pending_selection`、候选 ID 空、`dispatch_status=not_dispatched`；仅支持当前 v2 契约。版本变化、过期、晚响应均不能使旧候选成为当前可用计划。选择通过独立 API 返回并在页面展示，不回写 B1。
- 页面除账单抽屉外已确认；账单暂缓。未部署、未向真实设备下发，不把合成工况/预测费用当作实测收益。

## 其他模块待办

- M1：2026-09-08 补齐历史告警的场站、电站和设备节点归属校验，无关旧记录保留但不展示或计数；10 项后端测试及本地 Mock 浏览器回归通过，未部署。时间来源和 SQLite 历史告警已实现，部署须同步页面/Python 并保留持久化目录；见[时间与历史告警说明](docs/m1/时间与历史告警说明.md)。
- M2：历史效率/瓶颈查询已实现，部署须同步 HTML、Python、Flow；见[历史查询说明](docs/m2/历史查询说明.md)。
- M3：OOM 修复 `2058a6f`、模型提示 `208676c`、默认日期修复 `97c9cd1` 与 Worker 1.5 GiB 限制待部署核验；5 分钟 AutoARIMA 曾触发 OOM，修复部署前不恢复旧 Worker 循环。切站/训练历史统计改动尚未运行测试；页面只维护生产 Template，不生成 page.html 或旧同步文件。见[部署说明](docs/m3/部署说明.md)。
- 以上均为历史完成/待部署记录，未在本轮重新核实服务或生产状态；部署、SSH 和 EMS 操作不在当前范围。

## 续接入口与验证

- 使用 `.venv/bin/python` 与 `unittest`，依赖通过 `pyproject.toml` / `uv.lock` 管理。当前全M4验证结果见上方，不再自动继续历史AI选型任务。
- 本地预览 `http://127.0.0.1:8844/m4`；只读刷新不会主动优化。实际运行前仍需有效输入、已保存调度参数与经营偏好。
- [调度参数配置说明](docs/m4/调度参数配置说明.md)、[真实数据接口盘点](docs/m4/真实数据接口盘点.md)、[编排说明](docs/m4/orchestrator/README.md)。

## 2026-09-08 本任务追加：电站 1 真实数据影子试运行

- 用户确认试运行后，先通过本机8844执行原规则全链路，run_id `53243a88-6de1-4061-a910-539d51044141`，completed，仅emu12参与；预测购电峰值543.851 kW，需量超限39.851 kW，EMS未下发。
- 随后用户要求“emu11查看状态，在线即可，先排除告警”。18:38原始采样显示emu11的EMU/BCU/PCS均wait且持续更新，SOC2%，alert仍保留。仅在本任务独立影子试算中把emu11告警降为提示，未改主服务规则、SQLite或上游告警，未豁免其他柜及非告警约束。
- 双柜试算completed：1044 kWh、200/200 kW、初始SOC2.325%；9月8日18:45起96点，选balanced，预测最大购电530.197 kW、峰值超限26.197 kW、累计超限52.394 kWh；SOC2%–95.000004%，期末2%。19:00–21:00仍超限，主要因初始可用电量很少、峰前补电时间不足。读取到的真实需量接口当前就是504 kW，本轮未修改。
- 三候选逐点独立验证、指标复算、求解器选择与独立比较器一致、求解后重取版本/状态一致均通过。双柜结果仅保存在独立试算目录，未写主服务决策历史；页面默认准入仍按原规则。效率1.0/循环成本0/时效86400秒均沿用已保存配置，未改客户口径。
- 报告及前后原始证据：`outputs/m4/field-trials/20260908T103328Z-station-1/试运行报告.md`；成功双柜目录`emu11-advisory-cycle-1-validated`，脚本`run_advisory_trial.py`。两次脚本日期解析错误在求解前阻断，修正后重新取数通过；失败证据保留。
- 自动审批拒绝创建每15分钟共两轮的后续自动化，理由为重复忽略emu11告警尚需明确授权。**未创建自动化，不得用其他工具绕过；等待用户明确批准后才可再次申请。** 当前仅一轮双柜验证完成，尚未验证多周期稳定性。未修改其他任务的清理进度或源码。

- 后续仅用18:38保存快照量化晚峰缺口：控制在504 kW需105.787 kWh，初始可用3.393 kWh、峰前可补50 kWh，缺52.394 kWh；等效18:45初始SOC至少7.344%，或额外约15.72分钟200 kW补电（沿用100%效率，未核对更早时段负荷，非执行指令）。结果追加至试运行报告，未继续真实求解或创建自动化。

## 2026-09-08 本任务追加：峰前电量准备提示（已完成，未提交）

- 用户连续要求“下一步”，本轮实际接入日常运行页面：在最终方案中显示预测峰段、所需电池侧电量、方案峰前可用电量、缺口和该时段预计超限；详情提供各峰段、SOC与效率依据、来源时间/版本。功率不足、容量不足、无充电能力分别提示，满功率补电时间仅作理论下限。其他候选曲线不改变最终方案估算。
- 新增 `m4/settings/peak_preparation.py`，仅对保存的已验证请求/计划做数值解释，不改变优化目标、准入规则或用户参数。`decision-result`新增可选 `record.peak_preparation`（m4-peak-preparation-v1），只有completed且最终选择/证据独立校验后提供；补齐候选公开metrics中的max_grid_import_kw。旧服务缺字段显示暂无估算；异常格式/跨计划绑定、读取失败/切站均不会沿用错误估算。
- 数值测试覆盖效率、PV抵扣、多峰段、取方案峰前SOC、功率与电量不足区分、容量不足/零充电、充电导致超限、求解容差和不合法计划。最终8项数值测试 + 17项决策链/运行测试共25项通过；新增浏览器场景生成器，浏览器回归覆盖电量/功率/无峰段、旧字段缺失、错误绑定、候选切换与8组尺寸主题（1440/768/390/320×明暗），已实际查看桌面/平板/手机截图。`git diff --check`通过；未重跑全M4。
- 截图 `/tmp/m4-peak-preparation-screens/`。单元测试有Starlette/httpx弃用提示，浏览器Mock退出有resource_tracker信号量提示，均退出0，不影响本轮验证。
- 本地8844在两站无运行中决策时重启加载新代码，本轮PID35101（续接需重查）；实际GET决策结果返回新字段，页面200，重启前后station1参数/偏好JSON一致。读取的是既有单柜正式记录53243a88...，双柜临时告警例外结果仍为独立报告，不导入正式历史。本轮未触发真实求解、未创建被拒绝的自动化、未下发EMS。修改尚未提交。
