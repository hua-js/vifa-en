'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../web/场站三条能效链路能流图.html'), 'utf8');
function source(name) {
  const match = html.match(new RegExp(`^    (?:async )?function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^    \\}`, 'm'));
  assert.ok(match, name);
  return match[0];
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function response(stationId, day = '2026-10-12') {
  return { ok: true, json: async () => ({ status: 'ok', data: {
    operation: 'dashboard', range: { start_time: `${day}T00:00:00+08:00` },
    realtime: { inputs: { bus_id: stationId, data_time: `${day}T10:00:00+08:00` }, result: { calculation_mode: 'measured' } },
  } }) };
}
function fixture() {
  const calls = [], renders = [], curves = [], loading = [], queries = [];
  const timers = new Map();
  let timerId = 0;
  const sandbox = {
    activeStationId: 'ES02', supportedStationIds: new Set(['ES01', 'ES02']),
    dashboardSnapshots: new Map(), fetchInFlight: false, activeController: null,
    selectedCurveDate: '', eventHistoryMode: false, eventController: null,
    root: { dataset: {} }, statusNode: { dataset: {}, textContent: '' }, curveStatus: {},
    apiUrl: '/energy-efficiency-api', AbortController, URL,
    window: { location: { href: 'https://example.invalid/m2' },
      setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
      clearTimeout(id) { timers.delete(id); } },
    todayDate: () => '2026-10-12', formatTime: value => value,
    fetch(url, options) { const read = deferred(); calls.push({ url: String(url), options, ...read }); return read.promise; },
    renderRealtime(value) { renders.push(value); }, renderCurrentEfficiency() {},
    renderCurve(value) { curves.push(value); },
    clearCurve(message) { queries.push(['clear', message]); },
    setQueryStatus() {}, setStationLoading(value) { loading.push(value); },
    syncStationUi() {}, queryCurve(date) { queries.push(['history', date]); },
    console: { error() {} },
  };
  vm.createContext(sandbox);
  for (const name of ['renderDashboard', 'fetchData', 'selectStation']) vm.runInContext(source(name), sandbox);
  if (html.includes('function restoreDashboardSnapshot(')) vm.runInContext(source('restoreDashboardSnapshot'), sandbox);
  return { sandbox, calls, renders, curves, loading, queries, timers };
}
async function seed(state, stationId = 'ES02', day) {
  state.sandbox.activeStationId = stationId;
  const request = state.sandbox.fetchData();
  state.calls.at(-1).resolve(response(stationId, day));
  await request;
}

test('M2 renders the selected station snapshot before its refresh completes', async () => {
  const state = fixture();
  await seed(state, 'ES02');
  await seed(state, 'ES01');
  state.renders.length = 0;
  state.sandbox.selectStation('ES02');
  assert.equal(state.renders.length, 1, 'cached data must render without waiting for the new request');
  assert.match(state.sandbox.statusNode.textContent, /上次.*正在更新/);
  assert.equal(state.loading.at(-1), false, 'a cached station must not have a blocking overlay');
  assert.match(state.calls.at(-1).url, /station_id=ES02/);
  state.calls.at(-1).resolve(response('ES02'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.sandbox.statusNode.dataset.state, 'ready');
  assert.equal(state.timers.size, 0);
});

test('M2 keeps the cached station data when its background refresh fails', async () => {
  const state = fixture();
  await seed(state);
  state.queries.length = 0;
  const request = state.sandbox.fetchData(true);
  state.calls.at(-1).reject(new Error('unavailable'));
  await request;
  assert.equal(state.queries.length, 0, 'failed refresh must not clear an available current-day curve');
  assert.match(state.sandbox.statusNode.textContent, /显示上次数据/);
  assert.equal(state.sandbox.statusNode.dataset.state, 'error');
  assert.equal(state.sandbox.fetchInFlight, false);
});

test('M2 rejects a mismatched station before rendering or caching its response', async () => {
  const state = fixture();
  const request = state.sandbox.fetchData();
  state.calls[0].resolve(response('ES01'));
  await request;
  assert.equal(state.renders.length, 0);
  assert.equal(state.sandbox.dashboardSnapshots.size, 0);
  assert.equal(state.sandbox.statusNode.dataset.state, 'error');
});

test('M2 does not restore a previous-day snapshot', async () => {
  const state = fixture();
  await seed(state, 'ES02', '2026-10-11');
  await seed(state, 'ES01');
  state.renders.length = 0;
  state.sandbox.selectStation('ES02');
  assert.equal(state.renders.length, 0);
  assert.equal(state.loading.at(-1), true);
  state.calls.at(-1).resolve(response('ES02'));
  await new Promise(resolve => setImmediate(resolve));
});

test('M2 preserves historical selection when restoring realtime station data', async () => {
  const state = fixture();
  await seed(state, 'ES02');
  await seed(state, 'ES01');
  state.sandbox.selectedCurveDate = '2026-10-10';
  state.curves.length = 0;
  state.renders.length = 0;
  state.sandbox.selectStation('ES02');
  assert.equal(state.renders.length, 1);
  assert.equal(state.curves.length, 0, 'live snapshot must not replace the selected historical curve');
  assert.ok(state.queries.some(item => item[0] === 'history' && item[1] === '2026-10-10'));
  state.calls.at(-1).resolve(response('ES02'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.curves.length, 0);
});

test('M2 ignores a late response after switching stations', async () => {
  const state = fixture();
  const previous = state.sandbox.fetchData();
  state.sandbox.activeStationId = 'ES01';
  const current = state.sandbox.fetchData(true);
  state.calls[1].resolve(response('ES01'));
  await current;
  state.calls[0].resolve(response('ES02'));
  await previous;
  assert.equal(state.renders.length, 1);
  assert.equal(state.sandbox.dashboardSnapshots.has('ES02'), false);
  assert.equal(state.sandbox.fetchInFlight, false);
  assert.equal(state.timers.size, 0);
});

test('M2 falls back to a fresh request when a snapshot cannot be rendered', async () => {
  const state = fixture();
  await seed(state);
  state.sandbox.renderRealtime = () => { throw new Error('snapshot render unavailable'); };
  const request = state.sandbox.fetchData(true);
  assert.equal(state.calls.length, 2, 'a broken snapshot must not strand the refresh lock');
  state.sandbox.renderRealtime = () => {};
  state.calls.at(-1).resolve(response('ES02'));
  await request;
  assert.equal(state.sandbox.fetchInFlight, false);
  assert.equal(state.sandbox.statusNode.dataset.state, 'ready');
});

test('M2 discards snapshots on an authorization failure', async () => {
  const state = fixture();
  await seed(state);
  const request = state.sandbox.fetchData(true);
  state.calls.at(-1).resolve({ ok: false, status: 401 });
  await request;
  assert.equal(state.sandbox.dashboardSnapshots.size, 0);
  assert.equal(state.sandbox.statusNode.dataset.state, 'error');
});
