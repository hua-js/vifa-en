# M3 双电站独立预测与 Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 M3 从错误的单站三序列模型修正为两个电站各自独立的总负荷与 SOC 预测，并让同源 HTML 同时展示两个站的真实结果。

**Architecture:** 继续以 `station_id` 作为 Worker、缓存和持久化的隔离边界，每个站只拥有 `station_total_load`、`storage_soc` 两条序列。新增固定目标的原始能源 HTTP 客户端，按完整 `es_sn` 拉取和聚合；Dashboard API 只输出两个公开站点键及嵌套站点数据，浏览器轮询只读缓存或结果表，不触发模型。

**Tech Stack:** Python 3.12, StatsForecast 2.1.1, Pandas 2.3.3, Pydantic 2.13.4, HTTPX 0.28.1, FastAPI 0.141.1, Uvicorn 0.52.3, `unittest`, vanilla HTML/CSS/SVG, Node.js 22, Playwright.

**Spec:** `docs/m3/specs/2026-08-26-m3-two-station-forecast-dashboard-design.md`

## Global Constraints

- ES01、ES02 是两个独立电站；生产身份来自配置中的完整 `es_sn`。
- 每站只预测 `station_total_load` 与 `storage_soc`。
- `station_total_load` 只使用该站 `load_power`；不得加 `solar_power`，不得跨站求和。
- 四条序列独立聚合、训练、选模、预测、回退和持久化。
- 15 分钟桶、`h=96`、`freq="15min"`、Asia/Shanghai 和既有 StatsForecast 候选集保持不变。
- 浏览器仅调用同源 `GET /energy-forecast-api`；GET 不运行模型。
- 完整 `es_sn`、Token、源 URL 和结果表凭据不得进入浏览器、公开响应或日志。
- 最新结果和七日验收均按完整 `station_id` 隔离，通过 HTTP 持久化。
- 不修改 M1、M2，不新增 Node-RED/NocoBase 工程配置。
- 新 HTML 与新 Dashboard 契约必须同时部署；不兼容旧三序列响应。
- 工作区不是 Git 仓库；每个任务以测试和文件检查点结束，不执行 commit。
- 执行 Task 1 前先运行下列基线命令；Task 7 必须用同一清单做逐文件校验，证明 M1、M2 没有变化：

```bash
rg --files m1 m2 m2 m3/node_red \
  | LC_ALL=C sort \
  | xargs -I{} shasum -a 256 "{}" \
  > /tmp/m3_protected_before.sha256
```

## File Structure

- Modify `m3/worker/contracts.py`: 每站两序列的领域合同。
- Modify `m3/worker/domain/forecasting.py`: 负荷/SOC 单位与裁剪判断。
- Modify `m3/worker/domain/training_data.py`: 继续按通用 `SeriesId` 生成训练集。
- Modify `m3/worker/services/station_cache.py`: 每站两序列缓存。
- Modify `m3/worker/services/forecast_service.py`: 两序列选模、快照和发布。
- Modify `m3/worker/services/acceptance_service.py`: 每站两序列验收。
- Modify `m3/worker/api/routes.py` and `m3/worker/api/models.py`: 两序列运维状态响应。
- Modify `m3/worker/config.py`: 精确双站身份与公开名称配置。
- Create `m3/worker/clients/raw_energy_api.py`: 固定目标的原始数据分页客户端与 15 分钟聚合。
- Modify `m3/worker/main.py`: 将原始数据客户端接入既有多站 ForecastService。
- Modify `m3/worker/sinks/forecast_sink.py`: 每站 192 个验收预测点及两序列验证。
- Modify `m3/contracts/nocobase_collections.json`: 两序列枚举和验收键。
- Create `m3/worker/dashboard_contracts.py`: 双站 Dashboard 的严格 Pydantic DTO 与公开构建器。
- Create `m3/worker/services/live_dashboard_service.py`: 单站独立刷新、缓存和总体状态汇总。
- Create `m3/worker/api/live_dashboard.py`: 同源 HTML 与只读 Dashboard 路由。
- Modify `m3/worker/live_dashboard_app.py`: 联调 Uvicorn 入口与资源生命周期。
- Remove `m3/worker/live_dashboard.py` after all imports migrate: 删除错误的跨站聚合实现。
- Modify `场站未来能耗预测.html`: 双站四卡四图与 60 秒只读轮询。
- Modify focused `tests/test_m3_*.py` fixtures from three series to two per station.
- Create `m3/tests/test_m3_raw_energy_api.py`: 源 HTTP、身份和聚合隔离测试。
- Replace `m3/tests/test_m3_live_dashboard.py`: 双站服务、缓存、接口和模型联调测试。
- Modify `m3/tests/m3_dashboard_e2e.js`: 双站桌面/窄屏 E2E。
- Modify `m3/M3 场站未来能耗预测设计.md` and `docs/m3/部署说明.md`: 标注双站新合同和联调启动方式，不增加外部工程工件。

---

### Task 1: Migrate the Domain Contract to Two Series per Station

**Files:**
- Modify: `m3/worker/contracts.py`
- Modify: `m3/worker/domain/forecasting.py`
- Modify: `m3/worker/domain/training_data.py`
- Modify: `m3/tests/m3_test_support.py`
- Modify: `m3/tests/test_m3_contracts.py`
- Modify: `m3/tests/test_m3_training_data.py`
- Modify: `m3/tests/test_m3_forecasting.py`

**Interfaces:**
- Consumes: existing `ObservationPoint`, `ForecastPoint`, `ForecastSeries`, `LatestSnapshot` shapes.
- Produces: `SeriesId = Literal["station_total_load", "storage_soc"]`, `SERIES_IDS = ("station_total_load", "storage_soc")`, and a safe single-series forecast boundary for every later task.

- [ ] **Step 1: Write failing contract tests for the new exact series set**

Add literal assertions to `m3/tests/test_m3_contracts.py`:

```python
SERIES = (
    ("station_total_load", "kW", 800.0),
    ("storage_soc", "%", 55.0),
)


def test_latest_snapshot_requires_exactly_load_and_soc_once(self):
    values = latest_snapshot_values(series=[forecast_series(*item) for item in SERIES])
    snapshot = LatestSnapshot(**values)
    self.assertEqual(
        [item.unique_id for item in snapshot.series],
        ["station_total_load", "storage_soc"],
    )
    for invalid in (
        [forecast_series(*SERIES[0])],
        [forecast_series(*SERIES[0]), forecast_series(*SERIES[0])],
        [forecast_series(*SERIES[0]), forecast_series("storage_1_soc", "%", 55.0)],
    ):
        with self.subTest(invalid=invalid), self.assertRaises(ValueError):
            LatestSnapshot(**{**values, "series": invalid})


def test_storage_soc_uses_percent_bounds(self):
    with self.assertRaises(ValueError):
        ObservationPoint(
            unique_id="storage_soc",
            ds="2026-08-26T09:45:00+08:00",
            y=100.1,
            quality="valid",
            source_revision=1,
        )
```

Update `m3/tests/test_m3_forecasting.py` with the exact clipping behavior:

```python
def test_two_series_physical_clipping(self):
    self.assertEqual(clip_value("station_total_load", -5), (-5.0, 0.0, True))
    self.assertEqual(clip_value("storage_soc", 103), (103.0, 100.0, True))
```

Also patch `forecast_frame()` to fail after its SeasonalNaive fallback and assert `forecast_one_safe()` returns only that series as `status="error"`, with zero points and a safe `fallback_reason="forecast_failed"`; it must not raise or include the exception text.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_contracts \
  m3.tests.test_m3_training_data \
  m3.tests.test_m3_forecasting -v
```

Expected: failures show `storage_soc` is rejected and `LatestSnapshot` still requires three terminal series.

- [ ] **Step 3: Implement the exact two-series domain**

Use one canonical tuple in `m3/worker/contracts.py`:

```python
SeriesId = Literal["station_total_load", "storage_soc"]
SERIES_IDS: tuple[SeriesId, ...] = ("station_total_load", "storage_soc")


def is_load_series(unique_id: SeriesId) -> bool:
    return unique_id == "station_total_load"
```

Add the safe boundary in `m3/worker/domain/forecasting.py`, where `TrainingDataset` and `Champion` are already defined/imported:

```python
def forecast_one_safe(
    dataset: TrainingDataset,
    champion: Champion | None,
    as_of: datetime,
) -> ForecastSeries:
    try:
        return forecast_one(dataset, champion, as_of)
    except Exception:
        unique_id = dataset.frame["unique_id"].iloc[0]
        return ForecastSeries(
            unique_id=unique_id,
            unit="kW" if is_load_series(unique_id) else "%",
            model_name=champion.model_name if champion is not None else "none",
            status="error",
            points=[],
            fallback_reason="forecast_failed",
        )
```

Change `ObservationPoint`, `ForecastSeries`, and `LatestSnapshot` validation to use `SERIES_IDS`. `LatestSnapshot` must require length two and exact order after the snapshot builder canonicalizes it. `ForecastSeries` must require a non-null safe `fallback_reason` for `degraded`, `insufficient_history`, and `error`, and forbid it for `ok`/`warming_up`. In `m3/worker/domain/forecasting.py`, use `is_load_series()` for unit selection and physical clipping. Remove every `storage_1_soc`/`storage_2_soc` fixture in the three focused test files and replace it with `storage_soc`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: all focused tests pass with zero failures.

- [ ] **Step 5: Run an old-name rejection scan**

Run:

```bash
rg -n "storage_1_soc|storage_2_soc" \
  m3/worker/contracts.py \
  m3/worker/domain/forecasting.py \
  m3/worker/domain/training_data.py
rg -n "storage_1_soc|storage_2_soc" \
  m3/tests/test_m3_contracts.py \
  m3/tests/test_m3_training_data.py \
  m3/tests/test_m3_forecasting.py
```

Expected: no production match; the test scan contains exactly the one intentional old-name rejection fixture in `test_latest_snapshot_requires_exactly_load_and_soc_once` and no positive fixture/expectation.

---

### Task 2: Add Exact Station Configuration and the Raw Energy HTTP Client

**Files:**
- Modify: `m3/worker/config.py`
- Create: `m3/worker/clients/raw_energy_api.py`
- Create: `m3/tests/test_m3_raw_energy_api.py`
- Modify: `m3/tests/test_m3_contracts.py`

**Interfaces:**
- Consumes: fixed NocoBase list envelope `{data, meta}`, full configured `es_sn`, and time windows in Asia/Shanghai.
- Produces: `StationBinding`, `Settings.stations`, `Settings.station_ids`, `RawEnergySourceClient.latest_timestamp(station_id) -> datetime`, and `RawEnergySourceClient.list_observations(station_id, start, end) -> list[ObservationPoint]`.

- [ ] **Step 1: Write failing tests for exact station identity and load mapping**

Create `m3/tests/test_m3_raw_energy_api.py` with a real HTTPX mock boundary:

```python
def test_station_query_uses_full_es_sn_and_load_power_only(self):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        rows = minute_rows(
            es_sn="plant-alpha-ES01",
            load_power="40",
            solar_power=999,
            emus_soc=61,
            start="2026-08-26T01:00:00.000Z",
            count=15,
        )
        return httpx.Response(
            200,
            json={
                "data": rows,
                "meta": {"hasNext": False, "page": 1, "pageSize": 1000},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = RawEnergySourceClient(
            "https://source.example/api/t_es_data:list",
            "source-token",
            http,
            allowed_station_ids=("plant-alpha-ES01", "plant-beta-ES02"),
        )
        points = client.list_observations(
            "plant-alpha-ES01",
            datetime.fromisoformat("2026-08-26T09:00:00+08:00"),
            datetime.fromisoformat("2026-08-26T09:15:00+08:00"),
        )

    self.assertEqual([point.unique_id for point in points], ["station_total_load", "storage_soc"])
    self.assertEqual(points[0].y, 40.0)
    self.assertEqual(points[1].y, 61.0)
    encoded_filter = json.loads(seen[0].url.params["filter"])
    self.assertEqual(encoded_filter["es_sn"], {"$eq": "plant-alpha-ES01"})
    self.assertNotIn("source-token", str(seen[0].url))
```

Add a cross-station negative test: return one ES02 row for an ES01 query and assert `M3Error.code == "source_contract_invalid"` before any point is returned.

Add table-driven aggregation cases for both stations: changing or adding `solar_power` never changes `station_total_load`; changing ES01 rows never changes the ES02 point list; 11 samples, a missing head/tail, negative load, and SOC outside 0..100 each produce only the affected station/series as an invalid observation (`quality="invalid"`, `y=None`). Also assert `latest_timestamp("plant-alpha-ES01")` sends the exact ES01 equality filter and never selects the newer ES02 row.

Add `m3/tests/test_m3_contracts.py` coverage for this exact environment value:

```python
M3_STATIONS_JSON = json.dumps([
    {"station_id": "plant-alpha-ES01", "station_key": "station_1", "station_name": "1# 电站"},
    {"station_id": "plant-beta-ES02", "station_key": "station_2", "station_name": "2# 电站"},
])
```

Reject missing stations, duplicate full IDs, duplicate public keys, reversed public-key order, unknown keys, whitespace/control-character display names, `station_1` IDs that do not end in `ES01`, and `station_2` IDs that do not end in `ES02`. The suffix validates a configured full identity; it must never be used to auto-discover or accept an unknown station.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_contracts \
  m3.tests.test_m3_raw_energy_api -v
```

Expected: import failure for `m3.worker.clients.raw_energy_api` and missing `M3_STATIONS_JSON` parsing.

- [ ] **Step 3: Implement exact station bindings**

Add to `m3/worker/config.py`:

```python
@dataclass(frozen=True)
class StationBinding:
    station_id: str
    station_key: Literal["station_1", "station_2"]
    station_name: str


@dataclass(frozen=True)
class Settings:
    stations: tuple[StationBinding, StationBinding]
    raw_source_url: HttpUrl
    raw_source_api_token: SecretStr
    source_base_url: HttpUrl
    source_api_token: SecretStr
    nocobase_base_url: HttpUrl
    nocobase_api_key: SecretStr
    admin_api_token: SecretStr
    timezone: str = "Asia/Shanghai"

    @property
    def station_ids(self) -> tuple[str, str]:
        return tuple(binding.station_id for binding in self.stations)
```

`Settings.from_env()` must require `M3_STATIONS_JSON`, `M3_RAW_SOURCE_URL`, and exactly one of `M3_RAW_SOURCE_API_TOKEN` or `M3_RAW_SOURCE_API_TOKEN_FILE`. For the file variant, read one non-empty bearer token from the exact configured file without logging its contents. Parse an exact two-item station JSON list, enforce public-key order `station_1`, `station_2`, and retain the existing control-plane/NocoBase settings and secret masking. Export a shared `parse_station_bindings(value: str)` helper so the local Dashboard entrypoint validates the same station mapping without requiring unrelated production result-table credentials.

- [ ] **Step 4: Implement the bounded raw source client**

`m3/worker/clients/raw_energy_api.py` must:

- Validate one fixed HTTP(S) URL with no credentials, query, or fragment.
- Require the exact two configured full IDs at construction and reject any other station ID before performing HTTP.
- Normalize one raw token or one `Bearer `-prefixed token into a single `Authorization` header; reject empty/control-character values and never put the token in URLs, exception strings, or object representations.
- Request `filter`, `page`, `pageSize`, and `sort=timestamp` with a 1,000-row page and 300-page cap. Every history filter must contain both exact `es_sn` equality and `[start, end)` timestamp bounds; the latest-row filter must contain exact `es_sn` equality.
- Require the exact `{data, meta}` response envelope and exact `hasNext/page/pageSize` metadata.
- Reject redirects, oversized responses, unordered timestamps, duplicates, rows outside `[start, end)`, and rows whose `es_sn` differs from the requested full station ID.
- Parse numeric strings without accepting booleans, NaN, Infinity, or whitespace-normalized garbage.
- Aggregate only that station: 15-minute mean of `load_power`, last timely `emus_soc`, at least 12 samples, head/tail tolerance 2 minutes.
- Ignore `solar_power` even when present.
- Return each bucket in `station_total_load`, `storage_soc` order with the existing quality/revision contract.

Expose these exact methods:

```python
class RawEnergySourceClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        http: httpx.Client,
        *,
        allowed_station_ids: tuple[str, str],
    ) -> None:
        """Bind one fixed target and the exact two configured station identities."""

    def latest_timestamp(self, station_id: str) -> datetime:
        """Return the newest timezone-aware timestamp for one configured full es_sn."""

    def list_observations(
        self, station_id: str, start: datetime, end: datetime
    ) -> list[ObservationPoint]:
        """Return ordered two-series 15-minute points for one station."""
```

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass and neither response text nor failure messages contain `source-token`.

- [ ] **Step 6: Verify the source boundary contains no cross-station arithmetic**

Run:

```bash
rg -n "solar_power.*\+|ES01.*ES02|ES02.*ES01" \
  m3/worker/clients/raw_energy_api.py \
  m3/tests/test_m3_raw_energy_api.py
```

Expected: no production-code match; test names or fixture declarations may mention both station labels but no arithmetic expression may combine them.

---

### Task 3: Migrate Cache, Forecast Service, Operations API, and Runtime Wiring

**Files:**
- Modify: `m3/worker/services/station_cache.py`
- Modify: `m3/worker/services/forecast_service.py`
- Modify: `m3/worker/services/acceptance_service.py` (constructor dependency split only)
- Modify: `m3/worker/api/routes.py`
- Modify: `m3/worker/api/models.py`
- Modify: `m3/worker/main.py`
- Modify: `m3/tests/m3_test_support.py`
- Modify: `m3/tests/test_m3_forecast_service.py`
- Modify: `m3/tests/test_m3_worker_api.py`
- Modify: `m3/tests/test_m3_scheduler.py`

**Interfaces:**
- Consumes: Task 1 `SERIES_IDS`, Task 2 `Settings.stations` and `RawEnergySourceClient`.
- Produces: one `StationCache` and one independent pair of champions per full `station_id`; operations state returns exactly load/SOC champions.

- [ ] **Step 1: Write failing two-station isolation tests**

Add to `m3/tests/test_m3_forecast_service.py`:

```python
def test_es01_refresh_cannot_change_es02_cache_or_forecast(self):
    service, source, sink, caches = make_two_station_service()
    service.bootstrap("plant-alpha-ES01", AS_OF)
    service.bootstrap("plant-beta-ES02", AS_OF)
    service.select_models("plant-alpha-ES01")
    service.select_models("plant-beta-ES02")
    before = [point.model_copy(deep=True) for point in caches["plant-beta-ES02"].window("station_total_load")]

    source.replace_station_load("plant-alpha-ES01", 4321.0)
    service.run_forecast("plant-alpha-ES01", AS_OF)

    self.assertEqual(caches["plant-beta-ES02"].window("station_total_load"), before)
    self.assertEqual([call.station_id for call in sink.latest_calls], ["plant-alpha-ES01"])
```

Extend `m3/tests/m3_test_support.py` with a station-keyed fake observation source whose `replace_station_load(station_id, value)` mutates only that station, plus a recording sink whose `latest_calls` records successful publications. These are test fakes only; production code must not expose mutation helpers.

Add state-response assertions in `m3/tests/test_m3_worker_api.py`:

```python
self.assertEqual(
    list(state.json()["champions"]),
    ["station_total_load", "storage_soc"],
)
```

Add a per-series failure test: inject a `forecast_one_safe` fake that returns a normal 96-point load series and a zero-point `storage_soc` error series. Assert the same station still publishes one degraded `LatestSnapshot`, the load forecast remains intact, and the peer station cache/publication is unchanged.

Add resource-wiring assertions that `build_resources()` constructs `RawEnergySourceClient` with `settings.raw_source_url` and `settings.raw_source_api_token`, retains `SourceApiClient` only for acceptance-context control calls, and gives NocoBase its own client and credentials.

- [ ] **Step 2: Run focused service tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_forecast_service \
  m3.tests.test_m3_worker_api \
  m3.tests.test_m3_scheduler -v
```

Expected: failures identify remaining three-series constants and the old normalized `SourceApiClient` wiring.

- [ ] **Step 3: Replace service-local constants with the canonical series tuple**

Import `SERIES_IDS` from `m3.worker.contracts` in `station_cache.py`, `forecast_service.py`, and `api/routes.py`. The snapshot builder must require exactly two terminal series:

```python
by_id = {item.unique_id: item for item in typed_series}
if len(typed_series) != len(SERIES_IDS) or set(by_id) != set(SERIES_IDS):
    raise M3Error(
        "forecast_incomplete",
        "Latest snapshot requires load and SOC exactly once",
    )
```

Initialize station champions and cache windows from the same tuple. In `run_forecast()`, allow a `None` champion only for the corresponding insufficient-history dataset, call `forecast_one_safe()` independently for each series, and publish the exact two-series snapshot even when one series is insufficient/error. Aggregate the station snapshot to `degraded` without discarding the healthy series. Keep `_pending` and `_completed` keyed by full `station_id`; do not introduce a global cross-station snapshot.

- [ ] **Step 4: Wire the raw client without broadening inbound APIs**

In `m3/worker/main.py`, construct `RawEnergySourceClient` with `settings.raw_source_url`, its dedicated bearer, and `allowed_station_ids=settings.station_ids`. Pass it to `ForecastService` and to the acceptance actual-backfill boundary. Retain the existing normalized `SourceApiClient` only as the acceptance-context control client; it must not supply model history. Keep the existing FastAPI operations surface at the same five paths. Build caches from `settings.station_ids`, which now derives from the exact two `StationBinding` values.

Change `AcceptanceService` construction to use two explicit named dependencies, `context_source` and `observation_source`, so `get_acceptance_context()` cannot accidentally go to the raw data collection and actual backfill cannot silently return to the old normalized history endpoint. If the existing HTTP alert client remains enabled, keep its fixed alert base URL and bearer separate from the raw-source URL; do not reuse raw source credentials for alert writes.

- [ ] **Step 5: Update fixtures and run GREEN**

Replace old SOC fixtures with `storage_soc`, change expected champion dictionaries from three to two keys, and keep all station-locking tests intact. Run the Step 2 command. Expected: all focused service tests pass.

- [ ] **Step 6: Verify operations API scope**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_worker_api.WorkerApiTests.test_openapi_has_only_five_paths_and_manual_schema_forbids_properties -v
```

Expected: pass; the production Worker still has only health, state, forecast-run, model-selection-run, and job-state paths.

---

### Task 4: Migrate HTTP Persistence and Seven-Day Acceptance to Two Series per Station

**Files:**
- Modify: `m3/worker/services/acceptance_service.py`
- Modify: `m3/worker/sinks/forecast_sink.py`
- Modify: `m3/contracts/nocobase_collections.json`
- Modify: `m3/tests/test_m3_acceptance_service.py`
- Modify: `m3/tests/test_m3_nocobase_sink.py`
- Modify: `m3/tests/test_m3_collection_contract.py`
- Modify: `m3/tests/fixtures/contracts/latest-smoke-record.json`

**Interfaces:**
- Consumes: canonical per-station `LatestSnapshot` with two series, raw observation HTTP source for actual backfill, and the existing HTTP control source for acceptance context only.
- Produces: one latest snapshot per `station_id`, 192 forecast templates per daily acceptance batch, 672 expected points per individual seven-day series, 1,344 expected points overall, and evaluation keys `station_total_load`, `storage_soc`, `overall`.

- [ ] **Step 1: Write failing persistence-count and station-isolation tests**

Update acceptance fixtures to:

```python
SERIES = (
    ("station_total_load", "kW", 800.0),
    ("storage_soc", "%", 55.0),
)
EXPECTED_POINT_COUNT = 2 * 96
```

Add to `m3/tests/test_m3_nocobase_sink.py`:

```python
def test_latest_upsert_key_is_full_station_id(self):
    first = make_latest_snapshot(station_id="plant-alpha-ES01")
    second = make_latest_snapshot(station_id="plant-beta-ES02")
    sink.publish_latest(first)
    sink.publish_latest(second)
    self.assertEqual(
        [call.filter for call in api.update_or_create_calls],
        [
            {"station_id": "plant-alpha-ES01"},
            {"station_id": "plant-beta-ES02"},
        ],
    )
```

Add a collection-contract assertion:

```python
self.assertEqual(EXPECTED_POINT_COUNT, 192)
collections = _collections(contract)
evaluation_checks = {
    item["name"]: item
    for item in collections["energy_forecast_evaluations"]["check_constraints"]
}
self.assertEqual(
    evaluation_checks["energy_forecast_evaluations_key_check"]["allowed_values"],
    ["station_total_load", "storage_soc", "overall"],
)
self.assertEqual(
    evaluation_checks["energy_forecast_evaluations_overall_counts_check"]
    ["expected_count"],
    1344,
)
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  m3.tests.test_m3_acceptance_service \
  m3.tests.test_m3_nocobase_sink \
  m3.tests.test_m3_collection_contract -v
```

Expected: failures show 288-point and old SOC enumeration assumptions.

- [ ] **Step 3: Implement two-series persistence invariants**

Import canonical `SERIES_IDS` in acceptance and sink code. Use the `context_source.get_acceptance_context()` and `observation_source.list_observations()` dependencies split in Task 3. Set acceptance template count to `len(SERIES_IDS) * 96`, which is 192. Keep `expected_count=672` per individual series and change the overall expected count from 2,016 to `len(SERIES_IDS) * 672`, which is 1,344. Build overall precedence from two station-local results only:

```python
for key in (*SERIES_IDS, "overall"):
    evaluation = evaluations[key]
    api.update_or_create(
        "energy_forecast_evaluations",
        {
            "station_id": station_id,
            "acceptance_run_id": acceptance_run_id,
            "evaluation_key": key,
        },
        evaluation,
    )
```

Change collection check values from `storage_1_soc`/`storage_2_soc` to `storage_soc`; change all 288-point invariants to 192, all 2,016-point limits to 1,344, and all “three series” application invariants to “two series”. Keep every unique constraint and filter beginning with `station_id`. Update `m3/tests/fixtures/contracts/latest-smoke-record.json` to the two-series shape and recompute its canonical content hash using the production helper rather than editing a digest by hand.

- [ ] **Step 4: Run persistence tests and verify GREEN**

Run the Step 2 command. Expected: all persistence and contract tests pass.

- [ ] **Step 5: Validate contract JSON and old-name removal**

Run:

```bash
.venv/bin/python -m json.tool m3/contracts/nocobase_collections.json >/dev/null
rg -n "storage_1_soc|storage_2_soc|288" \
  m3/worker/services/acceptance_service.py \
  m3/worker/sinks/forecast_sink.py \
  m3/contracts/nocobase_collections.json \
  m3/tests/fixtures/contracts/latest-smoke-record.json \
  m3/tests/test_m3_acceptance_service.py \
  m3/tests/test_m3_nocobase_sink.py
```

Then run a second scan for stale overall counts and language:

```bash
rg -n "2016|three_series|three series|three-series" \
  m3/worker/services/acceptance_service.py \
  m3/contracts/nocobase_collections.json \
  m3/tests/test_m3_acceptance_service.py
```

Expected: JSON parse succeeds and neither scan finds a stale old-series/count/wording match in the migrated production/persistence scope. `m3/tests/test_m3_collection_contract.py` retains explicitly scoped assertions for the byte-protected legacy Node-RED artifact, so do not use a global old-name replacement in that mixed test file; update only its Worker collection, smoke-record, and acceptance assertions.

---

### Task 5: Build the Nested Two-Station Dashboard Backend

**Files:**
- Create: `m3/worker/dashboard_contracts.py`
- Create: `m3/worker/services/live_dashboard_service.py`
- Create: `m3/worker/api/live_dashboard.py`
- Modify: `m3/worker/live_dashboard_app.py`
- Remove after migration: `m3/worker/live_dashboard.py`
- Replace: `m3/tests/test_m3_live_dashboard.py`

**Interfaces:**
- Consumes: `StationBinding`, `RawEnergySourceClient`, two-series domain forecasts.
- Produces: strict `DashboardEnvelope`, per-station cache refresh, `GET /energy-forecast-api`, and `GET /` serving the existing HTML.

- [ ] **Step 1: Write failing nested-contract tests**

Create literal expected topology in `m3/tests/test_m3_live_dashboard.py`:

```python
def test_dashboard_contains_two_public_stations_and_no_full_es_sn(self):
    envelope = build_dashboard_payload(
        [station_result(STATION_1), station_result(STATION_2)],
        generated_at=GENERATED_AT,
    )
    payload = envelope.model_dump(mode="json")["data"]
    self.assertEqual(payload["operation"], "forecast_dashboard")
    self.assertEqual(
        [(item["station_key"], item["station_name"]) for item in payload["stations"]],
        [("station_1", "1# 电站"), ("station_2", "2# 电站")],
    )
    self.assertTrue(all(
        [series["unique_id"] for series in item["series"]]
        == ["station_total_load", "storage_soc"]
        for item in payload["stations"]
    ))
    rendered = envelope.model_dump_json()
    self.assertNotIn("plant-alpha-ES01", rendered)
    self.assertNotIn("plant-beta-ES02", rendered)
```

Add independent-time assertions: station 1 forecast starts at `10:00`, station 2 at `09:45`, and both ranges remain unchanged in the same response.

Add refresh isolation:

```python
def test_failed_station_refresh_keeps_its_last_good_value_and_updates_peer(self):
    refresher.refresh_all()
    provider.fail_station("plant-alpha-ES01")
    provider.advance_station("plant-beta-ES02", minutes=15)
    refresher.refresh_all()
    snapshot = cache.snapshot(
        GENERATED_AT + timedelta(minutes=15)
    ).model_dump(mode="json")["data"]
    self.assertEqual(snapshot["stations"][0]["range"]["forecast_start"], STATION_1_AS_OF.isoformat())
    self.assertEqual(snapshot["stations"][1]["range"]["forecast_start"], (STATION_2_AS_OF + timedelta(minutes=15)).isoformat())
```

Add an endpoint test that performs two GET requests and proves the provider call count is unchanged.

- [ ] **Step 2: Run the Dashboard tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest m3.tests.test_m3_live_dashboard -v
```

Expected: missing dashboard DTO/service modules and old top-level `series` topology failures.

- [ ] **Step 3: Implement strict Dashboard DTOs**

In `m3/worker/dashboard_contracts.py`, create Pydantic models with `extra="forbid"`:

```python
class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardRange(ApiModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    history_start: datetime | None
    history_end: datetime | None
    actual_latest: datetime | None
    forecast_start: datetime | None
    forecast_end: datetime | None
    interval_seconds: Literal[900] = 900
    history_hours: Literal[24] = 24
    forecast_hours: Literal[24] = 24
    display_hours: Literal[48] = 48
    now_separator: datetime


class DashboardStationSystem(ApiModel):
    state: Literal["ready", "initializing", "degraded", "stale", "error"]
    mode: Literal["normal", "degraded", "initializing", "error"]
    generated_at: datetime | None
    stale: bool


class DashboardActualPoint(ApiModel):
    data_time: datetime
    value: float | None
    quality: Literal["valid", "invalid"]
    source_revision: int = Field(ge=0, strict=True)


class DashboardForecastPoint(ApiModel):
    data_time: datetime
    target_time: datetime
    value: float
    raw_value: float
    is_clipped: bool


class DashboardSeries(ApiModel):
    unique_id: SeriesId
    unit: Literal["kW", "%"]
    model_name: str | None
    status: Literal[
        "ok", "warming_up", "degraded",
        "insufficient_history", "initializing", "error",
    ]
    fallback_reason: str | None
    actual: list[DashboardActualPoint]
    forecast: list[DashboardForecastPoint]


class DashboardAcceptanceResult(ApiModel):
    unique_id: SeriesId
    expected_count: Literal[672]
    valid_count: int = Field(ge=0, le=672, strict=True)
    zero_actual_count: int = Field(ge=0, le=672, strict=True)
    mape_percent: float | None
    mae: float | None
    smape_percent: float | None
    outcome: Literal["passed", "failed", "insufficient_data"]


class DashboardAcceptance(ApiModel):
    acceptance_run_id: str
    status: Literal["in_progress", "passed", "failed", "insufficient_data"]
    completed_days: int = Field(ge=1, le=7, strict=True)
    expected_days: Literal[7] = 7
    results: list[DashboardAcceptanceResult]


class DashboardStation(ApiModel):
    station_key: Literal["station_1", "station_2"]
    station_name: str
    range: DashboardRange
    system: DashboardStationSystem
    series: tuple[DashboardSeries, DashboardSeries]
    acceptance: DashboardAcceptance | None = None


class DashboardAggregateSystem(ApiModel):
    state: Literal["ready", "initializing", "degraded", "stale"]
    generated_at: datetime
    healthy_station_count: int = Field(ge=0, le=2, strict=True)
    station_count: Literal[2] = 2


class DashboardData(ApiModel):
    operation: Literal["forecast_dashboard"] = "forecast_dashboard"
    system: DashboardAggregateSystem
    stations: tuple[DashboardStation, DashboardStation]


class DashboardEnvelope(ApiModel):
    status: Literal["ok"] = "ok"
    data: DashboardData
```

Validators must enforce station order, per-station series order, 24-hour ranges, forecast counts, point continuity, clipping truthfulness, unit/range rules, aggregate severity, and public station keys. An empty/error station uses nullable range endpoints, `generated_at=None`, empty point arrays, and no invented model or acceptance values. `DashboardAcceptance.results` is empty while in progress and is exactly the two ordered series when final. No DTO may define or serialize a full source identity field.

- [ ] **Step 4: Implement per-station cache and refresh**

In `m3/worker/services/live_dashboard_service.py`, use these boundaries:

```python
def floor_quarter_hour(value: datetime) -> datetime:
    validate_shanghai_timestamp(value, "latest source timestamp", quarter_hour=False)
    return value.replace(
        minute=(value.minute // 15) * 15,
        second=0,
        microsecond=0,
    )


def forecast_station(
    actual: list[ObservationPoint], as_of: datetime
) -> list[ForecastSeries]:
    forecasts = []
    for unique_id in SERIES_IDS:
        dataset = build_training_dataset(actual, unique_id)
        try:
            champion = select_champion(dataset)
        except M3Error as error:
            if error.code != "insufficient_history":
                champion = seasonal_naive_champion(
                    dataset, "model_selection_failed"
                )
            else:
                champion = None
        forecasts.append(forecast_one_safe(dataset, champion, as_of))
    return forecasts


@dataclass(frozen=True)
class DashboardStationResult:
    binding: StationBinding
    as_of: datetime
    generated_at: datetime
    actual: list[ObservationPoint]
    forecasts: list[ForecastSeries]


class LiveDashboardProvider:
    def build_station(self, binding: StationBinding) -> DashboardStationResult:
        latest = self._source.latest_timestamp(binding.station_id)
        as_of = floor_quarter_hour(latest)
        actual = self._source.list_observations(
            binding.station_id,
            as_of - timedelta(days=90),
            as_of,
        )
        forecasts = forecast_station(actual, as_of)
        return DashboardStationResult(binding, as_of, self._clock(), actual, forecasts)


class DashboardCache:
    def publish_station(self, result: DashboardStationResult) -> None:
        """Atomically replace one public station while preserving its peer."""

    def snapshot(self, now: datetime) -> DashboardEnvelope:
        """Recompute per-station stale flags and aggregate state without modeling."""
```

Implement `build_dashboard_payload(results, generated_at) -> DashboardEnvelope` in this module; it is the only adapter allowed to translate internal full station bindings into public station keys/names. `PeriodicDashboardRefresher.refresh_all()` must loop through the two configured bindings and catch failures separately. It must log only `station_key` and safe error type/code. Startup fails only when neither station has ever produced a valid snapshot; after one valid station exists, the API may start in aggregate degraded state.

- [ ] **Step 5: Implement same-origin routes and owned cleanup**

In `m3/worker/api/live_dashboard.py`, expose a router factory so the HTML path is an owned, testable dependency rather than an undefined module global:

```python
def create_live_dashboard_router(html_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/energy-forecast-api")
    def dashboard(request: Request) -> DashboardEnvelope:
        return request.app.state.dashboard_cache.snapshot(
            request.app.state.clock()
        )

    @router.get("/", response_class=FileResponse)
    def page() -> FileResponse:
        return FileResponse(
            html_path,
            media_type="text/html; charset=utf-8",
        )

    return router
```

In `m3/worker/live_dashboard_app.py`, define these lifecycle interfaces:

```python
@dataclass(frozen=True)
class LiveDashboardResources:
    cache: DashboardCache
    refresher: PeriodicDashboardRefresher
    source_http: httpx.Client
    clock: Callable[[], datetime]


def build_live_dashboard_resources_from_env() -> LiveDashboardResources:
    """Build owned resources; called from lifespan, never at import time."""


def create_live_dashboard_app(
    resources_factory: Callable[[], LiveDashboardResources],
    html_path: Path,
) -> FastAPI:
    """Create the import-safe shell and own startup/shutdown ordering."""
```

The builder parses `M3_STATIONS_JSON`, reads the configured secret file, and creates the allowed-station raw client, but it is called only inside FastAPI lifespan. The app factory stores cache/clock on `app.state`, starts the refresher before serving, stops it before closing `source_http`, includes only the router above, and disables docs/OpenAPI routes. The module-level `app` calls the app factory with the resource-builder function object and the fixed HTML path; importing the module must not read credentials, open HTTP clients, or contact the network.

- [ ] **Step 6: Run Dashboard tests and verify GREEN**

Run the Step 2 command. Expected: all Dashboard backend tests pass.

- [ ] **Step 7: Remove the obsolete monolithic module**

After `rg -n "m3.worker\.live_dashboard" m3/worker m3/tests` shows all imports point to the new modules, remove `m3/worker/live_dashboard.py`. Re-run the Step 2 command and `.venv/bin/python -m compileall -q m3/worker`.

---

### Task 6: Redesign the HTML for Two Station Sections and Read-Only Polling

**Files:**
- Modify: `场站未来能耗预测.html`
- Modify: `m3/tests/m3_dashboard_e2e.js`

**Interfaces:**
- Consumes: Task 5 nested `DashboardEnvelope` from `/energy-forecast-api`.
- Produces: two station sections, four metric cards, four 48-hour charts, and one non-overlapping 60-second polling loop.

- [ ] **Step 1: Replace the E2E fixture with the nested contract and verify RED**

In `m3/tests/m3_dashboard_e2e.js`, build two stations with different windows and literal first forecasts:

```javascript
const stations = [
  stationPayload("station_1", "1# 电站", "2026-08-26T10:00:00+08:00", 410, 61),
  stationPayload("station_2", "2# 电站", "2026-08-26T09:45:00+08:00", 730, 54),
];
```

Assert:

```javascript
assert.equal(await page.locator(".station-section").count(), 2);
assert.equal(await page.locator(".series-card").count(), 4);
assert.equal(await page.locator("svg.forecast-chart").count(), 4);
assert.deepEqual(
  await page.locator(".station-section h2").allTextContents(),
  ["1# 电站", "2# 电站"],
);
assert.deepEqual(
  await page.locator(".forecast-value").allTextContents(),
  ["410 kW", "61.0 %", "730 kW", "54.0 %"],
);
assert.equal(await page.locator(".station-acceptance").count(), 2);
```

For final acceptance fixtures, assert each station has exactly two ordered result rows (`station_total_load`, `storage_soc`); an in-progress station has zero result rows and does not alter its peer's final panel. Count intercepted `/energy-forecast-api` calls, advance a fake timer by 60 seconds, and assert the second GET occurs without any model/run POST.

Run `node m3/tests/m3_dashboard_e2e.js`. Expected: fail because the old page accepts one top-level three-series payload.

- [ ] **Step 2: Implement strict nested validation in the page**

Replace top-level `SERIES_IDS` with:

```javascript
const STATION_KEYS = ["station_1", "station_2"];
const SERIES_IDS = ["station_total_load", "storage_soc"];
```

Validate the exact top-level keys `operation/system/stations`, exact station order, public names, independent ranges, two series per station, and all existing point constraints. Reject unknown/extra fields and never accept `es_sn` or `station_id` in public station objects.

- [ ] **Step 3: Implement two static station sections and four charts**

Each station section must contain:

```html
<section class="station-section" data-station="station_1">
  <header class="station-head">
    <h2>1# 电站</h2>
    <span class="station-status">—</span>
  </header>
  <div class="series-grid">
    <article class="series-card" data-series="station_total_load"></article>
    <article class="series-card" data-series="storage_soc"></article>
  </div>
  <div class="chart-grid">
    <svg class="forecast-chart load-chart" role="img"></svg>
    <svg class="forecast-chart soc-chart" role="img"></svg>
  </div>
</section>
```

Create the equivalent `station_2` section with its own DOM IDs. Reuse safe SVG node creation, never inject API strings through `innerHTML`. Desktop uses two columns within each station; the existing mobile breakpoint stacks them and must avoid page-level horizontal overflow.

Place one `.station-acceptance` block inside each station section. All selectors used by render functions must be scoped from the owning `.station-section`; do not retain the old page-global acceptance IDs. A station may show `acceptance=null`, in-progress, or its own two final rows without reading or overwriting its peer's DOM.

- [ ] **Step 4: Add one guarded 60-second polling loop**

Keep the existing `inFlight` guard and use one timer:

```javascript
void loadDashboard();
const refreshTimer = window.setInterval(() => {
  if (document.visibilityState === "visible") void loadDashboard();
}, 60_000);
window.addEventListener("pagehide", () => window.clearInterval(refreshTimer), { once: true });
```

The loader may call only `GET /energy-forecast-api` with `cache: "no-store"`; it must not call Worker run endpoints.

- [ ] **Step 5: Run E2E and verify GREEN**

Run:

```bash
node m3/tests/m3_dashboard_e2e.js
```

Expected: `m3_dashboard_e2e_ok`.

- [ ] **Step 6: Run responsive browser checks**

Serve the page and API locally, then use Playwright to verify 1280×720 and 390×844. Assert two station sections and four cards are visible, `document.documentElement.scrollWidth <= document.documentElement.clientWidth`, error state is hidden, and browser console error list is empty.

---

### Task 7: Documentation, Real API Integration, and Full Regression

**Files:**
- Modify: `m3/M3 场站未来能耗预测设计.md`
- Modify: `docs/m3/部署说明.md`
- Keep: `docs/m3/specs/2026-08-26-m3-two-station-forecast-dashboard-design.md`
- Keep: `docs/m3/plans/2026-08-26-m3-two-station-forecast-dashboard.md`

**Interfaces:**
- Consumes: all prior task outputs and the protected `m3/worker/密钥.txt` local credential.
- Produces: verified local URL, real per-station predictions, and production-aligned operating instructions.

- [ ] **Step 1: Update M3 documentation without adding external projects**

Document these exact deployment inputs:

```text
M3_STATIONS_JSON=[
  {"station_id":"plant-alpha-ES01","station_key":"station_1","station_name":"1# 电站"},
  {"station_id":"plant-beta-ES02","station_key":"station_2","station_name":"2# 电站"}
]
M3_RAW_SOURCE_URL=https://vifa.hlszh.com/api/t_es_data:list
M3_RAW_SOURCE_API_TOKEN_FILE=/run/secrets/m3_raw_source_token
M3_TIMEZONE=Asia/Shanghai
```

The two `plant-*` IDs are syntactically valid non-production examples. Deployment must substitute the protected exact full IDs through its secret/config system and must not commit the production values. The local app continues to read `M3_LIVE_SECRET_FILE`, defaulting to the protected `m3/worker/密钥.txt`, while the production Worker uses either `M3_RAW_SOURCE_API_TOKEN` secret injection or `M3_RAW_SOURCE_API_TOKEN_FILE`. Document the local command:

```bash
.venv/bin/python -m uvicorn m3.worker.live_dashboard_app:app \
  --host 127.0.0.1 --port 8766 --no-access-log
```

State that the HTML and nested API contract deploy together, while result-table and ACL setup remain external operator work.

- [ ] **Step 2: Run the complete M3 Python regression**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_m3_*.py' -q
```

Expected: zero failures. Do not run M1/M2 tests.

- [ ] **Step 3: Run all M3 JavaScript contract tests**

Run:

```bash
node tests/test_m3_node_red_contract.js
node m3/tests/m3_dashboard_e2e.js
```

Expected: both commands exit zero. Existing Node-RED artifacts must remain byte-identical; a failure is reported as an out-of-scope compatibility issue and does not authorize editing Node-RED files.

- [ ] **Step 4: Run a protected real-source forecast**

For this one local integration check only, read `m3/worker/密钥.txt` inside the process, discover the two full station IDs without printing them, require exactly one `ES01` and one `ES02`, bind them to the two public keys, and start the local app. This discovery path is test harness logic and must not enter production code, where the exact full IDs remain mandatory configuration. Query `/energy-forecast-api` and print only this safe summary:

```text
station_1  station_total_load  SeasonalNaive  warming_up  actual=96 forecast=96
station_1  storage_soc         SeasonalNaive  warming_up  actual=96 forecast=96
station_2  station_total_load  SeasonalNaive  warming_up  actual=96 forecast=96
station_2  storage_soc         SeasonalNaive  warming_up  actual=96 forecast=96
```

Accept another approved StatsForecast champion/status when real history has reached the full-model threshold. Verify both first load forecasts against their own station histories; neither may equal a sum involving the peer station or `solar_power`.

- [ ] **Step 5: Verify public secrecy and browser rendering**

Scan production and public artifacts for bearer/token leakage:

```bash
rg -n "Bearer eyJ|TW_AEy0" \
  m3.worker '场站未来能耗预测.html' \
  --glob '!.local/密钥.txt'
```

Expected: no match. Test-only fake station IDs may exist in fixtures, but must not appear in HTML. During the real-source test, assert in-process that neither configured full `station_id` string is a substring of the serialized live response or HTML; print only `m3_live_identity_scan_ok`, never the protected values.

Open `http://127.0.0.1:8766/`, verify both station sections, four charts, correct first forecasts, no empty/error state, no console error, and 390px responsive layout. Keep the corrected tab open for user review.

- [ ] **Step 6: Final file-scope checkpoint**

Verify the protected-file baseline captured before Task 1:

```bash
shasum -a 256 -c /tmp/m3_protected_before.sha256
```

Expected: every M1, M2, and `m3/node_red` file reports `OK`. Record final test counts, real forecast windows, model names/statuses, and the local URL in the handoff without recording protected full station IDs.
