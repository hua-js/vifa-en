# M3 Weekly Load Forecast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the custom load forecast's daily fallback with auditable weekly-history model selection while preserving the independently anchored SOC forecast.

**Architecture:** Extend each cleaned training dataset with aligned usable-week evidence, implement lightweight weekly load profiles in a focused domain module, and dispatch load versus SOC through separate selection paths. Persist the weekly strategy and candidate scores in existing JSON manifests, filter performance aggregation by strategy version, and render the evidence in the canonical Node-RED HTML without changing database tables or public request shapes.

**Tech Stack:** Python 3.12, pandas 2.3.3, NumPy 2.4.6, StatsForecast 2.1.1, FastAPI/Pydantic, `unittest`, vanilla HTML/CSS/JavaScript, Node-RED Flow JSON.

**Spec:** `docs/m3/specs/2026-08-31-m3-weekly-load-forecast-design.md`

## Global Constraints

- Load forecasting uses historical `station_total_load` only; no weather, calendar, production-plan, M4-strategy, or other exogenous input is added.
- Load and SOC remain two series in one custom run, one result, and the existing three NocoBase collections.
- Supported intervals remain exactly `30, 60, 300, 900, 1800, 3600` seconds; forecast horizons remain 1–7 complete days.
- Load uses a 7-day period. SOC retains its existing model path and last-real-state offset anchoring.
- A usable week contains exactly `7 × points_per_day` aligned slots, no unresolved values, and at most 5% imputed points.
- 30-second and 60-second runs never construct AutoARIMA or MSTL load candidates.
- Load selection ranks WAPE, then MAE, then the fixed order `WeeklyNaive`, `WeeklyWeighted2`, `WeeklyMedian3`, `AutoARIMA`, `MSTL`.
- Load baseline is `WeeklyNaive`; old runs without `selection_policy = "weekly_load_v1"` do not enter new-policy performance aggregates.
- Existing task submission, polling, result, and performance URL/query shapes remain unchanged.
- No database collection or field migration is introduced.
- Use `.venv/bin/python -m unittest`, not `pytest`; the repository virtual environment does not install pytest.
- Commit locally after every task; do not push a Git branch or container image unless the user separately requests it.

---

### Task 1: Record aligned usable-week evidence

**Files:**
- Modify: `m3/worker/domain/custom_training_data.py:17-132`
- Create: `m3/tests/test_m3_custom_training_data.py`

**Interfaces:**
- Produces: `CustomWeekSummary(start, end, point_count, real_point_count, imputed_point_count, imputation_ratio)`.
- Produces: `CustomTrainingDataset.usable_weeks: tuple[CustomWeekSummary, ...] = ()`, appended with a default for compatibility with existing direct constructors; values are ordered oldest to newest and contain only the consecutive usable suffix ending at `config.history_end`.
- Preserves: existing `frame`, `imputed_keys`, `start`, `end`, `mode`, `interval_seconds`, and `points_per_day` fields for SOC compatibility.

- [ ] **Step 1: Write failing usable-week tests**

Create tests that build 15-minute `CustomObservationPoint` fixtures and assert exact week evidence:

```python
def test_latest_aligned_week_accepts_largest_integer_below_five_percent():
    dataset = build_dataset_with_week_imputation(imputed_points=33)
    self.assertEqual(len(dataset.usable_weeks), 1)
    week = dataset.usable_weeks[0]
    self.assertEqual(week.point_count, 672)
    self.assertEqual(week.imputed_point_count, 33)
    self.assertAlmostEqual(week.imputation_ratio, 33 / 672)

def test_week_above_five_percent_stops_older_week_discovery():
    dataset = build_dataset_with_two_weeks(latest_imputed_points=34)
    self.assertEqual(dataset.usable_weeks, ())

def test_partial_old_prefix_does_not_remove_three_complete_recent_weeks():
    dataset = build_dataset_starting_at("2026-08-06T08:00:00+08:00")
    self.assertEqual(len(dataset.usable_weeks), 3)
    self.assertEqual(
        dataset.usable_weeks[-1].end.isoformat(),
        "2026-08-31T00:00:00+08:00",
    )
```

For 15-minute data, 5% of 672 slots is 33.6. Therefore 33 imputed points are accepted and 34 are rejected by the exact-ratio rule.

- [ ] **Step 2: Run the new tests and verify the missing interface fails**

Run:

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_custom_training_data
```

Expected: `AttributeError` or import failure for `CustomWeekSummary` / `usable_weeks`.

- [ ] **Step 3: Implement week summaries and consecutive-suffix discovery**

Add immutable evidence and compute it after the cleaned frame and retained imputation keys are known:

```python
MAX_WEEK_IMPUTATION_RATIO = 0.05

@dataclass(frozen=True)
class CustomWeekSummary:
    start: datetime
    end: datetime
    point_count: int
    real_point_count: int
    imputed_point_count: int
    imputation_ratio: float

def _usable_week_summaries(
    frame: pd.DataFrame,
    imputed_keys: frozenset[tuple[str, datetime]],
    unique_id: str,
    config: CustomForecastConfig,
) -> tuple[CustomWeekSummary, ...]:
    expected = config.points_per_day * 7
    summaries: list[CustomWeekSummary] = []
    week_end = config.history_end
    while week_end - timedelta(days=7) >= config.history_start:
        week_start = week_end - timedelta(days=7)
        week = frame[(frame["ds"] >= week_start) & (frame["ds"] < week_end)]
        if len(week) != expected or week["y"].isna().any():
            break
        imputed = sum(
            (unique_id, pd.Timestamp(value).to_pydatetime()) in imputed_keys
            for value in week["ds"]
        )
        if imputed / expected > MAX_WEEK_IMPUTATION_RATIO:
            break
        summaries.append(CustomWeekSummary(
            start=week_start,
            end=week_end,
            point_count=expected,
            real_point_count=expected - imputed,
            imputed_point_count=imputed,
            imputation_ratio=imputed / expected,
        ))
        week_end = week_start
    return tuple(reversed(summaries))
```

Populate `usable_weeks` without changing the existing SOC `mode` calculation.

- [ ] **Step 4: Run training-data regression tests**

Run:

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_training_data \
  m3.tests.test_m3_training_data
```

Expected: all tests pass.

- [ ] **Step 5: Commit usable-week evidence**

```bash
git add m3/worker/domain/custom_training_data.py m3/tests/test_m3_custom_training_data.py
git commit -m "feat(m3): record usable weekly training windows"
```

---

### Task 2: Implement lightweight weekly load profiles and metrics

**Files:**
- Create: `m3/worker/domain/custom_load_profiles.py`
- Create: `m3/tests/test_m3_custom_load_profiles.py`

**Interfaces:**
- Produces: `LOAD_SELECTION_POLICY = "weekly_load_v1"`.
- Produces: `LoadCandidateScore(model_name, wape_percent, mae, mape_percent, scorable_point_count, skip_reason)`.
- Produces: `eligible_load_models(usable_week_count: int, interval_seconds: int) -> tuple[str, ...]`.
- Produces: `weekly_profile_values(dataset, model_name: str, *, origin: datetime, periods: int) -> list[float]`.
- Produces: `load_candidate_score(model_name: str, actual: pd.DataFrame, predicted: list[float], excluded_times: frozenset[datetime]) -> LoadCandidateScore` with exact numeric outputs.

- [ ] **Step 1: Write failing weekly-profile tests with hand-derived values**

Cover each model and selection tier:

```python
def test_weekly_naive_uses_exactly_seven_day_lag():
    values = weekly_profile_values(dataset, "WeeklyNaive", origin=start, periods=3)
    self.assertEqual(values, [70.0, 71.0, 72.0])

def test_weekly_weighted_two_uses_two_thirds_recent_week():
    values = weekly_profile_values(dataset, "WeeklyWeighted2", origin=start, periods=1)
    self.assertAlmostEqual(values[0], 2 / 3 * 90.0 + 1 / 3 * 30.0)

def test_weekly_median_three_rejects_one_abnormal_week():
    values = weekly_profile_values(dataset, "WeeklyMedian3", origin=start, periods=1)
    self.assertEqual(values, [50.0])

def test_high_frequency_models_are_lightweight_only():
    self.assertEqual(
        eligible_load_models(4, 30),
        ("WeeklyNaive", "WeeklyWeighted2", "WeeklyMedian3"),
    )
```

Add exact eligibility assertions for 0, 1, 2, 3, and 4 usable weeks at both 60-second and 300-second intervals. Add literal WAPE/MAE/MAPE assertions, a WAPE-unavailable MAE tie-break assertion, a fixed-model-order tie assertion, and verify imputed target timestamps are excluded from every metric.

- [ ] **Step 2: Run the profile tests and verify imports fail**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_custom_load_profiles
```

Expected: module or symbol import failure.

- [ ] **Step 3: Implement deterministic weekly profiles**

Use exact timedelta lookups against the cleaned frame:

```python
MODEL_ORDER = (
    "WeeklyNaive",
    "WeeklyWeighted2",
    "WeeklyMedian3",
    "AutoARIMA",
    "MSTL",
)

def eligible_load_models(usable_week_count: int, interval_seconds: int) -> tuple[str, ...]:
    names = ["WeeklyNaive"] if usable_week_count >= 1 else []
    if usable_week_count >= 3:
        names.append("WeeklyWeighted2")
    if usable_week_count >= 4:
        names.append("WeeklyMedian3")
        if interval_seconds >= 300:
            names.extend(("AutoARIMA", "MSTL"))
    return tuple(names)
```

For final forecasts, read the latest one, two, or three matching weekly lags. For holdout forecasts, accept an `origin` before the dataset end so no value at or after the holdout start can be read.

- [ ] **Step 4: Implement metric scoring and deterministic comparison keys**

Calculate all metrics over the same finite, non-excluded actual points:

```python
wape = 100 * sum(errors) / sum(abs(value) for value in actuals) if any(actuals) else None
mae = float(np.mean(errors)) if errors else None
mape = float(np.mean([
    100 * error / abs(actual)
    for actual, error in zip(actuals, errors)
    if actual != 0
])) if any(actual != 0 for actual in actuals) else None
```

Expose a comparison key that uses MAE only when WAPE is unavailable and always ends with `MODEL_ORDER.index(model_name)`.

- [ ] **Step 5: Run the focused tests and commit**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_custom_load_profiles
git add m3/worker/domain/custom_load_profiles.py m3/tests/test_m3_custom_load_profiles.py
git commit -m "feat(m3): add weekly load profile candidates"
```

---

### Task 3: Dispatch load selection separately from SOC selection

**Files:**
- Modify: `m3/worker/domain/custom_forecasting.py:22-249`
- Modify: `m3/tests/test_m3_custom_forecasting.py`

**Interfaces:**
- Extends: `CustomChampion` with immutable `candidate_scores: tuple[LoadCandidateScore, ...] = ()`, `selection_metric: str | None = None`, and `selection_status: str | None = None`.
- Produces: `select_load_champion(dataset, config) -> CustomChampion`.
- Produces: `weekly_naive_champion(dataset) -> CustomChampion` for the persisted load baseline.
- Preserves: `select_custom_champion(dataset, config) -> CustomChampion`, now dispatching by `dataset.frame["unique_id"].iloc[0]`.
- Preserves: `forecast_custom_series(...) -> CustomForecastSeries`; lightweight load winners use `weekly_profile_values`, StatsForecast winners use a weekly-aware engine, and SOC continues the existing path plus anchoring.

- [ ] **Step 1: Write failing load-dispatch and holdout tests**

Add tests proving:

```python
def test_one_or_two_usable_weeks_select_weekly_naive_without_cv():
    champion = select_custom_champion(load_dataset_with_weeks(2), config)
    self.assertEqual(champion.model_name, "WeeklyNaive")
    self.assertIsNone(champion.cv_mape_percent)
    self.assertEqual(champion.selection_reason, "fewer_than_three_usable_weeks")
    self.assertEqual(champion.selection_status, "warming_up")

def test_latest_week_unusable_raises_insufficient_history():
    with self.assertRaisesRegex(M3Error, "insufficient_history"):
        select_custom_champion(load_dataset_with_weeks(0), config)

def test_three_weeks_compare_weekly_naive_and_weighted_two_on_latest_holdout():
    champion = select_custom_champion(load_dataset_with_literal_week_values(), config)
    self.assertEqual(champion.model_name, "WeeklyWeighted2")
    self.assertEqual(champion.selection_metric, "wape_percent")

def test_soc_still_uses_existing_daily_candidate_pool():
    champion = select_custom_champion(soc_dataset, config)
    self.assertIn(champion.model_name, {"SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"})
```

Add a 30-second test that patches AutoARIMA/MSTL constructors and asserts they are never called, plus a 5-minute test that makes their holdout results participate.
Add a candidate-failure test proving one failed automatic model does not prevent another candidate winning. Add a final-forecast failure test proving a failed load winner falls back to `WeeklyNaive`, marks the output `degraded`, and records only the exception type. Add table-driven 1-day and 7-day tests asserting exact point counts, interval continuity, and `forecast_start <= target_time < forecast_end`.

- [ ] **Step 2: Run the dispatch tests and verify they fail on daily behavior**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_custom_forecasting
```

Expected: load model remains `SeasonalNaive` or weekly symbols are missing.

- [ ] **Step 3: Add a load-specific selection branch**

Use the latest complete usable week `[latest_week.start, latest_week.end)` as the common holdout and fit every candidate only on rows with `ds < latest_week.start`. Lightweight candidates call `weekly_profile_values` with `origin=latest_week.start`; AutoARIMA uses `season_length=config.weekly_season_length`; MSTL keeps `[daily, weekly]`. Convert each outcome to `LoadCandidateScore`, isolate candidate exceptions by safe type name, and choose by the metric comparison key.

For zero usable weeks, raise `M3Error("insufficient_history", ...)`. For one or two usable weeks, return `WeeklyNaive` with `selection_status="warming_up"` and no score. The SOC branch must retain the current four candidates and current CV behavior. Do not apply load WAPE ranking to SOC.

- [ ] **Step 4: Generate final lightweight load forecasts and preserve SOC anchoring**

In `_forecast_frame`, branch before StatsForecast construction:

```python
if is_load_series(unique_id) and champion.model_name.startswith("Weekly"):
    values = weekly_profile_values(
        dataset,
        champion.model_name,
        origin=config.forecast_start,
        periods=config.expected_points_per_series,
    )
    return load_frame(config, unique_id, champion.model_name, values), champion.model_name, None
```

Keep `soc_offset` guarded by `not is_load_series(unique_id)` so load output is never state-anchored.
When a selected load model fails during final forecasting, retry with `WeeklyNaive`, set `fallback_reason` to the safe exception type, and let `forecast_custom_series` emit `degraded`. If `WeeklyNaive` itself fails, raise `M3Error("weekly_naive_failed", ...)` without exposing the original exception message. Without a final fallback, use `champion.selection_status` before consulting the legacy dataset mode, so the one-to-two-week load path emits `warming_up`.

- [ ] **Step 5: Run domain regressions and commit**

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_training_data \
  m3.tests.test_m3_custom_load_profiles \
  m3.tests.test_m3_custom_forecasting \
  m3.tests.test_m3_forecasting
git add m3/worker/domain/custom_forecasting.py m3/tests/test_m3_custom_forecasting.py
git commit -m "feat(m3): select load forecasts by weekly backtest"
```

---

### Task 4: Persist weekly evidence and use a weekly load baseline

**Files:**
- Modify: `m3/worker/services/custom_forecast_service.py:177-278`
- Modify: `m3/worker/services/custom_forecast_repository.py:423-475`
- Create: `m3/tests/test_m3_custom_forecast_service.py`
- Modify: `m3/tests/test_m3_custom_forecast_repository.py`

**Interfaces:**
- Produces in `model_manifest`: top-level `selection_policy: "weekly_load_v1"` and per-series candidate scores/selection evidence.
- Produces in `source_manifest.series.<id>`: `usable_week_count` and `weeks` summaries.
- Changes load baseline series model name from `SeasonalNaive` to `WeeklyNaive`; SOC baseline remains `SeasonalNaive` with SOC anchoring.
- Preserves all NocoBase collection fields and public result response fields.

- [ ] **Step 1: Write failing service-manifest and baseline tests**

Use a fake source and in-memory repository boundary to execute one custom run and assert:

```python
self.assertEqual(run.model_manifest["selection_policy"], "weekly_load_v1")
self.assertEqual(
    run.model_manifest["series"]["station_total_load"]["model_name"],
    "WeeklyNaive",
)
self.assertEqual(
    run.source_manifest["series"]["station_total_load"]["usable_week_count"],
    1,
)
self.assertEqual(
    captured_baselines["station_total_load"].model_name,
    "WeeklyNaive",
)
```

Have the fake repository's `store_points` capture the baseline series by `unique_id`. Assert the model name on that captured object; do not add a `baseline_model_name` table field.

- [ ] **Step 2: Run service/repository tests and verify the daily-baseline assertion fails**

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_forecast_service \
  m3.tests.test_m3_custom_forecast_repository
```

Expected: load baseline is rejected unless named `SeasonalNaive`, and weekly manifest fields are absent.

- [ ] **Step 3: Build per-series baselines explicitly**

Replace the shared baseline comprehension with explicit series policy:

```python
baseline_series = [
    forecast_custom_series(
        datasets["station_total_load"],
        weekly_naive_champion(datasets["station_total_load"]),
        run.config,
    ),
    forecast_custom_series(
        datasets["storage_soc"],
        seasonal_naive_champion(datasets["storage_soc"]),
        run.config,
    ),
]
```

Update repository validation to require `WeeklyNaive` for `station_total_load` and `SeasonalNaive` for `storage_soc`. Continue storing only `baseline_forecast_value` in the existing point row.

- [ ] **Step 4: Serialize auditable model and source manifests**

Add safe serializers for `LoadCandidateScore` and `CustomWeekSummary`. Persist candidate metrics as numbers or null and skipped reasons as fixed safe codes/type names. Include `selection_policy` at the model-manifest top level so performance aggregation can distinguish old and new runs.

Use these stable per-series keys for load evidence: `model_name`, `selection_metric`, `selection_reason`, `selection_status`, `candidate_scores`, `training_start`, `training_end`, and `statsforecast_version`. Each candidate score contains `model_name`, `wape_percent`, `mae`, `mape_percent`, `scorable_point_count`, and `skip_reason`. Each source week contains `start`, `end`, `point_count`, `real_point_count`, `imputed_point_count`, and `imputation_ratio`.

- [ ] **Step 5: Run persistence tests and commit**

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_forecast_service \
  m3.tests.test_m3_custom_forecast_repository \
  m3.tests.test_m3_nocobase_sink
git add \
  m3/worker/services/custom_forecast_service.py \
  m3/worker/services/custom_forecast_repository.py \
  m3/tests/test_m3_custom_forecast_service.py \
  m3/tests/test_m3_custom_forecast_repository.py
git commit -m "feat(m3): persist weekly load selection evidence"
```

---

### Task 5: Keep old daily-policy evaluations out of weekly aggregates

**Files:**
- Modify: `m3/worker/services/custom_forecast_evaluation_service.py:290-365`
- Create: `m3/tests/test_m3_custom_forecast_evaluation.py`

**Interfaces:**
- Preserves: `CustomForecastRepository.list_comparable_evaluations(...)` and all persisted evaluation fields.
- Preserves: the public performance response and its existing URL/query parameters.
- Filters: only evaluations whose owning run has `model_manifest.selection_policy == "weekly_load_v1"`.

- [ ] **Step 1: Write a failing mixed-policy aggregation test**

Provide one old evaluated run without `selection_policy` and one new run with `weekly_load_v1`, both otherwise matching:

```python
performance = service.performance(
    station_id="ES01",
    interval_seconds=900,
    forecast_days=1,
    history_days=28,
    now=NOW,
)
self.assertEqual(performance["series"][0]["run_count"], 1)
self.assertEqual(performance["series"][0]["mape_percent"], 8.0)
```

- [ ] **Step 2: Run the evaluation test and verify both runs are currently mixed**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_custom_forecast_evaluation
```

Expected: `run_count` is 2 or the weighted MAPE includes the old run.

- [ ] **Step 3: Filter by owning run manifest without a schema migration**

Keep the repository query unchanged. In `CustomForecastEvaluationService.performance()`, create one run cache before iterating over evaluation keys, then filter each returned row through its owning run:

```python
run_cache: dict[str, StoredCustomRun | None] = {}

rows = self._repository.list_comparable_evaluations(
    station_id=station_id,
    evaluation_key=unique_id,
    interval_seconds=interval_seconds,
    forecast_days=forecast_days,
    model_policy=policy,
    calculated_since=since,
)

def uses_weekly_load_policy(row: dict[str, Any]) -> bool:
    run_id = row["run_id"]
    if run_id not in run_cache:
        run_cache[run_id] = self._repository.get_by_run_id(run_id)
    run = run_cache[run_id]
    return bool(
        run
        and run.model_manifest
        and run.model_manifest.get("selection_policy") == LOAD_SELECTION_POLICY
    )

rows = [row for row in rows if uses_weekly_load_policy(row)]
```

This resolves each distinct run at most once per performance request and requires no schema or repository-interface change.

- [ ] **Step 4: Run evaluation and API contract tests**

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_forecast_evaluation \
  m3.tests.test_m3_custom_forecast_repository
```

Expected: all focused evaluation and repository tests pass.

- [ ] **Step 5: Commit performance isolation**

```bash
git add \
  m3/worker/services/custom_forecast_evaluation_service.py \
  m3/tests/test_m3_custom_forecast_evaluation.py
git commit -m "fix(m3): isolate weekly forecast performance"
```

---

### Task 6: Render weekly model evidence and Shanghai timestamps

**Files:**
- Modify: `m3/node_red/m3_production_gateway_page.html`
- Modify: `m3/node_red/m3_production_gateway_flow.json`
- Create: `m3/node_red/sync_production_gateway_flow.py`
- Modify: `m3/tests/test_m3_deployment_artifacts.py:24-36,400-416`
- Modify: `m3/tests/m3_dashboard_e2e.js:1-20`

**Interfaces:**
- Treats: `m3/node_red/m3_production_gateway_page.html` as the canonical complete importable HTML.
- Leaves: `shared/场站未来能耗预测.html` unchanged and no longer treats it as the production source.
- Produces: Flow node `m3_prod_page_template.template` by injecting the existing auth globals into the canonical HTML.
- Consumes: optional `run.model_manifest.selection_policy`, per-series candidate scores, and optional weekly source summaries.
- Preserves: rendering of old runs without weekly fields.

- [ ] **Step 1: Write failing HTML/Flow contract tests**

Point `M3_HTML` and the browser E2E `HTML_PATH` at the canonical production page. Add assertions for visible configuration labels and strategy-safe old-run rendering:

```python
self.assertIn("负载周期：7 天", html)
self.assertIn("SOC 后处理：最后真实状态锚定", html)
self.assertIn("weekly_load_v1", html)
```

Add a test for a UTC result timestamp such as `2026-08-31T04:30:00Z` rendering as `2026/08/31 12:30` in Asia/Shanghai.

- [ ] **Step 2: Run frontend contracts and verify the new labels/time conversion fail**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_deployment_artifacts
node --test m3/tests/m3_dashboard_e2e.js
```

Expected: missing weekly labels/evidence and the old string-slicing timestamp behavior fail.

- [ ] **Step 3: Update configuration and result rendering**

Render candidate availability from interval and configured days before a run. After a run, prefer manifest evidence:

```javascript
const loadEvidence = run.model_manifest?.series?.station_total_load;
const usableWeeks = run.source_manifest?.series?.station_total_load?.usable_week_count;
text(root.querySelector("#result-model-meta"),
  loadEvidence && Number.isInteger(usableWeeks)
    ? `${usableWeeks} 个连续有效周 · 负载周期 7 天 · ${intervalLabel(run.interval_seconds)}粒度`
    : `${run.history_days} 天训练 · ${intervalLabel(run.interval_seconds)}粒度`);
```

Show the selected model's holdout WAPE, MAE, and MAPE only when the new manifest is present. Read `WeeklyNaive` from the same `candidate_scores` array as the load baseline and display its WAPE plus `(baseline WAPE - selected WAPE) / baseline WAPE × 100%` as relative improvement when the baseline WAPE is nonzero. Show safe skipped reasons; append “状态锚定” to the SOC model display without changing the API model name. For one-to-two-week warming-up runs, show the model and usable-week evidence while leaving unavailable score fields as `—`.

- [ ] **Step 4: Normalize every displayed instant to Asia/Shanghai**

Replace regex slicing with an explicit formatter:

```javascript
const SHANGHAI_DATE_TIME = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
function dateTime(value) {
  if (value === null) return "—";
  const parts = Object.fromEntries(
    SHANGHAI_DATE_TIME.formatToParts(new Date(value))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}/${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`;
}
```

Use the same time conversion in chart tooltips and axis labels.

- [ ] **Step 5: Add and run a deterministic Flow synchronization script**

The script reads the canonical HTML, injects the exact existing auth script before the unique marker, replaces only `m3_prod_page_template.template`, and supports `--check` without writing. Run it once in write mode:

```bash
.venv/bin/python m3/node_red/sync_production_gateway_flow.py
.venv/bin/python m3/node_red/sync_production_gateway_flow.py --check
```

- [ ] **Step 6: Run frontend regression tests and commit**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_deployment_artifacts
node --test m3/tests/m3_dashboard_e2e.js tests/test_m3_node_red_contract.js
git add \
  m3/node_red/m3_production_gateway_page.html \
  m3/node_red/m3_production_gateway_flow.json \
  m3/node_red/sync_production_gateway_flow.py \
  m3/tests/test_m3_deployment_artifacts.py \
  m3/tests/m3_dashboard_e2e.js
git commit -m "feat(m3): show weekly load forecast evidence"
```

---

### Task 7: Verify the complete weekly forecast path and deployment artifact

**Files:**
- Verify only: all files changed in Tasks 1–6

**Interfaces:**
- Verifies: the task request shape, existing tables, server-token gateway, prediction point persistence, and image entrypoints remain unchanged.

- [ ] **Step 1: Run focused Python behavior tests**

```bash
.venv/bin/python -m unittest -v \
  m3.tests.test_m3_custom_training_data \
  m3.tests.test_m3_custom_load_profiles \
  m3.tests.test_m3_custom_forecasting \
  m3.tests.test_m3_custom_forecast_service \
  m3.tests.test_m3_custom_forecast_repository \
  m3.tests.test_m3_custom_forecast_evaluation \
  m3.tests.test_m3_raw_energy_api \
  m3.tests.test_m3_nocobase_sink
```

Expected: all focused tests pass with zero failures and zero errors.

- [ ] **Step 2: Run frontend and deployment contracts**

```bash
.venv/bin/python -m unittest -v m3.tests.test_m3_deployment_artifacts
node --test m3/tests/m3_dashboard_e2e.js tests/test_m3_node_red_contract.js
.venv/bin/python m3/node_red/sync_production_gateway_flow.py --check
```

Expected: all commands exit 0 and the sync check reports no drift.

- [ ] **Step 3: Run compile and whitespace checks**

```bash
.venv/bin/python -m compileall -q m3/worker m3/tests
git diff --check
git status --short
```

Expected: compile and diff checks exit 0, and `git status --short` is empty because each implementation task was committed locally.

- [ ] **Step 4: Build the production image locally**

```bash
docker buildx build \
  --platform linux/amd64 \
  --file m3/deploy/Dockerfile \
  --tag vifa-m3:weekly-load-local \
  --load \
  .
docker image inspect vifa-m3:weekly-load-local --format '{{.Os}}/{{.Architecture}}'
```

Expected: build succeeds and inspect prints `linux/amd64`.

- [ ] **Step 5: Review final diff against the approved spec**

Check each spec section against code and tests: usable weeks, candidate tiers, high-frequency guard, WAPE ranking, weekly baseline, strategy-isolated performance, SOC anchoring, manifests, old-run compatibility, Shanghai time display, and unchanged database schema.
