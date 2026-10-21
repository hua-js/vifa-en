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
    operation: 'realtime', station_id: stationId, range: { start_time: `${day}T00:00:00+08:00` },
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
    curveSnapshots: new Map(), eventSnapshots: new Map(), range: null, detailDate: '2026-10-12',
    selectedCurveDate: '', eventHistoryMode: false, eventController: null,
    root: { dataset: {} }, statusNode: { dataset: {}, textContent: '' }, curveStatus: {},
    apiUrl: '/energy-efficiency-api', AbortController, URL,
    window: { location: { href: 'https://example.invalid/m2' },
      setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
      clearTimeout(id) { timers.delete(id); } },
    todayDate: () => '2026-10-12', formatTime: value => value,
    fetch(url, options) { const read = deferred(); calls.push({ url: String(url), options, ...read }); return read.promise; },
    renderRealtime(value) { renders.push(value); }, renderDailyEfficiency() {},
    cancelDayReads() {}, loadDay() {}, renderDayEvents() {},
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

test('M2 realtime becomes ready while independent detail reads remain pending', async () => {
  const state = fixture();
  state.sandbox.loadDay = () => new Promise(() => {});
  await seed(state);
  assert.match(state.calls[0].url, /operation=realtime/);
  assert.equal(state.sandbox.statusNode.dataset.state, 'ready');
  assert.equal(state.sandbox.fetchInFlight, false);
});

test('M2 realtime failure does not redraw independently failed detail partitions', async () => {
  const state = fixture();
  await seed(state);
  state.sandbox.curveSnapshots.set('ES02', { date: '2026-10-12', data: { trend: [] } });
  state.sandbox.eventSnapshots.set('ES02', { date: '2026-10-12', data: { events: [] } });
  state.sandbox.renderDayEvents = () => { throw new Error('detail status must remain untouched'); };
  const pending = state.sandbox.fetchData();
  state.calls.at(-1).reject(new Error('live failed'));
  await pending;
  assert.equal(state.curves.length, 0);
  assert.match(state.sandbox.statusNode.textContent, /显示上次数据/);
  assert.equal(state.sandbox.dashboardSnapshots.has('ES02'), true);
});

function detailFixture() {
  const reads = [], curves = [], events = [], clears = [];
  const sandbox = {
    activeStationId: 'ES02', dayReadGeneration: 0, detailDate: '',
    curveController: null, dayEventController: null, range: null,
    dayEvents: [], dayEventQueryState: {}, eventHistoryMode: false,
    curveSnapshots: new Map(), eventSnapshots: new Map(),
    curveStatus: {}, eventStatus: {}, AbortController,
    todayDate: () => '2026-10-12',
    readHistory(params, controller, stationId) {
      const read = deferred();
      reads.push({ params, controller, stationId, ...read });
      return read.promise;
    },
    renderCurve(data) { curves.push(data); sandbox.range = data.range; },
    renderDayEvents(data) { events.push(data); sandbox.dayEvents = data.events; },
    setQueryStatus(node, state, text) { Object.assign(node, { state, text }); },
    clearCurve(message, keepEvents) { clears.push({ message, keepEvents }); if (!keepEvents) sandbox.dayEvents = []; },
    setEventMessage() {},
  };
  vm.createContext(sandbox);
  for (const name of ['cancelDayReads', 'setDayEventStatus', 'loadDay']) vm.runInContext(source(name), sandbox);
  return { sandbox, reads, curves, events, clears };
}

test('M2 curve failure preserves an independently successful event read', async () => {
  const state = detailFixture();
  const pending = state.sandbox.loadDay('2026-10-12');
  state.reads.find(read => read.params.operation === 'events').resolve({ events: [{ id: 7 }] });
  state.reads.find(read => read.params.operation === 'curve').reject(new Error('curve failed'));
  await pending;
  assert.equal(state.sandbox.dayEvents[0].id, 7);
  assert.equal(state.sandbox.curveStatus.state, 'error');
  assert.equal(state.clears[0].keepEvents, true);
});

test('M2 ignores stale curve and event responses after a date change', async () => {
  const state = detailFixture();
  const first = state.sandbox.loadDay('2026-10-11');
  const second = state.sandbox.loadDay('2026-10-12');
  for (const read of state.reads) {
    read.resolve(read.params.operation === 'curve'
      ? { range: { start_time: read.params.date + 'T00:00:00+08:00' } }
      : { events: [{ date: read.params.start_date }] });
  }
  await Promise.all([first, second]);
  assert.equal(state.curves.length, 1);
  assert.match(state.curves[0].range.start_time, /^2026-10-12/);
  assert.equal(state.events.length, 1);
  assert.equal(state.events[0].events[0].date, '2026-10-12');
  assert.equal(state.reads[0].controller.signal.aborted, true);
});

test('M2 retains current-event failure state while history mode is visible', async () => {
  const state = detailFixture();
  state.sandbox.eventHistoryMode = true;
  const pending = state.sandbox.loadDay('2026-10-12');
  state.reads.find(read => read.params.operation === 'curve').resolve({ range: { start_time: '2026-10-12T00:00:00+08:00' } });
  state.reads.find(read => read.params.operation === 'events').reject(new Error('events failed'));
  await pending;
  assert.equal(state.sandbox.dayEventQueryState.state, 'error');
  assert.match(state.sandbox.dayEventQueryState.text, /加载失败/);
});
