# ES02 天气驱动光伏离线回测 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 在当前任务内逐项执行。用户已确认继续上一轮提出的数据集、对比曲线和误差报告范围；不再重复审批，不使用多代理。

**Goal:** 交付可复现的ES02十五分钟训练数据、按日滚动回测、功率曲线与误差报告。

**Architecture:** 独立的数据对齐与回测模块，由本地CLI读取已授权获取的实测快照及已入库CSV的相同来源文件。只生成本地分析工件；不发布运行预测，不接M4，不改变数据库。

**Tech Stack:** Python、现有NumPy/Pandas、unittest；绘图使用独立分析环境的Matplotlib。

**Spec:** `docs/m3/光伏预测数据库设计.md` 的ES02、天气来源/时间语义、十五分钟平均交流功率与输入快照约定；本计划补充本轮离线算法和验证边界。

## Global Constraints

- ES02，Asia/Shanghai，来源坐标23/113；原天气文件SHA-256须与已入库验证记录一致。
- 原实测快照完整保留并记录SHA-256；只使用 `es_sn/timestamp/ac_solar_power`，不包含凭据。
- 实测按分钟先均值、再按左闭右开十五分钟均值。每点至少12个非空有效分钟；不填零、不跨缺口插值。四个点均合格才纳入该小时，延续前置319小时口径。估计电量是分钟采样均值积分，不是电能表实绩。
- 原辐照时间标签为前一小时均值：整小时内四个十五分钟点使用小时结束标签的辐照。降水累计平均分到四个点。气温/湿度/云量/风速用两端整点瞬时值线性插值到区间中点。字段分别对齐，不统一平移。
- 小时天气展开为十五分钟不增加真实天气分辨率。使用事后再分析天气，评估类型固定 `retrospective_known_weather`；不称作历史时点已知输入或上线预报精度。
- 拟合仅使用预测日零点之前且区间已结束的功率标签；至少7个完整训练日。每日重新拟合，后面的功率不影响前面的模型及参数。天气数据可用性未证明，不作 operational 发布。
- 三模型固定比较：昨日同刻 SeasonalNaive、非负单系数辐照比例 IrradianceGain、天气交互特征 WeatherRidge。不根据测试集调整特征或正则参数。
- Ridge无截距，九个特征：GHI、GHI×温度、GHI×总云量、GHI×湿度、GHI×风速、水平直射、散射、GHI×时刻正弦/余弦。每折用训练集RMS缩放，固定目标 `mean((Xw-y)^2)+0.01*sum(w^2)`。非负截断；没有确认装机交流上限，不编造容量上限。
- 所有天气特征在GHI为零且直射/散射为零时为零，预测零；不把光伏缺失当夜间。
- 同一组有效点比较MAE、RMSE、WAPE；有辐照时段定义为GHI>20W/m²，并报告筛选数。日发电量比较只使用三模型与实测均覆盖96点的日期；缺点日展示缺口，不补成全天电量。
- 模型参数、训练边界、输入文件、输出文件、代码SHA及库版本均写入工件。重复运行不得覆盖已有输出目录。

## Task 1: 时间与质量对齐

**Files:** 新增 `m3/worker/domain/pv_training.py`、`m3/tests/test_pv_weather_backtest.py`。

**Interfaces:** `prepare_pv(rows: list[dict]) -> pd.DataFrame` 返回完整十五分钟网格及质量状态；`align_weather(pv, weather) -> pd.DataFrame` 返回带天气来源标签和 `eligible/rejection_reason` 的整网格。

- [x] 先写手算测试：01:00标签辐照400对应00:00至00:45四点；温度20→28时00:00点中值21，不能直接平移为28。
- [x] 测试11分钟不合格、12分钟合格、同分钟多采样不增加覆盖、空功率不补零、混站/重复精确时间/非有限与负数功率拒绝。
- [x] 运行 `python -m unittest m3.tests.test_pv_weather_backtest -v`，确认新模块缺失导致失败；实现后同命令通过。
- [x] 从原始快照生成整网格并复算319个合格小时；保存被排除记录及原因，保留训练数据来源列。

## Task 2: 模型与时间滚动回测

**Files:** 新增 `m3/worker/domain/pv_backtest.py`；扩展 `m3/tests/test_pv_weather_backtest.py`。

**Interfaces:** `rolling_backtest(aligned, min_complete_days=7) -> (points, folds)`；`summarize(points) -> dict` 生成同点误差和完整日期能量。

- [x] 先测试训练外功率扰动不改变该日预测；正则缩放只由训练样本决定；预测夜间零且非负。
- [x] 手算指标：实测[100,200]、预测[80,220]，MAE=20，RMSE=20，WAPE=40/300×100%；95点的日期不能输出全天电量。
- [x] 昨日缺失同刻保持空缺，不能按行偏移96行代替时间减一天；未来功率不得进入昨日基线。
- [x] 观察失败后实现；模型使用NumPy线性代数，不增加生产依赖。每折记录完整天数、最大标签结束时间、系数及RMS缩放。

```python
# 时间拆分的核心边界；天气的事后可用性另行披露。
train = eligible[eligible.index + pd.Timedelta(minutes=15) <= day]
test = eligible[(eligible.index >= day) & (eligible.index < day + pd.Timedelta(days=1))]
baseline = actual.reindex(test.index - pd.Timedelta(days=1))
```

## Task 3: 可复现工件与实数据验证

**Files:** 新增 `m3/scripts/backtest-pv-history.py`、`m3/scripts/plot-pv-backtest.py`；工件在 `outputs/m3/backtests/pv_backtest_es02_20260909/`。

- [x] CLI显式接收 `--pv-jsonl`、`--weather-csv`、`--output`，输出已存在即拒绝。读取已入库报告核对天气SHA及ES02批次。
- [x] 保存原快照、aligned_all.csv、training.csv、backtest_points.csv、metrics.json、folds.json、manifest.json、中文报告。JSON拒绝NaN，CSV空值保持空。
- [x] 真实运行一次，检查训练/测试严格按日期、共同评分点集合、完整日电量资格、预测有限非负、源码与输入哈希可重现。
- [x] 绘制所有回测日期功率曲线、同点误差和完整日电量对比；缺口绘制断线，不连成有效观测。
- [x] 用view_image看实际PNG，检查中文、图例、日期、单位、缺口与报告数值一致。
- [x] 自行阅读最终代码，运行仅新增规则相关测试；更新CODEX_HANDOFF并报告实际结果与再分析天气限制。

## References

- [Open-Meteo历史天气：来源与前一小时辐照语义](https://open-meteo.com/en/docs/historical-weather-api)
- [Ridge的L2正则目标](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html)
- [时间序列交叉验证与时间顺序](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split)
