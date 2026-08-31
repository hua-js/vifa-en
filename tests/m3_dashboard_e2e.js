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

function weeklyEvidenceFixture() {
  const forecastStart = "2026-08-31T12:15:00+08:00";
  const firstTargetMs = Date.parse("2026-08-31T04:30:00Z");
  const toUtc = (offset) => new Date(firstTargetMs + offset * 900_000).toISOString().replace(".000Z", "Z");
  const run = {
    run_id: "weekly-evidence-run",
    station_id: "plant-alpha-ES01",
    status: "succeeded",
    history_start: "2026-08-03T12:15:00+08:00",
    history_end: forecastStart,
    history_days: 28,
    forecast_start: forecastStart,
    forecast_end: "2026-09-01T12:15:00+08:00",
    forecast_days: 1,
    interval_seconds: 900,
    points_per_day: 96,
    expected_points_per_series: 96,
    model_policy: "full_selection",
    model_manifest: {
      selection_policy: "weekly_load_v1",
      model_policy: "full_selection",
      interval_seconds: 900,
      daily_season_length: 96,
      weekly_season_length: 672,
      series: {
        station_total_load: {
          model_name: "WeeklyWeighted2",
          selection_metric: "wape_percent",
          selection_reason: null,
          selection_status: null,
          candidate_scores: [
            { model_name: "WeeklyNaive", wape_percent: 12.5, mae: 10, mape_percent: 13, scorable_point_count: 96, skip_reason: null },
            { model_name: "WeeklyWeighted2", wape_percent: 10, mae: 8, mape_percent: 11, scorable_point_count: 96, skip_reason: null },
            { model_name: "WeeklyMedian3", wape_percent: null, mae: null, mape_percent: null, scorable_point_count: 0, skip_reason: "no_scorable_points" },
          ],
          training_start: "2026-08-03T12:15:00+08:00",
          training_end: forecastStart,
          statsforecast_version: "1.0.0",
        },
        storage_soc: {
          model_name: "SeasonalNaive",
          cv_mape_percent: null,
          selected_at: "2026-08-31T12:15:00+08:00",
          training_start: "2026-08-03T12:15:00+08:00",
          training_end: forecastStart,
          statsforecast_version: "1.0.0",
          selection_reason: null,
        },
      },
    },
    source_manifest: {
      history_start: "2026-08-03T12:15:00+08:00",
      history_end: forecastStart,
      interval_seconds: 900,
      observation_count: 2688,
      series: {
        station_total_load: { retained_points: 2688, imputed_points: 0, mode: "ready", usable_week_count: 3, weeks: [] },
        storage_soc: { retained_points: 2688, imputed_points: 0, mode: "ready", usable_week_count: 3, weeks: [] },
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
    ["station_total_load", "kW", "WeeklyWeighted2", 500],
    ["storage_soc", "%", "SeasonalNaive", 60],
  ].map(([unique_id, unit, model_name, base]) => ({
    unique_id,
    unit,
    model_name,
    points: Array.from({ length: 96 }, (_, index) => ({
      target_time: toUtc(index),
      horizon_step: index + 1,
      forecast_value: base + index / 10,
      actual_value: null,
      actual_quality: null,
      absolute_percentage_error: null,
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
        { unique_id: "station_total_load", mape_percent: null, baseline_mape_percent: null, relative_baseline_improvement_percent: null, scorable_point_count: 0, run_count: 0 },
        { unique_id: "storage_soc", mape_percent: null, baseline_mape_percent: null, relative_baseline_improvement_percent: null, scorable_point_count: 0, run_count: 0 },
      ],
    },
  };
}

function warmingEvidenceFixture() {
  const fixture = weeklyEvidenceFixture();
  fixture.run.run_id = "weekly-warming-run";
  fixture.run.model_manifest.series.station_total_load.model_name = "WeeklyNaive";
  fixture.run.model_manifest.series.station_total_load.selection_metric = null;
  fixture.run.model_manifest.series.station_total_load.selection_reason = "fewer_than_three_usable_weeks";
  fixture.run.model_manifest.series.station_total_load.selection_status = "warming_up";
  fixture.run.model_manifest.series.station_total_load.candidate_scores = [];
  fixture.run.source_manifest.series.station_total_load.usable_week_count = 1;
  fixture.result.run = fixture.run;
  fixture.result.series[0].model_name = "WeeklyNaive";
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
    { unique_id: "station_total_load", mape_percent: 13, baseline_mape_percent: 16, relative_baseline_improvement_percent: 18.75, scorable_point_count: 384, run_count: 4 },
    { unique_id: "storage_soc", mape_percent: 7, baseline_mape_percent: 9, relative_baseline_improvement_percent: 22.22, scorable_point_count: 384, run_count: 4 },
  ];
  return fixture;
}

(async () => {
  const fixtures = task5Fixtures();
  const payload = fixtures.ready;
  const html = fs.readFileSync(HTML_PATH);
  const htmlText = html.toString("utf8");
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
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  await context.addCookies([{ name: "m3_token", value: "must-not-send", url: origin }]);
  const page = await context.newPage();
  const apiRequests = [];
  const externalRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const redirectRequests = [];
  let responsePayload = payload;
  let responseMode = "payload";
  let customPayload = null;
  let releaseDelayed;
  let timeoutRequestStartedResolve;

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
  await page.route("**/energy-forecast-api*", async (route) => {
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
    if (customPayload !== null && requestUrl.pathname.startsWith(`${API_PATH}/custom-`)) {
      let data;
      if (route.request().method() === "POST") data = customPayload.run;
      else if (requestUrl.pathname.endsWith("/result")) data = customPayload.result;
      else if (requestUrl.pathname.startsWith(`${API_PATH}/custom-performance/`)) data = customPayload.performance;
      else throw new Error(`unexpected custom route ${route.request().method()} ${requestUrl.pathname}`);
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ status: "ok", data }),
      });
      return;
    }
    if (responseMode === "redirect") {
      await route.fulfill({ status: 302, headers: { Location: "/redirect-target" }, body: "" });
      return;
    }
    if (responseMode === "timeout") {
      timeoutRequestStartedResolve?.();
      timeoutRequestStartedResolve = undefined;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    await route.fulfill({
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
  await page.waitForFunction(() => window.__m3AuthMessages.some((item) => item.data?.type === "vifa-m3-auth-ready"));
  assert.deepStrictEqual(apiRequests, [], "API request must wait for current-user authentication");
  const readyMessage = await page.evaluate(() => window.__m3AuthMessages.find((item) => item.data?.type === "vifa-m3-auth-ready"));
  assert.strictEqual(readyMessage.origin, origin);
  assert.deepStrictEqual(Object.keys(readyMessage.data).sort(), ["nonce", "type"]);
  assert.match(readyMessage.data.nonce, /^[a-f0-9]{32}$/);

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
  await page.locator("#forecast-dashboard[data-state='ready']").waitFor({ timeout: 3000 });

  assert.deepStrictEqual(apiRequests.map((item) => [item.method, new URL(item.url).pathname, new URL(item.url).search, item.postData]), [
    ["GET", API_PATH, "", null],
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
  assert.deepStrictEqual(await page.locator(".station-section h2").allTextContents(), ["1# 电站"]);
  const themeToggle = page.locator("#theme-toggle");
  assert.strictEqual(await themeToggle.count(), 1, "theme toggle must exist");
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "dark");
  assert.match(await themeToggle.innerText(), /白天模式/);
  const darkPaper = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--paper").trim());
  await themeToggle.click();
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "light");
  assert.match(await themeToggle.innerText(), /黑夜模式/);
  assert.strictEqual(await themeToggle.getAttribute("aria-pressed"), "true");
  assert.notStrictEqual(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--paper").trim()), darkPaper);
  await page.screenshot({ path: "/tmp/m3-task6-light.png", fullPage: true });
  await themeToggle.click();
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "dark");
  const stationPicker = page.locator("#station-picker");
  assert.strictEqual(await stationPicker.count(), 1, "station picker must exist");
  assert.deepStrictEqual(await stationPicker.locator("option").allTextContents(), ["1# 电站", "2# 电站"]);
  assert.strictEqual(await stationPicker.inputValue(), "station_1");
  assert.strictEqual(await page.locator("[data-station='station_1']").isVisible(), true);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyNaive']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='WeeklyWeighted2']").evaluate((node) => node.classList.contains("disabled")), false);
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), false);
  await page.locator("#granularity").selectOption("60");
  assert.strictEqual(await page.locator(".candidate[data-model='AutoARIMA']").evaluate((node) => node.classList.contains("disabled")), true);
  await page.locator("#granularity").selectOption("900");
  const firstStation = payload.data.stations[0];
  const firstLoad = firstStation.series[0];
  const firstSoc = firstStation.series[1];
  const currentLoad = firstLoad.actual.filter((point) => point.value !== null).at(-1).value;
  const currentSoc = firstSoc.actual.filter((point) => point.value !== null).at(-1).value;
  const peakLoad = Math.max(...firstLoad.forecast.map((point) => point.value));
  const minimumSoc = Math.min(...firstSoc.forecast.map((point) => point.value));
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-current-load").innerText(), `${Math.round(currentLoad)} kW`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-peak-load").innerText(), `${Math.round(peakLoad)} kW`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-current-soc").innerText(), `${currentSoc.toFixed(1)} %`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .metric-minimum-soc").innerText(), `${minimumSoc.toFixed(1)} %`);
  assert.strictEqual(await page.locator("[data-station='station_1'] .model-current-mape").innerText(), "2.50%");
  assert.strictEqual(await page.locator("[data-station='station_1'] .model-baseline-wape").innerText(), "—");
  assert.strictEqual(await page.locator("[data-station='station_1'] .model-improvement").innerText(), "—");
  await stationPicker.selectOption("station_2");
  assert.strictEqual(await page.locator(".station-section").getAttribute("data-station"), "station_2");
  const secondLoad = payload.data.stations[1].series[0].actual.filter((point) => point.value !== null).at(-1).value;
  assert.strictEqual(await page.locator(".metric-current-load").innerText(), `${Math.round(secondLoad)} kW`);
  assert.match(await page.locator(".acceptance-summary").innerText(), /3 \/ 7/);
  await stationPicker.selectOption("station_1");
  assert.deepStrictEqual(await page.locator(".forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  assert.strictEqual(await page.locator(".forecast-start").innerText(), "2026/08/26 10:00");
  assert.deepStrictEqual(await page.locator(".readiness-hint").allTextContents(), ["已达到 Ready 条件"]);
  assert.deepStrictEqual(await page.locator(".readiness-meta").allTextContents(), ["有效历史 28.0 / 28 天"]);
  assert.strictEqual(await page.locator(".station-acceptance").count(), 1);
  assert.deepStrictEqual(
    await page.locator("[data-station='station_1'] .acceptance-result-row").evaluateAll((rows) => rows.map((row) => row.dataset.series)),
    SERIES_IDS,
  );
  assert.deepStrictEqual(
    await page.locator("[data-station='station_1'] .acceptance-result-row").first().locator("td").allTextContents(),
    ["场站总负荷", "660 / 672", "3", "2.50%", "1.25", "2.60%", "2.40%", "1.90%", "5.20%", "通过"],
  );

  await page.evaluate((data) => window.renderDashboard(data), fixtures.initializing_normal.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator("[data-station='station_1']").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator("[data-station='station_1'] .readiness-hint").innerText(), "距离 Ready 约 5.5 天");
  assert.strictEqual(await page.locator("[data-station='station_1'] .readiness-meta").innerText(), "有效历史 22.5 / 28 天");
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.match(fixtures.microsecond_snapshot.data.system.generated_at, /\.123456\+08:00$/);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.microsecond_snapshot.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "initializing");
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.match(fixtures.microsecond_empty_error.data.stations[0].range.now_separator, /\.123456\+08:00$/);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.microsecond_empty_error.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "degraded");
  assert.deepStrictEqual(await page.locator(".station-section").evaluateAll((nodes) => nodes.map((node) => node.dataset.state)), ["error", "initializing"]);
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
  assert.strictEqual(await page.locator(".acceptance-result-row").count(), 2);
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
  assert.strictEqual(await page.locator("[data-station='station_1'] .readiness-hint").isHidden(), true);
  assert.deepStrictEqual(await page.locator("[data-station='station_1'] .forecast-value").allTextContents(), ["410 kW", "61.0 %"]);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.stale.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "stale");
  assert.strictEqual(await page.locator("[data-station='station_1']").getAttribute("data-state"), "stale");
  assert.strictEqual(await page.locator("[data-station='station_1'] .readiness-hint").isHidden(), true);
  await page.evaluate((data) => window.renderDashboard(data), fixtures.error.data);
  assert.strictEqual(await page.locator("#forecast-dashboard").getAttribute("data-state"), "degraded");
  assert.deepStrictEqual(await page.locator("[data-station='station_1'] .forecast-value").allTextContents(), ["—", "—"]);
  assert.strictEqual(await page.locator("[data-station='station_1'] path.chart-line").count(), 0);
  assert.strictEqual(await page.locator("[data-station='station_1'] .acceptance-result-row").count(), 0);
  assert.strictEqual(await page.locator("[data-station='station_1'] .readiness-hint").isHidden(), true);
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
  await page.waitForTimeout(25);
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
  assert.strictEqual(apiRequests.length, beforePoll + 1);
  assert.ok(apiRequests.every((item) => item.method === "GET" && new URL(item.url).pathname === API_PATH && new URL(item.url).search === "" && item.postData === null));
  assert.ok(apiRequests.every((item) => !item.headers.cookie && item.headers.authorization === "Bearer current-user-token" && !item.headers.referer));
  assert.ok(apiRequests.every((item) => !/must-not-send|must-not-forward|example\.invalid/.test(JSON.stringify(item.headers))));

  customPayload = weeklyEvidenceFixture();
  await page.locator("#run-button").click();
  await page.locator("#result-model-meta").getByText("3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度").waitFor();
  assert.deepStrictEqual(await page.locator(".load-wape-value").allTextContents(), ["10.00%", "10.00%"]);
  assert.deepStrictEqual(await page.locator(".load-mae-value").allTextContents(), ["8.00", "8.00"]);
  assert.ok((await page.locator(".load-mape-value").allTextContents()).every((value) => value === "11.00%"));
  assert.ok((await page.locator(".baseline-value").allTextContents()).every((value) => value === "12.50%"));
  assert.ok((await page.locator(".improvement-value").allTextContents()).every((value) => value === "20.00%"));
  assert.match(await page.locator(".current-model-name").first().innerText(), /场站总负荷：WeeklyWeighted2 · 储能 SOC：SeasonalNaive（状态锚定）/);
  assert.match(await page.locator(".candidate[data-model='WeeklyMedian3'] .candidate-state").innerText(), /跳过：无可评分点/);
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
  customPayload = warmingEvidenceFixture();
  await page.locator("#run-button").click();
  await page.locator("#result-model-meta").getByText("1 个连续有效周 · 负载周期 7 天 · 15 分钟粒度").waitFor();
  assert.match(await page.locator(".current-model-name").first().innerText(), /场站总负荷：WeeklyNaive/);
  for (const selector of [".load-wape-value", ".load-mae-value", ".load-mape-value", ".baseline-value", ".improvement-value"]) {
    assert.ok((await page.locator(selector).allTextContents()).every((value) => value === "—"), selector);
  }
  assert.match(await page.locator(".candidate[data-model='WeeklyNaive'] .candidate-state").innerText(), /预热：暂无回测分数/);

  customPayload = legacyPerformanceFixture();
  await page.locator("#run-button").click();
  await page.locator("#result-model-meta").getByText("28 天训练 · 15 分钟粒度").waitFor();
  for (const selector of [".load-wape-value", ".load-mae-value", ".load-mape-value", ".baseline-value", ".improvement-value"]) {
    assert.ok((await page.locator(selector).allTextContents()).every((value) => value === "—"), `legacy run ${selector} must not show generic aggregate metrics`);
  }
  assert.deepStrictEqual(await page.locator(".legacy-load-mape-value").allTextContents(), ["13.00%"]);
  assert.deepStrictEqual(await page.locator(".compare-model-value").allTextContents(), ["13.00%"]);
  assert.deepStrictEqual(await page.locator(".compare-baseline-value").allTextContents(), ["16.00%"]);
  assert.deepStrictEqual(await page.locator(".baseline-note-value").allTextContents(), ["16.00%"]);

  await page.screenshot({ path: "/tmp/m3-task6-desktop.png", fullPage: true });
  assert.strictEqual(await page.locator("#error-state").isHidden(), true);
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
  assert.strictEqual(await page.locator(".station-section:visible").count(), 1);
  assert.strictEqual(await page.locator(".station-section:visible .series-card:visible").count(), 2);

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
  assert.deepStrictEqual(consoleErrors, []);
  assert.deepStrictEqual(pageErrors, []);

  const wrapperPage = await context.newPage();
  const wrapperApiRequests = [];
  await wrapperPage.route("**/energy-forecast-api*", async (route) => {
    wrapperApiRequests.push({
      url: route.request().url(),
      method: route.request().method(),
      headers: await route.request().allHeaders(),
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(payload),
    });
  });
  await wrapperPage.goto(`${origin}/nocobase-host`, { waitUntil: "domcontentloaded" });
  const iframe = wrapperPage.locator("#m3-block iframe");
  await iframe.waitFor();
  assert.strictEqual(await iframe.getAttribute("src"), `${origin}/ett`);
  assert.strictEqual(await iframe.getAttribute("sandbox"), "allow-scripts allow-same-origin");
  assert.strictEqual(await iframe.getAttribute("referrerpolicy"), "no-referrer");
  await wrapperPage.frameLocator("#m3-block iframe").locator("#forecast-dashboard[data-state='ready']").waitFor({ timeout: 3000 });
  assert.deepStrictEqual(await wrapperPage.evaluate(() => window.__m3CtxVarNames), ["ctx.token"]);
  assert.strictEqual(wrapperApiRequests.length, 1);
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
  assert.strictEqual(apiRequests.length, requestsBeforeServerTokenMode + 1);
  const serverTokenRequest = apiRequests.at(-1);
  assert.strictEqual(serverTokenRequest.method, "GET");
  assert.strictEqual(new URL(serverTokenRequest.url).pathname, API_PATH);
  assert.strictEqual(serverTokenRequest.headers.authorization, undefined);
  assert.strictEqual(serverTokenRequest.headers.cookie, undefined);
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
