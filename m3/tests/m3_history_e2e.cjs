'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { webkit } = require('playwright');
const { weeklyEvidenceFixture, task5Fixtures } = require('./m3_dashboard_e2e');
const output = process.env.M3_HISTORY_OUTPUT;
assert(output, 'Set M3_HISTORY_OUTPUT');
fs.mkdirSync(output, { recursive: true });
const html = fs.readFileSync(path.join(__dirname, '../node_red/m3_production_gateway_template.html'), 'utf8')
  .replace('{{{m3DashboardAuthModeJson}}}', '"query_token"')
  .replace('{{{m3NocobaseParentOriginJson}}}', '"https://m3.test"');
function fixture(day, station = 'station_1', interval = 900) {
  const base = weeklyEvidenceFixture();
  const delta = Date.parse(day + 'T00:00:00+08:00') - Date.parse(base.run.forecast_start);
  const shift = value => {
    if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value)) return new Date(Date.parse(value) + delta + 28800000).toISOString().replace('.000Z', '+08:00');
    if (Array.isArray(value)) return value.map(shift);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, shift(item)]));
    return value;
  };
  const result = shift(base.result), run = result.run;
  run.run_id = `${station}-${day}-${interval}`;
  run.station_id = station === 'station_1' ? 'plant-alpha-ES01' : 'plant-beta-ES02';
  run.interval_seconds = interval;
  run.points_per_day = run.expected_points_per_series = 86400 / interval;
  run.model_manifest.interval_seconds = run.source_manifest.interval_seconds = interval;
  for (const series of result.series) {
    series.points = Array.from({ length: run.points_per_day }, (_, i) => ({
      target_time: new Date(Date.parse(run.forecast_start) + i * interval * 1000).toISOString(), horizon_step: i + 1,
      forecast_value: series.unique_id === 'storage_soc' ? 60 + i / 100 : (day === '2026-08-31' ? 500 : 700) + i / 10,
      actual_value: series.unique_id === 'storage_soc' ? 61 : 710, actual_quality: 'valid', absolute_percentage_error: null,
    }));
  }
  return result;
}
(async () => {
  const browser = await webkit.launch({ headless: true });
  const results = [];
  try {
    for (const width of [1440, 834, 390, 320]) for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage();
      await page.clock.install({ time: new Date('2026-08-31T15:00:00+08:00') });
      const errors = [], methods = [], historicalQueries = [];
      let holdDay = null, releaseHeld;
      let heldGate = Promise.resolve();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('https://m3.test/**', async route => {
        const request = route.request(), url = new URL(request.url());
        methods.push(request.method());
        assert.equal(request.method(), 'GET', 'history browsing must be read-only');
        const reply = data => route.fulfill({ json: { status: 'ok', data } });
        if (url.pathname === '/ett') return route.fulfill({ contentType: 'text/html', body: html });
        if (url.pathname === '/energy-forecast-api') return route.fulfill({ json: task5Fixtures().ready });
        const station = url.pathname.includes('station_2') ? 'station_2' : 'station_1';
        if (url.pathname.endsWith('/latest')) {
          const day = url.searchParams.get('forecast_date') || '2026-08-31';
          if (url.searchParams.has('forecast_date')) historicalQueries.push({ day, station, interval: url.searchParams.get('interval_seconds') });
          if (day === holdDay) await heldGate;
          if (day === '2026-08-29') return route.fulfill({ status: 404, json: { status: 'error', error: { code: 'not_found' } } });
          return reply(fixture(day, station, Number(url.searchParams.get('interval_seconds'))).run);
        }
        if (url.pathname.includes('/custom-performance/')) {
          const performance = weeklyEvidenceFixture().performance;
          performance.station_id = fixture('2026-08-31', station).run.station_id;
          return reply(performance);
        }
        if (url.pathname.endsWith('/result')) {
          const match = /\/(station_[12])-(\d{4}-\d{2}-\d{2})-(\d+)\/result$/.exec(url.pathname);
          assert(match, 'known result identity');
          const result = fixture(match[2], match[1], Number(match[3]));
          if (url.searchParams.get('include_actuals') === 'false') for (const series of result.series) for (const point of series.points) {
            point.actual_value = point.actual_quality = null;
          }
          return reply(result);
        }
        throw new Error('Unexpected route: ' + url.pathname);
      });
      await page.goto('https://m3.test/ett?token=fixture-user-token', { waitUntil: 'domcontentloaded' });
      try {
        await page.waitForFunction(() => document.querySelector('.metric-peak-load').textContent !== '—');
      } catch (error) {
        fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ errors, task: await page.locator('#task-state').innerText(), error: await page.locator('#error-state').innerText() }));
        throw error;
      }
      const saved = await page.evaluate(() => sessionStorage.getItem('vifa.m3.customForecastRuns.v1'));
      await page.locator('#result-date').fill('2026-08-30');
      await page.locator('#result-query-button').click();
      await page.waitForFunction(() => document.querySelector('#result-query-note').textContent.includes('已加载'));
      assert.match(await page.locator('#result-date-note').innerText(), /历史预测.*2026\/08\/30.*生成于/);
      assert.equal(await page.locator('#run-button').isDisabled(), true);
      assert.equal(await page.locator('path[data-kind="actual"]').count(), 2);
      assert.equal(await page.evaluate(() => sessionStorage.getItem('vifa.m3.customForecastRuns.v1')), saved);
      if (theme === 'dark') await page.locator('#theme-toggle').click();
      await page.waitForTimeout(250);
      assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      for (const selector of ['#result-date', '#result-query-button', '#result-today-button']) {
        const box = await page.locator(selector).boundingBox();
        assert(box.x >= 0 && box.x + box.width <= width && box.height >= 40, selector + ' must remain usable');
      }
      await page.screenshot({ path: path.join(output, `${width}-${theme}.png`), fullPage: true });
      await page.locator('.result-query').screenshot({ path: path.join(output, `${width}-${theme}-query.png`) });
      await page.locator('#station-picker').selectOption('station_2');
      await page.waitForFunction(() => document.querySelector('#result-query-note').textContent.includes('已加载'));
      assert.equal(historicalQueries.at(-1).station, 'station_2');
      await page.locator('#granularity').selectOption('3600');
      await page.waitForFunction(() => document.querySelector('#result-query-note').textContent.includes('已加载'));
      assert.equal(historicalQueries.at(-1).interval, '3600');
      await page.locator('#result-date').fill('2026-08-29');
      await page.locator('#result-query-button').click();
      await page.waitForFunction(() => document.querySelector('#result-query-note').textContent.includes('暂无匹配'));
      assert.equal(await page.locator('path[data-kind="forecast"]').count(), 0);
      holdDay = '2026-08-28'; heldGate = new Promise(resolve => { releaseHeld = resolve; });
      await page.locator('#result-date').fill(holdDay);
      await page.locator('#result-query-button').click();
      await page.waitForFunction(() => document.querySelector('#result-query-note').textContent.includes('正在查询'));
      await page.locator('#result-today-button').click();
      await page.waitForFunction(() => document.querySelector('#result-date-note').textContent.includes('覆盖今天'));
      releaseHeld();
      await page.waitForTimeout(100);
      assert.match(await page.locator('#result-date-note').innerText(), /覆盖今天/);
      assert.equal(await page.locator('#result-date').inputValue(), '2026-08-31');
      assert.equal(await page.locator('#run-button').isEnabled(), true);
      assert.deepEqual(errors, []);
      results.push({ width, theme, historyWithActuals: true, stationAndInterval: true, empty: true, staleResponseIgnored: true, onlyGet: methods.every(method => method === 'GET') });
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ browser: 'WebKit', results }, null, 2));
  process.stdout.write(JSON.stringify({ scenarios: results.length, passed: true }) + '\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
