'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../deploy/node_red_gateway.py'), 'utf8');
const prepare = source.match(/PREPARE_JS = r"""([\s\S]*?)"""/)[1];
const wrap = source.match(/WRAP_JS = r"""([\s\S]*?)"""/)[1];
function run(code, msg, logs = []) {
  return vm.runInNewContext(`(() => {${code}\n})()`, { msg, node: { log: value => logs.push(value) } });
}

test('M1 gateway forwards current-user authorization to the fixed internal endpoint', () => {
  const msg = { req: { headers: { authorization: 'Bearer user-fixture' }, query: {} } };
  assert.equal(run(prepare, msg), msg);
  assert.equal(msg.headers.Authorization, 'Bearer user-fixture');
  assert.equal(msg.requestTimeout, 115000);
  assert.equal(msg.followRedirects, false);
  assert.equal(msg.method, undefined);
  assert.equal(msg.payload, '');
});
test('M1 gateway preserves query-token compatibility without forwarding query parameters', () => {
  const msg = { req: { headers: {}, query: { token: 'user-fixture', url: 'https://attacker.invalid' } } };
  run(prepare, msg);
  assert.equal(msg.headers.Authorization, 'Bearer user-fixture');
  assert.equal(msg.url, undefined);
});
test('M1 gateway rejects missing or malformed credentials before service access', () => {
  for (const token of ['', 'invalid token', 'x'.repeat(4097)]) {
    let status;
    const msg = { req: { headers: {}, query: { token } }, res: { status(value) { status = value; return this; }, json() {} } };
    assert.equal(run(prepare, msg), null);
    assert.equal(status, 401);
  }
});
test('M1 response never reflects a bearer header or treats malformed data as success', () => {
  const msg = { payload: 'bad JSON', statusCode: 'ETIMEDOUT', headers: { Authorization: 'Bearer user-fixture' } };
  run(wrap, msg);
  assert.equal(msg.payload.status, 'error');
  assert.equal(msg.statusCode, 502);
  assert.equal(msg.headers.Authorization, undefined);
});
test('M1 response preserves business failures and successful structured data', () => {
  for (const payload of [{ status: 'ok', data: { value: 1 } }, { status: 'error', message: '登录状态已失效' }]) {
    const msg = { payload, statusCode: 200 };
    run(wrap, msg);
    assert.equal(msg.payload, payload);
    assert.equal(msg.statusCode, 200);
  }
});
test('M1 operational logging reports only normalized status and source-failure counts', () => {
  const logs = [];
  const msg = { payload: { status: 'ok', data: { source_errors: { growatt: 'private-error-fixture' } } }, statusCode: 200, headers: { Authorization: 'Bearer private-user-fixture' } };
  run(wrap, msg, logs);
  assert.deepEqual(logs, ['m1_dashboard_completed status=ok source_failures=1']);
  assert.equal(JSON.stringify(logs).includes('private-'), false);
});
