const assert = require("assert");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawnSync } = require("child_process");
const { once } = require("events");
const { chromium } = require("playwright");

let browser = null;
let server = null;

function assertClose(actual, expected, message) {
  assert.ok(
    Math.abs(actual - expected) < 0.001,
    `${message}: expected ${expected}, got ${actual}`,
  );
}

(async () => {
  const htmlPath = path.resolve(__dirname, "..", "场站三条能效链路能流图.html");
  const html = fs.readFileSync(htmlPath);
  server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(
      new URL(request.url, "http://127.0.0.1").pathname,
    );
    if (pathname === "/场站三条能效链路能流图.html") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(html);
      return;
    }
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("not found");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const testPort = server.address().port;

  const fixtureScript = [
    "import json",
    "from m2.tests.station_efficiency_history_test_support import build_history_dashboard_response",
    'print(json.dumps({"status": "ok", "data": build_history_dashboard_response()}))',
  ].join("; ");
  const fixture = spawnSync(
    "python3",
    ["-c", fixtureScript],
    { cwd: process.cwd(), encoding: "utf8" },
  );
  assert.strictEqual(fixture.status, 0, fixture.stderr);
  const dashboardPayload = JSON.parse(fixture.stdout);

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const apiRequests = [];
  const externalRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  let delayInitialApi = false;
  let apiShouldFail = false;
  let useEmptyPayload = false;
  let useNoSurplusPayload = false;
  let usePartialCardPayload = false;
  let useGapBoundaryPayload = false;
  let holdNextApiResponse = false;
  let heldApiResponseStartedResolve = null;
  let releaseHeldApiResponse = null;
  let normalApiFulfilledResolve = null;
  let normalApiFulfilledGeneration = 0;

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.hostname !== "127.0.0.1") externalRequests.push(request.url());
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/energy-efficiency-api*", async (route) => {
    apiRequests.push(route.request().url());
    if (usePartialCardPayload) {
      const partialCardPayload = structuredClone(dashboardPayload);
      const partialCardTime = "2026-08-25T14:38:00+08:00";
      partialCardPayload.data.range.latest_time = partialCardTime;
      partialCardPayload.data.summary_today.end_time = partialCardTime;
      partialCardPayload.data.realtime.inputs.data_time = partialCardTime;
      partialCardPayload.data.realtime.inputs.pcs_charge_power = null;
      partialCardPayload.data.realtime.result.data_time = partialCardTime;
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(partialCardPayload),
      });
      return;
    }
    if (useNoSurplusPayload) {
      const noSurplusPayload = structuredClone(dashboardPayload);
      const noSurplusTime = "2026-08-25T14:37:00+08:00";
      noSurplusPayload.data.range.latest_time = noSurplusTime;
      noSurplusPayload.data.summary_today.end_time = noSurplusTime;
      noSurplusPayload.data.realtime.inputs.data_time = noSurplusTime;
      noSurplusPayload.data.realtime.inputs.pv_ac_power = 60;
      noSurplusPayload.data.realtime.inputs.load_power = 80;
      noSurplusPayload.data.realtime.result.data_time = noSurplusTime;
      noSurplusPayload.data.realtime.result.pv_to_storage_power = 0;
      noSurplusPayload.data.realtime.result.pv_storage_efficiency = null;
      noSurplusPayload.data.realtime.result.pv_storage_dc_efficiency = null;
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(noSurplusPayload),
      });
      return;
    }
    if (useEmptyPayload) {
      const emptyPayload = structuredClone(dashboardPayload);
      emptyPayload.data.range.latest_time = null;
      emptyPayload.data.summary_today.end_time = null;
      emptyPayload.data.summary_today.pv_storage_efficiency = null;
      emptyPayload.data.summary_today.storage_load_efficiency = null;
      emptyPayload.data.summary_today.pv_load_efficiency = null;
      emptyPayload.data.trend = [];
      emptyPayload.data.events = [];
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(emptyPayload),
      });
      return;
    }
    if (apiShouldFail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ status: "error" }),
      });
      return;
    }
    if (useGapBoundaryPayload) {
      const boundaryPayload = structuredClone(dashboardPayload);
      const boundaryLatestTime = "2026-08-25T14:05:00+08:00";
      boundaryPayload.data.range.latest_time = boundaryLatestTime;
      boundaryPayload.data.summary_today.end_time = boundaryLatestTime;
      boundaryPayload.data.realtime.inputs.data_time = boundaryLatestTime;
      boundaryPayload.data.realtime.result.data_time = boundaryLatestTime;
      boundaryPayload.data.trend = [
        {
          data_time: "2026-08-25T14:00:00+08:00",
          pvStorage: 80,
          storageLoad: 81,
          pvLoad: 82,
        },
        {
          data_time: "2026-08-25T14:02:00+08:00",
          pvStorage: 83,
          storageLoad: 84,
          pvLoad: 85,
        },
        {
          data_time: "2026-08-25T14:05:00+08:00",
          pvStorage: 86,
          storageLoad: 87,
          pvLoad: 88,
        },
      ];
      boundaryPayload.data.events = [
        {
          id: 99,
          event_type: "inverter_low_load",
          type: "逆变器低负载",
          device: "边界测试逆变器",
          start: boundaryLatestTime,
          end: null,
          evidence: "触发窗口 1 分钟",
          impact: ["光→储", "光→用"],
          status: "持续中",
        },
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(boundaryPayload),
      });
      return;
    }
    if (holdNextApiResponse) {
      holdNextApiResponse = false;
      await new Promise((resolve) => {
        releaseHeldApiResponse = resolve;
        heldApiResponseStartedResolve?.();
        heldApiResponseStartedResolve = null;
      });
      releaseHeldApiResponse = null;
    }
    if (delayInitialApi) {
      delayInitialApi = false;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(dashboardPayload),
    });
    normalApiFulfilledGeneration += 1;
    normalApiFulfilledResolve?.(normalApiFulfilledGeneration);
    normalApiFulfilledResolve = null;
  });
  await page.route("https://example.invalid/leak*", async (route) => {
    apiRequests.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(dashboardPayload),
    });
  });

  async function refreshDashboard(expectedLatestTime) {
    const previousRequestCount = apiRequests.length;
    const nextApiRequest = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/energy-efficiency-api",
      { timeout: 3000 },
    );
    await Promise.all([
      nextApiRequest,
      page.evaluate(() => {
        document
          .getElementById("three-energy-flow")
          .dispatchEvent(new Event("energy-dashboard-refresh"));
      }),
    ]);
    const expectedRequestCount = previousRequestCount + 1;
    const requestCountDeadline = Date.now() + 3000;
    while (
      apiRequests.length < expectedRequestCount &&
      Date.now() < requestCountDeadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.strictEqual(apiRequests.length, expectedRequestCount);
    await page.waitForFunction(
      (latestTime) => {
        const dashboardRoot = document.getElementById("three-energy-flow");
        return dashboardRoot?.dataset.dashboardState === "ready" &&
          dashboardRoot?.dataset.latestTime === latestTime;
      },
      expectedLatestTime,
    );
  }

  async function waitForStableDesktopOverlay() {
    await page.evaluate(() => new Promise((resolve, reject) => {
      let previous = null;
      let stableFrames = 0;
      let checkedFrames = 0;
      const check = () => {
        checkedFrames += 1;
        const overlay = document.querySelector(
          "#efficiency-trend-chart rect[aria-hidden='true']",
        );
        const trendSection = document.querySelector(".trend-section");
        const desktopTrack = document.querySelector(
          "#pv-storage-branch .flow-track",
        );
        if (overlay && trendSection && desktopTrack) {
          const overlayRect = overlay.getBoundingClientRect();
          const trendRect = trendSection.getBoundingClientRect();
          const desktopColumns = getComputedStyle(desktopTrack)
            .gridTemplateColumns.trim().split(/\s+/);
          const current = {
            overlay,
            left: overlayRect.left,
            top: overlayRect.top,
            width: overlayRect.width,
            height: overlayRect.height,
            trendLeft: trendRect.left,
            trendRight: trendRect.right,
          };
          const valid = window.innerWidth === 1280 &&
            desktopColumns.length > 1 &&
            overlayRect.width > 0 &&
            overlayRect.height > 0 &&
            trendRect.width > 0 &&
            overlayRect.left >= trendRect.left &&
            overlayRect.right <= trendRect.right;
          const unchanged = valid && previous &&
            previous.overlay === current.overlay &&
            previous.left === current.left &&
            previous.top === current.top &&
            previous.width === current.width &&
            previous.height === current.height &&
            previous.trendLeft === current.trendLeft &&
            previous.trendRight === current.trendRight;
          stableFrames = unchanged ? stableFrames + 1 : 0;
          previous = valid ? current : null;
          if (stableFrames >= 2) {
            resolve();
            return;
          }
        } else {
          previous = null;
          stableFrames = 0;
        }
        if (checkedFrames >= 180) {
          reject(new Error("desktop trend overlay did not stabilize"));
          return;
        }
        requestAnimationFrame(check);
      };
      requestAnimationFrame(check);
    }));
  }

  const fileName = encodeURIComponent("场站三条能效链路能流图.html");
  delayInitialApi = true;
  let navigationError = null;
  const navigationPromise = page
    .goto(
      `http://127.0.0.1:${testPort}/${fileName}`,
      { waitUntil: "networkidle" },
    )
    .catch((error) => {
      navigationError = error;
      return null;
    });
  await page.locator("#event-table-body").waitFor({ timeout: 3000 });
  assert.strictEqual(
    await page.locator("#event-table-body").innerText(),
    "正在加载瓶颈事件",
  );
  const navigationResponse = await navigationPromise;
  if (navigationError) throw navigationError;
  try {
    await page.locator("#three-energy-flow[data-dashboard-state='ready']").waitFor({
      timeout: 3000,
    });
  } catch (error) {
    const root = page.locator("#three-energy-flow");
    const rootCount = await root.count();
    const diagnostics = {
      pageUrl: page.url(),
      navigationStatus: navigationResponse?.status(),
      rootCount,
      rootState: rootCount
        ? await root.getAttribute("data-dashboard-state")
        : null,
      statusText: rootCount
        ? await page.locator("#dashboard-status").innerText()
        : null,
      bodyText: (await page.locator("body").innerText()).slice(0, 500),
      apiRequests,
      externalRequests,
      consoleErrors,
      pageErrors,
    };
    process.stderr.write(`dashboard diagnostics: ${JSON.stringify(diagnostics, null, 2)}\n`);
    throw error;
  }

  const station1Tab = page.getByRole("tab", { name: "电站1" });
  const station2Tab = page.getByRole("tab", { name: "电站2" });
  assert.strictEqual(await station1Tab.count(), 1);
  assert.strictEqual(await station2Tab.count(), 1);
  assert.strictEqual(await station1Tab.getAttribute("aria-selected"), "false");
  assert.strictEqual(await station2Tab.getAttribute("aria-selected"), "true");

  assert.strictEqual(await page.locator("#pv-storage-efficiency").innerText(), "88.83%");
  assert.strictEqual(await page.locator("#storage-load-efficiency").innerText(), "无运行数据");
  assert.strictEqual(await page.locator("#pv-load-efficiency").innerText(), "100.00%");
  assert.strictEqual(await page.locator("#pv-dc-power").innerText(), "190");
  assert.strictEqual(await page.locator("#pv-ac-power").innerText(), "183");
  assert.strictEqual(
    await page.locator("#pv-inverter-loss").innerText(),
    "光伏逆变器 · 损耗7 kW",
  );
  assert.strictEqual(
    await page.locator("#pv-storage-formula").innerText(),
    "95 ÷ 106.94",
  );
  assert.deepStrictEqual(
    await page.locator("#pv-shared-path .node-title").allInnerTexts(),
    ["光伏总输入", "光伏总输出"],
  );
  assert.deepStrictEqual(
    await page.locator("#three-energy-flow .flow-legend .legend-item").allInnerTexts(),
    ["光→储", "储→用", "光→用"],
  );
  assert.strictEqual(
    await page.locator("#pv-load-branch .branch-label > span").first().innerText(),
    "光→用",
  );
  assert.strictEqual(
    await page.locator("#pv-storage-branch .branch-label > span").first().innerText(),
    "光→储",
  );
  assert.deepStrictEqual(
    await page.locator("#pv-load-branch .node-title").allInnerTexts(),
    ["场站负载"],
  );
  assert.deepStrictEqual(
    await page.locator("#lane-storage-load .node-title").allInnerTexts(),
    ["BMS电池", "PCS", "柜内电表", "场站负载"],
  );
  assert.deepStrictEqual(
    await page.locator("#pv-storage-branch .node-title").allInnerTexts(),
    ["光伏余量", "混合供能", "柜内电表", "PCS", "BMS电池"],
  );
  assert.strictEqual(
    await page.locator("#grid-storage-share").innerText(),
    "0.00%",
  );
  assert.ok(
    (await page.locator("#grid-storage-share").locator("xpath=..").innerText())
      .includes("市电入储（推算）"),
  );
  assert.strictEqual(
    await page.locator("#pv-storage-branch").getAttribute("data-state"),
    "active",
  );
  await page.setViewportSize({ width: 700, height: 900 });
  const mobileColumnCounts = await page.evaluate(() => [
    "#pv-shared-path",
    "#pv-load-branch",
    "#pv-storage-branch",
    "#pv-storage-branch .flow-track",
    "#lane-storage-load .flow-track",
  ].map((selector) => getComputedStyle(document.querySelector(selector))
    .gridTemplateColumns.trim().split(/\s+/).length));
  assert.deepStrictEqual(mobileColumnCounts, [1, 1, 1, 1, 1]);
  await page.setViewportSize({ width: 1280, height: 720 });
  await waitForStableDesktopOverlay();
  assert.strictEqual(await page.locator("#current-efficiency-title").count(), 1);
  assert.strictEqual(await page.locator("#current-efficiency-title").innerText(), "当前链路效率");
  assert.strictEqual(await page.locator("#trend-title").innerText(), "今日24小时效率曲线");
  assert.strictEqual(await page.locator("#summary-pv-storage").innerText(), "88.83%");
  assert.strictEqual(await page.locator("#summary-storage-load").innerText(), "无运行数据");
  assert.strictEqual(await page.locator("#summary-pv-load").innerText(), "100.00%");
  assert.deepStrictEqual(
    await page.locator("#summary-card-pv-storage").innerText(),
    "光储链路\n88.83%",
  );
  assert.deepStrictEqual(
    await page.locator("#summary-card-storage-load").innerText(),
    "储用链路\n无运行数据",
  );
  assert.deepStrictEqual(
    await page.locator("#summary-card-pv-load").innerText(),
    "光用链路\n100.00%",
  );
  assert.strictEqual(
    (await page.locator("#efficiency-readout").innerText()).replace(/　/g, " "),
    "14:36 光储88.83% 储用无运行数据 光用100.00%",
  );
  assert.strictEqual(
    await page.locator("[data-hour-tick='0']").textContent(),
    "00:00",
  );
  assert.strictEqual(
    await page.locator("[data-hour-tick='24']").textContent(),
    "24:00",
  );
  assert.strictEqual(
    await page.locator("#efficiency-trend-chart").getAttribute("data-latest-time"),
    "2026-08-25T14:36:00+08:00",
  );
  assert.strictEqual(
    await page.locator("#three-energy-flow").getAttribute("data-latest-time"),
    "2026-08-25T14:36:00+08:00",
  );
  const pvPath = await page.locator("path[data-series='pvStorage']").getAttribute("d");
  assert.strictEqual((pvPath.match(/ M /g) || []).length, 2);
  const pvXCoordinates = [...pvPath.matchAll(/[ML]\s+([0-9.]+)/g)].map((match) => Number(match[1]));
  const chartWidth = Number(
    (await page.locator("#efficiency-trend-chart").getAttribute("viewBox")).split(/\s+/)[2],
  );
  assert.ok(pvXCoordinates.at(-1) < chartWidth - 18);
  const storagePath = await page.locator("path[data-series='storageLoad']").getAttribute("d");
  assert.strictEqual((storagePath.match(/ M /g) || []).length, 3);
  const chartOverlay = page.locator(
    "#efficiency-trend-chart rect[aria-hidden='true']",
  );
  const plotX = Number(await chartOverlay.getAttribute("x"));
  const plotWidth = Number(await chartOverlay.getAttribute("width"));
  const recoveredEvent = page.locator(
    "rect[data-event-type='battery_temperature_rise'][data-event-status='已恢复']",
  );
  const activeEvent = page.locator(
    "rect[data-event-type='inverter_low_load'][data-event-status='持续中']",
  );
  assert.strictEqual(
    await recoveredEvent.getAttribute("data-event-start"),
    "2026-08-24T23:50:00+08:00",
  );
  assert.strictEqual(
    await recoveredEvent.getAttribute("data-event-end"),
    "2026-08-25T00:10:00+08:00",
  );
  const recoveredX = Number(await recoveredEvent.getAttribute("x"));
  const recoveredWidth = Number(await recoveredEvent.getAttribute("width"));
  assert.ok(recoveredWidth > 0);
  assertClose(recoveredX, plotX, "recovered event starts at day boundary");
  assertClose(
    recoveredX + recoveredWidth,
    plotX + plotWidth * (10 / (24 * 60)),
    "recovered event ends at 00:10",
  );
  assert.strictEqual(
    await activeEvent.getAttribute("data-event-start"),
    "2026-08-25T14:30:00+08:00",
  );
  assert.strictEqual(
    await activeEvent.getAttribute("data-event-end"),
    "2026-08-25T14:36:00+08:00",
  );
  const activeX = Number(await activeEvent.getAttribute("x"));
  const activeWidth = Number(await activeEvent.getAttribute("width"));
  assert.ok(activeWidth > 0);
  assertClose(
    activeX + activeWidth,
    plotX + plotWidth * ((14 * 60 + 36) / (24 * 60)),
    "active event ends at latest_time",
  );
  assert.strictEqual(
    await page.locator("#event-table-body tr").count(),
    2,
  );
  assert.deepStrictEqual(
    await page.locator("#event-table-body tr").first().locator("td").allInnerTexts(),
    ["08/24 23:50", "电池温升", "1#电池簇", "5 分钟最大温升 3.50℃", "光→储、储→用", "已恢复"],
  );
  await waitForStableDesktopOverlay();
  await chartOverlay.scrollIntoViewIfNeeded();
  await chartOverlay.waitFor({ state: "visible" });
  await waitForStableDesktopOverlay();
  await chartOverlay.hover({ trial: true });
  const overlayBox = await chartOverlay.boundingBox();
  assert.ok(overlayBox);
  await chartOverlay.hover({
    position: {
      x: overlayBox.width * ((14 * 60 + 32) / (24 * 60)),
      y: overlayBox.height / 2,
    },
  });
  assert.strictEqual(
    (await page.locator("#efficiency-readout").innerText()).replace(/　/g, " "),
    "14:33 光储88.06% 储用90.00% 光用95.00%",
  );
  const requestsBeforeLockCheck = apiRequests.length;
  const generationBeforeLockCheck = normalApiFulfilledGeneration;
  holdNextApiResponse = true;
  const heldApiResponseStarted = new Promise((resolve) => {
    heldApiResponseStartedResolve = resolve;
  });
  const heldApiResponseFulfilled = new Promise((resolve) => {
    normalApiFulfilledResolve = resolve;
  });
  const lockedRefreshRequest = page.waitForRequest(
    (request) =>
      new URL(request.url()).pathname === "/energy-efficiency-api",
    { timeout: 3000 },
  );
  await Promise.all([
    lockedRefreshRequest,
    page.evaluate(() => {
      const root = document.getElementById("three-energy-flow");
      root.dispatchEvent(new Event("energy-dashboard-refresh"));
      root.dispatchEvent(new Event("energy-dashboard-refresh"));
    }),
  ]);
  await heldApiResponseStarted;
  try {
    assert.strictEqual(apiRequests.length, requestsBeforeLockCheck + 1);
    assert.strictEqual(
      await page.locator("#three-energy-flow").getAttribute("data-dashboard-state"),
      "loading",
    );
  } finally {
    assert.ok(releaseHeldApiResponse);
    releaseHeldApiResponse();
  }
  assert.strictEqual(
    await heldApiResponseFulfilled,
    generationBeforeLockCheck + 1,
  );
  await page.waitForFunction(() => {
    const dashboardRoot = document.getElementById("three-energy-flow");
    return dashboardRoot?.dataset.dashboardState === "ready" &&
      dashboardRoot?.dataset.latestTime === "2026-08-25T14:36:00+08:00";
  });
  assert.strictEqual(apiRequests.length, requestsBeforeLockCheck + 1);
  assert.strictEqual(apiRequests.length, 2);
  const initialApiUrl = new URL(apiRequests[0]);
  assert.strictEqual(initialApiUrl.searchParams.get("station_id"), "ES02");
  assert.strictEqual(initialApiUrl.searchParams.has("token"), false);

  await Promise.all([
    page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/energy-efficiency-api" &&
        new URL(request.url()).searchParams.get("station_id") === "ES01",
      { timeout: 3000 },
    ),
    station1Tab.click(),
  ]);
  await page.waitForFunction(() => {
    const dashboardRoot = document.getElementById("three-energy-flow");
    return dashboardRoot?.dataset.stationId === "ES01" &&
      dashboardRoot?.dataset.dashboardState === "ready";
  });
  assert.strictEqual(await station1Tab.getAttribute("aria-selected"), "true");
  assert.strictEqual(await station2Tab.getAttribute("aria-selected"), "false");
  assert.strictEqual(new URL(page.url()).searchParams.get("station_id"), "ES01");

  await Promise.all([
    page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/energy-efficiency-api" &&
        new URL(request.url()).searchParams.get("station_id") === "ES02",
      { timeout: 3000 },
    ),
    station2Tab.click(),
  ]);
  await page.waitForFunction(() => {
    const dashboardRoot = document.getElementById("three-energy-flow");
    return dashboardRoot?.dataset.stationId === "ES02" &&
      dashboardRoot?.dataset.dashboardState === "ready";
  });
  assert.strictEqual(await station1Tab.getAttribute("aria-selected"), "false");
  assert.strictEqual(await station2Tab.getAttribute("aria-selected"), "true");
  assert.strictEqual(new URL(page.url()).searchParams.get("station_id"), "ES02");
  assert.deepStrictEqual(externalRequests, []);
  assert.deepStrictEqual(consoleErrors, []);
  assert.deepStrictEqual(pageErrors, []);
  if (process.env.ENERGY_DASHBOARD_SCREENSHOT) {
    await page.screenshot({
      path: process.env.ENERGY_DASHBOARD_SCREENSHOT,
      fullPage: true,
    });
  }

  const boundaryLatestTime = "2026-08-25T14:05:00+08:00";
  useGapBoundaryPayload = true;
  let boundaryError = null;
  try {
    await refreshDashboard(boundaryLatestTime);
    const boundaryPath = await page
      .locator("path[data-series='pvStorage']")
      .getAttribute("d");
    assert.strictEqual((boundaryPath.match(/ M /g) || []).length, 2);
    assert.strictEqual((boundaryPath.match(/ L /g) || []).length, 1);

    const boundaryOverlay = page.locator(
      "#efficiency-trend-chart rect[aria-hidden='true']",
    );
    const boundaryPlotX = Number(await boundaryOverlay.getAttribute("x"));
    const boundaryPlotWidth = Number(await boundaryOverlay.getAttribute("width"));
    const expectedMarkerX = boundaryPlotX +
      boundaryPlotWidth * ((14 * 60 + 5) / (24 * 60));
    const zeroDurationEvent = page.locator(
      "rect[data-event-type='inverter_low_load'][data-event-status='持续中']",
    );
    assert.strictEqual(Number(await zeroDurationEvent.getAttribute("width")), 0);
    assertClose(
      Number(await zeroDurationEvent.getAttribute("x")),
      expectedMarkerX,
      "zero-duration event rect stays at its true time",
    );
    const eventMarker = page.locator(
      "line[data-event-marker='inverter_low_load']",
    );
    assert.strictEqual(await eventMarker.count(), 1);
    assert.strictEqual(
      await eventMarker.getAttribute("data-event-time"),
      boundaryLatestTime,
    );
    const markerX = Number(await eventMarker.getAttribute("x1"));
    assertClose(markerX, expectedMarkerX, "event marker stays at its true time");
    assertClose(
      Number(await eventMarker.getAttribute("x2")),
      markerX,
      "event marker is vertical",
    );
    assert.ok(markerX >= boundaryPlotX);
    assert.ok(markerX <= boundaryPlotX + boundaryPlotWidth);
    assert.ok(Number(await eventMarker.getAttribute("stroke-width")) >= 2);
    assert.strictEqual(
      await eventMarker.getAttribute("clip-path"),
      "url(#efficiency-event-clip)",
    );
  } catch (error) {
    boundaryError = error;
  } finally {
    useGapBoundaryPayload = false;
    try {
      await refreshDashboard("2026-08-25T14:36:00+08:00");
    } catch (restoreError) {
      if (!boundaryError) throw restoreError;
      boundaryError.message += `; restore failed: ${restoreError.message}`;
    }
  }
  if (boundaryError) throw boundaryError;
  const restoredStoragePath = await page
    .locator("path[data-series='storageLoad']")
    .getAttribute("d");
  assert.strictEqual((restoredStoragePath.match(/ M /g) || []).length, 3);
  assert.deepStrictEqual(consoleErrors, []);
  assert.deepStrictEqual(pageErrors, []);

  useNoSurplusPayload = true;
  await refreshDashboard("2026-08-25T14:37:00+08:00");
  assert.strictEqual(
    await page.locator("#pv-storage-branch").getAttribute("data-state"),
    "inactive",
  );
  useNoSurplusPayload = false;
  await refreshDashboard("2026-08-25T14:36:00+08:00");

  usePartialCardPayload = true;
  await refreshDashboard("2026-08-25T14:38:00+08:00");
  const partialCardOpacity = await page.evaluate(() => ({
    pcs: Number(getComputedStyle(document.getElementById("pcs-charge-power").closest(".card")).opacity),
    cabinet: Number(getComputedStyle(document.getElementById("cabinet-charge-power").closest(".card")).opacity),
  }));
  assert.ok(partialCardOpacity.pcs <= 0.5);
  assert.strictEqual(partialCardOpacity.cabinet, 1);
  usePartialCardPayload = false;
  await refreshDashboard("2026-08-25T14:36:00+08:00");

  useEmptyPayload = true;
  await refreshDashboard("");
  assert.strictEqual(
    await page.locator("path[data-series='pvStorage']").getAttribute("d"),
    "",
  );
  assert.strictEqual(
    await page.locator("path[data-series='storageLoad']").getAttribute("d"),
    "",
  );
  assert.strictEqual(
    await page.locator("path[data-series='pvLoad']").getAttribute("d"),
    "",
  );
  assert.strictEqual(await page.locator("#summary-card-pv-storage").isVisible(), true);
  assert.strictEqual(await page.locator("#summary-card-storage-load").isVisible(), true);
  assert.strictEqual(await page.locator("#summary-card-pv-load").isVisible(), true);
  assert.deepStrictEqual(
    await page.locator("#summary-card-pv-storage").innerText(),
    "光储链路\n88.83%",
  );
  assert.deepStrictEqual(
    await page.locator("#summary-card-storage-load").innerText(),
    "储用链路\n无运行数据",
  );
  assert.deepStrictEqual(
    await page.locator("#summary-card-pv-load").innerText(),
    "光用链路\n100.00%",
  );
  assert.strictEqual(
    await page.locator("#event-table-body").innerText(),
    "当前时间范围内无瓶颈事件",
  );
  useEmptyPayload = false;

  apiShouldFail = true;
  await page.evaluate(() => {
    document
      .getElementById("three-energy-flow")
      .dispatchEvent(new Event("energy-dashboard-refresh"));
  });
  await page
    .locator("#three-energy-flow[data-dashboard-state='error']")
    .waitFor({ timeout: 3000 });
  assert.strictEqual(
    await page.locator("#dashboard-status").innerText(),
    "后端数据加载失败",
  );
  assert.strictEqual(
    (await page.locator("#efficiency-readout").innerText()).replace(/　/g, " "),
    "光储无运行数据 储用无运行数据 光用无运行数据",
  );

  await browser.close();
  browser = null;
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  server = null;
  process.stdout.write("energy_dashboard_e2e_ok\n");
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  Promise.resolve(browser?.close())
    .catch(() => {})
    .then(
      () =>
        new Promise((resolve) => {
          if (!server?.listening) return resolve();
          server.close(() => resolve());
        }),
    )
    .finally(() => {
      process.exitCode = 1;
    });
});
