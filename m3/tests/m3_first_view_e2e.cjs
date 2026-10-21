'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { webkit } = require('playwright');
const output = process.env.M3_FIRST_VIEW_OUTPUT;
assert(output, 'Set M3_FIRST_VIEW_OUTPUT to the validation output directory');
fs.mkdirSync(output, { recursive: true });
const source = fs.readFileSync(path.join(__dirname, '../node_red/m3_production_gateway_template.html'), 'utf8')
  .replace('{{{m3DashboardAuthModeJson}}}', '"query_token"')
  .replace('{{{m3NocobaseParentOriginJson}}}', '"https://m3.test"');
const start = Date.parse('2026-10-20T00:00:00+08:00');
const timestamp = ms => new Date(ms + 28800000).toISOString().slice(0, 19) + '+08:00';
function forecast(station, actuals) {
  const run = { run_id: 'fixture-' + station, station_id: station === 'station_1' ? 'plant-ES01' : 'plant-ES02',
    status: 'succeeded', history_start: timestamp(start - 28 * 86400000), history_end: timestamp(start), history_days: 28,
    forecast_start: timestamp(start), forecast_end: timestamp(start + 86400000), forecast_days: 1,
    interval_seconds: 900, points_per_day: 96, expected_points_per_series: 96, model_policy: 'full_selection',
    model_manifest: { selection_policy: 'weekly_load_v2', series: { station_total_load: { model_name: 'WeeklyNaive', candidate_scores: [] } } },
    source_manifest: {}, error_code: null, started_at: timestamp(start), completed_at: timestamp(start + 60000),
    evaluated_at: null, created_at: timestamp(start), updated_at: timestamp(start + 60000) };
  return { run, series: ['station_total_load', 'storage_soc'].map(unique_id => ({
    unique_id, unit: unique_id === 'storage_soc' ? '%' : 'kW',
    model_name: unique_id === 'storage_soc' ? 'SeasonalNaive' : 'WeeklyNaive', current_score: null,
    points: Array.from({ length: 96 }, (_, i) => ({ target_time: timestamp(start + i * 900000), horizon_step: i + 1,
      forecast_value: unique_id === 'storage_soc' ? 50 + Math.sin(i / 12) * 10 : 600 + Math.sin(i / 12) * 100,
      actual_value: actuals && i < 2 ? (unique_id === 'storage_soc' ? 51 : 610) : null,
      actual_quality: actuals && i < 2 ? 'valid' : null, absolute_percentage_error: null }))
  })) };
}
function statistics(station) {
  return { station_id: station === 'station_1' ? 'plant-ES01' : 'plant-ES02', lookback_days: 7,
    interval_seconds: 900, forecast_days: 1, model_policy: 'full_selection', calculated_at: timestamp(start + 36000000),
    series: ['station_total_load', 'storage_soc'].map(unique_id => ({ unique_id, mape_percent: null,
      baseline_mape_percent: null, relative_baseline_improvement_percent: null, scorable_point_count: 0, run_count: 0,
      daily: Array.from({ length: 7 }, (_, i) => ({ date: timestamp(start + (i - 6) * 86400000).slice(0, 10),
        mape_percent: null, scorable_point_count: 0, run_count: 0 })) })) };
}
(async () => {
  const browser = await webkit.launch({ headless: true });
  const results = [];
  try {
    for (const width of [1440, 834, 390, 320]) for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage();
      await page.clock.install({ time: new Date(start + 36000000) });
      const errors = [], methods = [];
      let releaseActuals, releaseLatest, heldActuals = 0, holdLatest = false;
      const actualGate = new Promise(resolve => { releaseActuals = resolve; });
      const latestGate = new Promise(resolve => { releaseLatest = resolve; });
      page.on('pageerror', error => errors.push(error.message));
      await page.route('https://m3.test/**', async route => {
        const request = route.request(), url = new URL(request.url());
        methods.push(request.method());
        assert.equal(request.method(), 'GET', 'viewing must never create a prediction task');
        const reply = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) });
        if (url.pathname === '/ett') return route.fulfill({ contentType: 'text/html', body: source });
        if (url.pathname === '/energy-forecast-api') return route.fulfill({ status: 503, body: '{}' });
        const station = url.pathname.includes('station_2') ? 'station_2' : 'station_1';
        if (url.pathname.endsWith('/latest')) {
          if (holdLatest) await latestGate;
          return reply(forecast(station, false).run);
        }
        if (url.pathname.includes('/custom-performance/')) return reply(statistics(station));
        if (url.pathname.endsWith('/result')) {
          if (url.searchParams.get('include_actuals') !== 'false') { heldActuals++; await actualGate; }
          return reply(forecast(station, url.searchParams.get('include_actuals') !== 'false'));
        }
        throw new Error('Unexpected route: ' + url.pathname);
      });
      const url = 'https://m3.test/ett?token=fixture-user-token';
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      try {
        await page.waitForFunction(() => document.querySelector('.metric-peak-load').textContent !== '—', null, { timeout: 8000 });
      } catch (error) {
        fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ errors, state: await page.locator('#task-state').textContent(), error: await page.locator('#error-state').textContent(), methods }));
        await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
        throw error;
      }
      await page.waitForFunction(() => document.querySelector('#task-state').textContent.includes('正在更新实测'));
      assert.ok(heldActuals > 0, 'forecast must render while actuals are still pending');
      assert.equal(await page.locator('#result-progress-overlay').isVisible(), false);
      assert.ok(await page.locator('.load-chart path').count() > 0);
      if (theme === 'dark') await page.locator('#theme-toggle').click();
      assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false, `page overflow at ${width}/${theme}`);
      await page.screenshot({ path: path.join(output, `${width}-${theme}.png`), fullPage: true });
      releaseActuals();
      await page.waitForFunction(() => document.querySelector('.metric-current-soc').textContent !== '—');
      if (width === 1440 && theme === 'light') {
        await page.waitForTimeout(50);
        await page.selectOption('#station-picker', 'station_2');
        await page.waitForFunction(() => document.querySelector('.station-section').dataset.station === 'station_2'
          && document.querySelector('.metric-peak-load').textContent !== '—');
        holdLatest = true;
        await page.goto(url, { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => document.querySelector('#task-state').textContent.includes('上次预测'));
        assert.equal(await page.locator('#result-progress-overlay').isVisible(), false);
        releaseLatest();
      }
      assert.deepEqual(errors, []);
      results.push({ width, theme, forecastBeforeActuals: true, overflow, onlyGet: methods.every(method => method === 'GET') });
      releaseLatest();
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ browser: 'WebKit', results }, null, 2));
  process.stdout.write(JSON.stringify({ scenarios: results.length, passed: true }) + '\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
