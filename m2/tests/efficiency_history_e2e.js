// Both shipped HTML entries use the same history UI; all API data below is a local fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { chromium } = require('playwright');
const { setTimeout: delay } = require('node:timers/promises');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'M2能源链路.html'), 'utf8');
assert.equal(html, fs.readFileSync(path.join(root, 'm2/场站三条能效链路能流图.html'), 'utf8'));
const fixture = spawnSync('python3', ['-c', 'import json; from m2.tests.station_efficiency_history_test_support import build_history_dashboard_response; print(json.dumps(build_history_dashboard_response()))'], {cwd: root, encoding: 'utf8'});
assert.equal(fixture.status, 0, fixture.stderr);
const live = JSON.parse(fixture.stdout.replaceAll('2026-08-25', '2026-09-07').replaceAll('2026-08-24', '2026-09-06'));
const dayAfter = date => new Date(Date.parse(date) + 86400000).toISOString().slice(0, 10);
function history(date, station) {
  const trend = [];
  if (date !== '2026-09-01') for (let minute = 0; minute < 1440; minute++) {
    // Deliberately preserve an outage and non-operating chains.
    if (minute >= 720 && minute < 740) continue;
    const hour = Math.floor(minute / 60);
    const clock = `${String(hour).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
    const daylight = hour >= 6 && hour < 18;
    trend.push({data_time: `${date}T${clock}:00+08:00`, pvStorage: daylight && hour < 15 ? 87 + 4 * Math.sin(minute / 100) : null,
      storageLoad: hour >= 18 && hour < 22 ? 92 + Math.sin(minute / 40) : null, pvLoad: daylight ? 97 + Math.sin(minute / 60) : null});
  }
  return {operation: 'history', station_id: station,
    range: {start_time: `${date}T00:00:00+08:00`, end_time: `${dayAfter(date)}T00:00:00+08:00`, cutoff_time: `${dayAfter(date)}T00:00:00+08:00`, latest_time: trend.length ? `${date}T23:59:00+08:00` : null},
    trend, summary: {pv_storage_efficiency: trend.length ? 88.4 : null, storage_load_efficiency: null, pv_load_efficiency: trend.length ? 97.1 : null},
    events: trend.length ? [{...live.events[0], start: `${date}T12:00:00+08:00`, end: null}] : []};
}
function eventRows(station) {
  return Array.from({length: 12}, (_, index) => ({...live.events[index % 2], id: index,
    event_type: index % 2 ? 'battery_temperature_rise' : 'inverter_low_load',
    type: index % 2 ? '电池温升' : '逆变器低负载',
    start: `2026-09-06T${String(index + 1).padStart(2, '0')}:00:00+08:00`,
    end: index % 2 ? '2026-09-07T01:00:00+08:00' : null,
    status: index % 2 ? '已恢复' : '持续中', device: `${station} 设备 ${index}`,
    evidence: index === 0 ? '<img src=x onerror=alert(1)>' : index % 2 ? '5 分钟最大温升 3.50℃' : '负载率最低 12.40%'}));
}
let browser;
(async () => {
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1080}, locale: 'zh-CN'});
  await page.clock.setFixedTime(new Date('2026-09-07T06:36:00Z'));
  const errors = [];
  const requests = [];
  let failEvents = false;
  let failLive = false;
  let delayHistory = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
    if (url.pathname !== '/energy-efficiency-api') return route.abort();
    const query = Object.fromEntries(url.searchParams);
    requests.push(query);
    const operation = query.operation || 'dashboard';
    if ((operation === 'events' && failEvents) || (operation === 'dashboard' && failLive) || query.date === '2026-09-03') {
      return route.fulfill({status: 500, contentType: 'application/json', body: '{"status":"error"}'});
    }
    if (operation === 'history' && delayHistory && query.date === '2026-09-04') await delay(500);
    const data = operation === 'history' ? history(query.date, query.station_id) : operation === 'events'
      ? {operation: 'events', station_id: query.station_id, events: eventRows(query.station_id)} : live;
    await route.fulfill({contentType: 'application/json', body: JSON.stringify({status: 'ok', data})});
  });
  const ready = () => page.locator('#curve-query-status[data-state="ready"]').waitFor();
  const eventReady = () => page.locator('#event-query-status[data-state="ready"]').waitFor();
  async function queryCurve(date) {
    await page.locator('#curve-date').fill(date);
    await page.locator('#curve-query-form button[type="submit"]').click();
  }
  async function refreshLive() {
    await page.evaluate(() => document.getElementById('three-energy-flow').dispatchEvent(new Event('energy-dashboard-refresh')));
    await page.locator('#three-energy-flow[data-dashboard-state="ready"]').waitFor();
  }
  await page.goto('http://m2.test/');
  await page.waitForLoadState('networkidle');
  await ready();
  const liveEfficiency = await page.locator('#summary-pv-storage').textContent();
  await page.locator('#curve-prev').click();
  await ready();
  assert.match(await page.locator('#trend-title').innerText(), /2026-09-06/);
  assert.match(await page.locator('#efficiency-readout').innerText(), /当日累计效率/);
  assert.equal(await page.locator('#summary-pv-storage').textContent(), liveEfficiency);
  assert.match(await page.locator('path[data-series="pvStorage"]').getAttribute('d'), /M .* M /);
  assert.equal(await page.locator('rect[data-event-type]').getAttribute('data-event-end'), '2026-09-07T00:00:00+08:00');
  await refreshLive();
  assert.match(await page.locator('#trend-title').innerText(), /2026-09-06/);
  assert.match(await page.locator('#efficiency-readout').innerText(), /当日累计效率/);

  await page.locator('#events-history').click();
  await eventReady();
  assert.equal(await page.locator('#event-table-body tr').count(), 10);
  assert.match(await page.locator('#event-query-status').innerText(), /共 12 条记录/);
  assert.match(await page.locator('#event-table-body').innerText(), /2026-09-07 01:00/);
  await page.locator('#event-page-next').click();
  assert.equal(await page.locator('#event-table-body tr').count(), 2);
  assert.match(await page.locator('#event-table-body').innerText(), /<img src=x onerror=alert\(1\)>/);
  assert.equal(await page.locator('#event-table-body img').count(), 0);
  await page.locator('#event-type-filter').selectOption('battery_temperature_rise');
  assert.equal(await page.locator('#event-table-body tr').count(), 6);
  await page.locator('#event-status-filter').selectOption('持续中');
  assert.match(await page.locator('#event-table-body').innerText(), /无瓶颈事件/);
  await page.locator('#event-type-filter').selectOption('');
  await page.locator('#event-status-filter').selectOption('');
  await page.locator('#event-table-body button').first().click();
  await ready();
  assert.match(await page.locator('#trend-title').innerText(), /2026-09-06/);
  assert.equal(await page.locator('#events-history').getAttribute('aria-pressed'), 'true');

  const beforeInvalid = requests.length;
  await page.locator('#event-start-date').fill('2026-07-01');
  await page.locator('#event-query-form button[type="submit"]').click();
  assert.match(await page.locator('#event-query-status').innerText(), /最多31天/);
  assert.equal(requests.length, beforeInvalid);
  // Invalid unsubmitted dates must never leave the previous station's records visible.
  await page.locator('[data-station-id="ES01"]').click();
  await ready();
  assert.doesNotMatch(await page.locator('#event-table-body').innerText(), /ES02/);
  await page.locator('[data-event-days="30"]').click();
  await eventReady();
  assert.match(await page.locator('#event-query-status').innerText(), /2026-08-09 至 2026-09-07/);
  assert.match(await page.locator('#event-table-body').innerText(), /ES01/);
  assert.ok(requests.some(query => query.operation === 'history' && query.station_id === 'ES01' && query.date === '2026-09-06'));

  failEvents = true;
  await page.locator('#event-query-form button[type="submit"]').click();
  await page.locator('#event-query-status[data-state="error"]').waitFor();
  assert.match(await page.locator('#event-table-body').innerText(), /加载失败/);
  failEvents = false;
  await page.locator('#event-query-form button[type="submit"]').click();
  await eventReady();
  await queryCurve('2026-09-01');
  await ready();
  assert.match(await page.locator('#curve-query-status').innerText(), /暂无已保存/);
  assert.equal(await page.locator('path[data-series="pvStorage"]').getAttribute('d'), '');
  assert.equal(await page.locator('#event-table-body tr').count(), 10);
  await queryCurve('2026-09-03');
  await page.locator('#curve-query-status[data-state="error"]').waitFor();
  assert.equal(await page.locator('path[data-series="pvStorage"]').getAttribute('d'), '');
  delayHistory = true;
  await queryCurve('2026-09-04');
  await queryCurve('2026-09-05');
  await ready();
  await delay(600);
  assert.match(await page.locator('#trend-title').innerText(), /2026-09-05/);
  assert.match(await page.locator('#curve-query-status').innerText(), /2026-09-05/);
  failLive = true;
  await page.evaluate(() => document.getElementById('three-energy-flow').dispatchEvent(new Event('energy-dashboard-refresh')));
  await page.locator('#three-energy-flow[data-dashboard-state="error"]').waitFor();
  assert.match(await page.locator('#efficiency-readout').innerText(), /当日累计效率/);
  assert.notEqual(await page.locator('path[data-series="pvStorage"]').getAttribute('d'), '');
  failLive = false;
  await page.locator('#curve-today').click();
  await ready();
  assert.equal(await page.locator('#trend-title').innerText(), '今日24小时效率曲线');
  assert.equal(await page.locator('#curve-next').isDisabled(), true);
  await page.locator('#events-current').click();
  assert.equal(await page.locator('#event-history-controls').isVisible(), false);
  assert.equal(await page.locator('#event-table-body tr').count(), live.events.length);

  await queryCurve('2026-09-06');
  await ready();
  await page.locator('#events-history').click();
  await eventReady();
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({width, height: 1080});
    await page.locator('.trend-section').scrollIntoViewIfNeeded();
    await delay(100);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false, `page overflow at ${width}px`);
    const tickBoxes = await page.locator('[data-hour-tick]').evaluateAll(nodes => nodes.map(node => {
      const box = node.getBoundingClientRect(); return {left: box.left, right: box.right};
    }));
    for (let index = 1; index < tickBoxes.length; index++) {
      assert.ok(tickBoxes[index - 1].right <= tickBoxes[index].left, `axis labels overlap at ${width}px`);
    }
    assert.ok(await page.locator('.bottleneck-section table').evaluate(table => table.scrollWidth >= 960));
    await page.screenshot({path: `/tmp/m2-history-${width}.png`, fullPage: true});
    await page.locator('.trend-section').screenshot({path: `/tmp/m2-history-curve-${width}.png`});
    await page.locator('.bottleneck-section').screenshot({path: `/tmp/m2-history-events-${width}.png`});
  }
  await page.setViewportSize({width: 1440, height: 1080});
  await page.locator('#m2-theme-toggle').click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await delay(150);
  await page.screenshot({path: '/tmp/m2-history-dark.png', fullPage: true});
  assert.deepEqual(errors, []);
  await browser.close();
  browser = null;
  console.log('efficiency_history_e2e_ok: date queries, 31-day validation, pagination, filters, station isolation, errors, request races, live refresh, 320–1440px, dark theme');
})().catch(async error => {
  console.error(error);
  await browser?.close();
  process.exitCode = 1;
});
