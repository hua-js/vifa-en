# M3 Custom Forecast OOM Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent five-minute daily custom forecasts from exhausting host memory and prevent orphaned running forecasts from entering an infinite container-restart loop.

**Architecture:** Keep the deterministic weekly load models available at five-minute resolution, but admit StatsForecast automatic models only at intervals of fifteen minutes or coarser. During worker startup, fail orphaned `running` custom runs with a safe interruption code and resume only genuinely `queued` runs, allowing the bounded daily retry policy to submit a fresh run under the safe model policy.

**Tech Stack:** Python 3.12, StatsForecast, FastAPI worker services, unittest.

**Spec:** Approved bounded design in the 2026-09-04 production OOM incident task; no separate spec document.

## Global Constraints

- Five-minute forecasts must retain `WeeklyNaive`, `WeeklyWeighted2`, `WeeklyRegimeAdjusted`, and `WeeklyMedian3` when their existing usable-week requirements are met.
- `AutoARIMA` and `MSTL` remain eligible only when `interval_seconds >= 900` and existing usable-week requirements are met.
- Startup must transition orphaned `running` custom runs to `failed` with `error_code="worker_interrupted"` and must schedule only `queued` runs.
- Preserve existing daily retry limit and all unrelated user changes, including untracked `.agents/`.

---

### Task 1: Bound automatic load candidates

**Files:**
- Modify: `tests/test_m3_custom_load_profiles.py`
- Modify: `tests/test_m3_custom_forecasting.py`
- Modify: `m3_worker/domain/custom_load_profiles.py`

**Interfaces:**
- Consumes: `eligible_load_models(usable_week_count: int, interval_seconds: int) -> tuple[str, ...]`.
- Produces: the same interface with automatic candidates excluded below 900 seconds.

- [x] **Step 1: Write the failing boundary tests**

Change the five-minute expectation to the four weekly models, prove the five-minute selection path never constructs `StatsForecast`, and retain automatic-model behavior coverage at fifteen minutes:

```python
def test_eligibility_tiers_at_five_minutes(self):
    # usable_week_count=4 resolves to weekly candidates only

def test_automatic_models_start_at_fifteen_minutes(self):
    self.assertEqual(
        eligible_load_models(4, 900),
        (
            "WeeklyNaive",
            "WeeklyWeighted2",
            "WeeklyRegimeAdjusted",
            "WeeklyMedian3",
            "AutoARIMA",
            "MSTL",
        ),
    )
```

- [x] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_m3_custom_load_profiles.EligibilityTests
```

Expected: the five-minute case fails because current production code still returns `AutoARIMA` and `MSTL`.

- [x] **Step 3: Implement the minimum eligibility guard**

In `eligible_load_models`, change automatic model admission from `interval_seconds >= 300` to `interval_seconds >= 900` without altering weekly tiers.

- [x] **Step 4: Run the eligibility tests and verify GREEN**

Run the command from Step 2 and expect all eligibility tests to pass.

### Task 2: Fail orphaned running jobs during recovery

**Files:**
- Modify: `tests/test_m3_custom_forecast_service.py`
- Modify: `m3_worker/services/custom_forecast_service.py`

**Interfaces:**
- Consumes: `CustomForecastService.recover() -> int` and repository methods `list_recoverable()` and `transition(...)`.
- Produces: recovery behavior that fails every orphaned `running` run and schedules every `queued` run; the return value remains the number of recoverable records handled.

- [x] **Step 1: Add a recovery repository double and failing behavior test**

Create a repository double containing queued and running `StoredCustomRun` values. Assert that `recover()` schedules only queued runs, transitions running runs to `failed` with `worker_interrupted` at `NOW`, and completes every running transition before queued scheduling can raise a capacity error.

```python
scheduled = []
with patch.object(service, "_schedule", side_effect=scheduled.append):
    recovered = service.recover()

self.assertEqual(recovered, 2)
self.assertEqual([run.run_id for run in scheduled], ["queued-run"])
self.assertEqual(repository.transitions, [("running-run", "failed", NOW, "worker_interrupted")])
```

- [x] **Step 2: Run the recovery test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_m3_custom_forecast_service.CustomForecastServiceTests.test_recover_fails_orphaned_running_run_and_schedules_queued_run
```

Expected: failure because current recovery schedules both records and performs no transition.

- [x] **Step 3: Implement orphan handling**

Update `recover()` in two phases: first transition every `running` record to `failed` with `error_code="worker_interrupted"` using `self._now()`, then call `_schedule` only for queued records. Let repository and scheduling errors propagate so startup readiness continues to report recovery failure.

- [x] **Step 4: Run the recovery test and verify GREEN**

Run the command from Step 2 and expect the recovery behavior test to pass.

### Task 3: Regression verification and commit

**Files:**
- Verify: `tests/test_m3_custom_load_profiles.py`
- Verify: `tests/test_m3_custom_forecast_service.py`
- Verify: `tests/test_m3_daily_custom_forecast_service.py`
- Verify: full Python and Node-RED suites required by the M3 change surface.

**Interfaces:**
- Consumes: the model eligibility and recovery behavior from Tasks 1 and 2.
- Produces: a verified, deployable commit.

- [x] **Step 1: Run focused regression tests**

```bash
.venv/bin/python -m unittest tests.test_m3_custom_load_profiles tests.test_m3_custom_forecast_service tests.test_m3_daily_custom_forecast_service
```

- [x] **Step 2: Run the full repository verification appropriate to M3**

Run the full Python suite and the existing Node-RED/E2E commands documented by the repository. Record only commands actually executed and their results.

- [x] **Step 3: Review the diff for unrelated changes and secrets**

```bash
git diff --check
git status --short
git diff -- m3_worker/domain/custom_load_profiles.py m3_worker/services/custom_forecast_service.py tests/test_m3_custom_load_profiles.py tests/test_m3_custom_forecast_service.py
```

- [x] **Step 4: Commit the verified fix**

```bash
git add docs/superpowers/plans/2026-09-04-m3-custom-forecast-oom-guard.md m3_worker/domain/custom_load_profiles.py m3_worker/services/custom_forecast_service.py tests/test_m3_custom_load_profiles.py tests/test_m3_custom_forecasting.py tests/test_m3_custom_forecast_service.py
git commit -m "fix(m3): bound custom forecast recovery"
```
