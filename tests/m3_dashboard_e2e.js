"use strict";

const assert = require("assert");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { once } = require("events");
const { spawnSync } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const HTML_PATH = path.join(ROOT, "m3", "node_red", "m3_production_gateway_page.html");
const PYTHON = process.env.M3_TEST_PYTHON || path.join(ROOT, ".venv", "bin", "python");
const API_PATH = "/energy-forecast-api";
const SERIES_IDS = ["station_total_load", "storage_soc"];
let browser;
let server;

const clone = (value) => JSON.parse(JSON.stringify(value));

function task5Fixtures() {
  const script = [
    "from copy import deepcopy",
    "import json",
    "from tests.test_m3_live_dashboard import STATION_1, STATION_2, STATIONS, GENERATED_AT, station_result",
    "from m3_worker.dashboard_contracts import DashboardEnvelope",
    "from m3_worker.services.live_dashboard_service import DashboardCache, build_dashboard_payload",
    "base = build_dashboard_payload([station_result(STATION_1), station_result(STATION_2)], generated_at=GENERATED_AT).model_dump(mode='python')",
    "ready = deepcopy(base)",
    "ready['data']['system'].update(state='ready', healthy_station_count=2)",
    "first_values = ((410.0, 61.0), (730.0, 54.0))",
    "for station_index, station in enumerate(ready['data']['stations']):",
    "    station['system'].update(state='ready', mode='normal', stale=False)",
    "    station['readiness'] = {'required_days':28,'available_days':28.0,'remaining_days':0.0}",
    "    for series_index, series in enumerate(station['series']):",
    "        series.update(status='ok', fallback_reason=None)",
    "        series['forecast'][0].update(value=first_values[station_index][series_index], raw_value=first_values[station_index][series_index], is_clipped=False)",
    "ready['data']['stations'][0]['acceptance'] = {'acceptance_run_id':'run-station-1','status':'passed','completed_days':7,'expected_days':7,'results':[{'unique_id':uid,'expected_count':672,'valid_count':660,'zero_actual_count':3,'mape_percent':2.5,'mae':1.25,'smape_percent':2.6,'wape_percent':2.4,'median_ape_percent':1.9,'p90_ape_percent':5.2,'outcome':'passed'} for uid in ('station_total_load','storage_soc')]} ",
    "ready['data']['stations'][1]['acceptance'] = {'acceptance_run_id':'run-station-2','status':'in_progress','completed_days':3,'expected_days':7,'results':[]}",
    "initializing_normal = deepcopy(base)",
    "initializing_normal['data']['stations'][0]['system']['mode'] = 'normal'",
    "initializing_normal['data']['stations'][0]['readiness'] = {'required_days':28,'available_days':22.5,'remaining_days':5.5}",
    "initializing_normal['data']['stations'][1]['readiness'] = {'required_days':28,'available_days':24.0,'remaining_days':4.0}",
    "degraded = deepcopy(ready)",
    "degraded['data']['stations'][0]['system'].update(state='degraded', mode='degraded')",
    "degraded['data']['system'].update(state='degraded', healthy_station_count=1)",
    "stale = deepcopy(ready)",
    "stale['data']['stations'][0]['system'].update(state='stale', mode='normal', stale=True)",
    "stale['data']['system'].update(state='stale', healthy_station_count=1)",
    "error = deepcopy(ready)",
    "error_station = error['data']['stations'][0]",
    "for key in ('history_start','history_end','actual_latest','forecast_start','forecast_end'): error_station['range'][key] = None",
    "error_station['system'].update(state='error', mode='error', generated_at=None, stale=False)",
    "for series in error_station['series']: series.update(model_name=None, status='error', fallback_reason='station_unavailable', actual=[], forecast=[])",
    "error_station['acceptance'] = None",
    "error_station['readiness'] = None",
    "error['data']['system'].update(state='degraded', healthy_station_count=1)",
    "fixtures = {name: json.loads(DashboardEnvelope.model_validate(value).model_dump_json()) for name, value in {'ready':ready,'initializing_normal':initializing_normal,'degraded':degraded,'stale':stale,'error':error}.items()}",
    "microsecond_now = GENERATED_AT.replace(microsecond=123456)",
    "full_cache = DashboardCache(STATIONS)",
    "full_cache.publish_station(station_result(STATION_1))",
    "full_cache.publish_station(station_result(STATION_2))",
    "fixtures['microsecond_snapshot'] = json.loads(full_cache.snapshot(microsecond_now).model_dump_json())",
    "empty_cache = DashboardCache(STATIONS)",
    "empty_cache.mark_station_failure(STATION_1)",
    "fixtures['microsecond_empty_error'] = json.loads(empty_cache.snapshot(microsecond_now).model_dump_json())",
    "print(json.dumps(fixtures, ensure_ascii=False))",
  ].join("\n");
  const result = spawnSync(PYTHON, ["-c", script], { cwd: ROOT, encoding: "utf8" });
  assert.strictEqual(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

async function assertRejected(page, payload, label) {
  await page.evaluate((data) => window.renderDashboard(data), payload.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "error", label);
  assert.strictEqual(await page.locator("#error-state").isVisible(), true, label);
}

function pathCoordinates(pathData) {
  return [...pathData.matchAll(/[ML]\s+([\d.]+)\s+([\d.]+)/g)].map((match) => ({ x: Number(match[1]), y: Number(match[2]) }));
}

async function invalidSvgLineCoordinates(page) {
  return page.locator("svg line").evaluateAll((lines) => lines.flatMap((line) => ["x1", "x2", "y1", "y2"].flatMap((name) => {
    const value = line.getAttribute(name);
    return value === null || !Number.isFinite(Number(value))
      ? [{ chart: line.closest("svg")?.getAttribute("class") ?? null, name, value }]
      : [];
  })));
}

async function inlineBarPercent(page, selector) {
  return page.locator(selector).evaluate((node) => {
    if (!node.style.width.endsWith("%")) throw new TypeError("bar width must be a percentage");
    const value = Number.parseFloat(node.style.width);
    if (!Number.isFinite(value)) throw new TypeError("bar width must be finite");
    return value;
  });
}

function weeklyEvidenceFixture() {
  const forecastStart = "2026-08-31T12:15:00+08:00";
  const firstTargetMs = Date.parse(forecastStart);
  const toUtc = (offset) => new Date(firstTargetMs + offset * 900_000).toISOString().replace(".000Z", "Z");
  const run = {
    run_id: "weekly-evidence-run",
    station_id: "plant-alpha-ES01",
    status: "succeeded",
    history_start: "2026-08-02T12:15:00+08:00",
    history_end: forecastStart,
    history_days: 29,
    forecast_start: forecastStart,
    forecast_end: "2026-09-01T12:15:00+08:00",
    forecast_days: 1,
    interval_seconds: 900,
    points_per_day: 96,
    expected_points_per_series: 96,
    model_policy: "full_selection",
    model_manifest: {
      selection_policy: "weekly_load_v2",
      model_policy: "full_selection",
      interval_seconds: 900,
      daily_season_length: 96,
      weekly_season_length: 672,
      series: {
        station_total_load: {
          model_name: "WeeklyRegimeAdjusted",
          realized_model_name: "WeeklyRegimeAdjusted",
          realized_status: "ok",
          realized_fallback_reason: null,
          selection_metric: "wape_percent",
          selection_reason: null,
          selection_status: null,
          candidate_scores: [
            { model_name: "WeeklyNaive", wape_percent: 12.5, mae: 10, mape_percent: 13, scorable_point_count: 96, skip_reason: null },
            { model_name: "WeeklyWeighted2", wape_percent: 10, mae: 8, mape_percent: 11, scorable_point_count: 96, skip_reason: null },
            { model_name: "WeeklyRegimeAdjusted", wape_percent: 8, mae: 7, mape_percent: 9, scorable_point_count: 96, skip_reason: null },
            { model_name: "WeeklyMedian3", wape_percent: null, mae: null, mape_percent: null, scorable_point_count: 0, skip_reason: "no_scorable_points" },
          ],
          training_start: "2026-08-02T12:15:00+08:00",
          training_end: forecastStart,
          statsforecast_version: "1.0.0",
        },
        storage_soc: {
          model_name: "SOCWeeklyDelta",
          cv_mape_percent: null,
          selected_at: "2026-08-31T12:15:00+08:00",
          training_start: "2026-08-02T12:15:00+08:00",
          training_end: forecastStart,
          statsforecast_version: "1.0.0",
          selection_reason: null,
        },
      },
    },
    source_manifest: {
      history_start: "2026-08-02T12:15:00+08:00",
      history_end: forecastStart,
      interval_seconds: 900,
      observation_count: 2784,
      series: {
        station_total_load: {
          source_available_start: "2026-08-02T12:15:00+08:00",
          training_start: "2026-08-02T12:15:00+08:00",
          source_available_points: 2784,
          leading_no_data_points: 0,
          invalid_points: 0,
          negative_invalid_points: 0,
          retained_points: 2784,
          imputed_points: 0,
          mode: "ready",
          usable_week_count: 3,
          weeks: [],
        },
        storage_soc: {
          source_available_start: "2026-08-02T12:15:00+08:00",
          training_start: "2026-08-02T12:15:00+08:00",
          source_available_points: 2784,
          leading_no_data_points: 0,
          invalid_points: 0,
          negative_invalid_points: 0,
          retained_points: 2784,
          imputed_points: 0,
          mode: "ready",
          usable_week_count: 3,
          weeks: [],
        },
      },
    },
    error_code: null,
    started_at: "2026-08-31T12:15:00+08:00",
    completed_at: "2026-08-31T12:16:00+08:00",
    evaluated_at: null,
    created_at: "2026-08-31T12:14:00+08:00",
    updated_at: "2026-08-31T12:16:00+08:00",
  };
  const series = [
    ["station_total_load", "kW", "WeeklyRegimeAdjusted", 500],
    ["storage_soc", "%", "SOCWeeklyDelta", 60],
  ].map(([unique_id, unit, model_name, base]) => ({
    unique_id,
    unit,
    model_name,
    points: Array.from({ length: 96 }, (_, index) => ({
      target_time: toUtc(index),
      horizon_step: index + 1,
      forecast_value: base + index / 10,
      actual_value: unique_id === "station_total_load" ? base + index / 10 : null,
      actual_quality: unique_id === "station_total_load" ? "valid" : null,
      absolute_percentage_error: unique_id === "station_total_load" ? 0 : null,
    })),
  }));
  return {
    run,
    result: { run, series },
    performance: {
      station_id: "plant-alpha-ES01",
      lookback_days: 7,
      interval_seconds: 900,
      forecast_days: 1,
      model_policy: "full_selection",
      calculated_at: "2026-08-31T12:16:00+08:00",
      series: [
        {
          unique_id: "station_total_load", mape_percent: 5.2, baseline_mape_percent: 7.1, relative_baseline_improvement_percent: 26.76, scorable_point_count: 240, run_count: 3,
          daily: [
            { date: "2026-08-24", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-25", mape_percent: 6.0, scorable_point_count: 80, run_count: 1 },
            { date: "2026-08-26", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-27", mape_percent: 4.5, scorable_point_count: 80, run_count: 1 },
            { date: "2026-08-28", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-29", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-30", mape_percent: 5.1, scorable_point_count: 80, run_count: 1 },
          ],
        },
        {
          unique_id: "storage_soc", mape_percent: 3.4, baseline_mape_percent: 4.2, relative_baseline_improvement_percent: 19.05, scorable_point_count: 240, run_count: 3,
          daily: [
            { date: "2026-08-24", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-25", mape_percent: 3.8, scorable_point_count: 80, run_count: 1 },
            { date: "2026-08-26", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-27", mape_percent: 3.1, scorable_point_count: 80, run_count: 1 },
            { date: "2026-08-28", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-29", mape_percent: null, scorable_point_count: 0, run_count: 0 },
            { date: "2026-08-30", mape_percent: 3.3, scorable_point_count: 80, run_count: 1 },
          ],
        },
      ],
    },
  };
}

function pendingPerformanceFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "pending-performance-run";
  fixture.result.run = fixture.run;
  fixture.performance.calculated_at = "2026-08-31T12:16:00+08:00";
  fixture.performance.series.forEach((series) => {
    series.mape_percent = null;
    series.baseline_mape_percent = null;
    series.relative_baseline_improvement_percent = null;
    series.scorable_point_count = 0;
    series.run_count = 0;
    series.daily.forEach((day) => {
      day.mape_percent = null;
      day.scorable_point_count = 0;
      day.run_count = 0;
    });
  });
  return fixture;
}

function oneMinuteCrossBrowserFixture() {
  const fixture = weeklyEvidenceFixture();
  const run = fixture.run;
  const startMs = Date.parse(run.forecast_start);
  run.run_id = "latest-station-2-minute-run";
  run.station_id = "plant-beta-ES02";
  run.interval_seconds = 60;
  run.points_per_day = 1440;
  run.expected_points_per_series = 1440;
  run.model_manifest.interval_seconds = 60;
  run.model_manifest.daily_season_length = 1440;
  run.model_manifest.weekly_season_length = 10080;
  run.source_manifest.interval_seconds = 60;
  run.source_manifest.observation_count = 41760;
  for (const source of Object.values(run.source_manifest.series)) {
    source.source_available_points = 41760;
    source.retained_points = 41760;
  }
  fixture.result.run = run;
  fixture.result.series.forEach((series, seriesIndex) => {
    if (series.unique_id === "storage_soc") {
      series.model_name = "SOCWeeklyDelta5mLinear";
    }
    const base = seriesIndex === 0 ? 720 : 58;
    series.points = Array.from({ length: 1440 }, (_, index) => ({
      target_time: new Date(startMs + index * 60_000).toISOString().replace(".000Z", "Z"),
      horizon_step: index + 1,
      forecast_value: base + index / 100,
      actual_value: null,
      actual_quality: null,
      absolute_percentage_error: null,
    }));
  });
  fixture.performance.station_id = run.station_id;
  fixture.performance.interval_seconds = 60;
  return fixture;
}

function warmingEvidenceFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "weekly-warming-run";
  fixture.run.model_manifest.series.station_total_load.model_name = "WeeklyNaive";
  fixture.run.model_manifest.series.station_total_load.selection_metric = null;
  fixture.run.model_manifest.series.station_total_load.selection_reason = "fewer_than_three_usable_weeks";
  fixture.run.model_manifest.series.station_total_load.selection_status = "warming_up";
  fixture.run.model_manifest.series.station_total_load.realized_model_name = "WeeklyNaive";
  fixture.run.model_manifest.series.station_total_load.realized_status = "warming_up";
  fixture.run.model_manifest.series.station_total_load.realized_fallback_reason = null;
  fixture.run.model_manifest.series.station_total_load.candidate_scores = [];
  fixture.run.source_manifest.series.station_total_load.usable_week_count = 1;
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "WeeklyNaive";
  return fixture;
}

function degradedReconstructedWeekFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "weekly-degraded-reconstructed-run";
  const load = fixture.run.model_manifest.series.station_total_load;
  load.model_name = "WeeklyNaive";
  load.selection_metric = null;
  load.selection_reason = "latest_week_high_imputation";
  load.selection_status = "degraded";
  load.realized_model_name = "WeeklyNaive";
  load.realized_status = "degraded";
  load.realized_fallback_reason = "latest_week_high_imputation";
  load.candidate_scores = [];
  fixture.run.source_manifest.series.station_total_load.usable_week_count = 0;
  fixture.run.source_manifest.series.station_total_load.retained_points = 2304;
  fixture.run.source_manifest.series.station_total_load.imputed_points = 48;
  fixture.run.source_manifest.series.station_total_load.weeks = [];
  fixture.run.source_manifest.series.storage_soc.retained_points = 2304;
  fixture.run.source_manifest.series.storage_soc.imputed_points = 0;
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "WeeklyNaive";
  return fixture;
}

function realizedFallbackFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "weekly-realized-fallback-run";
  const load = fixture.run.model_manifest.series.station_total_load;
  load.realized_model_name = "WeeklyNaive";
  load.realized_status = "degraded";
  load.realized_fallback_reason = "RuntimeError";
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "WeeklyNaive";
  return fixture;
}

function maeSelectionFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "weekly-mae-selection-run";
  const load = fixture.run.model_manifest.series.station_total_load;
  load.model_name = "WeeklyWeighted2";
  load.realized_model_name = "WeeklyWeighted2";
  load.selection_metric = "mae";
  load.selection_reason = "wape_unavailable";
  load.candidate_scores = [
    { model_name: "WeeklyNaive", wape_percent: null, mae: 2, mape_percent: null, scorable_point_count: 96, skip_reason: null },
    { model_name: "WeeklyWeighted2", wape_percent: null, mae: 1, mape_percent: null, scorable_point_count: 96, skip_reason: null },
  ];
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "WeeklyWeighted2";
  return fixture;
}

function legacyPerformanceFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "legacy-performance-run";
  delete fixture.run.model_manifest;
  delete fixture.run.source_manifest;
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "SeasonalNaive";
  fixture.performance.series = [
    { unique_id: "station_total_load", mape_percent: 73, baseline_mape_percent: 91, relative_baseline_improvement_percent: 19.78, scorable_point_count: 384, run_count: 4 },
    { unique_id: "storage_soc", mape_percent: 47, baseline_mape_percent: 59, relative_baseline_improvement_percent: 20.34, scorable_point_count: 384, run_count: 4 },
  ];
  return fixture;
}

function legacyNullManifestFixture() {
  const fixture = legacyPerformanceFixture();
  fixture.run.run_id = "legacy-null-manifest-run";
  fixture.run.model_manifest = null;
  fixture.run.source_manifest = null;
  fixture.result.run = fixture.run;
  return fixture;
}

(async () => {
  const fixtures = task5Fixtures();
  const payload = fixtures.ready;
  const html = fs.readFileSync(HTML_PATH);
  const htmlText = html.toString("utf8");
  const syntaxInjectedHtml = htmlText.replace(
    "  <script>\n    (() => {",
    `  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "postmessage"; window.__M3_NOCOBASE_PARENT_ORIGIN__ = ${JSON.stringify("https://ems.lvkpower.com")};</script>\n  <script>\n    (() => {`,
  );
  assert.notStrictEqual(syntaxInjectedHtml, htmlText, "postmessage auth injection marker missing");
  assert.doesNotThrow(() => {
    for (const match of syntaxInjectedHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(match[1]);
  }, "postmessage-injected production page must parse before it boots");
  const productionBlockPath = path.join(
    ROOT,
    "m3",
    "nocobase",
    "m3_forecast_iframe_block.production.js",
  );
  assert.strictEqual(
    fs.existsSync(productionBlockPath),
    true,
    `missing production NocoBase block: ${productionBlockPath}`,
  );
  const nocobaseBlockText = fs.readFileSync(productionBlockPath, "utf8");
  assert.strictEqual(htmlText.includes("innerHTML"), false);
  assert.strictEqual(/<script\s+[^>]*src=/i.test(htmlText), false);
  assert.strictEqual(/<link\s+[^>]*href=/i.test(htmlText), false);
  assert.strictEqual(/@import|url\(\s*["']?https?:/i.test(htmlText), false);
  assert.strictEqual(/forecast-run|model-selection-run|\/run\b/i.test(htmlText), false);

  let servedHtml = "";
  let servedNocobaseHost = "";
  server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
    if (pathname === "/ett") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      response.end(servedHtml);
      return;
    }
    if (pathname === "/nocobase-host") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      response.end(servedNocobaseHost);
      return;
    }
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("not found");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  const origin = `http://127.0.0.1:${port}`;
  servedHtml = htmlText.replace(
    "  <script>\n    (() => {",
    `  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "postmessage"; window.__M3_NOCOBASE_PARENT_ORIGIN__ = ${JSON.stringify(origin)};</script>\n  <script>\n    (() => {`,
  );
  assert.notStrictEqual(servedHtml, htmlText, "parent origin injection marker missing");
  const testBlockText = nocobaseBlockText.replace(
    'const M3_IFRAME_URL = "https://opdash.lvkpower.com/ett";',
    `const M3_IFRAME_URL = ${JSON.stringify(`${origin}/ett`)};`,
  );
  assert.notStrictEqual(testBlockText, nocobaseBlockText, "NocoBase iframe URL marker missing");
  servedNocobaseHost = `<!doctype html><html><body><div id="m3-block"></div><script>
    window.__m3CtxVarNames = [];
    window.ctx = {
      element: document.querySelector("#m3-block"),
      getVar: async (name) => { window.__m3CtxVarNames.push(name); return "wrapper-current-user-token"; },
    };
  </script><script>${testBlockText}</script></body></html>`;

  browser = await chromium.launch({ headless: true });
  const unavailableContext = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const unavailablePage = await unavailableContext.newPage();
  await unavailablePage.addInitScript(() => {
    const NativeDate = Date;
    const fixedNow = NativeDate.parse("2026-09-03T09:00:00+08:00");
    window.Date = class extends NativeDate {
      constructor(...args) {
        super(...(args.length ? args : [fixedNow]));
      }

      static now() { return fixedNow; }
    };
  });
  await unavailablePage.route(/\/energy-forecast-api(?:\/|$|\?)/, async (route) => {
    await route.fulfill({
      status: 502,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify({ status: "error", error: { code: "dashboard_unavailable", message: "预测服务暂不可用" } }),
    });
  });
  const unavailableHtml = htmlText.replace(
    "  <script>\n    (() => {",
    `  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "server_token"; window.__M3_NOCOBASE_PARENT_ORIGIN__ = ${JSON.stringify(origin)};</script>\n  <script>\n    (() => {`,
  );
  servedHtml = unavailableHtml;
  await unavailablePage.goto(`${origin}/ett`, { waitUntil: "domcontentloaded" });
  await unavailablePage.locator("#error-state").waitFor({ state: "visible" });
  assert.deepStrictEqual(
    await unavailablePage.locator("#history-start, #history-end").evaluateAll((inputs) => inputs.map((input) => input.value)),
    ["2026-08-06", "2026-09-02"],
    "task dates must remain usable when the first dashboard request fails",
  );
  await unavailableContext.close();
  servedHtml = htmlText.replace(
    "  <script>\n    (() => {",
    `  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "postmessage"; window.__M3_NOCOBASE_PARENT_ORIGIN__ = ${JSON.stringify(origin)};</script>\n  <script>\n    (() => {`,
  );

  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  await context.addCookies([{ name: "m3_token", value: "must-not-send", url: origin }]);
  const page = await context.newPage();
  const apiRequests = [];
  const apiResponses = [];
  const externalRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const redirectRequests = [];
  let responsePayload = payload;
  let responseMode = "payload";
  let customPayload = null;
  let latestCustomPayload = null;
  let holdLatestLookup = false;
  let latestLookupStartedResolve;
  let releaseLatestLookup;
  let releaseDelayed;
  let timeoutRequestStartedResolve;

  async function fulfillApi(route, response) {
    const body = typeof response.body === "string" ? response.body : "";
    apiResponses.push({
      method: route.request().method(),
      path: new URL(route.request().url()).pathname,
      status: response.status,
      bodyPreview: body.length <= 1000 ? body : `${body.slice(0, 1000)}…`,
    });
    await route.fulfill(response);
  }

  function requestDiagnostic(request) {
    const requestUrl = new URL(request.url);
    return {
      method: request.method,
      path: requestUrl.pathname,
      search: requestUrl.search,
      postData: request.postData,
    };
  }

  async function waitForInitialDashboardReady() {
    try {
      await page.locator("#forecast-dashboard[data-state='ready']").waitFor({ timeout: 3000 });
    } catch (error) {
      const beforeRender = await page.evaluate(() => {
        const dashboard = document.querySelector("#forecast-dashboard");
        const errorState = document.querySelector("#error-state");
        return {
          rootDataState: dashboard?.dataset.state ?? null,
          errorState: errorState === null ? null : { visible: !errorState.hidden, text: errorState.textContent },
        };
      });
      let renderReturn;
      try {
        renderReturn = await page.evaluate((dashboard) => {
          if (typeof window.renderDashboard !== "function") return "renderDashboard unavailable";
          return window.renderDashboard(dashboard);
        }, responsePayload?.data ?? null);
      } catch (renderError) {
        renderReturn = `renderDashboard threw: ${renderError instanceof Error ? renderError.message : String(renderError)}`;
      }
      throw new Error(`initial dashboard did not become ready: ${JSON.stringify({
        ...beforeRender,
        renderReturn,
        pageErrors,
        consoleErrors,
        apiRequestCount: apiRequests.length,
        apiRequests: apiRequests.map(requestDiagnostic),
        apiResponses,
      }, null, 2)}\n${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function waitForCustomResultModelMeta(expectedText, requestStart, responseStart) {
    try {
      const deadline = Date.now() + 3000;
      while (Date.now() < deadline) {
        const receivedNewResult = apiResponses.slice(responseStart).some(
          (response) => response.path.startsWith(`${API_PATH}/custom-`)
            && response.path.endsWith("/result")
            && response.status === 200,
        );
        const renderedMeta = await page.locator("#result-model-meta").textContent();
        if (receivedNewResult && renderedMeta === expectedText) return;
        await page.waitForTimeout(10);
      }
      throw new Error("new custom result did not reach the expected rendered state");
    } catch (error) {
      const pageState = await page.evaluate(() => {
        const errorState = document.querySelector("#error-state");
        const runButton = document.querySelector("#run-button");
        const sessionStorageSnapshot = Object.fromEntries(
          Object.keys(sessionStorage).sort().map((key) => [
            key,
            /(token|auth|secret)/i.test(key) ? "<redacted>" : sessionStorage.getItem(key),
          ]),
        );
        return {
          runButton: runButton === null ? null : { disabled: runButton.disabled, title: runButton.title },
          taskState: document.querySelector("#task-state")?.textContent ?? null,
          errorState: errorState === null ? null : { visible: !errorState.hidden, text: errorState.textContent },
          resultModelMeta: document.querySelector("#result-model-meta")?.textContent ?? null,
          currentModelName: document.querySelector(".current-model-name")?.textContent ?? null,
          sessionStorage: sessionStorageSnapshot,
        };
      });
      const customRequests = apiRequests.slice(requestStart)
        .filter((request) => new URL(request.url).pathname.startsWith(`${API_PATH}/custom-`))
        .map(requestDiagnostic);
      const customResponses = apiResponses.slice(responseStart)
        .filter((response) => response.path.startsWith(`${API_PATH}/custom-`));
      throw new Error(`custom result did not render: ${JSON.stringify({
        expectedText,
        ...pageState,
        customRequests,
        customResponses,
        pageErrors,
        consoleErrors,
      }, null, 2)}\n${error instanceof Error ? error.message : String(error)}`);
    }
  }

  await page.addInitScript(() => {
    window.__m3AuthMessages = [];
    window.addEventListener("message", (event) => {
      window.__m3AuthMessages.push({ origin: event.origin, data: event.data });
    });
    const intervals = new Map();
    const timeouts = new Map();
    let nextId = 1;
    window.setInterval = (callback, milliseconds) => {
      const id = nextId++;
      intervals.set(id, { callback, milliseconds, elapsed: 0 });
      return id;
    };
    window.clearInterval = (id) => intervals.delete(id);
    window.setTimeout = (callback, milliseconds) => {
      const id = nextId++;
      timeouts.set(id, { callback, remaining: milliseconds });
      return id;
    };
    window.clearTimeout = (id) => timeouts.delete(id);
    window.__m3AdvanceTimeouts = async (milliseconds) => {
      for (const [id, timer] of [...timeouts]) {
        timer.remaining -= milliseconds;
        if (timer.remaining <= 0) {
          timeouts.delete(id);
          timer.callback();
          await Promise.resolve();
        }
      }
    };
    window.__m3AdvanceIntervals = async (milliseconds) => {
      for (const timer of intervals.values()) {
        timer.elapsed += milliseconds;
        while (timer.elapsed >= timer.milliseconds) {
          timer.elapsed -= timer.milliseconds;
          timer.callback();
          await Promise.resolve();
        }
      }
    };
    window.__m3IntervalCount = () => intervals.size;
  });

  page.on("request", (request) => {
    if (new URL(request.url()).origin !== origin) externalRequests.push(request.url());
  });
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route(/\/energy-forecast-api(?:\/|$|\?)/, async (route) => {
    const allHeaders = await route.request().allHeaders();
    apiRequests.push({
      method: route.request().method(), url: route.request().url(),
      postData: route.request().postData(), headers: allHeaders,
    });
    if (releaseDelayed === null) {
      await new Promise((resolve) => { releaseDelayed = resolve; });
      releaseDelayed = undefined;
    }
    const requestUrl = new URL(route.request().url());
    const latestMatch = new RegExp(`^${API_PATH}/custom-runs/(station_[12])/latest$`).exec(requestUrl.pathname);
    if (latestMatch !== null) {
      const candidate = latestMatch[1] === "station_2" ? latestCustomPayload : customPayload;
      const matchesFixture = candidate !== null
        && requestUrl.searchParams.get("interval_seconds") === String(candidate.run.interval_seconds)
        && requestUrl.searchParams.get("forecast_days") === String(candidate.run.forecast_days);
      if (holdLatestLookup && matchesFixture) {
        latestLookupStartedResolve?.();
        latestLookupStartedResolve = undefined;
        await new Promise((resolve) => { releaseLatestLookup = resolve; });
        releaseLatestLookup = undefined;
      }
      await fulfillApi(route, matchesFixture ? {
        status: 200,
        contentType: "application/json; charset=utf-8",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ status: "ok", data: candidate.run }),
      } : {
        status: 404,
        contentType: "application/json; charset=utf-8",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ status: "error", error: { code: "not_found", message: "预测任务不存在" } }),
      });
      return;
    }
    const routedCustomPayload = latestCustomPayload !== null && (
      requestUrl.pathname.includes(latestCustomPayload.run.run_id)
      || requestUrl.pathname === `${API_PATH}/custom-performance/station_2`
    ) ? latestCustomPayload : customPayload;
    if (routedCustomPayload !== null && requestUrl.pathname.startsWith(`${API_PATH}/custom-`)) {
      let data;
      if (route.request().method() === "POST") data = routedCustomPayload.run;
      else if (requestUrl.pathname.endsWith("/result")) data = routedCustomPayload.result;
      else if (requestUrl.pathname.startsWith(`${API_PATH}/custom-performance/`)) data = routedCustomPayload.performance;
      else if (requestUrl.pathname === `${API_PATH}/custom-runs/${routedCustomPayload.run.run_id}`) data = routedCustomPayload.run;
      else throw new Error(`unexpected custom route ${route.request().method()} ${requestUrl.pathname}`);
      await fulfillApi(route, {
        status: 200,
        contentType: "application/json; charset=utf-8",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ status: "ok", data }),
      });
      return;
    }
    if (responseMode === "redirect") {
      await fulfillApi(route, { status: 302, headers: { Location: "/redirect-target" }, body: "" });
      return;
    }
    if (responseMode === "timeout") {
      timeoutRequestStartedResolve?.();
      timeoutRequestStartedResolve = undefined;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    await fulfillApi(route, {
      status: 200,
      contentType: "application/json; charset=utf-8",
      headers: { "Cache-Control": "public, max-age=3600" },
      body: JSON.stringify(responsePayload),
    });
  });
  await page.route("**/redirect-target", async (route) => {
    redirectRequests.push(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });

  await page.goto(`${origin}/ett?token=must-not-forward&api=https://example.invalid/leak`, { waitUntil: "domcontentloaded" });
  const preResultSelectionBasis = await page.locator(".selection-basis").innerText();
  await page.waitForFunction(() => window.__m3AuthMessages.some((item) => item.data?.type === "vifa-m3-auth-ready"));
  assert.deepStrictEqual(apiRequests, [], "API request must wait for current-user authentication");
  const readyMessage = await page.evaluate(() => window.__m3AuthMessages.find((item) => item.data?.type === "vifa-m3-auth-ready"));
  assert.strictEqual(readyMessage.origin, origin);
  assert.deepStrictEqual(Object.keys(readyMessage.data).sort(), ["nonce", "type"]);
  assert.match(readyMessage.data.nonce, /^[a-f0-9]{32}$/);

  await page.evaluate(() => window.renderError("E2E cleanup probe"));
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "error");
  assert.strictEqual(await page.locator("#error-state").innerText(), "E2E cleanup probe");
  assert.deepStrictEqual(pageErrors, [], "renderError cleanup must not throw for the canonical DOM");

  await page.evaluate(({ allowedOrigin, nonce }) => {
    const send = (originValue, sourceValue, data) => window.dispatchEvent(new MessageEvent("message", {
      origin: originValue,
      source: sourceValue,
      data,
    }));
    send("https://attacker.invalid", window, { type: "vifa-m3-auth-token", nonce, token: "wrong-origin-token" });
    send(allowedOrigin, null, { type: "vifa-m3-auth-token", nonce, token: "wrong-source-token" });
    send(allowedOrigin, window, { type: "vifa-m3-auth-token", nonce: "0".repeat(32), token: "wrong-nonce-token" });
    send(allowedOrigin, window, { type: "vifa-m3-auth-token", nonce, token: "extra-field-token", extra: true });
    send(allowedOrigin, window, { type: "vifa-m3-auth-token", nonce, token: "invalid\ntoken" });
  }, { allowedOrigin: origin, nonce: readyMessage.data.nonce });
  await page.waitForTimeout(25);
  assert.deepStrictEqual(apiRequests, [], "invalid authentication messages must be ignored");

  await page.evaluate(({ allowedOrigin, nonce }) => {
    window.dispatchEvent(new MessageEvent("message", {
      origin: allowedOrigin,
      source: window,
      data: { type: "vifa-m3-auth-token", nonce, token: "current-user-token" },
    }));
  }, { allowedOrigin: origin, nonce: readyMessage.data.nonce });
  await waitForInitialDashboardReady();

  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "暂无匹配预测结果");
  assert.deepStrictEqual(apiRequests.map((item) => [item.method, new URL(item.url).pathname, new URL(item.url).search, item.postData]), [
    ["GET", API_PATH, "", null],
    ["GET", `${API_PATH}/custom-runs/station_1/latest`, "?interval_seconds=900&forecast_days=1", null],
  ]);
  assert.strictEqual(new URL(apiRequests[0].url).origin, origin);
  assert.strictEqual(apiRequests[0].headers.authorization, "Bearer current-user-token");
  for (const name of ["cookie", "referer"]) assert.strictEqual(apiRequests[0].headers[name], undefined);
  assert.strictEqual(await page.evaluate(() => typeof window.__M3_NOCOBASE_PARENT_ORIGIN__), "undefined");
  assert.strictEqual(await page.evaluate(() => typeof window.__NOCOBASE_API_TOKEN__), "undefined");
  assert.deepStrictEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  assert.strictEqual(await page.locator("body").innerText().then((text) => text.includes("current-user-token")), false);
  assert.ok(!JSON.stringify(apiRequests[0].headers).includes("must-not-send"));
  assert.ok(!JSON.stringify(apiRequests[0].headers).includes("must-not-forward"));
  assert.deepStrictEqual(externalRequests, []);
  assert.strictEqual(await page.evaluate(() => window.__m3IntervalCount()), 1);
  assert.strictEqual(await page.locator(".station-section").count(), 1);
  assert.strictEqual(await page.locator(".series-card").count(), 2);
  assert.strictEqual(await page.locator("svg.forecast-chart").count(), 2);
  assert.strictEqual(await page.title(), "能耗预测");
  assert.strictEqual(await page.getByRole("heading", { level: 1, name: "能耗预测", exact: true }).count(), 1);
  assert.strictEqual(await page.locator(".brand-icon svg[aria-hidden='true']").count(), 1);
  assert.strictEqual(await page.getByText("手动预测", { exact: true }).count(), 0);
  assert.deepStrictEqual(await page.locator(".station-section h2").allTextContents(), ["1# 电站"]);
  assert.strictEqual(await page.locator("#task-zone-title").innerText(), "预测任务");
  assert.strictEqual(await page.locator("#history-start").count(), 1);
  assert.strictEqual(await page.locator("#history-end").count(), 1);
  assert.strictEqual(await page.locator("#forecast-days").count(), 1);
  assert.strictEqual(await page.locator("#granularity").count(), 1);
  assert.strictEqual(await page.locator("#run-button").count(), 1);
  assert.strictEqual(await page.locator(".model-summary").count(), 0, "result model strip must be removed");
  assert.strictEqual(await page.locator("#performance-zone-title").innerText(), "预测摘要");
  assert.deepStrictEqual(
    await page.locator("[data-summary] .prediction-summary-label").allTextContents(),
    [
      "当前预测负载 MAPE",
      "预测峰值负载",
      "预测最低负载",
      "当前预测 SOC MAPE",
      "预测最高 SOC",
      "预测最低 SOC",
    ],
  );
  assert.strictEqual(await page.locator(".selection-metric-display").count(), 0, "holdout metrics must stay hidden");
  assert.strictEqual(await page.locator(".dual-mape").count(), 1, "7-day load/SOC MAPE must remain visible");
  assert.deepStrictEqual(
    await page.locator(".candidate-name").allTextContents(),
    ["周周期", "双周加权", "周期校准", "三周中位", "自动 ARIMA", "多周期分解"],
  );
  assert.strictEqual(
    await page.locator(".candidate[data-model='WeeklyRegimeAdjusted']").getAttribute("title"),
    "完整模型名称：WeeklyRegimeAdjusted",
  );
  const themeToggle = page.locator("#theme-toggle");
  assert.strictEqual(await themeToggle.count(), 1, "theme toggle must exist");
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "light");
  assert.match(await themeToggle.innerText(), /黑夜模式/);
  const lightPaper = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--paper").trim());
  await themeToggle.click();
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "dark");
  assert.match(await themeToggle.innerText(), /白天模式/);
  assert.strictEqual(await themeToggle.getAttribute("aria-pressed"), "false");
  assert.notStrictEqual(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--paper").trim()), lightPaper);
  await page.screenshot({ path: "/tmp/m3-task6-dark.png", fullPage: true });
  await themeToggle.click();
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "light");
  const stationPicker = page.locator("#station-picker");
  assert.strictEqual(await stationPicker.count(), 1, "station picker must exist");
  assert.deepStrictEqual(await stationPicker.locator("option").allTextContents(), ["1# 电站", "2# 电站"]);
  assert.strictEqual(await stationPicker.inputValue(), "station_1");
  assert.strictEqual(await page.locator("[data-station='station_1']").isVisible(), true);
  const noActualDashboard = clone(payload.data);
  const noActualStation = noActualDashboard.stations[0];
  noActualStation.range.actual_latest = null;
  noActualStation.series.forEach((series) => { series.actual = []; });
  assert.strictEqual(await page.evaluate((dashboard) => window.renderDashboard(dashboard), noActualDashboard), true);
  const noActualTimeAxes = await page.locator("svg.forecast-chart").evaluateAll((charts) => charts.map((chart) =>
    [...chart.querySelectorAll(".axis-label")].slice(-5).map((label) => label.textContent),
  ));
  assert.deepStrictEqual(noActualTimeAxes[0], noActualTimeAxes[1], "load and SOC must share one time axis");
  assert.strictEqual(noActualTimeAxes[0][0], "08/26 10:00", "empty actuals must not reserve a historical gap");
  await page.setViewportSize({ width: 600, height: 720 });
  await page.locator(".load-chart").evaluate((chart) => {
    const viewport = chart.closest(".chart-viewport");
    viewport.scrollLeft = 180;
    viewport.dispatchEvent(new Event("scroll"));
  });
  await page.waitForTimeout(25);
  assert.strictEqual(
    await page.locator(".soc-chart").evaluate((chart) => chart.closest(".chart-viewport").scrollLeft),
    180,
    "load and SOC charts must stay on the same visible timestamp",
  );
  await page.setViewportSize({ width: 1280, height: 720 });
  assert.strictEqual(await page.evaluate((dashboard) => window.renderDashboard(dashboard), payload.data), true);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyNaive']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyWeighted2']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyRegimeAdjusted']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.match(await page.locator(".candidate[data-model='AutoARIMA'] .candidate-role").innerText(), /仅 15 分钟及以上粒度/);
  assert.match(await page.locator(".candidate[data-model='MSTL'] .candidate-role").innerText(), /仅 15 分钟及以上粒度/);
  await page.locator("#granularity").selectOption("300");
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), true);
  assert.strictEqual(await page.locator(".candidate[data-model='MSTL']").evaluate((node) => node.classList.contains("disabled")), true);
  await page.locator("#granularity").selectOption("60");
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), true);
  await page.locator("#granularity").selectOption("900");
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='MSTL']").evaluate((node) => node.classList.contains("disabled")), false);

  latestCustomPayload = oneMinuteCrossBrowserFixture();
  holdLatestLookup = true;
  const latestLookupStarted = new Promise((resolve) => { latestLookupStartedResolve = resolve; });
  await stationPicker.selectOption("station_2");
  await page.locator("#granularity").selectOption("60");
  await latestLookupStarted;
  assert.strictEqual(await page.locator("#task-state").innerText(), "正在加载预测任务…");
  assert.strictEqual(await page.locator("#run-button").isDisabled(), true);
  assert.strictEqual(await page.locator("#result-progress-overlay").isVisible(), true);
  assert.strictEqual(await page.locator("#result-progress-label").innerText(), "正在加载预测任务…");
  assert.strictEqual(await page.locator(".station-section").getAttribute("aria-busy"), "true");
  releaseLatestLookup();
  holdLatestLookup = false;
  await page.waitForFunction(() => document.querySelector("#result-model-meta")?.textContent === "3 个连续有效周 · 负载周期 7 天 · 1 分钟粒度");
  assert.strictEqual(
    await page.locator(".current-model-name").first().innerText(),
    "负载：周期校准 · SOC：周差分（5分钟基准）",
  );
  assert.strictEqual(await page.locator("#total-points-inline").innerText(), "1,440");
  assert.strictEqual(await page.locator(".summary-peak-load").innerText(), "734.4 kW");
  assert.strictEqual(await page.locator(".summary-max-soc").innerText(), "72.4 %");
  assert.strictEqual(await page.locator("#task-state").innerText(), "预测完成");
  assert.strictEqual(await page.locator("#result-progress-overlay").isHidden(), true);
  assert.strictEqual(await page.locator(".station-section").getAttribute("aria-busy"), "false");
  await page.waitForFunction(() => !document.querySelector("#run-button")?.disabled);
  assert.strictEqual(await page.evaluate(() => sessionStorage.getItem("vifa.m3.customForecastRuns.v1") !== null), true);
  latestCustomPayload.result.series.forEach((series, seriesIndex) => {
    const base = seriesIndex === 0 ? 715 : 57;
    series.points.forEach((point, index) => {
      const hasActual = index < 1100 && index % 2 === 1;
      point.actual_value = hasActual ? base + index / 100 : null;
      point.actual_quality = hasActual ? "valid" : null;
      point.absolute_percentage_error = hasActual
        ? Math.abs(point.actual_value - point.forecast_value) / Math.abs(point.actual_value) * 100
        : null;
    });
  });
  await page.evaluate(async () => window.__m3AdvanceIntervals(60_000));
  await page.waitForFunction(() => document.querySelector(".series-card[data-series='station_total_load'] .actual-value")?.textContent === "726 kW");
  const sparseMinuteActualPath = await page.locator(".load-chart path[data-kind='actual']").getAttribute("d");
  assert.ok(
    sparseMinuteActualPath?.includes("L"),
    `a one-minute chart must draw actuals sampled every two minutes: ${sparseMinuteActualPath}`,
  );
  latestCustomPayload = null;
  await stationPicker.selectOption("station_1");
  await page.locator("#granularity").selectOption("900");
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "暂无匹配预测结果");
  assert.notStrictEqual(await page.locator("#total-points-inline").innerText(), "1,440");
  const firstStation = payload.data.stations[0];
  const firstLoad = firstStation.series[0];
  const firstSoc = firstStation.series[1];
  const currentLoad = firstLoad.actual.filter((point) => point.value !== null).at(-1).value;
  const currentSoc = firstSoc.actual.filter((point) => point.value !== null).at(-1).value;
  const peakLoad = Math.max(...firstLoad.forecast.map((point) => point.value));
  const minimumLoad = Math.min(...firstLoad.forecast.map((point) => point.value));
  const maximumSoc = Math.max(...firstSoc.forecast.map((point) => point.value));
  const minimumSoc = Math.min(...firstSoc.forecast.map((point) => point.value));
  const firstStationDiagnostics = await page.locator("[data-station='station_1']").evaluate((station) => ({
    stationDataState: station.dataset.state ?? null,
    series: [...station.querySelectorAll(".series-card")].map((card) => ({
      dataSeries: card.dataset.series ?? null,
      dataStatus: card.dataset.status ?? null,
      actual: card.querySelector(".actual-value")?.textContent ?? null,
      forecast: card.querySelector(".forecast-value")?.textContent ?? null,
      model: card.querySelector(".model-name")?.textContent ?? null,
    })),
  }));
  assert.strictEqual(
    await page.locator("[data-station='station_1'] .metric-current-load").innerText(),
    `${Math.round(currentLoad)} kW`,
    `selected station series did not render: ${JSON.stringify(firstStationDiagnostics)}`,
  );
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-peak-load").innerText(), `${Math.round(peakLoad)} kW`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-current-soc").innerText(), `${currentSoc.toFixed(1)} %`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-minimum-soc").innerText(), `${minimumSoc.toFixed(1)} %`);
  assert.strictEqual(await page.locator(".legacy-load-mape-value").innerText(), "2.50%");
  assert.strictEqual(await page.locator(".current-day-mape-value").innerText(), "2.50%");
  assert.deepStrictEqual(
    await page.locator("[data-summary] .prediction-summary-value").allTextContents(),
    [
      "2.50%",
      `${peakLoad.toFixed(1)} kW`,
      `${minimumLoad.toFixed(1)} kW`,
      "2.50%",
      `${maximumSoc.toFixed(1)} %`,
      `${minimumSoc.toFixed(1)} %`,
    ],
  );
  assert.strictEqual(await page.locator(".selection-metric-display").count(), 0);
  await stationPicker.selectOption("station_2");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-station"), "station_2");
  const secondLoad = payload.data.stations[1].series[0].actual.filter((point) => point.value !== null).at(-1).value;
  assert.strictEqual(await page.locator(".metric-current-load").innerText(), `${Math.round(secondLoad)} kW`);
  assert.match(await page.locator(".acceptance-summary").innerText(), /3 \/ 7/);
  assert.strictEqual(await page.locator(".legacy-load-mape-value").innerText(), "—");
  await stationPicker.selectOption("station_1");
  assert.strictEqual(await page.locator(".legacy-load-mape-value").innerText(), "2.50%");
  assert.deepStrictEqual(await page.locator(".forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  assert.strictEqual(await page.locator(".forecast-start").innerText(), "2026/08/26 10:00");
  assert.deepStrictEqual(await page.locator(".readiness-hint").allTextContents(), ["已达到 Ready 条件"]);
  assert.deepStrictEqual(await page.locator(".readiness-meta").allTextContents(), ["有效历史 28.0 / 28 天"]);
  assert.strictEqual(await page.locator(".station-acceptance").count(), 1);
  assert.deepStrictEqual(
    await page.locator(".station-acceptance .acceptance-result-row").evaluateAll((rows) => rows.map((row) => row.dataset.series)),
    SERIES_IDS,
  );
  assert.deepStrictEqual(
    await page.locator(".station-acceptance .acceptance-result-row").first().locator("td").allTextContents(),
    ["电站总负荷", "660 / 672", "3", "2.50%", "1.25", "2.60%", "2.40%", "1.90%", "5.20%", "通过"],
  );

  await page.evaluate((data) => window.renderDashboard(data), fixtures.initializing_normal.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator("[data-station='station_1']").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator(".readiness-hint").innerText(), "距离 Ready 约 5.5 天");
  assert.strictEqual(await page.locator(".readiness-meta").innerText(), "有效历史 22.5 / 28 天");
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.match(fixtures.microsecond_snapshot.data.system.generated_at, /\.123456\+08:00$/);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.microsecond_snapshot.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.match(fixtures.microsecond_empty_error.data.stations[0].range.now_separator, /\.123456\+08:00$/);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.microsecond_empty_error.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "degraded");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-state"), "error");
  await stationPicker.selectOption("station_2");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-state"), "initializing");
  await stationPicker.selectOption("station_1");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-state"), "error");
  for (const fraction of ["1", "123", "123456"]) {
    const fractional = clone(fixtures.microsecond_snapshot);
    fractional.data.system.generated_at = fractional.data.system.generated_at.replace(/\.123456\+08:00$/, `.${fraction}+08:00`);
    await page.evaluate((data) => window.renderDashboard(data), fractional.data);
    assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "initializing", `fraction_${fraction.length}`);
  }
  await page.evaluate((data) => window.renderDashboard(data), payload.data);

  const consoleBeforeRedirect = consoleErrors.length;
  responseMode = "redirect";
  await page.evaluate(() => window.loadDashboard());
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "ready");
  assert.deepStrictEqual(await page.locator(".forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  assert.strictEqual(await page.locator("#error-state").innerText(), "预测数据加载失败");
  assert.deepStrictEqual(redirectRequests, []);
  assert.ok(consoleErrors.slice(consoleBeforeRedirect).every((message) => /ERR_FAILED|Failed to load resource/.test(message)));
  consoleErrors.splice(consoleBeforeRedirect);
  responseMode = "payload";
  await page.evaluate(() => window.loadDashboard());
  await page.locator("#forecast-dashboard[data-state='ready']").waitFor();

  responseMode = "timeout";
  const requestsBeforeTimeout = apiRequests.length;
  const timeoutRequestStarted = new Promise((resolve) => { timeoutRequestStartedResolve = resolve; });
  const timeoutLoad = page.evaluate(() => window.loadDashboard());
  await timeoutRequestStarted;
  assert.strictEqual(apiRequests.length, requestsBeforeTimeout + 1);
  await page.evaluate(() => window.__m3AdvanceTimeouts(10_000));
  await timeoutLoad;
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "ready");
  assert.strictEqual(await page.locator("#error-state").innerText(), "请求超时");
  assert.deepStrictEqual(await page.locator(".forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  assert.ok(await page.locator("svg.forecast-chart > *").count() > 0);
  assert.strictEqual(await page.locator(".station-acceptance .acceptance-result-row").count(), 2);
  responseMode = "payload";
  const requestsBeforeRecovery = apiRequests.length;
  await page.evaluate(() => window.loadDashboard());
  assert.strictEqual(apiRequests.length, requestsBeforeRecovery + 1);
  await page.locator("#forecast-dashboard[data-state='ready']").waitFor();
  assert.deepStrictEqual(await page.locator(".forecast-value").allTextContents(), ["410 kW", "61.0 %"]);

  const chartStation = page.locator("[data-station='station_1']");
  assert.strictEqual(await chartStation.locator("path[data-kind='actual']").count(), 2);
  assert.strictEqual(await chartStation.locator("path[data-kind='forecast']").count(), 2);
  assert.strictEqual(await chartStation.locator("path[data-kind='actual']").first().evaluate((node) => getComputedStyle(node).strokeDasharray), "none");
  assert.notStrictEqual(await chartStation.locator("path[data-kind='forecast']").first().evaluate((node) => getComputedStyle(node).strokeDasharray), "none");
  const stationOne = payload.data.stations[0];
  const chartStart = Date.parse(stationOne.range.history_start);
  const chartEnd = Date.parse(stationOne.range.forecast_end);
  const chartX = (value) => 54 + (Date.parse(value) - chartStart) / (chartEnd - chartStart) * (1180 - 54 - 22);
  const loadActualCoordinates = pathCoordinates(await page.locator("[data-station='station_1'] .load-chart path[data-kind='actual']").getAttribute("d"));
  const loadForecastCoordinates = pathCoordinates(await page.locator("[data-station='station_1'] .load-chart path[data-kind='forecast']").getAttribute("d"));
  assert.ok(Math.abs(loadActualCoordinates.at(-1).x - chartX(stationOne.series[0].actual.at(-1).data_time)) < 0.02);
  assert.ok(Math.abs(loadForecastCoordinates[0].x - chartX(stationOne.series[0].forecast[0].target_time)) < 0.02);
  assert.ok(Math.abs(loadForecastCoordinates.at(-1).x - chartX(stationOne.range.forecast_end)) < 0.02);
  assert.ok(Math.abs(Number(await page.locator("[data-station='station_1'] .load-chart [data-kind='current-divider']").getAttribute("x1")) - chartX(stationOne.range.now_separator)) < 0.02);

  await page.evaluate((data) => window.renderDashboard(data), fixtures.degraded.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "degraded");
  assert.strictEqual(await page.locator("[data-station='station_1']").getAttribute("data-state"), "degraded");
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  assert.deepStrictEqual(await page.locator("[data-station='station_1'] .forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.stale.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "stale");
  assert.strictEqual(await page.locator("[data-station='station_1']").getAttribute("data-state"), "stale");
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.error.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "degraded");
  assert.deepStrictEqual(await page.locator("[data-station='station_1'] .forecast-value").allTextContents(), ["—", "—"]);
  assert.strictEqual(await page.locator("[data-station='station_1'] path.chart-line").count(), 0);
  assert.strictEqual(await page.locator(".station-acceptance .acceptance-result-row").count(), 0);
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  await page.evaluate((data) => window.renderDashboard(data), payload.data);

  const truthfullyClippedSoc = clone(payload);
  Object.assign(
    truthfullyClippedSoc.data.stations[0].series[1].forecast[0],
    { raw_value: 0.5, value: 2, is_clipped: true },
  );
  await page.evaluate((data) => window.renderDashboard(data), truthfullyClippedSoc.data);
  assert.strictEqual(
    await page.locator("#forecast-dashboard").getAttribute("data-state"),
    "ready",
    "truthful 2..99 SOC clipping must remain renderable",
  );
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.notStrictEqual(await page.locator("#generated-at").innerText(), "最近更新：—");
  await page.evaluate((data) => window.renderDashboard(data), payload.data);

  const malformedCases = [];
  const extraTop = clone(payload); extraTop.data.debug = true; malformedCases.push(["extra_top", extraTop]);
  const extraStation = clone(payload); extraStation.data.stations[0].unknown = "x"; malformedCases.push(["extra_station", extraStation]);
  const fullIdentity = clone(payload); fullIdentity.data.stations[0].station_id = "plant-alpha-ES01"; malformedCases.push(["station_id", fullIdentity]);
  const sourceIdentity = clone(payload); sourceIdentity.data.stations[1].es_sn = "plant-beta-ES02"; malformedCases.push(["es_sn", sourceIdentity]);
  const wrongOrder = clone(payload); wrongOrder.data.stations.reverse(); malformedCases.push(["station_order", wrongOrder]);
  const wrongName = clone(payload); wrongName.data.stations[0].station_name = "Other"; malformedCases.push(["public_name", wrongName]);
  const wrongSeries = clone(payload); wrongSeries.data.stations[0].series[1].unique_id = "unknown"; malformedCases.push(["unknown_series", wrongSeries]);
  const sharedRange = clone(payload); sharedRange.data.stations[1].range = clone(sharedRange.data.stations[0].range); malformedCases.push(["station_range", sharedRange]);
  const wrongType = clone(payload); wrongType.data.stations[0].series[0].actual[0].source_revision = "1"; malformedCases.push(["strict_type", wrongType]);
  const wrongUnit = clone(payload); wrongUnit.data.stations[1].series[1].unit = "kW"; malformedCases.push(["wrong_unit", wrongUnit]);
  const brokenActual = clone(payload); brokenActual.data.stations[0].series[0].actual[1].data_time = brokenActual.data.stations[0].series[0].actual[0].data_time; malformedCases.push(["actual_continuity", brokenActual]);
  const wrongClipping = clone(payload); Object.assign(wrongClipping.data.stations[0].series[0].forecast[0], { raw_value: -2, value: 0, is_clipped: false }); malformedCases.push(["clipping_truth", wrongClipping]);
  const wrongAggregate = clone(payload); wrongAggregate.data.system.state = "initializing"; malformedCases.push(["aggregate_severity", wrongAggregate]);
  const inProgressRows = clone(payload); inProgressRows.data.stations[1].acceptance.results = clone(payload.data.stations[0].acceptance.results); malformedCases.push(["in_progress_rows", inProgressRows]);
  const finalPartial = clone(payload); finalPartial.data.stations[0].acceptance.results.pop(); malformedCases.push(["final_partial", finalPartial]);
  const inventedEmpty = clone(fixtures.error); inventedEmpty.data.stations[0].system.generated_at = payload.data.stations[0].system.generated_at; malformedCases.push(["empty_error_invented_data", inventedEmpty]);
  const normalizedCalendarDate = clone(payload); normalizedCalendarDate.data.system.generated_at = "2026-02-31T10:02:00+08:00"; malformedCases.push(["normalized_calendar_date", normalizedCalendarDate]);
  const fractionalQuarter = clone(payload); fractionalQuarter.data.stations[0].series[0].actual[0].data_time = fractionalQuarter.data.stations[0].series[0].actual[0].data_time.replace("+08:00", ".1+08:00"); malformedCases.push(["fractional_quarter_hour", fractionalQuarter]);
  for (const [label, malformed] of malformedCases) await assertRejected(page, malformed, label);

  await page.evaluate((data) => {
    const malformed = structuredClone(data);
    Object.defineProperty(malformed.stations[0], "__proto__", { value: { polluted: true }, enumerable: true });
    window.renderDashboard(malformed);
  }, payload.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "error", "prototype_key");
  assert.strictEqual(await page.evaluate(() => ({}).polluted), undefined);

  const xss = clone(payload);
  xss.data.stations[0].series[0].model_name = '<img src=x onerror="window.__m3Xss=1">';
  await page.evaluate((data) => window.renderDashboard(data), xss.data);
  const model = page.locator("[data-station='station_1'] [data-series='station_total_load'] .model-name");
  assert.strictEqual(await model.innerText(), '<img src=x onerror="window.__m3Xss=1">');
  assert.strictEqual(await page.locator(".station-section img, .station-section script").count(), 0);
  assert.strictEqual(await page.evaluate(() => typeof window.__m3Xss), "undefined");

  const extraEnvelope = clone(payload);
  extraEnvelope.trace = "must-reject";
  responsePayload = extraEnvelope;
  await page.evaluate(() => window.loadDashboard());
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "ready", "extra_envelope");
  assert.strictEqual(await page.locator("#error-state").innerText(), "预测数据加载失败");
  responsePayload = payload;
  await page.evaluate(() => window.loadDashboard());
  await page.locator("#forecast-dashboard[data-state='ready']").waitFor();

  await page.evaluate((data) => window.renderDashboard(data), payload.data);
  releaseDelayed = null;
  const beforeConcurrent = apiRequests.length;
  const concurrent = page.evaluate(() => Promise.all([window.loadDashboard(), window.loadDashboard(), window.loadDashboard()]));
  const concurrentDeadline = Date.now() + 1000;
  while (apiRequests.length < beforeConcurrent + 1 && Date.now() < concurrentDeadline) {
    await page.waitForTimeout(10);
  }
  assert.strictEqual(apiRequests.length, beforeConcurrent + 1);
  assert.strictEqual(typeof releaseDelayed, "function");
  releaseDelayed();
  await concurrent;
  assert.strictEqual(apiRequests.length, beforeConcurrent + 1);

  const beforeHiddenPoll = apiRequests.length;
  await page.evaluate(async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    await window.__m3AdvanceIntervals(60_000);
  });
  await page.waitForTimeout(25);
  assert.strictEqual(apiRequests.length, beforeHiddenPoll);

  const beforePoll = apiRequests.length;
  const responsePromise = page.waitForResponse((response) => new URL(response.url()).pathname === API_PATH && response.request().method() === "GET");
  await page.evaluate(async () => {
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    await window.__m3AdvanceIntervals(60_000);
  });
  await responsePromise;
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "暂无匹配预测结果");
  assert.deepStrictEqual(
    apiRequests.slice(beforePoll).map((item) => [item.method, new URL(item.url).pathname, new URL(item.url).search, item.postData]),
    [
      ["GET", API_PATH, "", null],
      ["GET", `${API_PATH}/custom-runs/station_1/latest`, "?interval_seconds=900&forecast_days=1", null],
    ],
  );
  assert.ok(apiRequests.every((item) => item.method === "GET" && new URL(item.url).origin === origin && item.postData === null));
  assert.ok(apiRequests.every((item) => !item.headers.cookie && item.headers.authorization === "Bearer current-user-token" && !item.headers.referer));
  assert.ok(apiRequests.every((item) => !/must-not-send|must-not-forward|example\.invalid/.test(JSON.stringify(item.headers))));

  customPayload = weeklyEvidenceFixture();
  customPayload.result.series.find((series) => series.unique_id === "storage_soc").points.forEach((point) => {
    point.actual_value = point.forecast_value;
    point.actual_quality = "valid";
    point.absolute_percentage_error = 0;
  });
  const weeklyRequestStart = apiRequests.length;
  const weeklyResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", weeklyRequestStart, weeklyResponseStart);
  assert.strictEqual(await page.locator(".readiness-selected").count(), 1);
  assert.strictEqual(
    await page.locator(".readiness-selected").innerText(),
    "请求历史：29 天 · 源数据覆盖：29.0 天（4.1 周）· 起点：2026/08/02 12:15",
  );
  assert.strictEqual(
    await page.locator(".readiness-meta").innerText(),
    "训练起点：2026/08/02 12:15 · 有效训练点折算：29.0 天",
  );
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  assert.strictEqual(await inlineBarPercent(page, ".readiness-track span"), 75);
  assert.strictEqual(await page.locator(".current-day-mape-value").innerText(), "0.00%");
  assert.strictEqual(await page.locator(".current-day-mape-note").innerText(), "96 / 96 个实际点 · 完整结果");
  assert.strictEqual(await page.locator(".summary-peak-load").innerText(), "509.5 kW");
  assert.match(await page.locator(".summary-peak-load-note").innerText(), /2026\/09\/01 12:00/);
  assert.strictEqual(await page.locator(".summary-min-load").innerText(), "500.0 kW");
  assert.strictEqual(await page.locator(".current-soc-mape-value").innerText(), "0.00%");
  assert.strictEqual(await page.locator(".current-soc-mape-note").innerText(), "96 / 96 个实际点 · 完整结果");
  assert.strictEqual(await page.locator(".summary-max-soc").innerText(), "69.5 %");
  assert.strictEqual(await page.locator(".summary-min-soc").innerText(), "60.0 %");
  assert.match(await page.locator(".prediction-summary-sentence").innerText(), /预计负载峰值为 509\.5 kW；最低 SOC 为 60\.0 %/);
  assert.strictEqual(await page.locator(".current-model-name").first().innerText(), "负载：周期校准 · SOC：周差分");
  assert.deepStrictEqual(
    await page.locator(".mape-panel").first().locator(".mape-date").allTextContents(),
    ["08/24", "08/25", "08/26", "08/27", "08/28", "08/29", "最新 08/30"],
  );
  assert.deepStrictEqual(
    await page.locator(".mape-panel").first().locator(".mape-value").allTextContents(),
    ["—", "6.00%", "—", "4.50%", "—", "—", "5.10%"],
  );
  assert.deepStrictEqual(
    await page.locator(".mape-panel").nth(1).locator(".mape-value").allTextContents(),
    ["—", "3.80%", "—", "3.10%", "—", "—", "3.30%"],
  );
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyRegimeAdjusted']").evaluate((node) => node.classList.contains("selected")), true);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyRegimeAdjusted'] .candidate-state").innerText(), "已选定");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyMedian3'] .candidate-state").innerText(), "本次未参与");
  await page.locator("#granularity").selectOption("60");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyMedian3'] .candidate-state").innerText(), "等待当前电站数据");
  await page.locator("#granularity").selectOption("900");
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "预测完成");
  await stationPicker.selectOption("station_2");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-station"), "station_2");
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "暂无匹配预测结果");
  assert.strictEqual(await page.locator(".prediction-summary-sentence").innerText(), "预计负载峰值为 730.0 kW；最低 SOC 为 54.0 %");
  assert.ok((await page.locator(".mape-value").allTextContents()).every((value) => value === "—"), "station switch must clear daily MAPE from the previous station");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyMedian3'] .candidate-state").innerText(), "可参与");
  const stationOneReturnRequestStart = apiRequests.length;
  const stationOneReturnResponseStart = apiResponses.length;
  await stationPicker.selectOption("station_1");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-station"), "station_1");
  await waitForCustomResultModelMeta(
    "3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度",
    stationOneReturnRequestStart,
    stationOneReturnResponseStart,
  );
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "预测完成"
    && document.querySelector(".candidate[data-model='WeeklyRegimeAdjusted']")?.classList.contains("selected")
    && !document.querySelector("#run-button")?.disabled);
  assert.strictEqual(await page.locator("#task-state").innerText(), "预测完成");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyMedian3'] .candidate-state").innerText(), "本次未参与");
  assert.ok((await page.locator(".load-chart .axis-label").allTextContents()).includes("08/31 12:15"));
  await page.locator(".load-chart").evaluate((svg) => {
    const bounds = svg.getBoundingClientRect();
    const firstTargetX = 54 + 15 / (24 * 60) * (1180 - 54 - 22);
    svg.onpointermove({ clientX: bounds.left + firstTargetX / 1180 * bounds.width });
  });
  assert.match(
    await page.locator(".load-chart").evaluate((svg) => svg.closest(".chart-viewport").querySelector(".chart-tooltip").textContent),
    /2026\/08\/31 12:30/,
  );
  assert.deepStrictEqual(await invalidSvgLineCoordinates(page), [], "custom charts must not render invalid SVG line coordinates");

  customPayload = pendingPerformanceFixture();
  const pendingRequestStart = apiRequests.length;
  const pendingResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", pendingRequestStart, pendingResponseStart);
  assert.ok((await page.locator(".mape-value").allTextContents()).every((value) => value === "—"));
  const forecastPathBeforeRefresh = await page.locator(".load-chart .chart-line[data-kind='forecast']").getAttribute("d");
  const evaluatedPayload = weeklyEvidenceFixture();
  customPayload.run.status = "evaluated";
  customPayload.run.evaluated_at = "2026-09-01T12:17:00+08:00";
  customPayload.run.updated_at = "2026-09-01T12:17:00+08:00";
  customPayload.result.run = customPayload.run;
  customPayload.result.series[1].points.forEach((point, index) => {
    point.actual_value = 60 + index / 10;
    point.actual_quality = "valid";
    point.absolute_percentage_error = Math.abs(point.actual_value - point.forecast_value) / point.actual_value * 100;
  });
  customPayload.performance = evaluatedPayload.performance;
  await page.evaluate(async () => window.__m3AdvanceIntervals(60_000));
  await page.waitForFunction(() => document.querySelector(".mape-panel .mape-value")?.textContent === "—"
    && [...document.querySelectorAll(".mape-panel .mape-value")].some((node) => node.textContent === "5.10%"), null, { timeout: 1000 });
  assert.strictEqual(await page.locator("#task-state").innerText(), "预测完成 · 已评估");
  assert.strictEqual(await page.locator(".series-card[data-series='storage_soc'] .actual-value").innerText(), "69.5 %");
  assert.strictEqual(await page.locator(".load-chart .chart-line[data-kind='forecast']").getAttribute("d"), forecastPathBeforeRefresh);

  customPayload = realizedFallbackFixture();
  const fallbackRequestStart = apiRequests.length;
  const fallbackResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", fallbackRequestStart, fallbackResponseStart);
  assert.strictEqual(await page.locator(".current-model-name").first().innerText(), "负载：周周期 · SOC：周差分");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyRegimeAdjusted']").evaluate((node) => node.classList.contains("selected")), true);
  assert.strictEqual(await page.locator(".series-card[data-series='station_total_load'] .model-name").innerText(), "周周期");
  assert.strictEqual(await page.locator(".series-card[data-series='station_total_load'] .series-status").innerText(), "降级");
  assert.strictEqual(await page.locator(".series-card[data-series='station_total_load'] .fallback").innerText(), "回退：RuntimeError");
  assert.strictEqual((await page.locator("body").innerText()).includes("password"), false);

  customPayload = maeSelectionFixture();
  const maeRequestStart = apiRequests.length;
  const maeResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", maeRequestStart, maeResponseStart);
  assert.strictEqual(await page.locator("#policy-copy").innerText(), "主评分不可用；按备用评分选择负载模型");
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyWeighted2'] .candidate-state").innerText(), "已选定");

  customPayload = warmingEvidenceFixture();
  const warmingRequestStart = apiRequests.length;
  const warmingResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("1 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", warmingRequestStart, warmingResponseStart);
  assert.match(await page.locator(".current-model-name").first().innerText(), /负载：周周期/);
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyNaive'] .candidate-state").innerText(), "预热中");

  customPayload = degradedReconstructedWeekFixture();
  const degradedRequestStart = apiRequests.length;
  const degradedResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("0 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", degradedRequestStart, degradedResponseStart);
  assert.strictEqual(
    await page.locator("#policy-copy").innerText(),
    "最近一周缺失较多；使用插补后的周周期模型降级预测",
  );
  assert.strictEqual(
    await page.locator(".candidate[data-model='WeeklyNaive'] .candidate-state").innerText(),
    "降级预测",
  );
  assert.strictEqual(
    await page.locator(".series-card[data-series='station_total_load'] .series-status").innerText(),
    "降级",
  );
  assert.strictEqual(await page.locator(".readiness-hint").isHidden(), true);
  assert.strictEqual(
    await page.locator(".readiness-selected").innerText(),
    "请求历史：29 天 · 源数据覆盖：29.0 天（4.1 周）· 起点：2026/08/02 12:15",
  );
  assert.strictEqual(
    await page.locator(".readiness-meta").innerText(),
    "训练起点：2026/08/02 12:15 · 有效训练点折算：23.5 天",
  );
  assert.strictEqual(await inlineBarPercent(page, ".readiness-track span"), 0);

  customPayload = legacyPerformanceFixture();
  const legacyRequestStart = apiRequests.length;
  const legacyResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await page.locator("#task-state").getByText("任务版本已失效，请重新预测", { exact: true }).waitFor();
  assert.strictEqual(await page.locator("#error-state").innerText(), "任务版本已失效，请重新预测");
  assert.strictEqual(await page.locator("#result-model-meta").textContent(), "0 个连续有效周 · 负载周期 7 天 · 15 分钟粒度");
  assert.match(await page.locator(".current-model-name").first().innerText(), /负载：周周期/);
  assert.strictEqual(await page.evaluate(() => sessionStorage.getItem("vifa.m3.customForecastRuns.v1")), null);
  assert.deepStrictEqual(
    apiRequests.slice(legacyRequestStart).map((request) => [request.method, new URL(request.url).pathname]),
    [["POST", `${API_PATH}/custom-runs/station_1`]],
    "an incompatible terminal run must be rejected before result/performance reads",
  );
  assert.deepStrictEqual(
    apiResponses.slice(legacyResponseStart).map((response) => [response.method, response.path, response.status]),
    [["POST", `${API_PATH}/custom-runs/station_1`, 200]],
  );
  assert.strictEqual(
    preResultSelectionBasis,
    "模型选择依据：留出周评分 → 固定模型顺序",
    "pre-result policy copy must name the deterministic final tie-break",
  );

  customPayload = legacyNullManifestFixture();
  const legacyNullRequestStart = apiRequests.length;
  const legacyNullResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await page.locator("#task-state").getByText("任务版本已失效，请重新预测", { exact: true }).waitFor();
  assert.strictEqual(await page.locator("#error-state").innerText(), "任务版本已失效，请重新预测");
  assert.strictEqual(await page.evaluate(() => sessionStorage.getItem("vifa.m3.customForecastRuns.v1")), null);
  assert.deepStrictEqual(
    apiRequests.slice(legacyNullRequestStart).map((request) => [request.method, new URL(request.url).pathname]),
    [["POST", `${API_PATH}/custom-runs/station_1`]],
  );
  assert.deepStrictEqual(
    apiResponses.slice(legacyNullResponseStart).map((response) => [response.method, response.path, response.status]),
    [["POST", `${API_PATH}/custom-runs/station_1`, 200]],
  );

  customPayload = weeklyEvidenceFixture();
  const replacementRequestStart = apiRequests.length;
  const replacementResponseStart = apiResponses.length;
  await page.locator("#run-button").click();
  await waitForCustomResultModelMeta("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度", replacementRequestStart, replacementResponseStart);
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);

  await page.screenshot({ path: "/tmp/m3-task6-desktop.png", fullPage: true });
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
  assert.strictEqual(await page.locator(".station-section:visible").count(), 1);
  assert.strictEqual(await page.locator(".station-section:visible .chart-panel:visible").count(), 2);
  assert.strictEqual(await page.locator(".station-section .series-card").count(), 2);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(50);
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
  assert.strictEqual(await page.locator(".station-section:visible .result-grid").evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(" ").length), 1);
  assert.strictEqual(await page.locator(".chart-viewport").count(), 2);
  assert.strictEqual(await page.locator(".station-section:visible .chart-viewport").count(), 2);
  for (const viewport of await page.locator(".station-section:visible .chart-viewport").all()) {
    const metrics = await viewport.evaluate((node) => {
      const svg = node.querySelector("svg");
      const labels = [...svg.querySelectorAll(".axis-label")];
      return {
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        svgWidth: svg.getBoundingClientRect().width,
        minimumLabelHeight: Math.min(...labels.map((label) => label.getBoundingClientRect().height)),
      };
    });
    assert.ok(metrics.scrollWidth > metrics.clientWidth, JSON.stringify(metrics));
    assert.ok(metrics.svgWidth >= 800, JSON.stringify(metrics));
    assert.ok(metrics.minimumLabelHeight >= 9, JSON.stringify(metrics));
    assert.strictEqual(await viewport.evaluate((node) => {
      node.scrollLeft = node.scrollWidth;
      const moved = node.scrollLeft > 0;
      node.scrollLeft = 0;
      return moved;
    }), true);
  }
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.strictEqual(await page.locator(".station-section").count(), 1);
  assert.strictEqual(await page.locator(".series-card").count(), 2);
  await page.screenshot({ path: "/tmp/m3-task6-mobile.png", fullPage: true });
  assert.ok(consoleErrors.every((message) => /status of 404 \(Not Found\)/.test(message)), JSON.stringify(consoleErrors));
  assert.deepStrictEqual(pageErrors, []);

  const wrapperPage = await context.newPage();
  const wrapperApiRequests = [];
  await wrapperPage.route(/\/energy-forecast-api(?:\/|$|\?)/, async (route) => {
    wrapperApiRequests.push({
      url: route.request().url(),
      method: route.request().method(),
      headers: await route.request().allHeaders(),
    });
    const latest = new URL(route.request().url()).pathname.endsWith("/latest");
    await route.fulfill({
      status: latest ? 404 : 200,
      contentType: "application/json; charset=utf-8",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(latest
        ? { status: "error", error: { code: "not_found", message: "预测任务不存在" } }
        : payload),
    });
  });
  await wrapperPage.goto(`${origin}/nocobase-host`, { waitUntil: "domcontentloaded" });
  const iframe = wrapperPage.locator("#m3-block iframe");
  await iframe.waitFor();
  assert.strictEqual(await iframe.getAttribute("src"), `${origin}/ett`);
  assert.strictEqual(await iframe.getAttribute("sandbox"), "allow-scripts allow-same-origin");
  assert.strictEqual(await iframe.getAttribute("referrerpolicy"), "no-referrer");
  await wrapperPage.frameLocator("#m3-block iframe").locator("#forecast-dashboard[data-state='ready']").waitFor({ timeout: 3000 });
  await wrapperPage.frameLocator("#m3-block iframe").locator("#task-state").getByText("暂无匹配预测结果", { exact: true }).waitFor();
  assert.deepStrictEqual(await wrapperPage.evaluate(() => window.__m3CtxVarNames), ["ctx.token"]);
  assert.strictEqual(wrapperApiRequests.length, 2);
  assert.strictEqual(wrapperApiRequests[0].method, "GET");
  assert.strictEqual(new URL(wrapperApiRequests[0].url).pathname, API_PATH);
  assert.strictEqual(new URL(wrapperApiRequests[0].url).search, "");
  assert.strictEqual(wrapperApiRequests[0].headers.authorization, "Bearer wrapper-current-user-token");
  assert.strictEqual(wrapperApiRequests[0].headers.cookie, undefined);
  assert.strictEqual(wrapperApiRequests[0].headers.referer, undefined);
  assert.strictEqual((await wrapperPage.locator("body").innerText()).includes("wrapper-current-user-token"), false);
  assert.deepStrictEqual(await wrapperPage.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  await wrapperPage.close();

  customPayload = null;
  await page.evaluate(() => sessionStorage.clear());
  servedHtml = htmlText.replace(
    "  <script>\n    (() => {",
    `  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "server_token"; window.__M3_NOCOBASE_PARENT_ORIGIN__ = ${JSON.stringify(origin)};</script>\n  <script>\n    (() => {`,
  );
  const requestsBeforeServerTokenMode = apiRequests.length;
  await page.goto(`${origin}/ett`, { waitUntil: "domcontentloaded" });
  await page.locator("#forecast-dashboard[data-state='ready']").waitFor({ timeout: 3000 });
  await page.waitForFunction(() => document.querySelector("#task-state")?.textContent === "暂无匹配预测结果");
  const serverTokenRequests = apiRequests.slice(requestsBeforeServerTokenMode);
  assert.strictEqual(
    serverTokenRequests.length,
    2,
    JSON.stringify(serverTokenRequests.map(requestDiagnostic)),
  );
  const serverTokenRequest = serverTokenRequests[0];
  assert.strictEqual(serverTokenRequest.method, "GET");
  assert.strictEqual(new URL(serverTokenRequest.url).pathname, API_PATH);
  assert.strictEqual(serverTokenRequest.headers.authorization, undefined);
  assert.strictEqual(serverTokenRequest.headers.cookie, undefined);
  assert.ok(serverTokenRequests.every((request) => request.headers.authorization === undefined && request.headers.cookie === undefined));
  assert.deepStrictEqual(
    await page.evaluate(() => window.__m3AuthMessages.filter((item) => item.data?.type === "vifa-m3-auth-ready")),
    [],
  );
  assert.strictEqual(await page.evaluate(() => typeof window.__M3_DASHBOARD_AUTH_MODE__), "undefined");
  assert.strictEqual(await page.evaluate(() => window.__m3IntervalCount()), 1);

  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide")));
  assert.strictEqual(await page.evaluate(() => window.__m3IntervalCount()), 0);
  await browser.close();
  browser = undefined;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  server = undefined;
  process.stdout.write("m3_dashboard_e2e_ok\n");
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  Promise.resolve(browser?.close()).catch(() => {}).then(() => new Promise((resolve) => {
    if (!server?.listening) return resolve();
    server.close(() => resolve());
  })).finally(() => { process.exitCode = 1; });
});
