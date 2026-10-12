'use strict';
// Supply the installed, production-matching Node-RED HTTP Request module.
// This runs the real core node against local fixtures, never production APIs.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const nativeModule = process.env.NODE_RED_HTTP_MODULE;
if (!nativeModule) throw new Error('NODE_RED_HTTP_MODULE is required');
const source = fs.readFileSync(path.join(__dirname, '../deploy/node_red_gateway.py'), 'utf8');
const prepare = source.match(/PREPARE_JS = r"""([\s\S]*?)"""/)[1];
let Constructor;
const RED = {
  settings: { proxyOptions: { env: { no_proxy: '*', http_proxy: '', https_proxy: '', all_proxy: '' } } },
  log: { warn() {} },
  _: value => value,
  nodes: {
    registerType(name, constructor) { Constructor = constructor; },
    createNode(node) {
      node.handlers = {};
      node.warnings = [];
      node.errors = [];
      node.credentials = {};
      node.on = (event, callback) => { node.handlers[event] = callback; };
      node.status = () => {};
      node.metric = () => false;
      node.warn = value => node.warnings.push(value);
      node.error = value => node.errors.push(typeof value === 'string' ? value : value.code);
    },
  },
};
const loaded = require(nativeModule)(RED);
function message() {
  return vm.runInNewContext(`(() => {${prepare}\n})()`, {
    msg: { req: { headers: { authorization: 'Bearer user-fixture' }, query: {} } },
  });
}
async function send(url, msg) {
  await loaded;
  const node = new Constructor({ url, method: 'GET', ret: 'obj', paytoqs: 'ignore', senderr: false, headers: [] });
  const response = await new Promise((resolve, reject) => {
    node.handlers.input(msg, resolve, error => { if (error) reject(error); });
  });
  assert.deepEqual(node.warnings, []);
  node.handlers.close();
  return response;
}
async function fixture(handler, work) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await work(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}

test('production-matching native HTTP Request node does not follow a redirect or forward credentials to its target', async () => {
  const hits = [];
  await fixture((req, res) => {
    hits.push(req.url);
    assert.equal(req.method, 'GET');
    assert.equal(req.headers.authorization, 'Bearer user-fixture');
    res.writeHead(302, { Location: '/forbidden', 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'error', message: 'fixture redirect' }));
  }, async url => {
    const msg = message();
    assert.equal(msg.requestTimeout, 115000);
    assert.equal(msg.followRedirects, false);
    const response = await send(url + '/redirect', msg);
    assert.equal(response.statusCode, 302);
    assert.equal(response.payload.message, 'fixture redirect');
    assert.deepEqual(hits, ['/redirect']);
  });
});

test('production-matching native HTTP Request node honors the message timeout rather than its global default', async () => {
  await fixture((req, res) => {
    const timer = setTimeout(() => res.end('{}'), 1000);
    res.on('close', () => clearTimeout(timer));
  }, async url => {
    const msg = message();
    assert.equal(msg.requestTimeout, 115000);
    msg.requestTimeout = 30; // Shorten only the local fixture's deadline.
    const response = await send(url + '/slow', msg);
    assert.equal(response.statusCode, 'ETIMEDOUT');
  });
});
