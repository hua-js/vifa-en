'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../node_red/m3_production_gateway_template.html'), 'utf8');
function source(name) {
  const match = html.match(new RegExp(`^      (?:async )?function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^      \\}`, 'm'));
  assert.ok(match, name);
  return match[0];
}
function load(sandbox, names) {
  vm.createContext(sandbox);
  names.forEach(name => vm.runInContext(source(name), sandbox));
  return sandbox;
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function performanceFixture() {
  const calls = [];
  let day = '2026-10-12';
  const sandbox = {
    performanceConfiguration: () => ({ interval_seconds: 900, forecast_days: 1, history_days: 28 }),
    stationPicker: { value: 'station_1' }, performanceLookupToken: 0,
    selectedPerformance: null, performanceState: '', performanceReads: new Map(),
    authMode: 'server_token', userTokenAuth: false, apiToken: null, disposed: false,
    CUSTOM_PERFORMANCE_PATH: '/energy-forecast-api/custom-performance', URLSearchParams,
    shanghaiDateValue: () => day,
    renderSelectedPerformance() {}, validateCustomPerformance: value => value,
    assert, performanceSnapshotKey: () => "same-policy", rememberPerformanceSnapshot() {},
    customRequest(url) { const read = deferred(); calls.push({ url, ...read }); return read.promise; },
  };
  load(sandbox, ['refreshSelectedPerformance']);
  return { sandbox, calls, setDay(value) { day = value; } };
}

test('M3 shares one in-flight statistics read for identical configurations', async () => {
  const { sandbox, calls } = performanceFixture();
  const first = sandbox.refreshSelectedPerformance();
  const second = sandbox.refreshSelectedPerformance();
  assert.equal(calls.length, 1, 'same configuration must not send duplicate statistics requests');
  const value = { label: 'current statistics fixture' };
  calls[0].resolve(value);
  await Promise.all([first, second]);
  assert.equal(sandbox.selectedPerformance.value, value);
  assert.equal(sandbox.performanceReads.size, 0);
  const next = sandbox.refreshSelectedPerformance();
  assert.equal(calls.length, 2, 'completed reads must not hide a later refresh');
  calls[1].resolve(value);
  await next;
});

test('M3 separates statistics reads by station and ignores a late old response', async () => {
  const { sandbox, calls } = performanceFixture();
  const first = sandbox.refreshSelectedPerformance();
  sandbox.stationPicker.value = 'station_2';
  const second = sandbox.refreshSelectedPerformance();
  assert.equal(calls.length, 2);
  const current = { label: 'station two fixture' };
  calls[1].resolve(current);
  await second;
  calls[0].resolve({ label: 'old station one fixture' });
  await first;
  assert.equal(sandbox.selectedPerformance.value, current);
  assert.equal(sandbox.performanceReads.size, 0);
});

test('M3 removes failed statistics reads so a later refresh can retry', async () => {
  const { sandbox, calls } = performanceFixture();
  const request = sandbox.refreshSelectedPerformance();
  calls[0].reject(new Error('unavailable'));
  await request;
  assert.equal(sandbox.performanceState, '统计加载失败');
  assert.equal(sandbox.performanceReads.size, 0);
  const retry = sandbox.refreshSelectedPerformance();
  assert.equal(calls.length, 2);
  calls[1].resolve({});
  await retry;
});

test('M3 does not merge a statistics read across the business-day boundary', async () => {
  const { sandbox, calls, setDay } = performanceFixture();
  const first = sandbox.refreshSelectedPerformance();
  setDay('2026-10-13');
  const second = sandbox.refreshSelectedPerformance();
  assert.equal(calls.length, 2);
  calls[0].resolve({}); calls[1].resolve({});
  await Promise.all([first, second]);
  assert.equal(sandbox.performanceReads.size, 0);
});

function stationFixture() {
  const nodes = new Map(), renders = [], latestReads = [];
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, { dataset: {}, textContent: '', hidden: true, setAttribute() {} });
    return nodes.get(key);
  };
  const sandbox = {
    STATION_KEYS: ['station_1', 'station_2'], STATION_NAMES: ['1# 电站', '2# 电站'],
    stationPicker: { value: 'station_2' }, forecastDays: { value: '1' }, granularity: { value: '900' },
    latestLookupToken: 0, performanceLookupToken: 0, selectedPerformance: null, performanceState: '',
    displayedConfigurationRunKey: null, customBusy: false, hasRenderedCustomResult: false,
    customDisplayDay: "2026-10-12", resultSnapshots: new Map(), performanceSnapshots: new Map(),
    prefetchStationViews() {}, saveViewSnapshots() {},
    storeRunId(stationKey, runId) { sandbox.customRunIds[stationKey] = runId; },
    customResults: new Map(), customRunIds: { station_1: 'test-run-1', station_2: 'test-run-2' },
    root: { querySelector: node, querySelectorAll: () => [] },
    resultSection: node('.station-section'), resultProgressOverlay: node('overlay'),
    runButton: node('button'), errorState: node('error'), taskState: node('task'),
    selectedOutputConfiguration: () => ({ interval_seconds: 900, forecast_days: 1 }),
    stopCustomPolling() {}, clearSection() {}, updateTaskConfiguration() {}, startRefreshTimer() {},
    text(target, value) { target.textContent = value; },
    clearStoredRunId(stationKey) { delete sandbox.customRunIds[stationKey]; },
    initializeTaskDates() {}, loadDashboard() {}, refreshSelectedPerformance() {},
    renderCustomResult(value) { renders.push(value); sandbox.hasRenderedCustomResult = true; },
    loadLatestMatchingRun(...args) { latestReads.push(args); },
    Date: class extends Date { constructor(value) { super(value === undefined ? '2026-10-12T10:00:00+08:00' : value); } },
    milliseconds: value => Date.parse(value),
  };
  load(sandbox, ['shanghaiDateValue', 'runMatchesOutput', 'runCoversToday', 'resultSnapshotKey', 'selectStation', 'changeStation']);
  for (const stationKey of sandbox.STATION_KEYS) sandbox.customResults.set(stationKey, {
    stationKey, result: { run: { run_id: sandbox.customRunIds[stationKey], forecast_days: 1, interval_seconds: 900,
      forecast_start: '2026-10-12T00:00:00+08:00', forecast_end: '2026-10-13T00:00:00+08:00' } },
  });
  return { sandbox, renders, latestReads };
}

test('M3 retains both station caches and displays the selected one while refreshing', () => {
  const { sandbox, renders, latestReads } = stationFixture();
  sandbox.changeStation();
  assert.equal(sandbox.customResults.size, 2, 'switching stations must not clear all cached results');
  assert.equal(renders.length, 1);
  assert.equal(renders[0].stationKey, 'station_2');
  assert.match(sandbox.taskState.textContent, /上次预测.*正在更新/);
  assert.equal(latestReads.length, 1);
  assert.equal(latestReads[0][3], false, 'cached data must not be covered by a blocking overlay');
});

test('M3 keeps the stored run identity for a retained station cache', () => {
  const { sandbox } = stationFixture();
  sandbox.changeStation();
  assert.equal(sandbox.customRunIds.station_2, 'test-run-2', 'a failed latest lookup must not strand the cached run refresh');
});

test('M3 falls back to a fresh lookup when a cached result cannot render', () => {
  const { sandbox, latestReads } = stationFixture();
  sandbox.renderCustomResult = () => { throw new Error('snapshot render unavailable'); };
  sandbox.changeStation();
  assert.equal(sandbox.customResults.has('station_2'), false);
  assert.equal(sandbox.customResults.has('station_1'), true);
  assert.equal(latestReads.length, 1);
  assert.equal(latestReads[0][3], true);
});

test('M3 does not display a cached run from yesterday', () => {
  const { sandbox, renders, latestReads } = stationFixture();
  const cached = sandbox.customResults.get('station_2');
  cached.result.run.forecast_start = '2026-10-11T00:00:00+08:00';
  cached.result.run.forecast_end = '2026-10-12T00:00:00+08:00';
  sandbox.changeStation();
  assert.equal(renders.length, 0);
  assert.equal(latestReads[0][3], true);
});

test('M3 does not display a cache with a different output configuration', () => {
  const { sandbox, renders, latestReads } = stationFixture();
  sandbox.customResults.get('station_2').result.run.interval_seconds = 3600;
  sandbox.changeStation();
  assert.equal(renders.length, 0);
  assert.equal(latestReads[0][3], true);
});

function bootFixture(mode = 'server_token', origin = 'https://example.invalid', search = '') {
  const nodes = new Map(), calls = [], listeners = new Map(), messages = [];
  let timerId = 0;
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, style: {}, textContent: '', value: '', hidden: true,
      setAttribute() {}, removeAttribute() {}, replaceChildren() {}, addEventListener() {}, removeEventListener() {},
      querySelector: node, querySelectorAll: () => [], classList: { remove() {}, toggle() {}, contains: () => false },
    });
    return nodes.get(selector);
  };
  node('#station-picker').value = 'station_1';
  node('#forecast-days').value = '1';
  node('#granularity').value = '900';
  const parent = { postMessage(message) { messages.push(message); } };
  const sandbox = {
    AbortController, URL, URLSearchParams, TextEncoder, crypto: require('node:crypto').webcrypto,
    Date: class extends Date {
      constructor(value) { super(value === undefined ? '2026-10-12T10:00:00+08:00' : value); }
      static now() { return Date.parse('2026-10-12T10:00:00+08:00'); }
    },
    localStorage: { getItem: () => null, setItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: node, documentElement: node('html'), visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: {
      __M3_DASHBOARD_AUTH_MODE__: mode, __M3_NOCOBASE_PARENT_ORIGIN__: origin,
      location: { href: 'https://example.invalid/m3' + search }, history: { replaceState() {} }, parent,
      setTimeout() { return ++timerId; }, clearTimeout() {},
      addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener() {},
    },
    fetch(url, options) { calls.push({ url, options }); return new Promise(() => {}); },
  };
  vm.createContext(sandbox);
  const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(value => value.includes('const API_PATH ='));
  vm.runInContext(script, sandbox);
  return { sandbox, calls, listeners, messages, parent };
}

test('M3 starts the selected forecast and statistics without waiting for the dashboard', async () => {
  const { calls } = bootFixture();
  await new Promise(setImmediate);
  assert.ok(calls.some(read => read.url.includes('/station_1/latest?')), 'selected forecast must start while dashboard is pending');
  assert.ok(calls.some(read => read.url.includes('/custom-performance/station_1?')));
  assert.equal(calls.filter(read => read.url === '/energy-forecast-api').length, 1);
  assert.ok(calls.every(read => read.options.method === 'GET'));
});

test('M3 keeps startup reads behind valid page configuration and authentication', async () => {
  assert.equal(bootFixture('server_token', 'invalid origin').calls.length, 0);
  assert.equal(bootFixture('query_token').calls.length, 0);
  const state = bootFixture('postmessage');
  assert.equal(state.calls.length, 0);
  const nonce = state.messages.at(-1).nonce;
  const data = vm.runInContext(`({type:'vifa-m3-auth-token',nonce:${JSON.stringify(nonce)},token:'test-user-token'})`, state.sandbox);
  state.listeners.get('message')({ source: state.parent, origin: 'https://example.invalid', data });
  await new Promise(setImmediate);
  assert.equal(state.calls.length, 4);
  assert.ok(state.calls.every(read => read.options.headers.Authorization === 'Bearer test-user-token'));
});

test('M3 metadata rendering does not restart an already running forecast lookup', () => {
  let lookups = 0;
  const sandbox = {
    root: { dataset: {}, querySelector: () => ({ textContent: '' }) }, errorState: {},
    hasRenderedDashboard: false, currentViewStarted: true, lastDashboard: null,
    stationPicker: { value: 'station_1' }, STATE_LABELS: { ready: '正常' },
    validateDashboard: value => value, initializeTaskDates() {},
    text(target, value) { target.textContent = value; }, dateTime: value => value,
    selectStation() { lookups++; }, renderRefreshError() {}, renderError() {},
  };
  load(sandbox, ['renderDashboard']);
  assert.equal(sandbox.renderDashboard({ system: { state: 'ready', generated_at: '2026-10-12T10:00:00+08:00', station_count: 2, healthy_station_count: 2 } }), true);
  assert.equal(lookups, 0, 'late metadata must not clear or reload the already displayed forecast');
});

test('M3 preserves a successful fast forecast when dashboard metadata fails', () => {
  let cleared = 0;
  const sandbox = {
    hasRenderedDashboard: false, hasRenderedCustomResult: true, errorState: { hidden: true },
    text(target, value) { target.textContent = value; }, renderError() { cleared++; },
  };
  load(sandbox, ['renderRefreshError']);
  sandbox.renderRefreshError('看板状态更新失败');
  assert.equal(cleared, 0);
  assert.equal(sandbox.errorState.hidden, false);
});

test('M3 leaves a retained forecast visible while its result is refreshed', async () => {
  const read = deferred();
  const run = { run_id: 'test-run', status: 'evaluated', forecast_days: 1, interval_seconds: 900 };
  const sandbox = {
    stationPicker: { value: 'station_1' }, latestLookupToken: 0, hasRenderedCustomResult: true,
    runMatchesOutput: () => true, loadEvidenceFor: () => ({}), stopCustomPolling() {},
    customRunIds: { station_1: run.run_id }, customResults: new Map(), taskState: {},
    CUSTOM_RUN_LABELS: { evaluated: '预测完成 · 已评估' }, CUSTOM_RUN_PATH: '/custom-runs',
    resultProgressOverlay: { hidden: true },
    text(target, value) { target.textContent = value; },
    setCustomBusy(busy, label) { sandbox.resultProgressOverlay.hidden = !busy; sandbox.taskState.textContent = label; },
    rememberResultSnapshot() {}, refreshSelectedCustomResult() {},
    customRequest: () => read.promise, validateCustomResult: value => value, renderCustomResult() {},
  };
  load(sandbox, ['handleCustomRun']);
  const request = sandbox.handleCustomRun('station_1', run);
  assert.equal(sandbox.resultProgressOverlay.hidden, true, 'background refresh must not cover the retained forecast');
  assert.match(sandbox.taskState.textContent, /上次预测.*正在更新/);
  read.resolve({ run });
  await request;
});

test('M3 cold view renders the saved forecast without waiting for actual refresh', async () => {
  const read = deferred(), paths = [], rendered = [];
  let actualRefreshes = 0;
  const run = { run_id: 'saved-day', status: 'succeeded' };
  const sandbox = {
    stationPicker: { value: 'station_1' }, latestLookupToken: 1, hasRenderedCustomResult: false,
    customRunIds: { station_1: run.run_id }, customResults: new Map(), taskState: {},
    runMatchesOutput: () => true, loadEvidenceFor: () => ({}), stopCustomPolling() {},
    CUSTOM_RUN_LABELS: { succeeded: '预测完成' }, CUSTOM_RUN_PATH: '/custom-runs',
    resultProgressOverlay: { hidden: true }, text(target, value) { target.textContent = value; },
    setCustomBusy(busy, label) { sandbox.resultProgressOverlay.hidden = !busy; sandbox.taskState.textContent = label; },
    customRequest(url) { paths.push(url); return read.promise; }, validateCustomResult: value => value,
    rememberResultSnapshot() {}, renderCustomResult(value) { rendered.push(value); },
    refreshSelectedCustomResult() { actualRefreshes++; return new Promise(() => {}); },
  };
  load(sandbox, ['handleCustomRun']);
  const opening = sandbox.handleCustomRun('station_1', run);
  assert.match(paths[0], /include_actuals=false$/);
  read.resolve({ run });
  await opening;
  assert.equal(rendered.length, 1);
  assert.equal(actualRefreshes, 1);
  assert.equal(sandbox.resultProgressOverlay.hidden, true);
  assert.equal(rendered[0].actualsIncluded, false);
});

test('M3 prefetch never replaces the actuals already shown for the same run', () => {
  const sandbox = { resultSnapshots: new Map(), assertSnapshotStation() {},
    runCoversToday: () => true, loadEvidenceFor: () => ({}), milliseconds: Date.parse, saveViewSnapshots() {} };
  load(sandbox, ['resultSnapshotKey', 'rememberResultSnapshot']);
  const run = { run_id: 'saved-day', forecast_days: 1, interval_seconds: 900, completed_at: '2026-10-20T00:01:00+08:00' };
  const full = { stationKey: 'station_1', result: { run }, actualsIncluded: true };
  sandbox.rememberResultSnapshot(full);
  sandbox.rememberResultSnapshot({ stationKey: 'station_1', result: { run }, actualsIncluded: false });
  assert.equal([...sandbox.resultSnapshots.values()][0], full);
});

test('M3 clears cached views and invalidates pending lookups when authentication expires', () => {
  const { sandbox } = stationFixture();
  Object.assign(sandbox, {
    authMode: 'query_token', apiToken: 'test-user-token', currentViewStarted: true,
    readEpoch: 0, snapshotScope: null, snapshotPreparation: null, prefetchInFlight: null,
    inFlight: null, readRequests: new Map(), hasRenderedDashboard: true, lastDashboard: {},
    performanceReads: new Map([['pending', Promise.resolve({})]]), refreshTimer: null,
    window: { clearTimeout() {} }, renderRefreshError() {},
  });
  vm.runInContext(source('requestAuthentication'), sandbox);
  sandbox.requestAuthentication();
  assert.equal(sandbox.customResults.size, 0);
  assert.equal(sandbox.performanceReads.size, 0);
  assert.equal(sandbox.currentViewStarted, false);
  assert.equal(sandbox.apiToken, null);
  assert.equal(sandbox.latestLookupToken, 1);
  assert.equal(sandbox.performanceLookupToken, 1);
});
