'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');
const http = require('node:http');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../deploy/node_red_gateway.py'), 'utf8');
const code = source.match(/WRAP_JS = r"""([\s\S]*?)"""/)[1];
function wrap(payload, accept, compressor = zlib, statusCode = 200) {
  const logs = [], warnings = [];
  const msg = {payload, statusCode, req: {headers: {'accept-encoding': accept}},
    headers: {Authorization: 'Bearer fixture-private', 'Content-Length': '999'}};
  vm.runInNewContext(`(() => {${code}\n})()`, {
    msg, Buffer, m1Zlib: compressor,
    node: {log: text => logs.push(text), warn: text => warnings.push(text)},
  });
  return {msg, logs, warnings};
}
function payload() {
  return {status: 'ok', data: {
    nodes: [{id: 393279639650306, sn: 'emu13', node_type: 'meter'}],
    realtime: [{fk_en: 393279639650306, power: 391.9, timestamp: '2026-10-22T11:23:14+08:00'}],
    alert_history: {status: 'ok', total: 1293, limit: 1000,
      records: Array.from({length: 1000}, (_, i) => ({id: 'derived:load_spike:load1',
        category: 'load_spike', fk_site_id: '363738052231168', fk_en_id: 'load1',
        device_sn: 'ES01', start_time: new Date(1792630000000 + i * 20000).toISOString(),
        value: 1100 + i, threshold: 1000, unit: 'kW', txt: '负载功率突增'}))},
  }};
}

test('gzip preserves the entire business JSON, station PV and all 1000 history records', () => {
  const value = payload();
  const {msg, logs} = wrap(value, 'gzip, deflate, br');
  assert.equal(msg.headers['Content-Encoding'], 'gzip');
  assert.equal(msg.headers.Vary, 'Accept-Encoding');
  assert.equal(msg.headers['Cache-Control'], 'no-store');
  assert.equal(msg.headers.Authorization, undefined);
  assert.equal(msg.headers['Content-Length'], undefined);
  assert.deepEqual(JSON.parse(zlib.gunzipSync(msg.payload)), value);
  assert.ok(msg.payload.length < Buffer.byteLength(JSON.stringify(value)) / 5);
  assert.deepEqual(logs, ['m1_dashboard_completed status=ok source_failures=0']);
});

test('encoding negotiation honors explicit gzip quality and wildcard exclusions', () => {
  for (const accept of ['GZip; q=1', 'br, gzip;q=0.5', '*;q=0.8']) {
    assert.equal(wrap(payload(), accept).msg.headers['Content-Encoding'], 'gzip', accept);
  }
  for (const accept of [undefined, '', 'br', 'xgzip', 'gzip;q=0', 'gzip; q=0.0, *;q=1', '*;q=0', 'gzip;q=broken']) {
    const value = payload();
    const {msg} = wrap(value, accept);
    assert.equal(msg.headers['Content-Encoding'], undefined, String(accept));
    assert.equal(msg.payload, value);
  }
});

test('small and error responses keep the existing envelope and HTTP status', () => {
  for (const [value, status] of [[{status: 'ok', data: {}}, 200], [{status: 'error', message: '请重新登录'}, 401]]) {
    const {msg} = wrap(value, 'gzip', zlib, status);
    assert.equal(msg.payload, value);
    assert.equal(msg.statusCode, status);
    assert.equal(msg.headers['Content-Encoding'], undefined);
  }
  const {msg} = wrap('bad JSON', 'gzip', zlib, 'ETIMEDOUT');
  assert.equal(msg.statusCode, 502);
  assert.equal(msg.payload.status, 'error');
});

test('compression failure falls back to the valid response without exposing private errors', () => {
  const value = payload();
  const {msg, warnings} = wrap(value, 'gzip', {gzipSync() {throw new Error('fixture-private');}});
  assert.equal(msg.payload, value);
  assert.equal(msg.headers['Content-Encoding'], undefined);
  assert.deepEqual(warnings, ['m1_response_compression_failed']);
});

test('the HTTP wire response can be decoded with its advertised encoding', async () => {
  const value = payload();
  const server = http.createServer((req, res) => {
    const {msg} = wrap(value, req.headers['accept-encoding']);
    res.writeHead(msg.statusCode, msg.headers);
    res.end(Buffer.isBuffer(msg.payload) ? msg.payload : JSON.stringify(msg.payload));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const response = await new Promise((resolve, reject) => {
      http.get({hostname: '127.0.0.1', port: server.address().port, headers: {'Accept-Encoding': 'gzip'}}, res => {
        const parts = [];
        res.on('data', chunk => parts.push(chunk));
        res.on('end', () => resolve({headers: res.headers, body: Buffer.concat(parts)}));
      }).on('error', reject);
    });
    assert.equal(response.headers['content-encoding'], 'gzip');
    assert.deepEqual(JSON.parse(zlib.gunzipSync(response.body)), value);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});
