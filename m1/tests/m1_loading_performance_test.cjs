'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const {fixture} = require('./availability_harness.cjs');
const {payload} = require('./availability_fixture.cjs');

for (const stage of ['request', 'body']) {
  test(`M1 aborts a stalled ${stage}, retains old data and releases the refresh lock`, async () => {
    let signal;
    let attempts = 0;
    const { sandbox, timers, previous, refreshInfo, notices } = fixture((_url, options) => {
      attempts++;
      if (attempts > 1) return Promise.resolve({ ok: true, json: async () => payload(2) });
      signal = options.signal;
      const pending = new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true });
      });
      return stage === 'body' ? Promise.resolve({ ok: true, json: () => pending }) : pending;
    });
    const originalHint = refreshInfo.textContent;
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
    assert.equal(refreshInfo.textContent, originalHint, 'cached reads fail silently');
    assert.deepEqual(notices, []);
    assert.equal(timers.size, 0);
    await sandbox.fetchData();
    assert.equal(attempts, 2);
    assert.equal(sandbox.allData.realtime.find(row=>row.fk_en===1010).power, 22);
    assert.equal(timers.size, 0);
  });
}

test('M1 clears its timeout after a successful read', async () => {
  const { sandbox, timers } = fixture(async () => ({ ok: true, json: async () => payload(2) }));
  await sandbox.fetchData();
  assert.equal(sandbox.fetchInFlight, false);
  assert.equal(timers.size, 0);
});
