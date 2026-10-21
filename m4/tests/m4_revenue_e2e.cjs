'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webkit}=require('playwright');
const {execFileSync}=require('node:child_process'),{createHash}=require('node:crypto');
const root=path.resolve(__dirname,'../..');
const fixture=process.env.M4_FALLBACK_FIXTURE_DIR;
assert(fixture,'Set M4_FALLBACK_FIXTURE_DIR');
const read=name=>JSON.parse(fs.readFileSync(path.join(fixture,name),'utf8'));
const saved=read('station-2-validated-fallback-snapshot.json'),input=read('station-2-daily-inputs.json');
const settings=read('station-2-settings.json'),project=read('ui-project.json');
const html=fs.readFileSync(process.env.M4_REVENUE_HTML_PATH||path.join(root,'m4/web/M4优化调度控制台-线上版.html'),'utf8');
const output=path.resolve(process.env.M4_REVENUE_OUTPUT_DIR||path.join(root,'outputs/m4/tests/2026-10-21-revenue-estimates'));fs.mkdirSync(output,{recursive:true});
const date='2026-10-14',stamp=date+'T13:50:00+08:00';
function revenue(station){return {schema_version:'m4-today-revenue-v1',station_id:station,date,timezone:'Asia/Shanghai',fetched_at:stamp,cards:{
 total:{amount_yuan:150,status:'ready',updated_at:stamp},grid:{amount_yuan:100,import_kwh:200,export_kwh:10,status:'ready',updated_at:stamp},
 storage:{amount_yuan:50,charge_kwh:100,discharge_kwh:150,status:'ready',updated_at:stamp},pv:{amount_yuan:100,generation_kwh:200,self_use_kwh:190,status:'ready',updated_at:stamp}}};}
// Exercise the real calculator without network, API lifespan or scheduling.
const calculated=JSON.parse(execFileSync(process.env.M4_PYTHON||path.join(root,'.venv/bin/python'),['-c',`
import json
from datetime import datetime, timedelta
from m4.tests.test_m4_today_revenue import Source, rows
from m4.settings.today_revenue import TodayRevenueService, TZ
day=datetime(2026,10,14,tzinfo=TZ)
def calculate(powers):
    records=rows(*powers)
    for i,row in enumerate(records): row['timestamp']=(day+timedelta(seconds=30*i)).isoformat()
    service=TodayRevenueService(Source(records))
    service._tariff=lambda *args: {'values':[1.0]*96,'sell_price_per_kwh':0.3}
    return service.fetch('station-2',now=day+timedelta(seconds=30*(len(records)-1)))
normal=(30,100,50,20)
print(json.dumps({'estimated':calculate([normal,(30,-1,50,20),normal,normal]),
                  'unavailable':calculate([(10,-1,20,-30)]*2),
                  'complete':calculate([normal]*2)}))
`],{cwd:root,encoding:'utf8'}));
fs.writeFileSync(path.join(output,'backend-scenarios.json'),JSON.stringify(calculated,null,2));
(async()=>{
 const browser=await webkit.launch({headless:true});const results=[],requests=[],errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});await page.clock.install({time:new Date(stamp)});
  let failed=false,currentRevenue=null,revenueGate=null;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://m4.test/**',async route=>{
   const req=route.request(),url=new URL(req.url());assert.equal(req.method(),'GET','Read-only UI must not write or start tasks');
   if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
   assert.equal(req.headers().authorization,'Bearer fixture-only-token');
   const endpoint=url.pathname.split('/').at(-1),station=url.pathname.split('/')[3];requests.push({endpoint,station});
   if(endpoint==='project')return route.fulfill({json:project});
   if(endpoint==='today-revenue'){if(revenueGate)await revenueGate;return failed?route.fulfill({status:502,json:{detail:'统计读取失败'}}):route.fulfill({json:currentRevenue||revenue(station)});}
   if(endpoint==='storage-log')return route.fulfill({json:{schema_version:'m4-storage-log-v1',station_id:station,date,timezone:'Asia/Shanghai',observed_until:stamp,items:[{start_at:date+'T02:00:00+08:00',end_at:date+'T03:00:00+08:00',mode:'charge',power_kw:120.5}]}});
   if(endpoint==='daily-plan'){
    const job=structuredClone(saved);job.station_id=station;job.automation={enabled:true,next_check_at:stamp};job.rolling_history={station_id:station,status:'empty',rows:[]};job.rolling={status:'disabled'};job.ems_table_write={enabled:true};job.daily_dispatch={station_id:station,date,status:'waiting'};
    return route.fulfill({json:job});
   }
   if(endpoint==='daily-inputs')return route.fulfill({json:{...input,station_id:station,can_compare:false,comparison:null}});
   if(endpoint==='settings')return route.fulfill({json:{...settings,station_id:station}});
   if(endpoint==='control-sources')return route.fulfill({json:{...input.sources.controls,station_id:station}});
   if(endpoint==='decision-history')return route.fulfill({json:{station_id:station,items:[]}});
   throw Error('Unexpected route '+url.pathname);
  });
  await page.goto('https://m4.test/m4?token=fixture-only-token');await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('#revenue-refresh').inputValue(),'60');
  assert.equal(await page.locator('#revenue-total').innerText(),'150.00');
  await page.waitForFunction(()=>document.querySelector('.reference-overview').classList.contains('summary-hidden'));
  assert.equal(await page.locator('.reference-overview>.decision').isVisible(),false);
  assert.equal(await page.locator('.cost-comparison').isVisible(),false);
  assert.equal(await page.locator('#chart-panel').isVisible(),true);
  assert.equal(await page.locator('#schedule-tab-plan').isVisible(),false);
  assert.equal(await page.locator('#schedule-plan-panel').isVisible(),false);
  assert.equal(await page.locator('#schedule-log-panel').isVisible(),true);
  assert.equal(requests.filter(r=>r.endpoint==='storage-log').length,0,'Log is lazy');
  const initial=requests.filter(r=>r.endpoint==='today-revenue').length;
  await page.clock.fastForward(59000);await page.waitForLoadState('networkidle');
  assert.equal(requests.filter(r=>r.endpoint==='today-revenue').length,initial);
  await page.clock.fastForward(1100);await page.waitForLoadState('networkidle');
  assert.equal(requests.filter(r=>r.endpoint==='today-revenue').length,initial+1);
  await page.locator('#schedule-tab-log').click();await page.waitForLoadState('networkidle');
  assert.match(await page.locator('#storage-log-rows').innerText(),/充电[\s\S]*120.500/);
  const logs=requests.filter(r=>r.endpoint==='storage-log').length;
  await page.clock.fastForward(60000);await page.waitForLoadState('networkidle');
  assert.equal(requests.filter(r=>r.endpoint==='storage-log').length,logs,'No log polling');
  await page.locator('#storage-log-refresh').click();await page.waitForLoadState('networkidle');
  assert.equal(requests.filter(r=>r.endpoint==='storage-log').length,logs+1);
  await page.locator('#schedule-tab-log').press('ArrowLeft');assert.equal(await page.locator('#schedule-tab-log').getAttribute('aria-selected'),'true');
  failed=true;await page.evaluate(()=>refreshTodayRevenue());
  assert.equal(await page.locator('#revenue-total').innerText(),'150.00');
  assert.match(await page.locator('#revenue-status-total').innerText(),/更新失败/);failed=false;await page.evaluate(()=>refreshTodayRevenue());
  // Restoring a visible page must keep its amount while a slow read is pending.
  let releaseRevenue;
  revenueGate=new Promise(resolve=>{releaseRevenue=resolve;});
  const pendingRead=page.waitForRequest(req=>new URL(req.url()).pathname.endsWith('/today-revenue'));
  await page.evaluate(()=>{window.pendingRevenue=refreshTodayRevenue(true);});await pendingRead;
  assert.equal(await page.locator('#revenue-total').innerText(),'150.00');
  assert.match(await page.locator('#revenue-status-total').innerText(),/正在更新/);
  revenueGate=null;releaseRevenue();await page.evaluate(()=>window.pendingRevenue);
  // A reload uses an authenticated, same-day snapshot before the network finishes.
  currentRevenue={...revenue('station-2'),fetched_at:await page.evaluate(()=>new Date().toISOString())};
  await page.evaluate(()=>refreshTodayRevenue());
  revenueGate=new Promise(resolve=>{releaseRevenue=resolve;});
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>document.querySelector('#revenue-total').textContent==='150.00');
  assert.match(await page.locator('#revenue-status-total').innerText(),/正在更新/);
  revenueGate=null;releaseRevenue();await page.waitForLoadState('networkidle');
  // Cached background responses are followed by a bounded short read of the result.
  currentRevenue={...currentRevenue,refresh_status:'refreshing'};
  await page.evaluate(()=>refreshTodayRevenue());
  assert.match(await page.locator('#revenue-status-total').innerText(),/正在更新/);
  const refreshDeadline=await page.evaluate(()=>revenueRetryUntil);
  await page.clock.fastForward(100);await page.evaluate(()=>refreshTodayRevenue());
  assert.equal(await page.evaluate(()=>revenueRetryUntil),refreshDeadline,'Regular refresh must not extend the background polling deadline');
  const beforeRetry=requests.filter(r=>r.endpoint==='today-revenue').length;
  currentRevenue={...currentRevenue,refresh_status:'ready',cards:structuredClone(currentRevenue.cards)};
  currentRevenue.cards.total.amount_yuan=151;currentRevenue.cards.storage.amount_yuan=51;
  await page.clock.fastForward(1100);await page.waitForLoadState('networkidle');
  assert.equal(requests.filter(r=>r.endpoint==='today-revenue').length,beforeRetry+1);
  assert.equal(await page.locator('#revenue-total').innerText(),'151.00');
  // Restoring another station, an expired response or yesterday's response is rejected.
  const rejected=await page.evaluate(()=>{
   const data=structuredClone(revenueSnapshot),key=revenueKey('station-2');
   return [
    {...data,station_id:'station-1'},
    {...data,fetched_at:new Date(Date.now()-120001).toISOString()},
    {...data,date:'2000-01-01'}
   ].map(invalid=>{revenueCache.set(key,invalid);return restoreRevenue('station-2',today());});
  });
  assert.deepEqual(rejected,[false,false,false]);
  currentRevenue=calculated.estimated;await page.evaluate(()=>refreshTodayRevenue());
  assert.equal(await page.locator('#revenue-total').innerText(),'0.59');
  assert.equal(await page.locator('#revenue-storage').innerText(),'0.17');
  assert.equal(await page.locator('#revenue-pv').innerText(),'0.42');
  assert.equal(await page.locator('#revenue-grid').innerText(),'0.75');
  for(const name of ['storage','pv','total'])assert.match(await page.locator('#revenue-status-'+name).innerText(),/^暂估/);
  assert.match(await page.locator('#revenue-status-grid').innerText(),/^统计更新/);
  assert.equal(currentRevenue.cards.total.amount_coverage.excluded_seconds,60);
  assert.equal(currentRevenue.coverage.excluded_intervals.length,1);
  assert.equal(await page.locator('.revenue-footer').isVisible(),false);
  await page.locator('#today-revenue').screenshot({path:path.join(output,'estimated-revenue.png')});
  // A day with no usable money intervals must remain unknown, never zero.
  currentRevenue=calculated.unavailable;await page.evaluate(()=>refreshTodayRevenue());
  assert.equal(await page.locator('#revenue-total').innerText(),'—');
  assert.equal(await page.locator('#revenue-storage').innerText(),'—');
  assert.equal(await page.locator('#revenue-grid').innerText(),'0.08');
  assert.doesNotMatch(await page.locator('#revenue-status-total').innerText(),/^暂估/);
  // Fully valid input recovers from the unavailable state through the same GET path.
  currentRevenue=calculated.complete;await page.evaluate(()=>refreshTodayRevenue());
  assert.equal(await page.locator('#revenue-total').innerText(),'0.59');
  assert.match(await page.locator('#revenue-status-total').innerText(),/^统计更新/);
  currentRevenue=calculated.estimated;await page.evaluate(()=>refreshTodayRevenue());
  for(const width of [1440,834,390])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:1000});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),width+' '+theme+' page overflow');
   const file=path.join(output,width+'-'+theme+'.png');await page.screenshot({path:file,fullPage:true,animations:'disabled'});
   results.push({width,theme,screenshot:file});
  }
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({browser:'WebKit',transport:'isolated GET fixtures with real local revenue calculator; no production requests',source_sha256:{html:createHash('sha256').update(html).digest('hex'),calculator:createHash('sha256').update(fs.readFileSync(path.join(root,'m4/settings/today_revenue.py'))).digest('hex')},checks:['four revenue cards','default 60s polling','lazy manual-only storage log','tab keyboard navigation','failure preserves prior values','fallback summary and cost comparison hidden','24h chart retained and plan tab hidden','real calculator mixed-validity response retains money and displays estimate','all-bad money remains unavailable','complete input recovers normal display','footer remains hidden'],results,errors,requests},null,2));
  console.log('Revenue interactions, read-only requests and six responsive/theme renders passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
