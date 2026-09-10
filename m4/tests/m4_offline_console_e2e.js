'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const result = JSON.parse(fs.readFileSync(path.join(root, 'm4/mock/orchestration/orchestration-result.json')));
const url = pathToFileURL(path.join(root, 'm4/web/M4优化调度控制台-线上版.html')).href+'?source=offline';
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => {if(/^https?:/.test(r.url())) requests.push(r.url());});
    await page.goto(url);
    assert.match(await page.locator('#mock-data-badge').innerText(), /B1/);
    for (const [key, station] of [['s1', result.stations[0]], ['s2', result.stations[1]]]) {
      await page.selectOption('#station-select', key);
      assert.match(await page.locator('#station-capacity').innerText(), key === 's1' ? /500/ : /1,500/);
      await page.locator('#candidate-comparison').evaluate(e => e.open = true);
      for (const candidate of station.optimization_result.candidates) {
        await page.locator(`[data-candidate="${candidate.profile_id}"]`).click();
        // Compare the rendered SVG and accessible readout with the real optimizer artifact.
        assert.equal(await page.locator('#plan-chart [data-point]').count(), 96);
        const chargeClasses = await page.locator('#plan-chart [data-point]').evaluateAll(nodes => nodes.map(n=>n.classList.contains('chart-charge')));
        assert.deepEqual(chargeClasses,candidate.plan.map(p=>p.mode==='charge' && p.target_power_kw>0));
        for (const i of [0, 24, 48, 60, 95]) {
          await page.locator('#point-slider').evaluate((e, i) => {e.value=i;e.dispatchEvent(new Event('input'));}, i);
          const expected = candidate.plan[i];
          const values = await page.locator('#point-readout b').allTextContents();
          assert.ok(Math.abs(Number(values[0].replaceAll(',', '')) - Math.abs(expected.target_power_kw)) <= .051);
          assert.ok(Math.abs(Number(values[1].replace('%', '')) - expected.expected_soc_pct) <= .051);
        }
        assert.equal(await page.locator('#actual-power-trace').count(), 0);
        assert.match(await page.locator('#decision-status').innerText(), /待选择/);
        assert.match(await page.locator('#execution-note').innerText(), /未下发/);
      }
    }
    const upload = async data => {
      await page.setInputFiles('#offline-file', {name:'result.json',mimeType:'application/json',buffer:Buffer.from(typeof data==='string'?data:JSON.stringify(data))});
      await page.waitForFunction(() => document.querySelector('#offline-load-status').dataset.state !== 'loading');
    };
    // Only the current neutral schema is accepted; retired states and versions are rejected.
    const current = structuredClone(result);
    current.schema_version = 'm4-orchestration-v2';
    current.stations.forEach(station => station.selection_status = 'pending_selection');
    await upload(current);
    assert.equal(await page.locator('#offline-load-status').getAttribute('data-state'),'ready');
    const crossVersion = structuredClone(current);
    crossVersion.stations[0].selection_status = 'pending_ai';
    await upload(crossVersion);
    assert.equal(await page.locator('#offline-load-status').getAttribute('data-state'),'error');
    const retired = structuredClone(result);
    retired.schema_version = 'm4-orchestration-v1';
    retired.stations.forEach(station => station.selection_status = 'pending_ai');
    await upload(retired);
    assert.equal(await page.locator('#offline-load-status').getAttribute('data-state'),'error');
    await upload(result);
    const partial = structuredClone(result);
    partial.overall_status='partial_failure';
    Object.assign(partial.stations[0], {status:'optimization_error', optimization_result:null,error:{code:'OPTIMIZATION_ERROR',message:'本站优化失败'}});
    await upload(partial);
    await page.selectOption('#station-select','s1');
    assert.equal(await page.locator('#plan-chart [data-point]').count(), 0);
    assert.match(await page.locator('#offline-empty').innerText(), /本站优化失败/);
    await page.selectOption('#station-select','s2');
    assert.equal(await page.locator('#plan-chart [data-point]').count(), 96);
    await upload('{');
    assert.match(await page.locator('#offline-load-status').innerText(), /失败/);
    assert.equal(await page.locator('#plan-chart [data-point]').count(), 0);
    // Malformed plans, station collisions and non-B1 dispatch states must never retain an old plan.
    for (const mutate of [
      r=>r.stations[0].optimization_result.candidates[0].plan.pop(),
      r=>r.stations.push(structuredClone(r.stations[0])),
      r=>r.stations[0].selected_candidate_id='cost',
      r=>r.stations[0].optimization_result.candidates[0].plan[0].timestamp='2026-09-04T00:15:00+08:00',
    ]) {
      const broken=structuredClone(result);mutate(broken);await upload(broken);
      assert.equal(await page.locator('#offline-load-status').getAttribute('data-state'),'error');
      assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
    }
    const failed=structuredClone(result);
    const failedCandidate=failed.stations[1].optimization_result.candidates[0];
    Object.assign(failedCandidate,{status:'timeout',plan:[],metrics:null});
    await upload(failed);
    assert.equal(await page.locator('[data-candidate="balanced"]').isDisabled(),true);
    assert.equal(await page.locator('#plan-chart [data-point]').count(),96);
    const unmatched=structuredClone(result);
    unmatched.stations[1].input_summary.source_versions.tariff='new-tariff';
    unmatched.stations[1].optimization_result.source_versions.tariff='new-tariff';
    await upload(unmatched);
    assert.match(await page.locator('#decision-saving').innerText(),/未提供基线/);
    assert.match(await page.locator('#metrics').innerText(),/缺少匹配输入/);
    await upload(result);
    await page.selectOption('#station-select','all');
    assert.equal(await page.locator('[data-station-summary]').count(),2);
    await page.selectOption('#station-select','s1');
    for (const width of [1440,768,390,320]) {
      await page.setViewportSize({width,height:1000});
      for (const theme of ['light','dark']) {
        await page.evaluate(theme => document.documentElement.dataset.theme=theme,theme);
        await page.evaluate(() => Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{}))));
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),true);
        if (process.env.M4_OFFLINE_SCREENSHOT_DIR) {
          fs.mkdirSync(process.env.M4_OFFLINE_SCREENSHOT_DIR,{recursive:true});
          await page.screenshot({path:path.join(process.env.M4_OFFLINE_SCREENSHOT_DIR,`${width}-${theme}.png`),fullPage:true});
        }
      }
    }
    await page.locator('#open-plan').click();
    assert.match(await page.locator('#drawer-body').innerText(), /mock-station-1-20260904/);
    await page.keyboard.press('Escape');
    await page.locator('#open-bill').click();
    assert.match(await page.locator('#drawer-body').innerText(), /模拟账单/);
    await page.keyboard.press('Escape');
    await Promise.all([page.waitForURL(/source=demo/),page.selectOption('#data-source','demo')]);
    await page.waitForLoadState('load');
    assert.equal(await page.locator('#actual-power-trace').count(),1);
    assert.deepEqual(errors,[]); assert.deepEqual(requests,[]);
    console.log('PASS: B1 artifact mapping, station/candidate switching, failure isolation, import recovery, preview-only status, layouts/themes and unchanged bill access');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
