'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { webkit } = require('playwright');
const { weeklyEvidenceFixture, task5Fixtures } = require('./m3_dashboard_e2e');
const output = process.env.M3_RECENT_OUTPUT;
assert(output, 'Set M3_RECENT_OUTPUT');
fs.mkdirSync(output, { recursive: true });
const html = fs.readFileSync(path.join(__dirname, '../node_red/m3_production_gateway_template.html'), 'utf8')
  .replace('{{{m3DashboardAuthModeJson}}}', '"query_token"')
  .replace('{{{m3NocobaseParentOriginJson}}}', '"https://m3.test"');
function fixture() {
  const data = weeklyEvidenceFixture();
  const run = data.result.run;
  const load = run.model_manifest.series.station_total_load;
  Object.assign(load, { model_name: 'RecentScheduleDayReference', selection_status: 'degraded',
    selection_reason: 'recent_schedule_day_reference', realized_model_name: 'RecentScheduleDayReference',
    realized_status: 'degraded', realized_fallback_reason: 'recent_schedule_day_reference', candidate_scores: [] });
  run.source_manifest.series.station_total_load.usable_week_count = 0;
  run.source_manifest.series.station_total_load.weeks = [];
  data.result.series.find(s => s.unique_id === 'station_total_load').model_name = 'RecentScheduleDayReference';
  return data;
}
(async () => {
  const browser = await webkit.launch({ headless: true });
  const results = [];
  try {
    for (const width of [1440, 1024, 768, 390]) for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.clock.install({ time: new Date('2026-08-31T15:00:00+08:00') });
      await page.route('https://m3.test/**', async route => {
        const url = new URL(route.request().url());
        assert.equal(route.request().method(), 'GET');
        const data = fixture();
        if (url.pathname === '/ett') return route.fulfill({ contentType: 'text/html', body: html });
        if (url.pathname === '/energy-forecast-api') return route.fulfill({ json: task5Fixtures().ready });
        if (url.pathname.endsWith('/latest')) return route.fulfill({ json: { status: 'ok', data: data.result.run } });
        if (url.pathname.endsWith('/result')) return route.fulfill({ json: { status: 'ok', data: data.result } });
        if (url.pathname.includes('/custom-performance/')) return route.fulfill({ json: { status: 'ok', data: data.performance } });
        throw new Error('Unexpected route: ' + url.pathname);
      });
      await page.goto('https://m3.test/ett?token=fixture-user-token', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => document.querySelector('#policy-copy').textContent.includes('近期同排班'));
      if (theme === 'dark') await page.locator('#theme-toggle').click();
      await page.waitForTimeout(250);
      assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
      assert.match(await page.locator('#policy-copy').textContent(), /降级预测/);
      assert.match(await page.locator('#result-model-meta').textContent(), /近期同排班实测参考/);
      assert.match(await page.locator('.station-status').textContent(), /降级预测/);
      assert.match(await page.locator('.selection-basis').textContent(), /近期同排班实测参考/);
      assert.ok(await page.locator('.load-chart path').count() > 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(output, `${width}-${theme}.png`) });
      await page.locator('#details-toggle').click();
      await page.locator('#policy-copy').screenshot({ path: path.join(output, `${width}-${theme}-policy.png`) });
      results.push({ width, theme, degradedCopy: true, matchedDayReference: true, chart: true, noOverflow: true });
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ browser: 'WebKit', results }, null, 2));
  console.log(JSON.stringify({ scenarios: results.length, passed: true }));
})().catch(e => { console.error(e); process.exitCode = 1; });
