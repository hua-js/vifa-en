"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../node_red/m3_production_gateway_template.html"), "utf8");
function source(name) {
  const start = html.search(new RegExp(`      (?:async )?function ${name}\\(`));
  assert.ok(start >= 0, name);
  const rest = html.slice(start + 1);
  const end = rest.search(/\n      (?:async )?function /);
  return end < 0 ? rest : rest.slice(0, end);
}
const timers = new Map();
let seq = 0;
const sandbox = {
  document: { visibilityState: "visible" },
  window: {
    setTimeout(fn, delay) { timers.set(++seq, { fn, delay }); return seq; },
    clearTimeout(id) { timers.delete(id); },
  },
  customPollTimer: null, selectedPollRunId: null, customPollDelay: 2000,
  customPollInFlight: false, disposed: false, latestLookupToken: 0,
  refreshTimer: null, lastRefreshSlot: "initial", currentRefreshSlot: () => "initial",
  startRefreshTimer() {}, userTokenAuth: false, apiToken: null,
  stationPicker: { value: "station_1" }, CUSTOM_RUN_PATH: "/custom-runs",
  requests: 0, customRequest: async () => { sandbox.requests++; return { status: "running" }; },
  validateCustomRun: x => x,
  handleCustomRun: async () => {}, setCustomBusy() {}, renderRefreshError() {},
  rollTaskDates() {}, loadDashboard() {}, refreshSelectedCustomResult() {}, refreshSelectedPerformance() {},
};
vm.createContext(sandbox);
for (const name of ["stopCustomPolling", "scheduleCustomPoll", "pollCustomRun", "refreshVisiblePage"]) {
  vm.runInContext(source(name), sandbox);
}
async function main() {
  const delays = [];
  for (let i = 0; i < 6; i++) {
    sandbox.scheduleCustomPoll("station_1", "run-1");
    assert.equal(timers.size, 1);
    delays.push([...timers.values()][0].delay);
  }
  assert.deepEqual(delays, [2000, 4000, 8000, 15000, 15000, 15000]);
  sandbox.document.visibilityState = "hidden";
  sandbox.refreshVisiblePage();
  assert.equal(timers.size, 0);
  assert.equal(sandbox.selectedPollRunId, "run-1");
  await sandbox.pollCustomRun("station_1", "run-1");
  assert.equal(sandbox.requests, 0);
  sandbox.document.visibilityState = "visible";
  sandbox.refreshVisiblePage();
  assert.equal(timers.size, 1);
  sandbox.refreshVisiblePage();
  assert.equal(timers.size, 1);
  // An in-progress request must not be duplicated on return to the page.
  sandbox.customPollInFlight = true;
  await sandbox.pollCustomRun("station_1", "run-1");
  assert.equal(sandbox.requests, 0);
  sandbox.customPollInFlight = false;
  sandbox.stopCustomPolling();
  assert.equal(timers.size, 0);
  await sandbox.pollCustomRun("station_1", "run-1");
  assert.equal(sandbox.requests, 0);
  sandbox.scheduleCustomPoll("station_1", "run-2");
  assert.equal([...timers.values()][0].delay, 2000);
  await sandbox.pollCustomRun("station_1", "run-2");
  assert.equal(sandbox.requests, 1);
  sandbox.stopCustomPolling();
  let now = Date.parse("2026-09-22T10:02:00+08:00");
  let refreshes = 0;
  Object.assign(sandbox, {
    Date: { now: () => now }, granularity: { value: "900" }, forecastDays: { value: "1" },
    ALLOWED_INTERVALS: [30, 60, 300, 900, 1800, 3600], lastRefreshSlot: null,
    loadDashboard() { refreshes++; },
  });
  for (const name of ["refreshIntervalMs", "currentRefreshSlot", "startRefreshTimer"]) {
    vm.runInContext(source(name), sandbox);
  }
  sandbox.startRefreshTimer(true);
  assert.equal([...timers.values()][0].delay, 13 * 60000);
  // Returning within the same quarter-hour must not perform another read.
  now += 60000;
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 0);
  now = Date.parse("2026-09-22T10:15:00+08:00");
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 1);
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 1);
  sandbox.document.visibilityState = "hidden";
  sandbox.refreshVisiblePage();
  assert.equal(timers.size, 0);
  now += 45 * 60000;
  sandbox.document.visibilityState = "visible";
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 2, "missed intervals produce one catch-up read");
  sandbox.granularity.value = "3600";
  sandbox.startRefreshTimer(true);
  assert.equal([...timers.values()][0].delay, 3600000);
  now += 15 * 60000;
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 2, "hourly forecasts must not refresh each quarter-hour");
  sandbox.granularity.value = "30";
  sandbox.startRefreshTimer(true);
  assert.equal([...timers.values()][0].delay, 30000);
  now += 30000;
  sandbox.refreshVisiblePage();
  assert.equal(refreshes, 3);
  sandbox.document.visibilityState = "hidden";
  sandbox.refreshVisiblePage();
  assert.equal(timers.size, 0);
  // Reproduce the observed 25.6s response using a virtual clock.
  let finishFetch;
  let aborted = false;
  let rendered = 0;
  Object.assign(sandbox, {
    userTokenAuth: false, apiToken: null, inFlight: null, API_PATH: "/energy-forecast-api",
    AbortController: class { constructor() { this.signal = {}; } abort() { aborted = true; } },
    fetch: () => new Promise(resolve => { finishFetch = resolve; }),
    scanShape() {}, exactObject() {}, assert,
    renderDashboard() { rendered++; },
  });
  vm.runInContext(source("loadDashboard"), sandbox);
  const request = sandbox.loadDashboard();
  assert.equal(timers.size, 1);
  const abortTimer = [...timers.values()][0];
  if (abortTimer.delay <= 25601) abortTimer.fn();
  assert.equal(aborted, false, "the observed response must not be aborted at 10s");
  assert.equal(abortTimer.delay, 45000);
  finishFetch({ ok: true, status: 200, json: async () => ({ status: "ok", data: {} }) });
  await request;
  assert.equal(rendered, 1);
  assert.equal(timers.size, 0);
  console.log("M3 polling: backoff, hidden pause, resume, deduplication and stop passed");
  console.log("M3 dashboard: 25.6s response accepted within 45s budget");
  console.log("M3 cadence: selected granularity, same-slot deduplication and one catch-up read passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
