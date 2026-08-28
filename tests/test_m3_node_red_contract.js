"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const CONTRACT_PATH = path.join(ROOT, "m3", "node_red", "forecast_contract.js");
const FLOW_PATH = path.join(ROOT, "m3", "node_red", "energy_forecast_flow.json");
const ALERT_CLIENT_PATH = path.join(ROOT, "m3_worker", "clients", "alert_api.py");
const NOCOBASE_CONTRACT_PATH = path.join(ROOT, "m3", "contracts", "nocobase_collections.json");
const contract = require(CONTRACT_PATH);

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }
function throwsCode(fn, code) {
  assert.throws(fn, (error) => error && error.code === code, `expected ${code}`);
}
function shanghai(ms) {
  return new Date(ms + 8 * 60 * 60 * 1000).toISOString().replace(".000Z", "+08:00").replace("Z", "+08:00");
}
function mappedRecord(time, load = 800, soc1 = 50, soc2 = 60, revisionMs = 1000) {
  const at = Date.parse(time);
  return {
    time,
    load_kw: load,
    soc_1: soc1,
    soc_2: soc2,
    updated_at: shanghai(at + revisionMs),
  };
}
function denseRecords(start = "2026-08-25T00:00:00+08:00") {
  const startMs = Date.parse(start);
  return Array.from({ length: 30 }, (_, index) => mappedRecord(
    shanghai(startMs + index * 30000),
    800 + index,
    50 + index / 100,
    60 + index / 100,
    1000 + index,
  ));
}
const MAPPING = Object.freeze({
  timestamp: "time",
  stationTotalLoad: "load_kw",
  storage1Soc: "soc_1",
  storage2Soc: "soc_2",
  updatedAt: "updated_at",
});

function makeForecastPoints(start, uniqueId) {
  const startMs = Date.parse(start);
  return Array.from({ length: 96 }, (_, index) => {
    const raw = uniqueId === "station_total_load" ? 700 + index : 40 + index / 10;
    return {
      data_time: shanghai(startMs + index * 900000),
      target_time: shanghai(startMs + (index + 1) * 900000),
      horizon_step: index + 1,
      raw_forecast: raw,
      forecast_value: raw,
      is_clipped: false,
    };
  });
}
function makeLatest(overrides = {}, start = "2026-08-25T01:00:00+08:00") {
  return {
    station_id: "station-1",
    as_of: "2026-08-25T01:02:00+08:00",
    generated_at: "2026-08-25T01:02:08+08:00",
    source_data_end: "2026-08-25T00:45:00+08:00",
    status: "ok",
    series_payload: [
      { unique_id: "station_total_load", unit: "kW", model_name: "AutoETS", status: "ok", points: makeForecastPoints(start, "station_total_load"), fallback_reason: null },
      { unique_id: "storage_1_soc", unit: "%", model_name: "AutoARIMA", status: "ok", points: makeForecastPoints(start, "storage_1_soc"), fallback_reason: null },
      { unique_id: "storage_2_soc", unit: "%", model_name: "SeasonalNaive", status: "degraded", points: makeForecastPoints(start, "storage_2_soc"), fallback_reason: "champion_failed" },
    ],
    model_manifest: {},
    content_hash: "a".repeat(64),
    updated_at: "2026-08-25T01:02:09+08:00",
    ...overrides,
  };
}
function makeActuals() {
  return [
    { unique_id: "station_total_load", ds: "2026-08-25T00:30:00+08:00", y: 790, quality: "valid", source_revision: 2 },
    { unique_id: "station_total_load", ds: "2026-08-25T00:45:00+08:00", y: null, quality: "invalid", source_revision: 3 },
    { unique_id: "storage_1_soc", ds: "2026-08-25T00:45:00+08:00", y: 55, quality: "valid", source_revision: 3 },
    { unique_id: "storage_2_soc", ds: "2026-08-25T00:45:00+08:00", y: 65, quality: "valid", source_revision: 3 },
  ];
}
function makeAcceptance() {
  return {
    acceptance_run_id: "run-20260825",
    status: "passed",
    completed_days: 7,
    expected_days: 7,
    results: [
      { unique_id: "station_total_load", expected_count: 672, valid_count: 650, zero_actual_count: 10, mape_percent: 12.5, mae: 3.2, smape_percent: 11.1, outcome: "passed" },
      { unique_id: "storage_1_soc", expected_count: 672, valid_count: 651, zero_actual_count: 9, mape_percent: 9.5, mae: 1.3, smape_percent: 9.1, outcome: "passed" },
      { unique_id: "storage_2_soc", expected_count: 672, valid_count: 652, zero_actual_count: 8, mape_percent: 8.5, mae: 1.2, smape_percent: 8.1, outcome: "passed" },
    ],
  };
}

function makeBatches(count = 7, runId = "run-20260825") {
  const first = Date.parse("2026-08-25T01:02:00+08:00");
  return Array.from({ length: count }, (_, index) => ({
    id: index + 1,
    station_id: "station-1",
    acceptance_run_id: runId,
    issued_at: shanghai(first + index * 86400000),
    write_state: "complete",
  })).reverse();
}

function makeAcceptancePoints(batches) {
  return batches.flatMap((batch) => {
    const start = Date.parse(batch.issued_at) - 120000;
    return ["station_total_load", "storage_1_soc", "storage_2_soc"].flatMap((uniqueId) =>
      Array.from({ length: 96 }, (_, index) => ({
        batch_id: batch.id,
        unique_id: uniqueId,
        data_time: shanghai(start + index * 900000),
        actual_quality: null,
      })),
    );
  });
}

function makeEvaluationRows(outcomes = ["passed", "passed", "passed"], overallOutcome = "passed") {
  const results = makeAcceptance().results.map((item, index) => ({
    station_id: "station-1",
    acceptance_run_id: "run-20260825",
    evaluation_key: item.unique_id,
    expected_count: item.expected_count,
    valid_count: item.valid_count,
    zero_actual_count: item.zero_actual_count,
    mape_percent: outcomes[index] === "failed" ? 31 : outcomes[index] === "insufficient_data" ? 20 : item.mape_percent,
    mae: item.mae,
    smape_percent: item.smape_percent,
    outcome: outcomes[index],
  }));
  return [...results, {
    station_id: "station-1",
    acceptance_run_id: "run-20260825",
    evaluation_key: "overall",
    expected_count: 2016,
    valid_count: results.reduce((sum, item) => sum + item.valid_count, 0),
    zero_actual_count: results.reduce((sum, item) => sum + item.zero_actual_count, 0),
    mape_percent: null,
    mae: null,
    smape_percent: null,
    outcome: overallOutcome,
  }];
}

function page(data, pageSize, count = data.length, totalPage = count === 0 ? 0 : Math.ceil(count / pageSize)) {
  return { data, meta: { count, page: 1, pageSize, totalPage } };
}

function utcTimestamp(value, fraction = null) {
  const iso = new Date(Date.parse(value)).toISOString();
  return fraction === null ? iso.replace(".000Z", "Z") : iso.replace(/\.\d{3}Z$/, `.${fraction}Z`);
}

function zeroOffsetTimestamp(value, fraction = null) {
  return utcTimestamp(value, fraction).replace("Z", "+00:00");
}

function zeroFractionShanghai(value, digits = "000") {
  return value.replace("+08:00", `.${digits}+08:00`);
}

function rewriteLatestInstants(latest, pointTimestamp) {
  return {
    ...latest,
    as_of: utcTimestamp(latest.as_of),
    generated_at: utcTimestamp(latest.generated_at, "123456"),
    source_data_end: zeroOffsetTimestamp(latest.source_data_end, "000"),
    updated_at: zeroOffsetTimestamp(latest.updated_at, "987654"),
    series_payload: latest.series_payload.map((series, seriesIndex) => ({
      ...series,
      points: series.points.map((point) => ({
        ...point,
        data_time: pointTimestamp(point.data_time, seriesIndex),
        target_time: pointTimestamp(point.target_time, seriesIndex),
      })),
    })),
  };
}

test("strict bearer authorization compares the exact scheme and value", () => {
  assert.strictEqual(contract.authorizeBearer("Bearer source-secret", "source-secret"), true);
  for (const header of [undefined, "source-secret", "bearer source-secret", "Bearer  source-secret", "Bearer source-secret ", "Basic source-secret"])
    throwsCode(() => contract.authorizeBearer(header, "source-secret"), "unauthorized");
});

test("station, names, and Shanghai timestamps reject path and coercion material", () => {
  assert.strictEqual(contract.validateStationId("station-1"), "station-1");
  for (const station of ["", ".", "..", "../station", "station/1", "站点", true, "a".repeat(129)])
    throwsCode(() => contract.validateStationId(station), "invalid_request");
  assert.strictEqual(contract.parseShanghaiTimestamp("2026-08-25T01:15:00+08:00", { quarterHour: true }), Date.parse("2026-08-25T01:15:00+08:00"));
  assert.strictEqual(contract.parseShanghaiTimestamp("2026-08-25T01:15:00.000000+08:00", { quarterHour: true }), Date.parse("2026-08-25T01:15:00+08:00"));
  for (const timestamp of ["2026-08-25T01:15:00Z", "2026-08-25T01:15:00+0800", "2026-08-25T01:15:01+08:00", "2026-08-25T01:15:00.000001+08:00", "2026-02-30T01:15:00+08:00", 123])
    throwsCode(() => contract.parseShanghaiTimestamp(timestamp, { quarterHour: true }), "invalid_request");
});

test("observation query is exact, half-open, quarter aligned, seven-day bounded, and cursor bound", () => {
  const request = contract.validateObservationRequest({
    stationId: "station-1",
    query: { start: "2026-08-18T00:00:00+08:00", end: "2026-08-25T00:00:00+08:00" },
    cursorSecret: "x".repeat(32),
  });
  assert.deepStrictEqual(request, {
    stationId: "station-1", start: "2026-08-18T00:00:00+08:00", end: "2026-08-25T00:00:00+08:00", startMs: Date.parse("2026-08-18T00:00:00+08:00"), endMs: Date.parse("2026-08-25T00:00:00+08:00"), offset: 0,
  });
  for (const query of [
    { start: request.start, end: request.end, url: "https://attacker.invalid" },
    { start: request.start, end: request.end, token: "x" },
    { start: request.end, end: request.start },
    { start: request.start, end: "2026-08-25T00:00:01+08:00" },
    { start: request.start, end: "2026-08-25T00:15:00+08:00" },
  ]) throwsCode(() => contract.validateObservationRequest({ stationId: "station-1", query, cursorSecret: "x".repeat(32) }), "invalid_request");
});

test("mapping is exact and prototype-safe and rejects unsafe property paths", () => {
  assert.deepStrictEqual(contract.validateMapping(MAPPING), MAPPING);
  for (const mapping of [
    { ...MAPPING, targetUrl: "https://attacker.invalid" },
    { ...MAPPING, timestamp: "__proto__.polluted" },
    { ...MAPPING, stationTotalLoad: "constructor.prototype.x" },
    { ...MAPPING, storage1Soc: "a[0]" },
    { timestamp: "time", stationTotalLoad: "load_kw", storage1Soc: "soc_1" },
  ]) throwsCode(() => contract.validateMapping(mapping), "source_not_configured");
  const polluted = Object.create({ timestamp: "time" });
  polluted.stationTotalLoad = "load_kw";
  polluted.storage1Soc = "soc_1";
  polluted.storage2Soc = "soc_2";
  throwsCode(() => contract.validateMapping(polluted), "source_not_configured");
});

test("dense aggregation emits exactly three stable points with weighted load and fresh SOC", () => {
  const points = contract.aggregateObservations({
    stationId: "station-1", records: denseRecords(), mapping: MAPPING,
    start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00",
  });
  assert.strictEqual(points.length, 3);
  assert.deepStrictEqual(points.map((point) => point.unique_id), ["station_total_load", "storage_1_soc", "storage_2_soc"]);
  assert.ok(Math.abs(points[0].y - 814.5) < 1e-9);
  assert.strictEqual(points[0].quality, "valid");
  assert.strictEqual(points[1].y, 50.29);
  assert.strictEqual(points[2].y, 60.29);
  assert.deepStrictEqual(points.map((point) => point.ds), Array(3).fill("2026-08-25T00:00:00+08:00"));
  assert.strictEqual(points[0].source_revision, Date.parse("2026-08-25T00:14:31.029+08:00"));
});

test("load coverage includes bucket head, tail, and every two-minute gap", () => {
  const startMs = Date.parse("2026-08-25T00:00:00+08:00");
  const samples = (offsets) => offsets.map((seconds) => ({ time: startMs + seconds * 1000, value: 10 }));
  assert.strictEqual(contract.timeWeightedMean(samples([120, 240, 360, 480, 600, 720, 780]), startMs, startMs + 900000), 10);
  assert.strictEqual(contract.timeWeightedMean(samples([121, 241, 361, 481, 601, 721, 781]), startMs, startMs + 900000), null);
  assert.strictEqual(contract.timeWeightedMean(samples([0, 120, 240, 361, 481, 601, 721, 840]), startMs, startMs + 900000), null);
  assert.strictEqual(contract.timeWeightedMean(samples([0, 120, 240, 360, 480, 600, 720, 779]), startMs, startMs + 900000), null);
});

test("sparse and out-of-bounds samples stay explicit nulls and are never zero-filled", () => {
  const records = [mappedRecord("2026-08-25T00:00:00+08:00", -1, 101, -1)];
  const points = contract.aggregateObservations({ stationId: "station-1", records, mapping: MAPPING, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:30:00+08:00" });
  assert.strictEqual(points.length, 6);
  for (const point of points) assert.deepStrictEqual({ y: point.y, quality: point.quality }, { y: null, quality: "invalid" });
});

test("mapped values reject strings, booleans, non-finite numbers, unsafe records, and invalid revisions", () => {
  for (const mutation of [
    { load_kw: "800" }, { load_kw: true }, { load_kw: Infinity }, { soc_1: "50" }, { updated_at: "not-a-time" }, { updated_at: 12 },
  ]) {
    const record = { ...mappedRecord("2026-08-25T00:00:00+08:00"), ...mutation };
    throwsCode(() => contract.aggregateObservations({ stationId: "station-1", records: [record], mapping: MAPPING, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00" }), "source_contract_invalid");
  }
  const inherited = Object.create(mappedRecord("2026-08-25T00:00:00+08:00"));
  throwsCode(() => contract.aggregateObservations({ stationId: "station-1", records: [inherited], mapping: MAPPING, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00" }), "source_contract_invalid");
});

test("duplicate source samples deterministically select higher revisions and reject conflicts", () => {
  const lower = mappedRecord("2026-08-25T00:00:00+08:00", 100, 50, 60, 1000);
  const higher = mappedRecord("2026-08-25T00:00:00+08:00", 200, 55, 65, 2000);
  const points = contract.aggregateObservations({ stationId: "station-1", records: [...denseRecords().slice(1), higher, lower, higher], mapping: MAPPING, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00" });
  assert.strictEqual(points[0].source_revision, Date.parse("2026-08-25T00:14:31.029+08:00"));
  const conflict = { ...higher, load_kw: 201 };
  throwsCode(() => contract.aggregateObservations({ stationId: "station-1", records: [higher, conflict], mapping: MAPPING, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00" }), "source_contract_invalid");
});

test("updatedAt omission produces exact deterministic revision one", () => {
  const mapping = { timestamp: "time", stationTotalLoad: "load_kw", storage1Soc: "soc_1", storage2Soc: "soc_2" };
  const records = denseRecords().map(({ updated_at, ...record }) => record);
  const points = contract.aggregateObservations({ stationId: "station-1", records, mapping, start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T00:15:00+08:00" });
  assert.deepStrictEqual(points.map((point) => point.source_revision), [1, 1, 1]);
  assert.ok(points.every((point) => Number.isSafeInteger(point.source_revision) && point.source_revision > 0));
});

test("signed cursor pagination is deterministic, bounded, and cannot regress or cross-bind", () => {
  const points = Array.from({ length: 7 }, (_, index) => ({ unique_id: "station_total_load", ds: shanghai(Date.parse("2026-08-25T00:00:00+08:00") + index * 900000), y: index, quality: "valid", source_revision: 1 }));
  const binding = { stationId: "station-1", start: "2026-08-25T00:00:00+08:00", end: "2026-08-25T02:00:00+08:00" };
  const first = contract.paginatePoints({ points, ...binding, offset: 0, pageSize: 3, cursorSecret: "s".repeat(32) });
  assert.deepStrictEqual(first.points.map((point) => point.y), [0, 1, 2]);
  assert.strictEqual(typeof first.next_cursor, "string");
  const request = contract.validateObservationRequest({ stationId: binding.stationId, query: { start: binding.start, end: binding.end, cursor: first.next_cursor }, cursorSecret: "s".repeat(32) });
  assert.strictEqual(request.offset, 3);
  const second = contract.paginatePoints({ points, ...binding, offset: request.offset, pageSize: 3, cursorSecret: "s".repeat(32) });
  assert.deepStrictEqual(second.points.map((point) => point.y), [3, 4, 5]);
  for (const cursor of [first.next_cursor.slice(0, -1) + "x", "x".repeat(1025)])
    throwsCode(() => contract.validateObservationRequest({ stationId: binding.stationId, query: { start: binding.start, end: binding.end, cursor }, cursorSecret: "s".repeat(32) }), "invalid_request");
  throwsCode(() => contract.validateObservationRequest({ stationId: "station-2", query: { start: binding.start, end: binding.end, cursor: first.next_cursor }, cursorSecret: "s".repeat(32) }), "invalid_request");
});

test("raw record envelopes enforce exact bounded JSON arrays", () => {
  assert.deepStrictEqual(contract.extractRecordEnvelope({ data: denseRecords() }, { maxBytes: 100000, maxRecords: 100 }), denseRecords());
  assert.deepStrictEqual(contract.extractRecordEnvelope(page(denseRecords(), 100, 30, 1), { maxBytes: 100000, maxRecords: 100 }), denseRecords());
  for (const payload of [{ records: [] }, { data: [], token: "x" }, { data: "[]" }, { data: Array(101).fill({}) }])
    throwsCode(() => contract.extractRecordEnvelope(payload, { maxBytes: 100000, maxRecords: 100 }), "source_contract_invalid");
  for (const payload of [
    page(denseRecords(), 20, 30, 2),
    { data: denseRecords(), meta: { count: 30, page: 1, pageSize: 100 } },
    { data: denseRecords(), meta: { count: 30, page: 1, pageSize: 100, totalPage: 1, extra: 0 } },
    { data: denseRecords(), meta: { count: 30, page: true, pageSize: 100, totalPage: 1 } },
  ]) throwsCode(() => contract.extractRecordEnvelope(payload, { maxBytes: 100000, maxRecords: 100 }), "source_contract_invalid");
  throwsCode(() => contract.extractRecordEnvelope({ data: denseRecords() }, { maxBytes: 5, maxRecords: 100 }), "payload_too_large");
});

test("NocoBase complete pages require exact meta and prove one-page completeness", () => {
  const rows = [{ station_id: "station-1" }];
  assert.deepStrictEqual(contract.extractNocoBasePage(page(rows, 1), { maxBytes: 1024, maxRows: 1, page: 1, pageSize: 1, mode: "complete" }), rows);
  for (const payload of [
    { data: rows },
    { data: rows, meta: { count: 1, page: 1, pageSize: 1 } },
    { data: rows, meta: { count: 1, page: 1, pageSize: 1, totalPage: 1, extra: 0 } },
    { data: rows, meta: { count: true, page: 1, pageSize: 1, totalPage: 1 } },
    { data: rows, meta: { count: 1, page: 1.5, pageSize: 1, totalPage: 1 } },
    { data: rows, meta: { count: 1, page: 1, pageSize: 2, totalPage: 1 } },
    { data: Array(1000).fill({}), meta: { count: 2016, page: 1, pageSize: 1000, totalPage: 3 } },
  ]) throwsCode(() => contract.extractNocoBasePage(payload, { maxBytes: 100000, maxRows: 2016, page: 1, pageSize: payload.meta?.pageSize === 1000 ? 1000 : 1, mode: "complete" }), "dashboard_contract_invalid");
});

test("bounded newest-batches page permits later pages only when the head page is exactly full", () => {
  const rows = makeBatches(7);
  assert.deepStrictEqual(contract.extractNocoBasePage(page(rows, 7, 10, 2), { maxBytes: 100000, maxRows: 7, page: 1, pageSize: 7, mode: "head" }), rows);
  for (const payload of [page(rows.slice(0, 6), 7, 10, 2), page(rows, 7, 10, 3), page(rows, 6, 10, 2)])
    throwsCode(() => contract.extractNocoBasePage(payload, { maxBytes: 100000, maxRows: 7, page: 1, pageSize: 7, mode: "head" }), "dashboard_contract_invalid");
});

test("flow boundary helpers reject query overrides and malformed acceptance envelopes", () => {
  assert.deepStrictEqual(contract.validateEmptyQuery({}), {});
  for (const query of [{ url: "https://attacker.invalid" }, { station_id: "station-2" }, Object.create({ token: "x" })])
    throwsCode(() => contract.validateEmptyQuery(query), "invalid_request");
  const inactive = { active: false, acceptance_run_id: null, window_start: null, window_end: null };
  assert.deepStrictEqual(contract.extractAcceptanceEnvelope({ data: inactive }, { maxBytes: 1024 }), inactive);
  for (const payload of [{ data: inactive, token: "x" }, { data: [inactive] }, { status: "ok", data: inactive }])
    throwsCode(() => contract.extractAcceptanceEnvelope(payload, { maxBytes: 1024 }), "source_contract_invalid");
});

test("configured HTTP targets are exact and never accept credentials, query, fragments, or redirects", () => {
  assert.strictEqual(contract.validateConfiguredUrl("https://ems.internal/source", "/source"), "https://ems.internal/source");
  for (const url of ["ftp://ems.internal/source", "https://u:p@ems.internal/source", "https://ems.internal/source?url=x", "https://ems.internal/source#x", "https://ems.internal/other"])
    throwsCode(() => contract.validateConfiguredUrl(url, "/source"), "source_not_configured");
});

test("dashboard authentication derives a station and rejects arbitrary station or target parameters", () => {
  assert.strictEqual(contract.validateDashboardAuthInput({ token: "opaque" }, undefined), "opaque");
  assert.strictEqual(contract.validateDashboardAuthInput({}, "Bearer opaque"), "opaque");
  for (const input of [
    [{ token: "opaque", station_id: "station-2" }, undefined],
    [{ token: "opaque", url: "https://attacker.invalid" }, undefined],
    [{ token: "opaque" }, "Bearer second"],
    [{ token: "opaque\r\nX-Injected: yes" }, undefined],
    [{}, undefined],
  ]) throwsCode(() => contract.validateDashboardAuthInput(...input), input[0].url || input[0].station_id ? "invalid_request" : "unauthorized");
  assert.strictEqual(contract.validateDashboardAuthorization({ authorized: true, station_id: "station-1" }), "station-1");
  throwsCode(() => contract.validateDashboardAuthorization({ authorized: true, station_id: "../station" }), "unauthorized");
});

test("fixed outbound messages remove method and URL overrides and disable redirects", () => {
  const message = { url: "https://attacker.invalid", requestUrl: "https://attacker.invalid", method: "DELETE", query: { token: "x" }, headers: { Host: "attacker.invalid" } };
  contract.fixedRequestMessage(message, { token: "configured-token" });
  assert.strictEqual(message.url, undefined);
  assert.strictEqual(message.requestUrl, undefined);
  assert.strictEqual(message.method, undefined);
  assert.strictEqual(message.query, undefined);
  assert.strictEqual(message.followRedirects, false);
  assert.deepStrictEqual(message.headers, { Authorization: "Bearer configured-token" });
});

test("dashboard actual windows use the validated forecast bucket for formal and rolling runs", () => {
  assert.deepStrictEqual(contract.deriveDashboardActualWindow({ latest: makeLatest(), stationId: "station-1" }), {
    start: "2026-08-24T01:00:00+08:00", end: "2026-08-25T01:00:00+08:00",
  });
  const rolling = makeLatest({ as_of: "2026-08-25T01:17:00+08:00", generated_at: "2026-08-25T01:17:08+08:00" }, "2026-08-25T01:15:00+08:00");
  assert.deepStrictEqual(contract.deriveDashboardActualWindow({ latest: rolling, stationId: "station-1" }), {
    start: "2026-08-24T01:15:00+08:00", end: "2026-08-25T01:15:00+08:00",
  });
  const unavailable = makeLatest({
    as_of: "2026-08-25T01:17:00+08:00",
    generated_at: "2026-08-25T01:17:08+08:00",
    status: "degraded",
    series_payload: [
      { unique_id: "station_total_load", unit: "kW", model_name: "none", status: "insufficient_history", points: [], fallback_reason: "history_under_7_days" },
      { unique_id: "storage_1_soc", unit: "%", model_name: "none", status: "insufficient_history", points: [], fallback_reason: "history_under_7_days" },
      { unique_id: "storage_2_soc", unit: "%", model_name: "none", status: "insufficient_history", points: [], fallback_reason: "history_under_7_days" },
    ],
  });
  assert.strictEqual(contract.deriveDashboardActualWindow({ latest: unavailable, stationId: "station-1" }).end, "2026-08-25T01:15:00+08:00");
  assert.strictEqual(contract.deriveDashboardActualWindow({ latest: null, stationId: "station-1", nowMs: Date.parse("2026-08-25T01:17:59+08:00") }).end, "2026-08-25T01:15:00+08:00");
  throwsCode(() => contract.deriveDashboardActualWindow({ latest: { ...makeLatest(), station_id: "station-2" }, stationId: "station-1" }), "dashboard_contract_invalid");
});

test("latest capture derives formal and rolling actual windows before constructing the batch query", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  const node = flow.find((item) => item.id === "dashboard_latest_capture");
  const run = (latest) => {
    const fn = vm.runInNewContext(`(function(msg,node,global,env){${node.func}\n})`);
    return fn(
      { statusCode: 200, payload: page([latest], 1), m3: { stationId: "station-1" } },
      {},
      { get: () => contract },
      { get: (key) => key === "M3_DASHBOARD_READ_TOKEN" ? "read-token" : undefined },
    );
  };
  assert.deepStrictEqual(run(makeLatest())[0].m3.actualWindow, { start: "2026-08-24T01:00:00+08:00", end: "2026-08-25T01:00:00+08:00" });
  const rolling = makeLatest({ as_of: "2026-08-25T01:17:00+08:00", generated_at: "2026-08-25T01:17:08+08:00" }, "2026-08-25T01:15:00+08:00");
  assert.deepStrictEqual(run(rolling)[0].m3.actualWindow, { start: "2026-08-24T01:15:00+08:00", end: "2026-08-25T01:15:00+08:00" });
  const malformed = run({ ...makeLatest(), station_id: "station-2" });
  assert.strictEqual(malformed[0], null);
  assert.strictEqual(malformed[1].statusCode, 502);
});

test("acceptance context uses exact active/inactive fields and a seven-day Shanghai window", () => {
  const active = { active: true, acceptance_run_id: "run-20260825", window_start: "2026-08-25T01:00:00+08:00", window_end: "2026-09-01T01:00:00+08:00" };
  assert.deepStrictEqual(contract.validateAcceptanceContext(active), active);
  const explicitZeroPrecision = { ...active, window_start: zeroFractionShanghai(active.window_start), window_end: zeroFractionShanghai(active.window_end, "0") };
  assert.deepStrictEqual(contract.validateAcceptanceContext(explicitZeroPrecision), explicitZeroPrecision);
  const inactive = { active: false, acceptance_run_id: null, window_start: null, window_end: null };
  assert.deepStrictEqual(contract.validateAcceptanceContext(inactive), inactive);
  for (const value of [
    { ...active, active: 1 }, { ...active, target_url: "https://attacker.invalid" }, { ...active, window_end: "2026-08-26T01:00:00+08:00" }, { ...inactive, acceptance_run_id: "run" },
  ]) throwsCode(() => contract.validateAcceptanceContext(value), "source_contract_invalid");
});

test("alert contract is exact and matches the Python client's fixed path and acknowledgement", () => {
  const alert = { station_id: "station-1", task: "forecast", error_code: "forecast_failed", at: "2026-08-25T01:17:00+08:00" };
  assert.deepStrictEqual(contract.validateAlertRequest(alert), alert);
  assert.deepStrictEqual(contract.validateAlertRequest({ ...alert, at: zeroFractionShanghai(alert.at, "000000") }), { ...alert, at: zeroFractionShanghai(alert.at, "000000") });
  assert.deepStrictEqual(contract.alertAcknowledgement(), { status: "ok" });
  for (const body of [
    { ...alert, url: "https://attacker.invalid" }, { ...alert, task: "forecast-now" }, { ...alert, error_code: "token=secret" }, { ...alert, at: "2026-08-24T17:17:00Z" }, { ...alert, at: "2026-08-25T01:17:00.000001+08:00" }, { ...alert, station_id: "../station" },
  ]) throwsCode(() => contract.validateAlertRequest(body), "invalid_request");
  const python = fs.readFileSync(ALERT_CLIENT_PATH, "utf8");
  assert.ok(python.includes('ALERT_PATH = "/internal/energy-forecast/v1/alerts"'));
  assert.ok(python.includes('payload != {"status": "ok"}'));
  assert.deepStrictEqual(Object.keys(alert), ["station_id", "task", "error_code", "at"]);
});

test("dashboard preserves actual gaps and data_time/target_time while exposing an exact 48-hour range", () => {
  const dashboard = contract.buildDashboard({ latest: makeLatest(), actuals: makeActuals(), acceptance: makeAcceptance(), nowMs: Date.parse("2026-08-25T01:10:00+08:00") });
  assert.strictEqual(dashboard.operation, "forecast_dashboard");
  assert.deepStrictEqual(dashboard.range, {
    timezone: "Asia/Shanghai",
    history_start: "2026-08-24T01:00:00+08:00",
    history_end: "2026-08-25T01:00:00+08:00",
    actual_latest: "2026-08-25T00:45:00+08:00",
    forecast_start: "2026-08-25T01:00:00+08:00",
    forecast_end: "2026-08-26T01:00:00+08:00",
    interval_seconds: 900,
    history_hours: 24,
    forecast_hours: 24,
    display_hours: 48,
    now_separator: "2026-08-25T01:10:00+08:00",
  });
  assert.strictEqual(dashboard.series.length, 3);
  assert.deepStrictEqual(dashboard.series[0].actual[1], { data_time: "2026-08-25T00:45:00+08:00", value: null, quality: "invalid", source_revision: 3 });
  assert.deepStrictEqual(dashboard.series[0].forecast[0], { data_time: "2026-08-25T01:00:00+08:00", target_time: "2026-08-25T01:15:00+08:00", value: 700, raw_value: 700, is_clipped: false });
  assert.deepStrictEqual(dashboard.acceptance, makeAcceptance());
  assert.strictEqual(JSON.stringify(dashboard).includes("content_hash"), false);
});

test("dashboard sorts/dedupes actuals, rejects conflicts, and never exposes a fourth series", () => {
  const actuals = [makeActuals()[2], ...makeActuals(), { ...makeActuals()[0] }];
  const dashboard = contract.buildDashboard({ latest: makeLatest(), actuals, acceptance: makeAcceptance(), nowMs: Date.parse("2026-08-25T01:10:00+08:00") });
  assert.deepStrictEqual(dashboard.series.map((item) => item.unique_id), ["station_total_load", "storage_1_soc", "storage_2_soc"]);
  assert.strictEqual(dashboard.series[0].actual.length, 2);
  const revised = [...makeActuals(), { ...makeActuals()[0], y: 792, source_revision: 4 }];
  const revisedDashboard = contract.buildDashboard({ latest: makeLatest(), actuals: revised, acceptance: makeAcceptance() });
  assert.strictEqual(revisedDashboard.series[0].actual[0].value, 792);
  const conflicting = [...makeActuals(), { ...makeActuals()[0], y: 791 }];
  throwsCode(() => contract.buildDashboard({ latest: makeLatest(), actuals: conflicting, acceptance: makeAcceptance() }), "dashboard_contract_invalid");
  throwsCode(() => contract.buildDashboard({ latest: makeLatest({ series_payload: [...makeLatest().series_payload, { ...makeLatest().series_payload[0], unique_id: "other" }] }), actuals: [], acceptance: makeAcceptance() }), "dashboard_contract_invalid");
});

test("dashboard actual aliases select the higher revision and canonicalize output in either input order", () => {
  const lowerAlias = { ...makeActuals()[0], ds: "2026-08-25T00:30:00.000000+08:00", y: 789, source_revision: 1 };
  const higherCanonical = { ...makeActuals()[0], y: 792, source_revision: 4 };
  const expected = [{ data_time: "2026-08-25T00:30:00+08:00", value: 792, quality: "valid", source_revision: 4 }];
  for (const actuals of [[lowerAlias, higherCanonical], [higherCanonical, lowerAlias]]) {
    const dashboard = contract.buildDashboard({ latest: makeLatest(), actuals, acceptance: makeAcceptance() });
    assert.deepStrictEqual(dashboard.series[0].actual, expected);
    assert.strictEqual(dashboard.range.actual_latest, "2026-08-25T00:30:00+08:00");
  }
});

test("dashboard equal-revision semantically identical actual aliases collapse in either input order", () => {
  const canonical = makeActuals()[0];
  const zeroFractionAlias = { ...canonical, ds: "2026-08-25T00:30:00.000+08:00" };
  const expected = [{ data_time: "2026-08-25T00:30:00+08:00", value: 790, quality: "valid", source_revision: 2 }];
  for (const actuals of [[canonical, zeroFractionAlias], [zeroFractionAlias, canonical]]) {
    const dashboard = contract.buildDashboard({ latest: makeLatest(), actuals, acceptance: makeAcceptance() });
    assert.deepStrictEqual(dashboard.series[0].actual, expected);
  }
});

test("dashboard equal-revision actual aliases with different value or quality fail in either input order", () => {
  const canonical = makeActuals()[0];
  const valueConflict = { ...canonical, ds: "2026-08-25T00:30:00.000000+08:00", y: 791 };
  const qualityConflict = { ...canonical, ds: "2026-08-25T00:30:00.0+08:00", y: null, quality: "invalid" };
  for (const conflict of [valueConflict, qualityConflict]) {
    for (const actuals of [[canonical, conflict], [conflict, canonical]])
      throwsCode(() => contract.buildDashboard({ latest: makeLatest(), actuals, acceptance: makeAcceptance() }), "dashboard_contract_invalid");
  }
});

test("dashboard requires every predictable series to share the same as_of-aligned horizon", () => {
  const latest = makeLatest();
  latest.series_payload[1].points = makeForecastPoints("2026-08-25T01:15:00+08:00", "storage_1_soc");
  throwsCode(() => contract.buildDashboard({ latest, actuals: [], acceptance: makeAcceptance() }), "dashboard_contract_invalid");
  throwsCode(() => contract.buildDashboard({ latest: makeLatest({ as_of: "2026-08-25T01:17:00+08:00" }), actuals: [], acceptance: makeAcceptance() }), "dashboard_contract_invalid");
});

test("NocoBase aware instants accept equivalent offsets and public latest timestamps canonicalize to Shanghai", () => {
  const representations = [
    (value) => zeroFractionShanghai(value),
    (value) => utcTimestamp(value),
    (value) => zeroOffsetTimestamp(value, "000"),
  ];
  const latest = rewriteLatestInstants(makeLatest(), (value, seriesIndex) => representations[seriesIndex](value));
  latest.as_of = zeroFractionShanghai(makeLatest().as_of, "000000");
  const sourceActual = { ...makeActuals()[0], ds: zeroFractionShanghai(makeActuals()[0].ds) };
  const dashboard = contract.buildDashboard({
    latest,
    actuals: [sourceActual, ...makeActuals().slice(1)],
    acceptance: makeAcceptance(),
    stationId: "station-1",
    nowMs: Date.parse("2026-08-25T01:10:00+08:00"),
  });
  assert.strictEqual(dashboard.system.generated_at, "2026-08-25T01:02:08.123+08:00");
  assert.strictEqual(dashboard.range.forecast_start, "2026-08-25T01:00:00+08:00");
  assert.strictEqual(dashboard.range.forecast_end, "2026-08-26T01:00:00+08:00");
  assert.strictEqual(dashboard.series[0].actual[0].data_time, "2026-08-25T00:30:00+08:00");
  for (const series of dashboard.series) {
    assert.strictEqual(series.forecast[0].data_time, "2026-08-25T01:00:00+08:00");
    assert.strictEqual(series.forecast[0].target_time, "2026-08-25T01:15:00+08:00");
    assert.ok(series.forecast.every((point) => point.data_time.endsWith("+08:00") && point.target_time.endsWith("+08:00")));
  }
  assert.deepStrictEqual(contract.deriveDashboardActualWindow({ latest, stationId: "station-1" }), {
    start: "2026-08-24T01:00:00+08:00", end: "2026-08-25T01:00:00+08:00",
  });
});

test("NocoBase aware instants reject naive invalid-offset and fractional formal or quarter values", () => {
  const cases = [
    makeLatest({ generated_at: "2026-08-25T01:02:08" }),
    makeLatest({ updated_at: "2026-02-30T17:02:09Z" }),
    makeLatest({ as_of: "2026-08-24T17:02:00+14:01" }),
    makeLatest({ as_of: "2026-08-24T17:02:00.000001Z" }),
    makeLatest({ source_data_end: "2026-08-24T16:45:00.000001Z" }),
  ];
  const fractionalPoint = rewriteLatestInstants(makeLatest(), (value) => utcTimestamp(value));
  fractionalPoint.series_payload[0].points[0].data_time = "2026-08-24T17:00:00.000001Z";
  cases.push(fractionalPoint);
  for (const latest of cases)
    throwsCode(() => contract.buildDashboard({ latest, actuals: [], acceptance: makeAcceptance(), stationId: "station-1" }), "dashboard_contract_invalid");
});

test("dashboard fails closed on malformed or coerced latest, actual, and acceptance values", () => {
  const cases = [
    { latest: makeLatest({ station_id: "station-2" }), actuals: [], acceptance: makeAcceptance(), stationId: "station-1" },
    { latest: makeLatest({ generated_at: "2026-08-25T01:02:08" }), actuals: [], acceptance: makeAcceptance() },
    { latest: makeLatest({ status: true }), actuals: [], acceptance: makeAcceptance() },
    { latest: makeLatest(), actuals: [{ ...makeActuals()[0], y: "790" }], acceptance: makeAcceptance() },
    { latest: makeLatest(), actuals: [], acceptance: { ...makeAcceptance(), completed_days: 1.5 } },
  ];
  for (const value of cases) throwsCode(() => contract.buildDashboard(value), "dashboard_contract_invalid");
});

test("dashboard maps ready, degraded, stale, initializing, and error states without mock data", () => {
  const ready = contract.buildDashboard({ latest: makeLatest(), actuals: [], acceptance: makeAcceptance(), nowMs: Date.parse("2026-08-25T01:10:00+08:00") });
  assert.deepStrictEqual(ready.system, { state: "ready", mode: "normal", generated_at: "2026-08-25T01:02:08+08:00", stale: false });
  const degraded = contract.buildDashboard({ latest: makeLatest({ status: "degraded" }), actuals: [], acceptance: makeAcceptance(), nowMs: Date.parse("2026-08-25T01:10:00+08:00") });
  assert.strictEqual(degraded.system.state, "degraded");
  const stale = contract.buildDashboard({ latest: makeLatest(), actuals: [], acceptance: makeAcceptance(), nowMs: Date.parse("2026-08-25T02:00:00+08:00") });
  assert.strictEqual(stale.system.state, "stale");
  const initializing = contract.buildDashboard({ latest: null, actuals: [], acceptance: null, nowMs: Date.parse("2026-08-25T01:10:00+08:00"), stationId: "station-1", emptyState: "initializing" });
  assert.strictEqual(initializing.system.state, "initializing");
  assert.strictEqual(initializing.range.actual_latest, null);
  assert.strictEqual(initializing.series.length, 3);
  assert.ok(initializing.series.every((item) => item.actual.length === 0 && item.forecast.length === 0));
  const error = contract.buildDashboard({ latest: null, actuals: [], acceptance: null, stationId: "station-1", emptyState: "error" });
  assert.strictEqual(error.system.state, "error");
});

test("one through seven complete days without evaluations expose exact in-progress state and no invented rows", () => {
  for (const completedDays of [1, 2, 3, 4, 5, 6, 7]) {
    const batches = makeBatches(completedDays);
    const summary = contract.buildAcceptanceSummary({ stationId: "station-1", batches, points: makeAcceptancePoints(batches), evaluations: [] });
    assert.deepStrictEqual(summary, { acceptance_run_id: "run-20260825", status: "in_progress", completed_days: completedDays, expected_days: 7, results: [] });
  }
  throwsCode(() => contract.buildDashboard({ latest: makeLatest(), actuals: [], acceptance: { ...makeAcceptance(), status: "in_progress" } }), "dashboard_contract_invalid");
  throwsCode(() => contract.buildDashboard({ latest: makeLatest(), actuals: [], acceptance: { ...makeAcceptance(), results: [] } }), "dashboard_contract_invalid");
});

test("final evaluations require exact three series plus overall with Task9 count and outcome semantics", () => {
  const batches = makeBatches(7);
  const points = makeAcceptancePoints(batches);
  const summary = contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations: makeEvaluationRows() });
  assert.deepStrictEqual(summary, makeAcceptance());
  for (const evaluations of [
    makeEvaluationRows().slice(0, 3),
    makeEvaluationRows().slice(0, 2),
    [...makeEvaluationRows(), makeEvaluationRows()[0]],
    makeEvaluationRows().map((row) => row.evaluation_key === "overall" ? { ...row, expected_count: 672 } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "overall" ? { ...row, valid_count: row.valid_count - 1 } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "overall" ? { ...row, mape_percent: 0 } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "station_total_load" ? { ...row, valid_count: 670, zero_actual_count: 3 } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "station_total_load" ? { ...row, mape_percent: null } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "station_total_load" ? { ...row, outcome: "failed" } : row),
    makeEvaluationRows().map((row) => row.evaluation_key === "station_total_load" ? { ...row, acceptance_run_id: "other-run" } : row),
  ]) throwsCode(() => contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations }), "dashboard_contract_invalid");

  const precedenceRows = makeEvaluationRows(["failed", "insufficient_data", "passed"], "insufficient_data");
  precedenceRows[1] = { ...precedenceRows[1], valid_count: 604 };
  precedenceRows[3] = {
    ...precedenceRows[3],
    valid_count: precedenceRows.slice(0, 3).reduce((sum, row) => sum + row.valid_count, 0),
  };
  const precedence = contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations: precedenceRows });
  assert.strictEqual(precedence.status, "insufficient_data");
  throwsCode(() => contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations: precedenceRows.map((row) => row.evaluation_key === "overall" ? { ...row, outcome: "failed" } : row) }), "dashboard_contract_invalid");
});

test("formal batches are validated before run selection and form one continuous local daily topology", () => {
  const batches = makeBatches(7);
  const selected = contract.selectAcceptanceBatches(batches, "station-1");
  assert.strictEqual(selected.runId, "run-20260825");
  assert.deepStrictEqual(selected.batches, batches);
  for (const malformed of [
    batches.map((row, index) => index === 0 ? (({ acceptance_run_id, ...rest }) => rest)(row) : row),
    batches.map((row, index) => index === 0 ? { ...row, issued_at: "2026-08-31T01:03:00+08:00" } : row),
    batches.map((row, index) => index === 0 ? { ...row, issued_at: "2026-08-31T01:02:00.000001+08:00" } : row),
    batches.map((row, index) => index === 3 ? { ...row, issued_at: shanghai(Date.parse(row.issued_at) - 86400000) } : row),
    [batches[1], batches[0], ...batches.slice(2)],
    batches.map((row, index) => index === 2 ? { ...row, station_id: "station-2" } : row),
  ]) throwsCode(() => contract.selectAcceptanceBatches(malformed, "station-1"), "dashboard_contract_invalid");
  const prior = makeBatches(2, "older-run").map((row, index) => ({ ...row, id: 20 + index, issued_at: shanghai(Date.parse("2026-08-23T01:02:00+08:00") - index * 86400000) }));
  assert.strictEqual(contract.selectAcceptanceBatches([...batches.slice(0, 3), ...prior], "station-1").batches.length, 3);
});

test("formal NocoBase batches accept zero precision and equivalent offsets by Shanghai instant", () => {
  const batches = makeBatches(2).map((batch, index) => ({
    ...batch,
    issued_at: index === 0 ? utcTimestamp(batch.issued_at, "000") : zeroOffsetTimestamp(batch.issued_at),
  }));
  const points = makeAcceptancePoints(batches).map((point) => ({
    ...point,
    data_time: point.unique_id === "station_total_load"
      ? utcTimestamp(point.data_time, "000")
      : point.unique_id === "storage_1_soc" ? zeroOffsetTimestamp(point.data_time) : zeroFractionShanghai(point.data_time),
  }));
  assert.strictEqual(contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations: [] }).completed_days, 2);

  const localZero = makeBatches(1).map((batch) => ({ ...batch, issued_at: zeroFractionShanghai(batch.issued_at, "000000") }));
  const localZeroPoints = makeAcceptancePoints(localZero).map((point) => ({ ...point, data_time: zeroFractionShanghai(point.data_time, "0") }));
  assert.strictEqual(contract.buildAcceptanceSummary({ stationId: "station-1", batches: localZero, points: localZeroPoints, evaluations: [] }).completed_days, 1);

  for (const issued_at of ["2026-08-24T17:02:00.000001Z", "2026-08-24T17:03:00Z", "2026-08-25T01:02:00"])
    throwsCode(() => contract.selectAcceptanceBatches([{ ...makeBatches(1)[0], issued_at }], "station-1"), "dashboard_contract_invalid");
  const badPoint = makeAcceptancePoints(makeBatches(1));
  badPoint[0] = { ...badPoint[0], data_time: "2026-08-24T17:00:00.000001Z" };
  throwsCode(() => contract.buildAcceptanceSummary({ stationId: "station-1", batches: makeBatches(1), points: badPoint, evaluations: [] }), "dashboard_contract_invalid");
});

test("each formal 3x96 point topology anchors at issued_at minus two minutes and joins adjacent days", () => {
  const batches = makeBatches(2);
  const points = makeAcceptancePoints(batches);
  assert.strictEqual(contract.buildAcceptanceSummary({ stationId: "station-1", batches, points, evaluations: [] }).completed_days, 2);
  const shifted = points.map((point, index) => index === 0 ? { ...point, data_time: shanghai(Date.parse(point.data_time) + 900000) } : point);
  throwsCode(() => contract.buildAcceptanceSummary({ stationId: "station-1", batches, points: shifted, evaluations: [] }), "dashboard_contract_invalid");
  const missing = points.slice(1);
  throwsCode(() => contract.buildAcceptanceSummary({ stationId: "station-1", batches, points: missing, evaluations: [] }), "dashboard_contract_invalid");
});

test("each fixed points response proves it contains only its server-selected series", () => {
  const batch = makeBatches(1)[0];
  const rows = makeAcceptancePoints([batch]).filter((row) => row.unique_id === "station_total_load");
  assert.strictEqual(contract.validateAcceptancePointSeriesPage(rows, "station_total_load"), rows);
  throwsCode(() => contract.validateAcceptancePointSeriesPage([{ ...rows[0], unique_id: "storage_1_soc" }], "station_total_load"), "dashboard_contract_invalid");
  throwsCode(() => contract.validateAcceptancePointSeriesPage([{ ...rows[0], extra: true }], "station_total_load"), "dashboard_contract_invalid");
});

test("safe failures use generic bounded envelopes and clear sensitive routing fields", () => {
  const msg = { url: "https://attacker.invalid", headers: { Authorization: "Bearer secret" }, payload: { secret: true }, req: {} };
  const out = contract.toErrorMessage(msg, Object.assign(new Error("secret internal URL"), { code: "unauthorized" }));
  assert.strictEqual(out.statusCode, 401);
  assert.deepStrictEqual(out.payload, { status: "error", error: { code: "unauthorized", message: "request rejected" } });
  assert.strictEqual(out.url, undefined);
  assert.deepStrictEqual(out.headers, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  assert.strictEqual(JSON.stringify(out).includes("secret internal"), false);
});

test("flow is valid, secret-free, four-route HTTP-only graph with no Worker trigger or dynamic target", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  assert.ok(Array.isArray(flow));
  const httpIn = flow.filter((node) => node.type === "http in");
  assert.deepStrictEqual(httpIn.map((node) => `${node.method.toUpperCase()} ${node.url}`).sort(), [
    "GET /energy-forecast-api",
    "GET /internal/energy-forecast/v1/stations/:station_id/acceptance-context",
    "GET /internal/energy-forecast/v1/stations/:station_id/observations",
    "POST /internal/energy-forecast/v1/alerts",
  ]);
  const forbiddenTypes = new Set(["inject", "exec", "cron", "cronplus", "command"]);
  assert.deepStrictEqual(flow.filter((node) => forbiddenTypes.has(node.type)), []);
  const serialized = JSON.stringify(flow);
  for (const forbidden of ["/runs/forecast", "/runs/model-selection", "/bootstrap", "source-secret", "dashboard-secret", "admin-secret"])
    assert.strictEqual(serialized.includes(forbidden), false, forbidden);
  for (const node of flow.filter((item) => item.type === "http request")) {
    assert.ok(typeof node.url === "string" && node.url.startsWith("${M3_"), `${node.name} fixed env URL`);
    assert.strictEqual(node.followRedirects, false, `${node.name} redirects`);
    assert.notStrictEqual(node.url, "");
    assert.strictEqual(node.url.includes("{{{url"), false);
  }
});

test("every route reaches an HTTP response and internal auth precedes station or body use", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  const byId = new Map(flow.map((node) => [node.id, node]));
  function reachable(startId) {
    const seen = new Set([startId]);
    const queue = [startId];
    while (queue.length) {
      const node = byId.get(queue.shift());
      for (const wire of (node.wires || []).flat()) if (!seen.has(wire)) { seen.add(wire); queue.push(wire); }
    }
    return [...seen].map((id) => byId.get(id)).filter(Boolean);
  }
  for (const input of flow.filter((node) => node.type === "http in")) {
    const nodes = reachable(input.id);
    assert.ok(nodes.some((node) => node.type === "http response"), `${input.url} response`);
    const first = byId.get((input.wires || []).flat()[0]);
    assert.strictEqual(first.type, "function");
    assert.ok(first.func.includes("authorize"), `${input.url} auth first`);
  }
});

test("flow function code compiles and uses only fixed allowlisted collections/actions", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  for (const node of flow.filter((item) => item.type === "function"))
    new vm.Script(`(function(msg,node,global,env){${node.func}\n})`, { filename: node.name });
  const requestUrls = flow.filter((node) => node.type === "http request").map((node) => node.url);
  const allowedFragments = [
    "M3_SOURCE_RECORDS_URL", "M3_ACCEPTANCE_CONTEXT_URL", "M3_DASHBOARD_AUTH_URL", "M3_ALERT_SINK_URL",
    "/api/energy_forecast_latest:list", "/api/energy_forecast_batches:list", "/api/energy_forecast_points:list", "/api/energy_forecast_evaluations:list",
  ];
  for (const url of requestUrls) assert.ok(allowedFragments.some((fragment) => url.includes(fragment)), url);
});

test("dashboard validates latest and batch identity before constructing downstream filters", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  const latest = flow.find((node) => node.id === "dashboard_latest_capture").func;
  assert.ok(latest.indexOf("deriveDashboardActualWindow") >= 0);
  assert.ok(latest.indexOf("deriveDashboardActualWindow") < latest.indexOf("batchFilter"));
  const batches = flow.find((node) => node.id === "dashboard_batches_capture").func;
  assert.ok(batches.indexOf("selectAcceptanceBatches") >= 0);
  assert.ok(batches.indexOf("selectAcceptanceBatches") < batches.indexOf("pointFilter"));
  assert.strictEqual(batches.includes("rows[0].acceptance_run_id"), false);
  throwsCode(() => contract.selectAcceptanceBatches([{ id: 1, station_id: "station-1", issued_at: "2026-08-25T01:02:00+08:00", write_state: "complete" }], "station-1"), "dashboard_contract_invalid");
});

test("dashboard points use three fixed one-page series queries and never request 2016 rows at once", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  const requests = flow.filter((node) => node.type === "http request" && node.url.includes("/api/energy_forecast_points:list"));
  assert.strictEqual(requests.length, 3);
  const expected = ["station_total_load", "storage_1_soc", "storage_2_soc"];
  assert.deepStrictEqual(requests.map((node) => node.url.match(/unique_id%22%3A%22([^%]+)%22/)[1]), expected);
  for (const node of requests) {
    assert.ok(node.url.includes("pageSize=672"));
    assert.strictEqual(node.url.includes("pageSize=2016"), false);
    assert.strictEqual(node.url.includes("{{{m3.uniqueId"), false);
  }
  const byId = new Map(flow.map((node) => [node.id, node]));
  assert.deepStrictEqual(byId.get("dashboard_load_points_request").wires, [["dashboard_load_points_capture"]]);
  assert.deepStrictEqual(byId.get("dashboard_load_points_capture").wires[0], ["dashboard_soc1_points_request"]);
  assert.deepStrictEqual(byId.get("dashboard_soc1_points_capture").wires[0], ["dashboard_soc2_points_request"]);
  assert.deepStrictEqual(byId.get("dashboard_soc2_points_capture").wires[0], ["dashboard_evaluations_request"]);
  for (const [id, uniqueId] of [
    ["dashboard_load_points_capture", "station_total_load"],
    ["dashboard_soc1_points_capture", "storage_1_soc"],
    ["dashboard_soc2_points_capture", "storage_2_soc"],
  ]) {
    assert.ok(byId.get(id).func.includes(`validateAcceptancePointSeriesPage`));
    assert.ok(byId.get(id).func.includes(`\"${uniqueId}\"`));
  }
});

test("every NocoBase capture uses strict pagination and raw EMS windows reject paginated providers", () => {
  const flow = JSON.parse(fs.readFileSync(FLOW_PATH, "utf8"));
  const captureIds = ["dashboard_latest_capture", "dashboard_batches_capture", "dashboard_load_points_capture", "dashboard_soc1_points_capture", "dashboard_soc2_points_capture", "dashboard_evaluations_capture"];
  for (const id of captureIds) assert.ok(flow.find((node) => node.id === id).func.includes("extractNocoBasePage"), id);
  assert.strictEqual(JSON.stringify(flow).includes("extractNocoBaseRows"), false);
  assert.ok(flow.find((node) => node.id === "dashboard_evaluations_capture").func.includes("actualWindow"));
  assert.strictEqual(flow.find((node) => node.id === "dashboard_evaluations_capture").func.includes("Date.parse(msg.m3.latest.as_of)"), false);
});

test("Task11 Dashboard points ACL exactly matches the fixed flow with no remaining logical delta", () => {
  const nocobase = JSON.parse(fs.readFileSync(NOCOBASE_CONTRACT_PATH, "utf8"));
  const configured = nocobase.dashboard_role.collections.energy_forecast_points.fields_by_action.list.filter;
  const required = ["batch.station_id", "batch.acceptance_run_id", "batch.write_state", "unique_id"];
  assert.deepStrictEqual(configured, required);
  assert.deepStrictEqual(required.filter((field) => !configured.includes(field)), []);
  assert.deepStrictEqual(configured.filter((field) => !required.includes(field)), []);
});

(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      process.stdout.write(`ok - ${name}\n`);
    } catch (error) {
      failed += 1;
      process.stderr.write(`not ok - ${name}\n${error.stack}\n`);
    }
  }
  process.stdout.write(`${tests.length - failed}/${tests.length} tests passed\n`);
  if (failed) process.exitCode = 1;
})();
