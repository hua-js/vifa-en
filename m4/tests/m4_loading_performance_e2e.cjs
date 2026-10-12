'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {webkit} = require('playwright');
const {snapshot} = require('./m4_loading_test_fixture.cjs');
const html = fs.readFileSync(path.join(__dirname, '../web/M4优化调度控制台-线上版.html'), 'utf8');
const day = new Date(Date.now()+28800000).toISOString().slice(0,10);
const output = path.resolve(__dirname, '../../outputs/m4/tests', day+'-loading-performance');
function deferred() { let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve}; }
function job(station) {
  const value=snapshot(station,day);value.active_daily=snapshot(station,day);
  value.daily_dispatch={station_id:station,date:day,status:'completed',confirmed_daily_run_id:value.run_id};
  value.ems_table_write={enabled:true};value.automation={enabled:true,next_check_at:day+'T23:59:00+08:00'};
  return value;
}
function inputs(station) {
  return {schema_version:'m4-daily-inputs-v1',station_id:station,date:day,configuration_version:'live-config',
    usage:'retrospective_comparison_only',dispatch_status:'not_dispatched',can_compare:false,checks:[],
    sources:{current_soc:{station_id:station,status:'ready',soc_pct:45,observed_at:new Date().toISOString()},
      current_power:{station_id:station,status:'ready',power_kw:0,observed_at:new Date().toISOString()}}};
}
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await webkit.launch({headless:true,timeout:15000});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[],holds=new Map(),held=new Set();
    let inputFailure=false;
    const firstInputs=deferred();holds.set('station-2/daily-inputs',firstInputs);
    page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
    await page.route('https://m4.test/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      assert.equal(request.method(),'GET','browser loading must not start tasks or write tables');
      if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
      assert.equal(request.headers().authorization,'Bearer fixture-only-token');
      const endpoint=url.pathname.split('/').at(-1),station=url.pathname.split('/')[3];
      requests.push({station,endpoint});
      if(endpoint==='project')return route.fulfill({json:{schema_version:1,project_id:'fixture',configuration_version:'fixture-config',
        stations:[1,2].map(n=>({id:'station-'+n,source_code:'ES0'+n,name:'电站 '+n,has_pv:false,policy:'cost_first',grid_import_limit_kw:720}))}});
      const key=station+'/'+endpoint,hold=holds.get(key);
      if(hold){held.add(key);await hold.promise;held.delete(key);}
      if(endpoint==='daily-inputs')return inputFailure?route.fulfill({status:502,json:{detail:'实测读取失败'}}):route.fulfill({json:inputs(station)});
      if(endpoint==='daily-plan')return route.fulfill({json:job(station)});
      if(endpoint==='decision-history')return route.fulfill({json:{station_id:station,items:[]}});
      if(endpoint==='settings'||endpoint==='control-sources')return route.fulfill({json:{station_id:station,version:endpoint==='settings'?'live-config':'live-controls'}});
      throw Error('unexpected mock endpoint '+endpoint);
    });
    await page.goto('https://m4.test/m4?token=fixture-only-token',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('.decision .badge').textContent==='日计划 · 正在更新实测');
    assert.equal(await page.locator('#chart-panel').isVisible(),true);
    assert.equal(await page.locator('#loading-mask').isVisible(),false);
    assert.equal(await page.locator('#recalculate').isDisabled(),true);
    assert.equal(await page.locator('#dispatch-daily').isDisabled(),true);
    assert.equal(await page.evaluate(()=>context),null);
    assert(held.has('station-2/daily-inputs'),'daily-inputs must still be held when the plan is visible');
    await page.screenshot({path:path.join(output,'fast-plan-before-inputs.png'),fullPage:true,animations:'disabled'});
    holds.delete('station-2/daily-inputs');firstInputs.resolve();
    await page.waitForFunction(()=>context?.station==='station-2'&&!document.querySelector('#recalculate').disabled);
    await page.waitForFunction(()=>document.querySelector('#recent-logs').textContent.includes('fixture-run'));
    for(const endpoint of ['daily-inputs','daily-plan','settings','control-sources']) {
      assert.equal(requests.filter(r=>r.station==='station-2'&&r.endpoint===endpoint).length,1,endpoint+' repeated after startup');
    }
    // Hold an old station's plan and inputs, switch away, then release them.
    const oldPlan=deferred(),oldInputs=deferred();holds.set('station-1/daily-plan',oldPlan);holds.set('station-1/daily-inputs',oldInputs);
    await page.locator('[data-station-id="station-1"]').click();
    await page.waitForFunction(()=>document.querySelector('#station').value==='station-1');
    await page.locator('[data-station-id="station-2"]').click();
    await page.waitForFunction(()=>context?.station==='station-2'&&!document.querySelector('#recalculate').disabled);
    holds.delete('station-1/daily-plan');holds.delete('station-1/daily-inputs');oldPlan.resolve();oldInputs.resolve();
    await page.waitForLoadState('networkidle');
    assert.equal(await page.evaluate(()=>plan.station),'station-2');assert.equal(await page.evaluate(()=>context.station),'station-2');
    assert.match(await page.locator('#decision-copy').innerText(),/电站 2/);
    // A display snapshot survives a failed live refresh, without invented actuals.
    inputFailure=true;await page.evaluate(()=>loadCurrent());
    assert.equal(await page.locator('#chart-panel').isVisible(),true);
    assert.equal(await page.evaluate(()=>context.inputs.can_compare),false);
    assert.equal(await page.evaluate(()=>Object.keys(context.inputs.sources).length),0);
    inputFailure=false;await page.evaluate(()=>loadCurrent());
    const layouts=[];
    for(const width of [1440,834,390])for(const theme of ['light','dark']) {
      await page.setViewportSize({width,height:1000});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),width+' '+theme+' page overflow');
      assert.equal(await page.locator('#chart-panel').isVisible(),true);
      const localScrolls=await page.evaluate(()=>Array.from(document.querySelectorAll('.chart-wrap,.table-wrap,[class*="timeline"]'))
        .filter(e=>e instanceof HTMLElement&&e.clientWidth>0&&e.scrollWidth>e.clientWidth&&['auto','scroll'].includes(getComputedStyle(e).overflowX))
        .map(e=>{const before=e.scrollLeft;e.scrollLeft=40;const scrollable=e.scrollLeft>0;e.scrollLeft=before;return {element:e.id||e.className,scrollable};}));
      if(width===390){assert(localScrolls.length>0,'dense mobile content must provide local scrolling');assert(localScrolls.every(e=>e.scrollable));}
      const file=path.join(output,theme+'-'+width+'.png');await page.screenshot({path:file,fullPage:true,animations:'disabled'});
      layouts.push({width,theme,screenshot:file,local_scrolls:localScrolls});
    }
    assert.deepEqual(errors,[]);
    const result={browser:'WebKit',transport:'isolated GET fixtures; no production requests',
      checks:['fast plan before inputs','write buttons disabled during revalidation','startup sidebar request deduplication',
        'late station response isolation','failed live refresh preserves verified plan','six responsive/theme renders'],
      page_errors:errors,layouts,requests};
    console.log(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
