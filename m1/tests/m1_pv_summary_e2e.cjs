'use strict';
// Final HTML against a supplied read-only source snapshot; no production UI writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {webkit} = require('playwright');
const input = process.argv[2];
const out = process.argv[3];
if (!input || !out) throw new Error('snapshot and output directory are required');
const payload = JSON.parse(fs.readFileSync(input));
const html = fs.readFileSync(path.resolve(__dirname, '../web/dashboard_energy.html'));
const report = {browser: 'WebKit', scenes: [], errors: []};
const meters = ['ES01', 'ES02'].map((code, index) => {
  const station = payload.data.es_list.find(row => row.station_code === code);
  const groups = payload.data.nodes.filter(row => String(row.fk_es) === String(station.id) &&
    row.node_type === 'pv_grid_ac' && row.is_aggregate);
  const meter = payload.data.nodes.find(row => String(row.fk_es) === String(station.id) &&
    row.node_type === 'meter' && groups.some(group => String(row.parentId) === String(group.id)));
  const rt = payload.data.realtime.find(row => String(row.fk_en) === String(meter.id));
  assert.equal(rt.pv_device_sn, index ? 'emu27' : 'emu13');
  assert.equal(rt.data_quality, 'good');
  assert.ok(rt.pv_meter_timestamp);
  return {code, id: meter.id, rt};
});
assert.deepEqual(payload.data.pv_inverters, []);
let browser;
const server = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': req.url.startsWith('/result.json') ? 'application/json' : 'text/html'});
  res.end(req.url.startsWith('/result.json') ? JSON.stringify(payload) : html);
});
(async () => {
  fs.mkdirSync(out, {recursive: true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  browser = await webkit.launch({headless: true});
  for (const theme of ['light', 'dark']) {
    for (const [width, height] of [[1440,1000], [1024,1366], [768,1024], [390,844]]) {
      const context = await browser.newContext({viewport: {width,height}, timezoneId: 'Asia/Shanghai'});
      await context.addInitScript(value => localStorage.setItem('dashboard-theme', value), theme);
      await context.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push(error.message));
      await page.goto(origin + '/?demo=1&data=/result.json');
      await page.waitForFunction(() => allData && !fetchInFlight);
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      for (const {id, rt} of meters) {
        const card = page.locator(`[data-node-id="${id}"].node-card`);
        const text = await card.textContent();
        assert.ok(text.includes(rt.pv_device_sn));
        assert.ok(text.includes(await page.evaluate(value => fmtNum(value) + ' kW', rt.pv_output_power_kw)));
        assert.ok(text.includes(await page.evaluate(value => fmtNum(value) + ' kW', rt.pv_input_power_kw)));
        assert.doesNotMatch(text, /SOC|有效设备|9 台逆变器/);
        assert.equal(await card.locator('time').getAttribute('datetime'), rt.pv_meter_timestamp);
        const box = await card.boundingBox();
        assert.ok(box.width > 150 && box.x >= 0 && box.x + box.width <= width + 1);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(out, `${theme}-${width}.png`), fullPage: true});
      await page.getByRole('button', {name: '历史告警', exact: true}).click();
      await page.getByRole('button', {name: '当前告警', exact: true}).click();
      // A failed controller refresh keeps that device's old values and original time.
      const retained = await page.evaluate(id => {
        const previous = structuredClone(allData);
        const current = structuredClone(allData);
        const row = current.realtime.find(item => String(item.fk_en) === String(id));
        Object.assign(row, {power:null, pv_output_power_kw:null, pv_meter_timestamp:'', timestamp:'', data_quality:'missing'});
        current.source_errors = {'pv_meter:ES02':'fixture failure'};
        return retainAvailableSourceData(current, previous).realtime.find(item => String(item.fk_en) === String(id));
      }, meters[1].id);
      assert.equal(retained.pv_meter_timestamp, meters[1].rt.pv_meter_timestamp);
      assert.equal(retained.power, meters[1].rt.power);
      report.scenes.push({theme,width,height,meters:meters.map(row=>({code:row.code,id:row.id,time:row.rt.pv_meter_timestamp}))});
      await context.close();
    }
  }
  assert.deepEqual(report.errors, []);
  report.passed = true;
  fs.writeFileSync(path.join(out, 'browser.json'), JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:true,scenes:report.scenes.length,browser:report.browser}));
})().catch(error => {console.error(error);process.exitCode=1;})
  .finally(async () => {await browser?.close();await new Promise(resolve=>server.close(resolve));});
