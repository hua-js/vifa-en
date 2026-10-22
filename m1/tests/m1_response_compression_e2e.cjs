'use strict';
// WebKit + native HTTP wire check; uses only local fixture data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
const zlib = require('node:zlib');
const {webkit} = require('playwright');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'm1/deploy/node_red_gateway.py'), 'utf8');
const wrap = source.match(/WRAP_JS = r"""([\s\S]*?)"""/)[1];
const payload = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/dashboard_http_baseline.json'))).payload;
const meter = payload.data.nodes.find(node => node.id === 1002);
Object.assign(meter, {sn: 'emu13', name: '光伏计量表'});
Object.assign(payload.data.realtime.find(row => row.fk_en === meter.id), {
  power: 380, pv_input_power_kw: 400, pv_output_power_kw: 380, conversion_efficiency: 95,
  timestamp: '2026-10-22T11:00:00+08:00', run_status: 'generate', data_quality: 'good',
});
payload.data.alert_history = {status: 'ok', total: 1295, limit: 1000,
  records: Array.from({length: 1000}, (_, i) => ({id: 'fixture-history-' + i,
    category: 'pv_efficiency', fk_site_id: payload.data.sites[0].id, fk_en_id: meter.id,
    device_sn: 'emu13', txt: '光伏转换效率下降',
    start_time: new Date(Date.parse('2026-10-21T01:00:00Z') + i * 20000).toISOString()}))};
let browser;
const report = {browser: 'WebKit',scope: 'Local final HTML and gzip gateway fixture', requests: [], errors: []};
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/result.json')) {
    const msg = {payload, statusCode: 200, req, headers: {}};
    vm.runInNewContext(`(() => {${wrap}\n})()`, {msg, Buffer, m1Zlib: zlib, node: {log() {}, warn() {}}});
    report.requests.push({accept: req.headers['accept-encoding'], encoding: msg.headers['Content-Encoding']});
    res.writeHead(msg.statusCode, msg.headers);
    res.end(Buffer.isBuffer(msg.payload) ? msg.payload : JSON.stringify(msg.payload));
  } else {
    res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
    res.end(fs.readFileSync(path.join(root, 'm1/web/dashboard_energy.html')));
  }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  browser = await webkit.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, timezoneId: 'Asia/Shanghai'});
  const page = await context.newPage();
  page.on('pageerror', e => report.errors.push(e.message));
  await page.clock.setFixedTime(new Date('2026-10-22T04:00:00Z'));
  const origin = 'http://127.0.0.1:' + server.address().port;
  await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  await page.goto(origin + '/?demo=1&data=/result.json');
  await page.waitForFunction(() => allData && !fetchInFlight);
  assert.equal(report.requests[0].encoding, 'gzip');
  assert.equal(await page.evaluate(() => allData.alert_history.records.length), 1000);
  assert.match(await page.locator('[data-node-id="1002"]').textContent(), /380 kW/);
  await page.getByRole('button', {name: '历史告警', exact: true}).click();
  assert.equal(await page.locator('.alert-item').count(), 20);
  assert.match(await page.locator('#historyPageInfo').textContent(), /1 \/ 50/);
  await page.locator('#historyNext').click();
  assert.match(await page.locator('#historyPageInfo').textContent(), /2 \/ 50/);
  await page.evaluate(() => fetchData());
  assert.equal(await page.evaluate(() => allData.alert_history.records.length), 1000);
  assert.deepEqual(report.errors, []);
  report.passed = true;
  console.log(JSON.stringify(report));
})().catch(error => {console.error(error); process.exitCode = 1;})
  .finally(async () => {await browser?.close(); await new Promise(resolve => server.close(resolve));});
