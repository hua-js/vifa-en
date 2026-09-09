'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..'),fixture=JSON.parse(fs.readFileSync(path.join(root,'m4/mock/orchestration/orchestration-result.json'))),source=fixture.stations.find(s=>s.station_id==='station-1');
const ids=['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333'];
const base={run_id:ids[0],station_id:'station-1',status:'completed',started_at:fixture.started_at,finished_at:fixture.finished_at,selected:{profile_id:source.optimization_result.candidates[0].profile_id,plan_version:source.optimization_result.candidates[0].plan_version},reason:'测试历史方案',checked_at:fixture.finished_at,expires_at:fixture.finished_at,comparison:[],candidates:source.optimization_result.candidates,issues:[],stages:[],plan_start_at:source.input_summary.plan_start_at,input_summary:{input_observed_at:fixture.started_at,configuration_version:'v1',initial_soc_pct:20,energy_capacity_kwh:1044,max_charge_kw:200,max_discharge_kw:200,demand_limit_kw:504,source_versions:{load:'load-v1'}},policy:{version:'p1'}};
function shifted(minutes){const r=structuredClone(base);r.run_id=ids[1];r.input_summary.initial_soc_pct=30;r.input_summary.source_versions.load='load-v2';r.plan_start_at=new Date(Date.parse(r.plan_start_at)+minutes*60000).toISOString();r.input_summary.horizon_points=95;for(const c of r.candidates)c.plan=c.plan.slice(0,95);for(const c of r.candidates)for(const p of c.plan)p.timestamp=new Date(Date.parse(p.timestamp)+minutes*60000).toISOString();return r;}
const blocked={...base,run_id:ids[2],status:'blocked_configuration',selected:null,candidates:[],plan_start_at:null,issues:['尚未保存参数']};
const envelope=r=>({schema_version:'m4-decision-results-v1',station_id:r.station_id,usage:'historical_preview_only',dispatch_status:'not_dispatched',status:'available',record:r});
const summary=r=>({run_id:r.run_id,status:r.status,finished_at:r.finished_at,selected:r.selected,metrics:r.candidates.find(c=>c.profile_id===r.selected?.profile_id)?.metrics||null});
(async()=>{
 const host=spawn(path.join(root,'.venv/bin/python'),['tests/m4_selection_demo_server.py'],{cwd:root});let browser,release;
 try{
  const port=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('startup timeout')),20000);host.stdout.on('data',chunk=>{text+=chunk;const match=text.match(/PORT=(\d+)/);if(match){clearTimeout(timer);resolve(+match[1]);}});host.once('exit',c=>reject(Error('host exit '+c)));host.stderr.on('data',c=>process.stderr.write(c));});
  browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[],requests=[];let second=shifted(15),wrong=false;
  page.on('pageerror',e=>errors.push(e.stack));page.on('request',r=>{requests.push(r.url());if(r.method()!=='GET')writes.push(r.url());});
  await page.route('**/decision-history?*',route=>{const url=new URL(route.request().url()),station=url.pathname.includes('station-1')?'station-1':'station-2',offset=+url.searchParams.get('offset');return route.fulfill({json:{schema_version:'m4-decision-history-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',offset,next_offset:station==='station-1'&&offset===0?10:null,items:station==='station-2'?[]:offset===0?[summary(base),summary(second),{status:'unreadable',run_id:null},summary(blocked)]:[summary(base)]}});});
  await page.route('**/decision-results/*',route=>{const id=route.request().url().split('/').pop();return route.fulfill({json:envelope(wrong?{...base,station_id:'station-2'}:id===ids[0]?base:id===ids[1]?second:blocked)});});
  await page.goto(`http://127.0.0.1:${port}/m4`);await page.waitForLoadState('networkidle');await page.locator('#open-plan-history').click();await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===4);
  assert.equal(await page.locator('#daily-tab-history').getAttribute('aria-selected'),'true');assert.equal(await page.locator('#daily-panel-history').isVisible(),true);
  assert.equal(await page.locator('#plan-history-drawer').isVisible(),false);assert.equal(await page.locator('#history-station').isVisible(),false);
  const labels=await page.locator('#history-a option').allTextContents();
  assert.equal(new Set(labels).size,2,'same-second plans remain distinguishable without internal IDs');
  assert(labels.every(label=>label.includes('均衡方案')&&label.includes('记录 ')));
  assert.deepEqual(await page.locator('#history-a option').evaluateAll(options=>options.map(option=>option.value)),ids.slice(0,2),'selection still binds to real record IDs');
  for(const id of ids)assert(!(await page.locator('#daily-panel-history').innerText()).includes(id.slice(0,8)),'customer list and selectors hide raw IDs');
  assert.equal(await page.locator('#history-rows button:disabled').count(),1);await page.locator('#history-more').click();await page.waitForFunction(()=>document.querySelector('#history-more').hidden);assert.equal(await page.locator('#history-rows tr').count(),4,'duplicate run is deduplicated');
  assert.deepEqual(await page.locator('#history-a option').allTextContents(),labels,'pagination keeps existing labels stable');
  await page.locator(`[data-history-run="${ids[0]}"]`).click();await page.waitForFunction(()=>!document.querySelector('#history-output').hidden);assert.equal(await page.locator('#history-output details tbody tr').count(),96);assert.equal(await page.locator('#history-output polyline').count(),1);
  assert.equal(await page.locator('.history-technical').getAttribute('open'),null);
  assert(!(await page.locator('#history-output').innerText()).includes(ids[0].slice(0,8)),'detail summary hides the raw ID');
  await page.locator('.history-technical > summary').click();assert((await page.locator('.history-technical').innerText()).includes(ids[0]));await page.locator('.history-technical > summary').click();
  await page.locator('#history-compare').click();await page.waitForFunction(()=>document.querySelector('#history-comparison-summary')?.textContent.includes('共同 95'));
  assert.equal(await page.locator('#history-output polyline').count(),2);assert.match(await page.locator('.history-context').innerText(),/初始 SOC.*20 → 30/);assert.match(await page.locator('.history-context').innerText(),/两轮负荷预测来源记录不同/);
  assert.doesNotMatch(await page.locator('#history-output').innerText(),/load-v1|load-v2|11111111|22222222/);
  assert.match(await page.locator('.history-technical-context').textContent(),/load-v1 → load-v2/);
  await page.locator(`[data-history-run="${ids[1]}"]`).click();await page.waitForFunction(()=>document.querySelectorAll('#history-output details tbody tr').length===95);
  assert.match(await page.locator('#history-output details:not(.history-technical) > summary').textContent(),/95 点/);
  const endTime=new Date(Date.parse(second.plan_start_at)+95*900000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  assert((await page.locator('#history-output').textContent()).includes(endTime));
  await page.locator('#history-compare').click();await page.waitForFunction(()=>document.querySelector('#history-comparison-summary')?.textContent.includes('共同 95'));
  const screenshots='/tmp/m4-history-screens';fs.mkdirSync(screenshots,{recursive:true});
  for(const width of [1440,768,390,320])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:1000});await page.evaluate(theme=>document.documentElement.setAttribute('data-theme',theme),theme);await page.locator('#daily-panel-history').scrollIntoViewIfNeeded();
   assert(await page.locator('#daily-panel-history').evaluate(e=>e.scrollWidth<=e.clientWidth+1),`history panel overflow ${width} ${theme}`);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),`history page overflow ${width} ${theme}`);
   await page.screenshot({animations:"disabled",path:path.join(screenshots,`history-${width}-${theme}.png`),fullPage:true});await page.locator('#history-output').scrollIntoViewIfNeeded();await page.screenshot({animations:"disabled",path:path.join(screenshots,`comparison-${width}-${theme}.png`)});
  }
  second=shifted(1440);await page.locator('#history-compare').click();await page.waitForFunction(()=>document.querySelector('#history-comparison-summary')?.textContent.includes('没有共同'));assert.equal(await page.locator('#history-output svg').count(),0);
  await page.locator(`[data-history-run="${ids[2]}"]`).click();await page.waitForFunction(()=>document.querySelector('#history-output').textContent.includes('尚未保存参数'));assert.equal(await page.locator('#history-output svg').count(),0);
  wrong=true;await page.locator(`[data-history-run="${ids[0]}"]`).click();await page.waitForFunction(()=>document.querySelector('#history-status').dataset.error==='true');assert.equal(await page.locator('#history-output').isVisible(),false);wrong=false;
  let entered;const held=new Promise(r=>release=r),fetched=new Promise(r=>entered=r);await page.route('**/decision-results/'+ids[0],async route=>{entered();await held;await route.fulfill({json:envelope(base)});});
  await page.locator(`[data-history-run="${ids[0]}"]`).click();await fetched;await page.locator('#station-select').selectOption('s2');await page.waitForFunction(()=>document.querySelector('#history-status').textContent.includes('暂无'));release();release=null;await page.waitForLoadState('networkidle');assert.equal(await page.locator('#history-output').isVisible(),false);assert.equal(await page.locator('#history-compare').isDisabled(),true);
  assert.equal(await page.locator('#daily-tab-history').getAttribute('aria-selected'),'true');assert.equal(await page.locator('#daily-panel-history').isVisible(),true);
  await page.locator('#daily-tab-overview').click();assert.equal(await page.locator('#daily-panel-history').isVisible(),false);
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);assert(requests.every(url=>new URL(url).hostname==='127.0.0.1'));
  console.log('PASS: customer history tab, global station selection, history pagination/dedup, 96-point detail, 95-point alignment, source changes, no overlap, blocked/corrupt/wrong-station records, late response isolation, 8 theme/layout screenshots; GET only, Mock only.');
 }finally{if(release)release();if(browser)await browser.close();host.kill('SIGTERM');}
})().catch(e=>{console.error(e);process.exitCode=1;});
