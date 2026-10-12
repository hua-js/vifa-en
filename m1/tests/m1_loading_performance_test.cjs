'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../web/dashboard_energy.html'), 'utf8');
const source = html.match(/async function fetchData\(\) \{[\s\S]*?\n\}/)[0];

function fixture(fetchImpl) {
  const timers = new Map();
  const refreshInfo = { textContent: '' };
  const previous = { sites: ['previous'] };
  let timerId = 0;
  const sandbox = {
    fetchInFlight: false, TOKEN: null, DEMO_MODE: false,
    API_URL: '/dashboard_energy_api', DEMO_DATA_URL: 'result.json',
    API_REQUEST_TIMEOUT_MS: 120000, AbortController,
    allData: previous, lastSuccessfulData: previous,
    fetch: fetchImpl, isolateCustomerData: value => value,
    render() {}, renderDataUnavailable() {}, showToast() {},
    document: { getElementById: () => refreshInfo },
    console: { error() {} },
    setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { sandbox, timers, previous, refreshInfo };
}

for (const stage of ['request', 'body']) {
  test(`M1 aborts a stalled ${stage}, retains old data and releases the refresh lock`, async () => {
    let signal;
    let attempts = 0;
    const { sandbox, timers, previous, refreshInfo } = fixture((_url, options) => {
      attempts++;
      if (attempts > 1) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok', data: { sites: ['fresh'] } }) });
      signal = options.signal;
      const pending = new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true });
      });
      return stage === 'body' ? Promise.resolve({ ok: true, json: () => pending }) : pending;
    });
    const request = sandbox.fetchData();
    await Promise.resolve();
    assert.equal(timers.size, 1, 'a request timeout must be registered');
    assert.ok(signal, 'fetch must receive an abort signal');
    assert.equal([...timers.values()][0].delay, 120000);
    await sandbox.fetchData();
    assert.equal(attempts, 1, 'overlapping refreshes are still suppressed');
    [...timers.values()][0].fn();
    await request;
    assert.equal(signal.aborted, true);
    assert.equal(sandbox.fetchInFlight, false);
    assert.equal(sandbox.allData, previous);
    assert.match(refreshInfo.textContent, /超时/);
    assert.equal(timers.size, 0);
    await sandbox.fetchData();
    assert.equal(attempts, 2);
    assert.equal(sandbox.allData.sites[0], 'fresh');
    assert.equal(timers.size, 0);
  });
}

test('M1 clears its timeout after a successful read', async () => {
  const { sandbox, timers } = fixture(async () => ({ ok: true, json: async () => ({ status: 'ok', data: { sites: [] } }) }));
  await sandbox.fetchData();
  assert.equal(sandbox.fetchInFlight, false);
  assert.equal(timers.size, 0);
});
