# M4 Offline Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repository-local `m4_orchestrator` package that reads independent station JSON inputs, invokes the real M4 optimizer per station, and atomically writes one strict offline result containing all candidate plans while leaving AI and EMS inactive.

**Architecture:** A loader converts each input file into either a strict `OptimizationRequest` or a safe structured input error. A stateless orchestration service rejects duplicate station identities, isolates optimization failures by station, and returns one Pydantic result; a thin CLI serializes that result through an atomic writer. Two committed Mock inputs and one generated output snapshot prove the end-to-end contract without HTML, HTTP, AI, EMS, database, or device integration.

**Tech Stack:** Python 3.12, Pydantic 2.13.4, existing `m4_optimizer`, `argparse`, `json`, `tempfile`, `unittest`.

**Spec:** `m4/docs/superpowers/specs/2026-09-04-m4-offline-orchestration-design.md`

## Global Constraints

- Work on branch `feat/m4-offline-orchestration`; do not merge or push without user direction.
- The package is importable repository code, not a new deployed service.
- Do not modify either M4 HTML file in this plan.
- Do not call or simulate AI; every station result keeps `selection_status="pending_ai"` and `selected_candidate_id=null`.
- Do not call EMS; every station result keeps `dispatch_status="not_dispatched"` and `ems_task_id=null`.
- Do not add HTTP routes, database access, schedulers, network calls, credentials, or production configuration.
- Reuse `m4_optimizer.OptimizationRequest`, `M4Optimizer`, and `OptimizationResult`; do not duplicate or weaken their validation.
- Accept one or more inputs with unique station IDs; the committed Mock and acceptance tests cover two independent stations.
- Catch failures at the station orchestration boundary with `Exception`, never `BaseException`; public errors never contain exception text, JSON contents, absolute paths, or tracebacks.
- Use `.venv/bin/python` for tests because the workspace sandbox cannot access the user uv cache.
- Core rules use red-green TDD. Preserve unrelated user changes and keep commits scoped to one task.

## File Structure

### New package

- `m4_orchestrator/__init__.py`: stable public exports.
- `m4_orchestrator/__main__.py`: `python -m m4_orchestrator` entry point.
- `m4_orchestrator/contracts.py`: strict orchestration input, status, error, summary, station, and top-level result models.
- `m4_orchestrator/loader.py`: isolated UTF-8/JSON/Pydantic loading and safe hint extraction.
- `m4_orchestrator/service.py`: duplicate rejection, per-station optimization, availability classification, ordering, and overall status.
- `m4_orchestrator/writer.py`: deterministic JSON bytes and same-directory atomic replacement.
- `m4_orchestrator/cli.py`: argument parsing, dependency composition, output, and exit codes.
- `m4_orchestrator/README.md`: local usage, contract, failure semantics, and extension boundary.

### Mock artifacts

- `m4/mock/orchestration/station-1.json`: demand-focused smaller-station input.
- `m4/mock/orchestration/station-2.json`: PV-focused larger-station input.
- `m4/mock/orchestration/orchestration-result.json`: generated two-station output example.
- `m4/M4离线编排层阶段B1验收说明.md`: acceptance evidence and non-production boundary.

### Tests

- `tests/m4_orchestrator_test_support.py`: builders for loaded inputs, optimizer results, clocks, IDs, and fakes.
- `tests/test_m4_orchestrator_contracts.py`: strict model invariants.
- `tests/test_m4_orchestrator_loader.py`: file and validation error classification.
- `tests/test_m4_orchestrator_service.py`: station isolation, ordering, statuses, determinism, and duplicate rejection.
- `tests/test_m4_orchestrator_cli.py`: writer atomicity, CLI output, exit codes, and committed artifact acceptance.

---

### Task 1: Define strict orchestration contracts

**Files:**
- Create: `m4_orchestrator/__init__.py`
- Create: `m4_orchestrator/contracts.py`
- Create: `tests/m4_orchestrator_test_support.py`
- Create: `tests/test_m4_orchestrator_contracts.py`

**Interfaces:**
- Consumes: `m4_optimizer.OptimizationRequest` and `m4_optimizer.OptimizationResult`.
- Produces: `OrchestrationError`, `StationInput`, `InputSummary`, `StationOrchestrationResult`, `M4OrchestrationResult`, `OrchestrationStatus`, `StationStatus`, and `ErrorCode`.

- [ ] **Step 1: Write contract tests that fail before the package exists**

Create tests covering exact status literals, the mutually exclusive `StationInput.request/error` states, success/failure field alignment, fixed AI/EMS empty states, timezone-aware top-level timestamps, and strict rejection of extra fields.

```python
class M4OrchestratorContractTests(unittest.TestCase):
    def test_station_input_requires_exactly_one_request_or_error(self):
        request = make_request()
        with self.assertRaises(ValidationError):
            StationInput(input_ref="station-1.json")
        with self.assertRaises(ValidationError):
            StationInput(
                input_ref="station-1.json",
                request=request,
                error=OrchestrationError(
                    code="INPUT_JSON_ERROR",
                    message="input is not valid JSON",
                ),
            )

    def test_station_result_fixes_ai_and_ems_to_inactive_states(self):
        result = make_station_result(status="optimized")
        self.assertEqual(result.selection_status, "pending_ai")
        self.assertIsNone(result.selected_candidate_id)
        self.assertEqual(result.dispatch_status, "not_dispatched")
        self.assertIsNone(result.ems_task_id)
        with self.assertRaises(ValidationError):
            StationOrchestrationResult.model_validate(
                {**result.model_dump(), "selected_candidate_id": "cost"}
            )

    def test_failed_station_requires_safe_error_and_no_input_summary(self):
        with self.assertRaises(ValidationError):
            StationOrchestrationResult(
                input_ref="station-2.json",
                station_id="station-2",
                request_id="request-2",
                status="input_error",
                input_summary=make_input_summary(),
                optimization_result=None,
                error=None,
            )
```

The test support module must provide fixed `UTC` clocks and a minimal valid `OptimizationResult` with an empty candidate list for structural contract tests. It must import `make_request()` from `tests/m4_optimizer_test_support.py` instead of copying the optimizer request builder.

- [ ] **Step 2: Run the new contract tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests/test_m4_orchestrator_contracts.py -v
```

Expected: import failure for missing `m4_orchestrator`.

- [ ] **Step 3: Implement the strict models**

Use `ConfigDict(extra="forbid", strict=True)` for every public model. Define the exact literals:

```python
OrchestrationStatus = Literal["completed", "partial_failure", "failed"]
StationStatus = Literal[
    "optimized",
    "no_usable_candidate",
    "input_error",
    "optimization_error",
]
ErrorCode = Literal[
    "INPUT_NOT_FOUND",
    "INPUT_READ_ERROR",
    "INPUT_ENCODING_ERROR",
    "INPUT_JSON_ERROR",
    "INPUT_VALIDATION_ERROR",
    "DUPLICATE_STATION_ID",
    "OPTIMIZATION_ERROR",
    "NO_USABLE_CANDIDATE",
]
```

Define `StationInput` as the loader-to-service boundary:

```python
class StationInput(StrictModel):
    input_ref: str = Field(min_length=1)
    station_id_hint: str | None = None
    request_id_hint: str | None = None
    request: OptimizationRequest | None = None
    error: OrchestrationError | None = None

    @model_validator(mode="after")
    def validate_payload_state(self) -> "StationInput":
        if (self.request is None) == (self.error is None):
            raise ValueError("station input requires exactly one request or error")
        if any(character in self.input_ref for character in ("/", "\\", "\r", "\n")):
            raise ValueError("input_ref must be a safe label, not a path")
        return self
```

`InputSummary` contains only these fields: `plan_start_at`, `input_observed_at`, `source_versions`, `horizon_points`, `initial_soc_pct`, `energy_capacity_kwh`, `max_charge_kw`, `max_discharge_kw`, `demand_limit_kw`, `grid_import_limit_kw`, `grid_export_enabled`, and `grid_export_limit_kw`.

`StationOrchestrationResult` must use defaults whose types prohibit fabricated values:

```python
selection_status: Literal["pending_ai"] = "pending_ai"
selected_candidate_id: None = None
dispatch_status: Literal["not_dispatched"] = "not_dispatched"
ems_task_id: None = None
```

Its validator enforces:

- `optimized`: summary and optimizer result are present, error is absent;
- `no_usable_candidate`: summary, optimizer result, and `NO_USABLE_CANDIDATE` error are present;
- `input_error`: optimizer result and summary are absent, error is present;
- `optimization_error`: summary is present, optimizer result is absent, and `OPTIMIZATION_ERROR` is present.

`M4OrchestrationResult` rejects naive `started_at` or `finished_at`, rejects `finished_at < started_at`, requires nonempty `schema_version`, `run_id`, `orchestrator_version`, and `model_version`, and requires at least one station result. Its validator recomputes the status from station results: all `optimized` with no top-level errors is `completed`, a mixture containing at least one `optimized` with no top-level errors is `partial_failure`, and no `optimized` station is `failed`. Top-level errors are allowed only with `failed`. Duplicate identities remain representable in a failed result because Task 3 must report every rejected input.

- [ ] **Step 4: Export only the supported public API**

`m4_orchestrator/__init__.py` must export all public contract types. Do not export loader or CLI internals in Task 1.

- [ ] **Step 5: Run contract tests and existing optimizer contract tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests/test_m4_orchestrator_contracts.py \
  tests/test_m4_optimizer_contracts.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add m4_orchestrator/__init__.py m4_orchestrator/contracts.py tests/m4_orchestrator_test_support.py tests/test_m4_orchestrator_contracts.py
git commit -m "feat(m4): define offline orchestration contracts"
```

---

### Task 2: Load independent station JSON safely

**Files:**
- Create: `m4_orchestrator/loader.py`
- Create: `tests/test_m4_orchestrator_loader.py`
- Modify: `m4_orchestrator/__init__.py`

**Interfaces:**
- Consumes: `StationInput`, `OrchestrationError`, and `OptimizationRequest`.
- Produces: `load_station_input(path: Path, *, input_ref: str | None = None) -> StationInput`.

- [ ] **Step 1: Write loader tests for every specified branch**

Use `TemporaryDirectory` and actual UTF-8 files. Cover:

```python
def test_valid_json_returns_a_strict_request(self):
    path = self.write_json(make_request().model_dump(mode="json"))
    loaded = load_station_input(path)
    self.assertIsNotNone(loaded.request)
    self.assertIsNone(loaded.error)
    self.assertEqual(loaded.request.station_id, "station-test-001")

def test_json_and_validation_errors_are_distinct_and_safe(self):
    malformed = self.write_text("{not-json")
    invalid = self.write_json({"station_id": "station-visible", "request_id": "r"})
    malformed_result = load_station_input(malformed)
    invalid_result = load_station_input(invalid)
    self.assertEqual(malformed_result.error.code, "INPUT_JSON_ERROR")
    self.assertEqual(invalid_result.error.code, "INPUT_VALIDATION_ERROR")
    self.assertEqual(invalid_result.station_id_hint, "station-visible")
    for result in (malformed_result, invalid_result):
        rendered = result.model_dump_json()
        self.assertNotIn(str(self.temp_dir), rendered)
        self.assertNotIn("not-json", rendered)
```

Also test `INPUT_NOT_FOUND`, `INPUT_READ_ERROR` by patching `Path.read_text` to raise `PermissionError`, `INPUT_ENCODING_ERROR` with invalid UTF-8 bytes, default `input_ref == path.name`, caller-provided safe `input_ref`, contract rejection of path separators/newlines in `input_ref`, and hint extraction rejecting blank/non-string values.

- [ ] **Step 2: Run loader tests and verify RED**

```bash
.venv/bin/python -m unittest tests/test_m4_orchestrator_loader.py -v
```

Expected: import failure for missing `load_station_input`.

- [ ] **Step 3: Implement error-specific loading**

Implement the sequence exactly:

1. derive `safe_ref = input_ref or path.name` and reject blank `safe_ref`;
2. `Path.read_text(encoding="utf-8")`;
3. `json.loads(text)`;
4. safely extract exact nonblank string hints only from a top-level `dict`;
5. `OptimizationRequest.model_validate(payload)`;
6. return a success or error `StationInput`.

Catch in this order:

```python
except FileNotFoundError:
    return failed("INPUT_NOT_FOUND", "input file was not found")
except UnicodeDecodeError:
    return failed("INPUT_ENCODING_ERROR", "input file is not valid UTF-8")
except json.JSONDecodeError:
    return failed("INPUT_JSON_ERROR", "input file is not valid JSON")
except ValidationError:
    return failed("INPUT_VALIDATION_ERROR", "input does not match OptimizationRequest")
except OSError:
    return failed("INPUT_READ_ERROR", "input file could not be read")
```

Never include `str(exception)` in public data.

- [ ] **Step 4: Export the loader function and run tests**

```bash
.venv/bin/python -m unittest \
  tests/test_m4_orchestrator_loader.py \
  tests/test_m4_orchestrator_contracts.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add m4_orchestrator/__init__.py m4_orchestrator/loader.py tests/test_m4_orchestrator_loader.py
git commit -m "feat(m4): load station optimizer inputs"
```

---

### Task 3: Orchestrate stations with failure isolation

**Files:**
- Create: `m4_orchestrator/service.py`
- Create: `tests/test_m4_orchestrator_service.py`
- Modify: `m4_orchestrator/__init__.py`
- Modify: `tests/m4_orchestrator_test_support.py`

**Interfaces:**
- Consumes: `Sequence[StationInput]`, `M4Optimizer.optimize(request)`, and Task 1 result models.
- Produces: `M4Orchestrator.run(station_inputs: Sequence[StationInput]) -> M4OrchestrationResult`.

- [ ] **Step 1: Add deterministic optimizer fakes and service tests**

The test support fake records station IDs and returns results keyed by station ID. It can raise a configured `RuntimeError` or return a result whose candidate statuses are all failures.

Cover these public behaviors:

```python
def test_two_stations_are_optimized_independently_and_sorted(self):
    result = self.orchestrator.run([station_2, station_1])
    self.assertEqual(result.overall_status, "completed")
    self.assertEqual([item.station_id for item in result.stations], ["station-1", "station-2"])
    self.assertEqual(self.optimizer.calls, ["station-2", "station-1"])
    self.assertTrue(all(item.status == "optimized" for item in result.stations))

def test_input_and_optimizer_failures_do_not_block_a_healthy_station(self):
    result = orchestrator.run([bad_input, failing_station, healthy_station])
    self.assertEqual(result.overall_status, "partial_failure")
    self.assertEqual(self.optimizer.calls, ["station-failing", "station-healthy"])
    self.assertEqual(status_by_id["station-healthy"], "optimized")
    self.assertEqual(status_by_id["station-failing"], "optimization_error")

def test_duplicate_station_ids_reject_every_station_before_solving(self):
    result = orchestrator.run([first_duplicate, unique, second_duplicate])
    self.assertEqual(result.overall_status, "failed")
    self.assertEqual(self.optimizer.calls, [])
    self.assertEqual(result.errors[0].code, "DUPLICATE_STATION_ID")
    self.assertTrue(all(item.status == "input_error" for item in result.stations))
```

Also test empty inputs raise `ValueError`, all input errors produce `failed`, an optimizer result with only `infeasible/timeout/error` candidates becomes `no_usable_candidate`, mismatched result `station_id/request_id` becomes `optimization_error`, `KeyboardInterrupt` is not swallowed, selection/dispatch remain inactive, and public errors do not include raised exception text.

- [ ] **Step 2: Run service tests and verify RED**

```bash
.venv/bin/python -m unittest tests/test_m4_orchestrator_service.py -v
```

Expected: import failure for missing `M4Orchestrator`.

- [ ] **Step 3: Implement dependency-injected stateless service**

Use these constructor defaults and protocol:

```python
class Optimizer(Protocol):
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        raise NotImplementedError
```

`M4Orchestrator.__init__` has keyword-only parameters `model_version: str`, `orchestrator_version: str`, `optimizer: Optimizer | None = None`, `clock: Callable[[], datetime] | None = None`, and `run_id_factory: Callable[[], str] | None = None`. Its public method is exactly `run(self, station_inputs: Sequence[StationInput]) -> M4OrchestrationResult`.

Defaults are `M4Optimizer(model_version=model_version)`, an aware UTC clock, and `str(uuid4())`. Validate nonblank version strings in `__init__`.

Build `InputSummary` from the validated request only. For a successful optimizer call, verify returned `station_id` and `request_id` exactly match the request before classifying candidates.

Catch `Exception` only around one station's `optimizer.optimize()` and identity/classification processing. Do not catch around top-level contract construction, and do not catch `BaseException`.

The confirmed design is authoritative: partition station results before sorting,
with successful results first by `station_id` and error results by `input_ref`.
This replaces the earlier unified sort-key example:

```python
successful_results = sorted(
    (item for item in station_results if item.status == "optimized"),
    key=lambda item: (item.station_id or "", item.input_ref),
)
error_results = sorted(
    (item for item in station_results if item.status != "optimized"),
    key=lambda item: item.input_ref,
)
station_results = [*successful_results, *error_results]
```

For duplicate valid station IDs, do not call the optimizer for any input. Preserve already-invalid inputs with their original error; mark every otherwise-valid input `input_error` with `DUPLICATE_STATION_ID`, and also add one top-level duplicate error.

- [ ] **Step 4: Run service tests and focused optimizer regression**

```bash
.venv/bin/python -m unittest \
  tests/test_m4_orchestrator_service.py \
  tests/test_m4_optimizer_scenarios.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add m4_orchestrator/__init__.py m4_orchestrator/service.py tests/m4_orchestrator_test_support.py tests/test_m4_orchestrator_service.py
git commit -m "feat(m4): orchestrate stations independently"
```

---

### Task 4: Write results atomically and expose the CLI

**Files:**
- Create: `m4_orchestrator/writer.py`
- Create: `m4_orchestrator/cli.py`
- Create: `m4_orchestrator/__main__.py`
- Create: `tests/test_m4_orchestrator_cli.py`
- Modify: `m4_orchestrator/__init__.py`

**Interfaces:**
- Consumes: Task 2 `load_station_input`, Task 3 `M4Orchestrator.run`, and `M4OrchestrationResult`.
- Produces: `serialize_result(result) -> str`, `write_result_atomic(result, path) -> None`, `main(argv: Sequence[str] | None = None) -> int`, and module execution.

- [ ] **Step 1: Write writer and CLI tests**

Writer tests must prove deterministic UTF-8 JSON, trailing newline, parseability through `M4OrchestrationResult.model_validate_json`, and replacement safety:

```python
def test_replace_failure_preserves_existing_target_and_removes_temp_file(self):
    target.write_text("old-result\n", encoding="utf-8")
    with patch("m4_orchestrator.writer.os.replace", side_effect=OSError("blocked")):
        with self.assertRaises(OutputWriteError):
            write_result_atomic(make_orchestration_result(), target)
    self.assertEqual(target.read_text(encoding="utf-8"), "old-result\n")
    self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])
```

CLI tests patch only the optimizer dependency, not contracts or writer. Use actual temporary input/output files. Cover:

- two valid inputs write a valid result and return 0;
- one valid and one invalid input still writes `partial_failure` and returns 1;
- all invalid inputs write `failed` and return 1;
- duplicate station IDs call no optimizer and return 1;
- missing output directory returns 1, prints one safe stderr line, and creates no file;
- missing required arguments raise argparse `SystemExit(2)`;
- `python -m m4_orchestrator --help` is available through `__main__.py` without imports that perform I/O.

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
.venv/bin/python -m unittest tests/test_m4_orchestrator_cli.py -v
```

Expected: import failure for missing writer/CLI modules.

- [ ] **Step 3: Implement deterministic serialization and atomic replacement**

`serialize_result()` must be exactly equivalent to:

```python
payload = result.model_dump(mode="json")
return json.dumps(
    payload,
    ensure_ascii=False,
    indent=2,
    sort_keys=True,
    allow_nan=False,
) + "\n"
```

`write_result_atomic()` requires an existing parent directory. Use `NamedTemporaryFile` in the target directory, flush, `os.fsync`, close, then `os.replace`. On any exception after temporary file creation, unlink only that exact temporary path with `missing_ok=True` and raise `OutputWriteError("orchestration result could not be written")` without embedding the original exception. Never delete or truncate the existing target before `os.replace` succeeds.

- [ ] **Step 4: Implement CLI composition and exact exit codes**

Parser arguments:

```text
--input PATH              required, repeatable
--output PATH             required once
--model-version VALUE     required once
--orchestrator-version    optional, default m4-orchestrator-b1-v1
```

Generate safe stable input references as `input-{one_based_index}`; the loader never copies an absolute path into the result. Load every input before calling the service. Print one line to stdout after a successful write:

```text
wrote orchestration result: <overall_status>
```

Print only `failed to write orchestration result` to stderr for `OutputWriteError`. Return 0 only for `completed`; return 1 for `partial_failure`, `failed`, or output failure.

`__main__.py` contains only:

```python
from m4_orchestrator.cli import main

raise SystemExit(main())
```

- [ ] **Step 5: Run CLI, service, and loader tests**

```bash
.venv/bin/python -m unittest \
  tests/test_m4_orchestrator_loader.py \
  tests/test_m4_orchestrator_service.py \
  tests/test_m4_orchestrator_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add m4_orchestrator/__init__.py m4_orchestrator/__main__.py m4_orchestrator/writer.py m4_orchestrator/cli.py tests/test_m4_orchestrator_cli.py
git commit -m "feat(m4): add offline orchestration CLI"
```

---

### Task 5: Add two independent Mock stations and real end-to-end acceptance

**Files:**
- Create: `m4/mock/orchestration/station-1.json`
- Create: `m4/mock/orchestration/station-2.json`
- Create: `m4/mock/orchestration/orchestration-result.json`
- Modify: `tests/test_m4_orchestrator_service.py`
- Modify: `tests/test_m4_orchestrator_cli.py`

**Interfaces:**
- Consumes: the public CLI and real `M4Optimizer`.
- Produces: two standalone `OptimizationRequest` fixtures and one checked-in `M4OrchestrationResult` interface sample.

- [ ] **Step 1: Write acceptance tests before adding artifacts**

Tests must load the committed JSON through public contracts and assert:

```python
def test_committed_mock_inputs_are_independent_and_complete(self):
    first = load_station_input(MOCK_DIR / "station-1.json").request
    second = load_station_input(MOCK_DIR / "station-2.json").request
    self.assertEqual([first.station_id, second.station_id], ["station-1", "station-2"])
    self.assertNotEqual(first.request_id, second.request_id)
    self.assertNotEqual(first.capability.energy_capacity_kwh, second.capability.energy_capacity_kwh)
    self.assertNotEqual(first.constraints.demand_limit_kw, second.constraints.demand_limit_kw)
    self.assertNotEqual(first.source_versions, second.source_versions)
    self.assertEqual(len(first.points), 96)
    self.assertEqual(len(second.points), 96)

def test_real_two_station_run_returns_six_complete_candidates(self):
    result = M4Orchestrator(
        model_version="m4-stage-a-v1",
        orchestrator_version="m4-orchestrator-b1-v1",
        clock=fixed_clock,
        run_id_factory=lambda: "acceptance-run-001",
    ).run([load_station_input(STATION_2), load_station_input(STATION_1)])
    self.assertEqual(result.overall_status, "completed")
    self.assertEqual(len(result.stations), 2)
    for station in result.stations:
        self.assertEqual([c.profile_id for c in station.optimization_result.candidates], ["balanced", "cost", "pv"])
        for candidate in station.optimization_result.candidates:
            self.assertIn(candidate.status, {"optimal", "feasible"})
            self.assertEqual(len(candidate.plan), 96)
        self.assertEqual(station.selection_status, "pending_ai")
        self.assertIsNone(station.selected_candidate_id)
        self.assertEqual(station.dispatch_status, "not_dispatched")
```

Also parse the committed output sample with `M4OrchestrationResult.model_validate_json`, assert it has the same two station identities and six full candidates, assert every `selected_candidate_id` and `ems_task_id` is `None`, and assert the serialized result contains neither `http://` nor `https://` and has no `device_commands`, `access_token`, or `authorization` key.

- [ ] **Step 2: Run the artifact acceptance tests and verify RED**

```bash
.venv/bin/python -m unittest \
  tests.test_m4_orchestrator_service.M4OrchestratorRealAcceptanceTests \
  tests.test_m4_orchestrator_cli.M4OrchestratorArtifactTests -v
```

Expected: failures because the three Mock artifacts do not exist.

- [ ] **Step 3: Create station 1 JSON with fixed business data**

Use the exact stage A request schema and a fixed `2026-09-04T00:00:00+08:00` start. Station 1 has:

- `request_id="mock-station-1-20260904"`, `station_id="station-1"`;
- capacity 500 kWh, initial SOC 55%, maximum charge/discharge 120 kW;
- demand limit 260 kW and import limit 450 kW;
- no export; load is 220 kW except 340 kW at indexes 68–71;
- PV is 90 kW at indexes 40–55 and zero elsewhere;
- buy price is 0.32 yuan/kWh at indexes 0–27 and 92–95, 1.45 at indexes 68–71, and 0.78 elsewhere; sell price is zero;
- source and profile versions prefixed `mock-s1-`;
- 30-second development solver limit and 0.01 requested relative gap.

Generate all 96 explicit point objects before committing; do not add Python fallback data.

- [ ] **Step 4: Create station 2 JSON with visibly different fixed data**

Station 2 uses the same fixed horizon but has:

- `request_id="mock-station-2-20260904"`, `station_id="station-2"`;
- capacity 1,500 kWh, initial SOC 42%, maximum charge/discharge 300 kW;
- demand limit 520 kW and import limit 800 kW;
- export enabled with a 60 kW limit;
- load is 440 kW except 620 kW at indexes 68–71;
- PV is 900 kW at indexes 40–55 and zero elsewhere;
- buy price is 0.30 yuan/kWh at indexes 0–27 and 92–95, 1.50 at indexes 68–71, and 0.75 elsewhere; sell price is 0.15 throughout;
- source and profile versions prefixed `mock-s2-`;
- 30-second development solver limit and 0.01 requested relative gap.

Both stations use `input_observed_at=2026-09-03T23:55:00+08:00`, `max_input_age_seconds=1800`, 15-minute/96-point horizons, 95% charge/discharge efficiency, absolute SOC 10–90%, preferred SOC 20–80%, terminal tolerance 5%, and cycle cost 0.01 yuan/kWh. They use the same objective names and order from the approved stage A profiles, but every profile version is station-specific: `mock-s1-balanced-v1`, `mock-s1-cost-v1`, `mock-s1-pv-v1`, and the corresponding `mock-s2-*` values. Objective lock tolerances are absolute `1e-6` and relative `0.0`; the balanced combined layer weights `energy_cost=0.01` and `pv_unused=0.001`, while every single-term layer has weight `1.0`.

The two JSON files must not contain comments, NaN/Infinity, credentials, URLs, real EMS identifiers, or shared request/profile/source versions.

- [ ] **Step 5: Run the real CLI to generate the checked-in result**

```bash
.venv/bin/python -m m4_orchestrator \
  --input m4/mock/orchestration/station-1.json \
  --input m4/mock/orchestration/station-2.json \
  --output m4/mock/orchestration/orchestration-result.json \
  --model-version m4-stage-a-v1 \
  --orchestrator-version m4-orchestrator-b1-v1
```

Expected stdout: `wrote orchestration result: completed`; exit 0.

- [ ] **Step 6: Run real acceptance and determinism tests**

Add `assert_nested_close(testcase, left, right, path="root")` to the test support module. It recursively requires identical dictionary keys and list lengths, compares finite floats with `math.isclose(rel_tol=0.0, abs_tol=1e-6)`, and uses `testcase.assertEqual` for every other scalar. Execute the real orchestrator twice, call `model_dump(mode="json")`, recursively remove only top-level `run_id`, top-level `started_at/finished_at`, nested optimizer `started_at/finished_at`, and candidate `solve_seconds`, then compare the remaining structures with `assert_nested_close`. Do not round or mutate production results.

Run:

```bash
.venv/bin/python -m unittest \
  tests/test_m4_orchestrator_service.py \
  tests/test_m4_orchestrator_cli.py -v
```

Expected: all tests pass and the real two-station test completes within 30 seconds on the development machine.

- [ ] **Step 7: Commit Task 5**

```bash
git add m4/mock/orchestration tests/test_m4_orchestrator_service.py tests/test_m4_orchestrator_cli.py
git commit -m "test(m4): add offline two-station orchestration acceptance"
```

---

### Task 6: Document and verify the stage B1 boundary

**Files:**
- Create: `m4_orchestrator/README.md`
- Create: `m4/M4离线编排层阶段B1验收说明.md`
- Modify only if verification exposes a real defect: the smallest relevant `m4_orchestrator/*.py`, test, or Mock JSON file.

**Interfaces:**
- Consumes: the completed package, CLI, Mock inputs, output sample, design, and all tests.
- Produces: operator/developer usage documentation and final verification evidence.

- [ ] **Step 1: Write package usage and contract documentation**

The README must include:

- the exact CLI command from Task 5;
- Python usage importing `M4Orchestrator` and `load_station_input`;
- a compact output field table;
- `completed/partial_failure/failed` and all station statuses;
- exit codes 0, 1, and 2;
- AI/EMS fixed empty states;
- input safety, station isolation, duplicate rejection, atomic replacement, and deterministic-field rules;
- future FastAPI, AI, and EMS adapter boundaries;
- an explicit statement that the result is Mock/offline and not a production schedule or savings claim.

- [ ] **Step 2: Write the stage B1 acceptance note**

`m4/M4离线编排层阶段B1验收说明.md` must map each accepted requirement to a test or artifact, list both station fixture differences, link the input/output examples, record actual verification commands and counts, and state:

```text
本阶段没有 AI 选择、没有 EMS 下发、没有设备控制、没有真实节费结论。
```

Do not pre-fill test counts before running them.

- [ ] **Step 3: Run all M4 optimizer and orchestrator tests**

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_m4*.py' -v
```

Expected: zero failures and zero unexpected skips. Record the exact `Ran N tests in Xs` output.

- [ ] **Step 4: Run the entire Python test suite**

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: exit 0. Record total tests, passed count, and existing skipped count without double-counting skipped tests. Existing Starlette deprecation output and expected M3 error-path logs are not M4 failures.

- [ ] **Step 5: Record the real two-station performance baseline**

```bash
/usr/bin/time -p .venv/bin/python -B -m unittest \
  tests.test_m4_orchestrator_service.M4OrchestratorRealAcceptanceTests -v
```

Expected: exit 0. Record `real`, `user`, and `sys` as development evidence only, not a production SLA.

- [ ] **Step 6: Compile and inspect the complete diff**

```bash
.venv/bin/python -B -m compileall -q m4_orchestrator
git diff --check a9aebe2..HEAD
git status --short
```

Expected: compile and diff checks exit 0; status contains only the documentation changes being finalized.

- [ ] **Step 7: Update documents with actual evidence and commit**

```bash
git add m4_orchestrator/README.md m4/M4离线编排层阶段B1验收说明.md
git commit -m "docs(m4): document offline orchestration acceptance"
```

- [ ] **Step 8: Request final code review**

Review the complete range from design commit `a9aebe2` to the final implementation HEAD against the design and this plan. Critical and Important findings must be fixed and re-reviewed before completion. Minor findings must be recorded with an explicit disposition.

- [ ] **Step 9: Re-run final verification after review fixes**

Repeat Steps 3–6 against the final HEAD. Do not report completion from pre-review test output.

---

## Final Acceptance Checklist

- `m4_orchestrator` imports without file, network, database, AI, or EMS side effects.
- The CLI loads repeated independent JSON inputs and atomically writes one strict result.
- The committed result contains two stations, three candidates per station, and 96 points per usable candidate.
- One station failure does not block another; duplicate station IDs prevent every solve.
- AI and EMS fields remain explicitly inactive and cannot accept non-null values.
- Public errors are stable and do not leak sensitive or diagnostic internals.
- The output sample round-trips through `M4OrchestrationResult`.
- Existing `m4_optimizer` behavior remains unchanged.
- No M4 HTML, production configuration, database schema, API route, or device integration changed.
- Focused M4 tests, full project tests, compileall, diff check, performance probe, and final independent review pass on the final HEAD.
