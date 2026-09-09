'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'m4/mock/orchestration/orchestration-result.json')));
const source=fixture.stations.find(s=>s.station_id==='station-1');
const record={run_id:'solver-browser-fixture',station_id:'station-1',status:'completed',started_at:fixture.started_at,finished_at:fixture.finished_at,solve_seconds:1.25,solver_name:'pyomo-highs',solver_version:'HiGHS 1.15.1; Pyomo 6.10.1',model_version:'pyomo-model-v2',selector_version:'pyomo-highs-selection-v2',candidate_run_id:fixture.run_id,selected:{profile_id:'balanced',profile_version:source.optimization_result.candidates[0].profile_version,plan_version:source.optimization_result.candidates[0].plan_version},reason:'需量优先，按已保存候选顺序选择均衡方案。',checked_at:fixture.finished_at,expires_at:fixture.finished_at,input_sha256:'a'.repeat(64),comparison:[{metric:'profile_priority',remaining_ids:['balanced']}],candidates:source.optimization_result.candidates,issues:[],stages:[],plan_start_at:source.input_summary.plan_start_at,policy:{version:'browser-policy-v1',metric:'profile_priority',tie_order:['balanced','cost','pv'],demand_peak_tolerance_kw:0.000001,demand_energy_tolerance_kwh:0.000001,metric_tolerance:0}};
const generated=spawnSync(path.join(root,'.venv/bin/python'),['-m','tests.m4_peak_preparation_fixtures'],{cwd:root,encoding:'utf8'});assert.equal(generated.status,0,generated.stderr);const peaks=JSON.parse(generated.stdout);
record.peak_preparation=peaks.covered.summary;
function peakRecord(name){const p=peaks[name];return {...record,run_id:'peak-'+name,selected:{profile_id:'balanced',plan_version:p.candidate.plan_version},candidates:[p.candidate],peak_preparation:p.summary,plan_start_at:p.summary.plan_start_at};}
function result(station='station-1',value=record){return {schema_version:'m4-decision-results-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',status:value?'available':'empty',record:value};}
(async()=>{
 const host=spawn(path.join(root,'.venv/bin/python'),['tests/m4_selection_demo_server.py'],{cwd:root});
 let browser,release;const errors=[],requests=[],posts=[];
 try{
  const port=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('mock host startup timeout')),20000);host.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/PORT=(\d+)/);if(match){clearTimeout(timer);resolve(Number(match[1]));}});host.once('exit',code=>reject(Error('mock host exit '+code)));host.stderr.on('data',chunk=>process.stderr.write(chunk));});
  browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1100}});
  page.on('pageerror',e=>errors.push(e.stack));page.on('request',r=>requests.push(r.url()));
  let current=record,job=null;
  await page.route('**/decision-result',route=>{const station=route.request().url().includes('station-1')?'station-1':'station-2';return route.fulfill({json:result(station,station==='station-1'?current:null)});});
  await page.route('**/decision-runs',route=>{
   const station=route.request().url().includes('station-1')?'station-1':'station-2';
   if(route.request().method()==='POST'){posts.push(route.request().postDataJSON());job={run_id:'job-1',station_id:station,status:'running',stage:'model_solver',started_at:fixture.started_at,finished_at:null,message:'正在计算三套数学候选'};}
   return route.fulfill({status:route.request().method()==='POST'?202:200,json:{station_id:station,usage:'preview_only',dispatch_status:'not_dispatched',job:job?.station_id===station?job:null}});
  });
  await page.goto(`http://127.0.0.1:${port}/m4`);await page.waitForLoadState('networkidle');
  assert.deepEqual(errors,[]);
  const tabKeys=['overview','plan','devices','history'];
  async function assertTab(key){
   for(const candidate of tabKeys){
    const active=candidate===key,tab=page.locator('#daily-tab-'+candidate);
    assert.equal(await tab.getAttribute('role'),'tab');
    assert.equal(await tab.getAttribute('aria-selected'),String(active));
    assert.equal(await tab.getAttribute('aria-controls'),'daily-panel-'+candidate);
    assert.equal(await tab.getAttribute('tabindex'),active?'0':'-1');
    assert.equal(await page.locator('#daily-panel-'+candidate).isVisible(),active,'only the selected customer page is visible: '+candidate);
   }
  }
  await assertTab('overview');
  const tabOrder=await page.locator('[role="tab"][id^="daily-tab-"]').evaluateAll(tabs=>tabs.map(tab=>tab.id.replace('daily-tab-','')));
  assert.equal(tabOrder.length,4);assert.equal(tabOrder[0],'overview');
  await page.locator('#daily-tab-overview').focus();
  for(const [key,expected] of [['ArrowRight',tabOrder[1]],['ArrowLeft','overview'],['End',tabOrder.at(-1)],['Home','overview']]){
   await page.keyboard.press(key);await assertTab(expected);
   assert.equal(await page.locator('#daily-tab-'+expected).evaluate(tab=>tab===document.activeElement),true,'keyboard navigation moves focus');
  }
  for(const key of tabKeys){await page.locator('#daily-tab-'+key).click();await assertTab(key);}
  await page.locator('#daily-tab-overview').click();
  await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='completed');
  assert.match(await page.locator('#solver-result-choice').innerText(),/均衡方案/);
  assert.match(await page.locator('#solver-result-summary').innerText(),/历史快照.*当前状态未重新复核/);
  assert.match(await page.locator('#solver-result-model').innerText(),/HiGHS.*Pyomo.*1.3 秒/);
  assert.equal(await page.locator('#solver-result-evidence').getAttribute('open'),null);
  assert.equal(await page.locator('#daily-panel-overview #solver-result-panel').isVisible(),true);
  assert.equal(await page.locator('#daily-panel-plan #decision-plan-details').count(),1);
  assert.equal(await page.locator('#decision-plan-details').isVisible(),false);
  assert.equal(await page.locator('#decision-plan-details').getAttribute('open'),'');
  assert.equal(await page.locator('#daily-advanced').getAttribute('open'),null);
  assert.equal(await page.locator('.bill-card').isVisible(),false);
  assert.equal(await page.locator('#start-live-candidates').isVisible(),false);
  assert.equal(await page.locator('#select-candidate').isVisible(),false);
  assert.equal(await page.locator('#daily-plan-cost').isVisible(),true);
  assert.equal(await page.locator('#peak-preparation').getAttribute('data-state'),'covered');
  assert.equal(await page.locator('#peak-gap-value').innerText(),'0');
  assert.match(await page.locator('#peak-preparation').innerText(),/历史估算不代表当前实测/);
  await page.locator('#daily-inspect-inputs').click();
  await assertTab('devices');
  assert.equal(await page.locator('#live-input-panel').isVisible(),true);
  assert.equal(await page.locator('#daily-input-details > summary').isVisible(),false);
  // Exercise the real background result poll while a different customer page is open.
  current={...record,run_id:'background-refresh-fixture'};
  await page.waitForFunction(()=>document.querySelector('#peak-preparation').dataset.runId==='background-refresh-fixture',null,{timeout:40000});
  await assertTab('devices');
  assert.equal(await page.locator('#solver-result-panel').isVisible(),false);
  assert.equal(await page.locator('#decision-plan-details').isVisible(),false);
  await page.locator('#daily-tab-plan').click();await assertTab('plan');
  assert.equal(await page.locator('#decision-plan-chart rect').count(),96);
  assert.equal(await page.locator('#decision-plan-table tbody tr').count(),96);
  assert.match(await page.locator('#decision-plan-caption').innerText(),/该轮最终方案/);
  await page.locator('#solver-result-evidence summary').click();
  await page.locator('[data-decision-plan="pv"]').click();
  assert.match(await page.locator('#decision-plan-caption').innerText(),/对比候选.*光伏/);
  assert.match(await page.locator('#solver-result-choice').textContent(),/均衡方案/,'preview must not change final selection');
  assert.equal(await page.locator('#peak-preparation').getAttribute('data-state'),'covered');
  const short=structuredClone(peakRecord('clear'));
  short.candidates.forEach(c=>c.plan=c.plan.slice(0,95));
  short.peak_preparation.plan_end_at=new Date(Date.parse(short.plan_start_at)+95*900000).toISOString();
  current=short;await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state!=='loading');
  assert.equal(await page.locator('#solver-result-status').getAttribute('data-state'),'completed',await page.locator('#solver-result-summary').textContent());
  assert.equal(await page.locator('#decision-plan-table tbody tr').count(),95);
  assert.equal(await page.locator('#decision-plan-chart rect').count(),95);
  assert.match(await page.locator('#decision-plan-details > summary').textContent(),/23 小时 45 分钟/);
  assert.equal(await page.locator('#peak-preparation').getAttribute('data-state'),'clear');
  for(const width of [1440,768,390,320])for(const theme of ['light','dark']){
    await page.setViewportSize({width,height:1100});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth));
    await assertTab('plan');
    fs.mkdirSync('/tmp/m4-95-screens',{recursive:true});await page.locator('#daily-panel-plan').screenshot({path:`/tmp/m4-95-screens/final-${width}-${theme}.png`});
  }
  await page.setViewportSize({width:1440,height:1100});
  current={...short,candidates:short.candidates.map(c=>({...c,plan:c.plan.slice(0,94)}))};
  await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state!=='loading');
  assert.equal(await page.locator('#solver-result-status').getAttribute('data-state'),'error');
  current=record;await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state!=='loading');
  const failSubmit=route=>route.request().method()==='POST'?route.fulfill({status:503,json:{detail:'测试：计划服务暂不可用'}}):route.fallback();
  await page.route('**/decision-runs',failSubmit);
  await page.locator('#start-decision-run').click();
  await page.waitForFunction(()=>document.querySelector('#decision-run-progress').textContent.includes('计划服务暂不可用')&&!document.querySelector('#start-decision-run').disabled);
  assert.equal(await page.locator('#decision-run-progress').isVisible(),true,'a plan submission failure is visible outside overview');
  assert.equal(await page.locator('#decision-run-progress').getAttribute('data-state'),'error');
  await assertTab('plan');await page.unroute('**/decision-runs',failSubmit);
  await page.locator('#start-decision-run').click();
  await page.waitForFunction(()=>document.querySelector('#start-decision-run').textContent==='计划生成中…');
  assert.equal(await page.locator('#start-decision-run').isDisabled(),true);assert.equal(await page.locator('#start-live-candidates').isDisabled(),true);assert.equal(await page.locator('#select-candidate').isDisabled(),true);assert.equal(posts.length,1);assert.match(posts[0].request_id,/^[0-9a-f-]{36}$/);
  job={...job,status:'finished',stage:'blocked_selection',finished_at:fixture.finished_at,message:'实时复核未通过'};
  current={...record,run_id:'blocked-fixture',status:'blocked_selection',selected:null,peak_preparation:null,reason:'现场状态已变化，本轮阻断。',issues:['现场状态已变化'],candidates:record.candidates.map(c=>c.profile_id==='pv'?{...c,metrics:null,plan:[]}:c)};
  await page.locator('#refresh-solver-result').click();
  await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='blocked_selection');
  assert.equal(await page.locator('#solver-result-choice').textContent(),'无最终方案');
  assert.equal(await page.locator('#peak-preparation').isVisible(),false);
  assert.equal(await page.locator('#decision-plan-details').isVisible(),false);assert.equal(await page.locator('[data-decision-plan="pv"]').isDisabled(),true);
  await page.locator('#solver-result-evidence > summary').click();await page.locator('[data-decision-plan="cost"]').click();
  assert.match(await page.locator('#decision-plan-caption').innerText(),/对比候选/);
  await page.locator('#station-select').selectOption('s2');await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='empty');
  await assertTab('plan');
  assert.equal(await page.locator('#decision-plan-details').isVisible(),false);
  await page.locator('#daily-tab-overview').click();
  assert.equal(await page.locator('#solver-result-body').isVisible(),false);
  assert.equal(await page.locator('#daily-plan-empty').isVisible(),true);
  assert.match(await page.locator('#daily-action').innerText(),/保存运营偏好/);
  await page.locator('#daily-open-settings').click();assert.equal(await page.locator('#settings-drawer').isVisible(),true);await page.keyboard.press('Escape');
  assert.equal(await page.locator('#start-decision-run').isDisabled(),false,'both stations can start, backend handles missing configuration');
  await page.locator('#open-selection-policy').click();await page.waitForLoadState('networkidle');assert.equal(await page.locator('#policy-metric').isVisible(),true);await page.keyboard.press('Escape');
  await page.locator('#station-select').selectOption('s1');current=record;await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='completed');
  await page.locator('#station-select').selectOption('all');assert.equal(await page.locator('#all-workspace').isVisible(),true);assert.equal(await page.locator('#solver-result-panel').isVisible(),false);
  for(const key of tabKeys){assert.equal(await page.locator('#daily-tab-'+key).isVisible(),false);assert.equal(await page.locator('#daily-panel-'+key).isVisible(),false);}
  await page.locator('[data-station-summary="s1"] button').click();assert.equal(await page.locator('#solver-result-panel').isVisible(),true);
  await assertTab('overview');
  current=peakRecord('energy');await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#peak-preparation').dataset.state==='attention');
  assert.equal(await page.locator('#peak-required-value').innerText(),'25');assert.equal(await page.locator('#peak-gap-value').innerText(),'15');assert.match(await page.locator('#peak-preparation-action').innerText(),/35%.*22.5 分钟/);
  const screenshots='/tmp/m4-peak-preparation-screens';fs.mkdirSync(screenshots,{recursive:true});
  for(const width of [1440,768,390,320])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:1100});if(await page.locator('html').getAttribute('data-theme')!==theme)await page.locator('#theme-toggle').click();
   await page.locator('#solver-result-panel').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(screenshots,`decision-${width}-${theme}.png`),animations:'disabled',fullPage:true});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`page overflow ${width} ${theme}`);
   for(const key of ['plan','devices','history']){
    await page.locator('#daily-tab-'+key).click();await assertTab(key);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${key} page overflow ${width} ${theme}`);
    await page.screenshot({path:path.join(screenshots,`${key}-${width}-${theme}.png`),animations:'disabled',fullPage:true});
   }
   await page.locator('#daily-tab-overview').click();
  }
  for(const [name,pattern] of [['power',/增加 SOC 也不能单独消除/],['clear',/未超过需量上限/]]){
   current=peakRecord(name);await page.locator('#refresh-solver-result').click();await page.waitForFunction(name=>document.querySelector('#peak-preparation').dataset.runId==='peak-'+name&&document.querySelector('#peak-preparation').dataset.state===(name==='power'?'attention':'clear'),name);
   assert.match(await page.locator('#peak-preparation').innerText(),pattern);
  }
  current={...record,peak_preparation:null};await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#peak-preparation').dataset.state==='unavailable');assert.equal(await page.locator('#peak-preparation-grid').isVisible(),false);
  current={...record,peak_preparation:{...peaks.covered.summary,plan_version:'wrong-plan'}};await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='error');assert.equal(await page.locator('#peak-preparation').isVisible(),false);
  await page.route('**/station-1/decision-result',route=>route.fulfill({status:503,json:{detail:'测试读取失败'}}));await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='error');assert.equal(await page.locator('#solver-result-body').isVisible(),false);
  await page.unroute('**/station-1/decision-result');
  let entered;const held=new Promise(resolve=>release=resolve),fetched=new Promise(resolve=>entered=resolve);
  await page.route('**/station-1/decision-result',async route=>{entered();await held;await route.fulfill({json:result()});});
  await page.locator('#refresh-solver-result').click();await fetched;await page.locator('#station-select').selectOption('s2');release();release=null;await page.waitForLoadState('networkidle');assert.equal(await page.locator('#solver-result-status').getAttribute('data-state'),'empty');
  await page.unroute('**/station-1/decision-result');
  // A historical AI envelope cannot enter the solver decision panel.
  await page.route('**/station-1/decision-result',route=>route.fulfill({json:{...result(),schema_version:'m4-ai-results-v1',usage:'historical_preview'}}));
  await page.locator('#station-select').selectOption('s1');await page.locator('#refresh-solver-result').click();await page.waitForFunction(()=>document.querySelector('#solver-result-status').dataset.state==='error');assert.equal(await page.locator('#solver-result-body').isVisible(),false);
  assert.deepEqual(errors,[]);assert.equal(requests.some(url=>/ai-runs|ai-selection/.test(url)),false);assert.equal(requests.some(url=>new URL(url).hostname!=='127.0.0.1'),false);assert.equal(posts.length,1);
  console.log('PASS: four customer tabs, keyboard navigation, background polling isolation, solver decision final/blocked records, 95/96 points, comparison, run lock, station navigation, policy, failure/late response, AI envelope rejection; four tabs across 8 responsive theme layouts; mock only.');
 }finally{if(release)release();if(browser)await browser.close();host.kill('SIGTERM');}
})().catch(error=>{console.error(error);process.exitCode=1;});
