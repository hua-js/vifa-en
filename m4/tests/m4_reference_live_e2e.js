'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{chromium}=require('playwright');
const html=fs.readFileSync(path.join(__dirname,'../web/M4优化调度控制台-线上版.html'),'utf8');
const day=new Date(Date.now()+28800000).toISOString().slice(0,10),start=Date.parse(day+'T00:00:00+08:00');
const run='11111111-1111-4111-8111-111111111111';
function fixture(station){
 const factor=station==='station-1'?1:2;
 const points=Array.from({length:96},(_,i)=>({timestamp:new Date(start+i*900000).toISOString(),load_forecast_kw:600*factor,pv_forecast_kw:Math.max(0,Math.sin((i/4-6)/12*Math.PI))*(i>=24&&i<72?450:0)*factor,buy_price_per_kwh:i<28?.3:i>=64&&i<84?1.2:.7,tariff_period:i<28?'gu':i>=64&&i<84?'feng':'ping'}));
 let soc=50;
 const base=points.map(p=>({timestamp:p.timestamp,mode:'idle',target_power_kw:0,grid_import_kw:p.load_forecast_kw-p.pv_forecast_kw,expected_soc_pct:50,grid_export_kw:0,pv_unabsorbed_kw:0,demand_exceed_kw:0}));
 const plan=base.map((p,i)=>{const power=i<8?-50*factor:i>=72&&i<80?50*factor:0;soc-=power*.25/(1000*factor)*100;return {...p,mode:power<0?'charge':power>0?'discharge':'idle',target_power_kw:Math.abs(power),grid_import_kw:p.grid_import_kw-power,expected_soc_pct:soc};});
 const metrics=series=>({energy_cost:series.reduce((s,p,i)=>s+p.grid_import_kw*points[i].buy_price_per_kwh*.25,0),cycle_cost:series.reduce((s,p)=>s+p.target_power_kw*.25*.03,0),max_grid_import_kw:Math.max(...series.map(p=>p.grid_import_kw)),pv_self_use_rate:1,pv_self_use_kwh:points.reduce((s,p)=>s+p.pv_forecast_kw*.25,0)});
 const baseline={profile_id:'ems',plan_version:'ems/v1',plan:base,metrics:metrics(base)},chosen={profile_id:'balanced',plan_version:'plan/v1',plan,metrics:metrics(plan)};
 const request={station_id:station,plan_start_at:new Date(start).toISOString(),source_versions:{pv:'pv/v1',...(station==='station-2'?{pv_gap_policy:'outside_forecast_window_zero',pv_zero_filled_points:'24'}:{}),controls:'controls/v1',daily_policy:'m4-daily-peak-reserve-v1'},points,capability:{energy_capacity_kwh:1000*factor,max_charge_kw:100*factor,max_discharge_kw:100*factor,initial_soc_pct:50},constraints:{demand_limit_kw:1000*factor,soc_min_pct:20,soc_max_pct:90}};
 const comparison={schema_version:'m4-daily-comparison-v1',station_id:station,status:'optimized',recommended_source:'optimized',usage:'preview_only',dispatch_status:'not_dispatched',date:day,start_at:new Date(start).toISOString(),end_at:new Date(start+86400000).toISOString(),controls_version:'controls/v1',baseline_policy_version:'ems-demand-soc-duration-v7-pv-export-idle',candidate_plan_version:'plan/v1',baseline_cost_yuan:baseline.metrics.energy_cost,optimized_cost_yuan:chosen.metrics.energy_cost,savings_yuan:baseline.metrics.energy_cost-chosen.metrics.energy_cost,baseline,recommended:chosen,reason:'本轮全天费用更低，计划未下发。',daily_policy_version:'m4-daily-peak-reserve-v1',terminal_energy_rule:'not_less_than_baseline'};
 const record={station_id:station,run_id:run,status:'completed',started_at:new Date(start).toISOString(),finished_at:new Date().toISOString(),plan_start_at:new Date(start).toISOString(),input_summary:{configuration_version:'config/v1'},daily_comparison:comparison,selected:{profile_id:'balanced',plan_version:'plan/v1'},candidates:[chosen]};
 const job={station_id:station,status:'completed',run_id:run,request,result:{schema_version:'m4-decision-results-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',record}};
 return {job,inputs:{station_id:station,schema_version:'m4-daily-inputs-v1',date:day,usage:'retrospective_comparison_only',dispatch_status:'not_dispatched',configuration_version:'config/v1',can_compare:true,sources:{pv:{version:'pv/v1'}},checks:[],comparison:null}};
}
module.exports={fixture,html,day};
if(require.main===module)(async()=>{const browser=await chromium.launch({headless:true,channel:'chrome'});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[],writes=[];let mode='ready',held=null;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://m4.test/**',async route=>{
  const url=new URL(route.request().url());requests.push(url.pathname);if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
  assert.equal(route.request().headers().authorization,'Bearer fixture-only-token');
  const station=url.pathname.includes('station-2')?'station-2':'station-1',f=fixture(station),endpoint=url.pathname.split('/').at(-1);
  if(route.request().method()!=='GET'){writes.push(endpoint);assert.equal(endpoint,'daily-plan');return route.fulfill({status:202,json:{station_id:station,status:'running',run_id:run}});}
  if(endpoint==='daily-inputs'){
   if(mode==='error')return route.fulfill({status:502,json:{detail:'真实输入暂不可用'}});
   if(mode==='hold'&&station==='station-1')await new Promise(r=>held=r);
   if(mode==='pvchanged')f.inputs.sources.pv.version='pv/v2';
   if(mode==='blocked'){f.inputs.can_compare=false;f.inputs.checks=[{status:'missing',detail:'负荷预测覆盖不足'}];}
   return route.fulfill({json:f.inputs});
  }
  if(endpoint==='settings')return route.fulfill({json:{station_id:station,version:mode==='config'?'changed':'config/v1'}});
  if(endpoint==='control-sources')return route.fulfill({json:{station_id:station,version:'controls/v1'}});
  if(endpoint==='daily-plan'){
   if(mode==='blocked')return route.fulfill({json:{station_id:station,status:'empty',result:null}});
   if(mode==='short')f.job.result.record.daily_comparison.recommended.plan.pop();
   if(mode==='old')f.job.result.record.daily_comparison.date='2020-01-01';
   if(mode==='identity')f.job.station_id='wrong';
   return route.fulfill({json:f.job});
  }
  if(endpoint==='decision-history')return route.fulfill({json:{station_id:station,schema_version:'m4-decision-history-v1',dispatch_status:'not_dispatched',items:[],next_offset:null}});
  throw Error('unexpected API '+url.pathname);
 });
 await page.goto('http://m4.test/m4?token=fixture-only-token');await page.waitForFunction(()=>!document.querySelector('#chart-panel').hidden);
 assert.equal(await page.locator('[role=tab]').count(),2);assert.equal(await page.locator('#chart path').count(),4);
 assert.equal(await page.locator('#pv-rate').isVisible(),true);assert.match(await page.locator('.legend').innerText(),/光伏预测/);
 assert.doesNotMatch(await page.locator('body').innerText(),/模拟计划|示例电价|账单统计|候选方案|设备状态/);
 assert.equal(await page.locator('#saving').innerText(),'¥ 84.00');
 await page.locator('#time').fill('76');await page.locator('#time').dispatchEvent('input');assert.match(await page.locator('#inspector').innerText(),/储能放电/);
 await page.locator('#simulate').click();await page.waitForFunction(()=>!document.querySelector('#simulate').disabled);assert.deepEqual(writes,['daily-plan']);
 await page.locator('#toast').waitFor({state:'hidden'});
 const screens='/tmp/m4-reference-live';fs.mkdirSync(screens,{recursive:true});
 for(const width of [1440,834,390,320])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:1000});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
  for(const tab of ['decision','records']){await page.locator('#tab-'+tab).click();await page.waitForLoadState('networkidle');if(tab==='records')await page.waitForFunction(()=>!document.querySelector('#record-summary').textContent.includes('正在读取'));assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${tab} ${width} ${theme}`);await page.screenshot({path:path.join(screens,`${tab}-${width}-${theme}.png`),fullPage:true,animations:'disabled'});}
 }
 assert.equal(await page.locator('#records tr').count(),2);await page.locator('#record-station').selectOption('station-1');assert.equal(await page.locator('#records tr').count(),1);assert.match(await page.locator('#records').innerText(),/84.00/);await page.locator('#record-search').fill('missing-record');assert.equal(await page.locator('#records tr').count(),0);await page.locator('#reset').click();assert.equal(await page.locator('#records tr').count(),2);await page.locator('[data-record]').first().click();assert.equal(await page.locator('#detail tbody tr').count(),96);await page.locator('#close-detail').click();
 await page.locator('#tab-decision').click();
 for(const state of ['error','blocked','short','old','identity','config','pvchanged']){
  mode=state;await page.locator('#station').selectOption('station-2');await page.waitForLoadState('networkidle');assert.equal(await page.locator('#chart-panel').isVisible(),false,state);assert.equal(await page.locator('#metrics').isVisible(),false,state);
  mode='ready';await page.locator('#station').selectOption('station-1');await page.waitForFunction(()=>!document.querySelector('#chart-panel').hidden&&!document.querySelector('#simulate').disabled);
 }
 mode='hold';await page.locator('#station').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('.decision .badge').textContent.includes('缓存'));await page.locator('#station').selectOption('station-2');await page.waitForFunction(()=>document.querySelector('#decision-copy').textContent.includes('电站 2'));while(!held)await new Promise(r=>setTimeout(r,20));held();await page.waitForLoadState('networkidle');assert.match(await page.locator('#decision-copy').innerText(),/电站 2/);
 assert.match(await page.locator('#chart-context').innerText(),/光伏预测缺口 24 个时段按 0 kW 估算/);await page.screenshot({path:'/tmp/m4-pv-zero-gap.png',fullPage:true});
 mode='ready';await page.locator('#plan-date').fill('2020-01-01');await page.locator('#plan-date').dispatchEvent('change');assert.equal(await page.locator('#simulate').isDisabled(),true);assert.equal(await page.locator('#chart-panel').isVisible(),false);
 assert.deepEqual(errors,[]);assert(requests.every(r=>r==='/m4'||r.startsWith('/m4-api/')));
 console.log('PASS: screenshot-only layout, real API contracts, net savings/cycle cost, chart/slider, 96-row detail, single POST, same-origin auth, missing/invalid/expired/config guards, late-station response, 16 responsive/theme renders. Mock transport only.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
