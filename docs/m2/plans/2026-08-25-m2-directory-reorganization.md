# M2 Directory Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Move all M2-specific runtime, UI, tests, design documents, and SDD records under `m2/` without changing calculation behavior.

**Architecture:** `m2` becomes a Python package and owns its tests and HTML. Imports become root-runnable `m2.*` imports, while the browser test resolves the HTML relative to its own file. Mixed M1/M2 and M3 material remains outside the directory.

**Tech Stack:** Python 3 standard library, Node.js, Playwright, HTML/CSS/JavaScript.

**Spec:** `docs/m2/specs/2026-08-25-m2-directory-reorganization-design.md`

## Global Constraints

- Do not change any M2 calculation formula, public dictionary contract, event state-machine behavior, or dashboard UI behavior.
- Do not add network access, credentials, CLI behavior, or production mock data.
- Preserve archival SDD report contents even when their historical paths become stale.
- This workspace is not a Git repository; do not invent commit steps.

---

### Task 1: Establish the relocation baseline

**Files:**
- Read: `station_energy_backend.py`
- Read: `station_efficiency_history.py`
- Test: `tests/test_station_energy_backend.py`
- Test: `tests/test_station_efficiency_history.py`
- Test: `tests/energy_dashboard_e2e.js`

**Interfaces:**
- Consumes: current root-level M2 layout.
- Produces: verified baseline counts and browser result for comparison after relocation.

- [x] **Step 1: Run the Python baseline**

Run:

```sh
python3 -m unittest m2.tests.test_station_energy_backend m2.tests.test_station_efficiency_history -v
```

Expected: 76 tests pass.

- [x] **Step 2: Run syntax and browser baselines**

Run:

```sh
node --check tests/energy_dashboard_e2e.js
node tests/energy_dashboard_e2e.js
```

Expected: syntax exits 0 and browser prints `energy_dashboard_e2e_ok`.

### Task 2: Move the M2-owned files

**Files:**
- Create: `m2/__init__.py`
- Create: `m2/tests/__init__.py`
- Move: root M2 Python, HTML, Markdown, tests, plans/specs, and two SDD record directories listed in the spec.

**Interfaces:**
- Consumes: the exact ownership boundary in the spec.
- Produces: a self-contained `m2/` subtree.

- [x] **Step 1: Create package markers**

Create empty `m2/__init__.py` and `m2/tests/__init__.py` with short package docstrings.

- [x] **Step 2: Move runtime, UI, and tests**

Move the two Python modules and three root documents into `m2/`. Move all five M2 test/support files into `m2/tests/`.

- [x] **Step 3: Move plans, specs, and SDD records**

Move these existing files/directories beneath the matching `m2/` subtree:

```text
docs/m3/plans/2026-08-24-station-energy-backend.md
docs/m3/plans/2026-08-25-station-efficiency-history-bottlenecks.md
docs/m3/plans/2026-08-25-station-energy-source-records.md
docs/m3/specs/2026-08-24-station-energy-backend-design.md
docs/m3/specs/2026-08-25-station-energy-source-access-design.md
.superpowers/sdd/2026-08-25-station-efficiency-history-bottlenecks
.superpowers/sdd/2026-08-25-station-energy-source-records
```

- [x] **Step 4: Remove exact generated cache files**

Remove only the `station_energy_*` and corresponding test/support `.pyc` files already identified in root `__pycache__` directories, then remove directories only if empty.

### Task 3: Repair imports and live path references

**Files:**
- Modify: `m2/tests/test_station_energy_backend.py`
- Modify: `m2/tests/test_station_efficiency_history.py`
- Modify: `m2/tests/station_energy_test_support.py`
- Modify: `m2/tests/station_efficiency_history_test_support.py`
- Modify: `m2/tests/energy_dashboard_e2e.js`
- Modify: `m3/M3 场站未来能耗预测设计.md`
- Modify: `m3/M3 需求拷打.md`
- Modify: `docs/m3/plans/2026-08-25-m3-active-http-forecasting.md`

**Interfaces:**
- Consumes: `m2.station_energy_backend`, `m2.station_efficiency_history`, and `m2.tests.*`.
- Produces: root-runnable Python and Node test commands.

- [x] **Step 1: Update Python imports**

Replace production imports with `from m2.station_energy_backend ...` and `from m2.station_efficiency_history ...`; replace support imports with `from m2.tests...`.

- [x] **Step 2: Make E2E paths location-relative**

Resolve the HTML as `path.resolve(__dirname, "..", "web", "场站三条能效链路能流图.html")` and invoke the fixture through `m2.tests.station_efficiency_history_test_support`.

- [x] **Step 3: Update current M3 references**

Change live M3 references to the three moved M2 runtime/UI paths. Do not rewrite historical evidence within `m2/.superpowers/`.

- [x] **Step 4: Verify no live root imports remain**

Run:

```sh
rg -n 'from station_energy_|import station_energy_|tests\.station_energy_|tests\.station_efficiency_' m2/tests m3 docs/m3/plans/2026-08-25-m3-active-http-forecasting.md
```

Expected: no active old imports remain.

### Task 4: Verify the reorganized project

**Files:**
- Verify: `m2/`
- Verify: root directory ownership boundary.

**Interfaces:**
- Consumes: relocated package and path fixes.
- Produces: final regression evidence.

- [x] **Step 1: Run complete Python tests**

Run:

```sh
python3 -m unittest m2.tests.test_station_energy_backend m2.tests.test_station_efficiency_history -v
```

Expected: all 76 tests pass.

- [x] **Step 2: Compile moved Python modules**

Run:

```sh
PYTHONPYCACHEPREFIX=/private/tmp/vifa_m2_reorg_pycache python3 -m py_compile m2/station_energy_backend.py m2/station_efficiency_history.py m2/tests/test_station_energy_backend.py m2/tests/test_station_efficiency_history.py
```

Expected: exit 0.

- [x] **Step 3: Run browser regression**

Run:

```sh
node --check m2/tests/energy_dashboard_e2e.js
node m2/tests/energy_dashboard_e2e.js
```

Expected: syntax exits 0 and browser prints `energy_dashboard_e2e_ok`.

- [x] **Step 4: Check final ownership**

Run `rg --files` and confirm root no longer contains M2 runtime/UI/tests, while `m2/` contains all files listed by the spec.
