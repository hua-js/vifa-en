'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {once}=require('node:events');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..');
const parameters={energy_capacity_kwh:500,max_charge_kw:80,max_discharge_kw:90,charge_efficiency:.94,discharge_efficiency:.93,soc_min_pct:15,soc_max_pct:85,preferred_soc_min_pct:25,preferred_soc_max_pct:75,terminal_soc_tolerance_pct:4,grid_import_limit_kw:200,cycle_cost_per_kwh:.02,max_input_age_seconds:1800};
function controls(id,version='controls-1'){return {station_id:id,source_station_id:id==='station-1'?'ES01':'ES02',status:'ready',version,fetched_at:new Date().toISOString(),demand:{need_kw:version==='controls-3'?888:version==='controls-2'?777:504,reserved_kw:5,rated_capacity:630},reverse_flow:{re_kw:40,enabled:false,effective_limit_kw:null},schedule:[],source_health:{schedule:'ready'},issues:[],warnings:[]};}
function bundle(id,controlVersion='controls-1',partial=false){
 const second=id==='station-2',now=Date.now(),start=Math.ceil(now/900000)*900000;
 return {station_id:id,configuration_version:'v1',status:second?'blocked':'ready',fetched_at:new Date(now).toISOString(),plan_start_at:new Date(start).toISOString(),plan_end_at:new Date(start+86400000).toISOString(),issues:second?['本站无可参与柜']:[],warnings:partial?['emu11：告警标志未通过参与校验']:[],sources:{
  load:{status:'ready',values:Array(96).fill(second?222:111),coverage_points:96,source_kind:'rolling_with_manual',rolling_points:95,manual_points:1,rolling_generated_at:new Date(now-60000).toISOString()},pv:{status:'ready',values:Array(96).fill(second?20:0),method:second?'seven_day_same_slot_median':'station_without_pv'},tariff:{status:'ready',values:Array(96).fill(.6)},controls:controls(id,controlVersion),
  realtime:{status:second?'blocked':'ready',available:!second,energy_capacity_kwh:500,initial_soc_pct:second?null:partial?60:50,full_station_soc_pct:partial?40:50,participation_status:second?'none':partial?'partial':'full',participating_cabinet_ids:second?[]:partial?['emu12']:['emu11','emu12'],excluded_cabinet_ids:second?['emu21','emu22','emu23','emu24','emu25','emu26']:partial?['emu11']:[],available_energy_capacity_kwh:second?0:partial?250:500,available_max_charge_kw:second?0:partial?40:80,available_max_discharge_kw:second?0:partial?45:90,observed_at:new Date(now).toISOString(),sampled_grid_power_kw:123,sampled_pv_power_kw:second?25:0,observed_station_soc_pct:second?60:50,cabinets:Array.from({length:second?6:2},(_,i)=>({emu_sn:`emu${second?'2':'1'}${i+1}`,capacity_kwh:500/(second?6:2),soc_pct:second?60:partial?(i===0?20:60):50,observed_at:new Date(now).toISOString(),status:partial&&i===0?'fault':'wait',alert_status:second||partial&&i===0?'alert':null,available:!second&&(!partial||i===1),issues:second||partial&&i===0?['告警标志未通过参与校验']:[]}))}
 },points:[]};
}
(async()=>{
 let browser,mode='normal',heldInput=null,onHeldInput;const requests=[],errors=[],controlVersions={'station-1':'controls-1','station-2':'controls-1'};
 const server=http.createServer((req,res)=>{
  requests.push(req.method+' '+req.url);res.setHeader('Cache-Control','no-store');
  if(req.url==='/m4'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(fs.readFileSync(path.join(root,'m4/web/M4优化调度控制台-线上版.html')));}
  const id=req.url.includes('station-2')?'station-2':'station-1';res.setHeader('Content-Type','application/json');
  if(req.url.endsWith('/selection-policy'))return res.end(JSON.stringify({station_id:id,revision:1,version:'policy-v1',policy:{station_id:id,version:'policy-v1',metric:'profile_priority',metric_tolerance:0,demand_peak_tolerance_kw:.000001,demand_energy_tolerance_kwh:.000001,tie_order:['balanced','cost','pv']}}));
  if(req.url.endsWith('/decision-result'))return res.end(JSON.stringify({schema_version:'m4-decision-results-v1',station_id:id,usage:'historical_preview_only',dispatch_status:'not_dispatched',status:'empty',record:null}));
  if(req.url.endsWith('/decision-runs'))return res.end(JSON.stringify({station_id:id,usage:'preview_only',dispatch_status:'not_dispatched',job:null}));
  if(req.url.endsWith('/settings'))return res.end(JSON.stringify({station_id:id,revision:1,version:'v1',updated_at:new Date().toISOString(),capability_source:'manual_limits',parameters}));
  if(req.url.endsWith('/control-sources')){if(mode==='control-error'&&id==='station-1'){res.statusCode=502;return res.end(JSON.stringify({detail:'控制配置接口读取失败'}));}return res.end(JSON.stringify(controls(id,controlVersions[id])));}
  if(req.url.endsWith('/inputs')){if(mode==='error'&&id==='station-1'){res.statusCode=502;return res.end(JSON.stringify({detail:'真实输入读取失败'}));}const input=bundle(id,controlVersions[id],['partial','missing-alert','unknown-alert'].includes(mode)&&id==='station-1');if(mode==='missing-alert')delete input.sources.realtime.cabinets[0].alert_status;if(mode==='unknown-alert'){input.sources.realtime.cabinets[0].alert_status=null;input.sources.realtime.cabinets[0].issues=['alert_status 存在活动告警或未知告警状态'];}if(mode==='advisory'&&id==='station-1'){input.sources.realtime.alarm_policy='emu11-alert-advisory-v1';input.sources.realtime.cabinets[0].alert_status='alert';input.sources.realtime.cabinets[0].alert_validation='advisory';input.sources.realtime.cabinets[0].advisories=['alert_status 存在活动告警或未知告警状态；仅提示，不影响输入准入'];input.warnings=['emu11：告警仅提示，不影响输入准入'];}if(mode==='short'){input.horizon_points=95;input.plan_end_at=new Date(Date.parse(input.plan_start_at)+95*900000).toISOString();for(const key of ['load','pv','tariff']){input.sources[key].values=input.sources[key].values.slice(0,95);input.sources[key].coverage_points=95;}input.sources.load.source_kind='rolling';input.sources.load.manual_points=0;}const body=JSON.stringify(input);if(mode==='hold'&&id==='station-1'){heldInput=()=>res.end(body);onHeldInput();return;}return res.end(body);}
  res.statusCode=404;res.end('{}');
 });
 try{
  server.listen(0,'127.0.0.1');await once(server,'listening');browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/m4`);await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'ready');
  assert.match(await page.locator('#metrics').innerText(),/111/);
  assert.match(await page.locator('#live-soc-method').textContent(),/等分后加权/);
  await page.locator('#daily-inspect-inputs').click();
  assert.equal(await page.locator('#daily-tab-devices').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#daily-panel-devices').isVisible(),true);
  assert.equal(await page.locator('.live-cabinet-details').getAttribute('open'),'');
  assert.equal(await page.locator('#live-cabinet-rows').isVisible(),true);
  assert.equal(await page.locator('#live-cabinet-rows tr').count(),2);
  assert.match(await page.locator('#live-source-grid').innerText(),/滚动 95 点.*手动补充 1 点/);
  assert.match(await page.locator('#live-source-grid').innerText(),/滚动更新/);
  assert.match(await page.locator('#live-cabinet-rows').innerText(),/250 kWh/);
  await page.selectOption('#station-select','s2');assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'blocked');
  assert.equal(await page.locator('#daily-tab-devices').getAttribute('aria-selected'),'true');
  assert.match(await page.locator('#live-input-issues').innerText(),/无可参与柜/);assert.equal(await page.locator('#live-cabinet-rows tr').count(),6);
  assert.doesNotMatch(await page.locator('#live-cabinet-rows').innerText(),/emu11/);
  assert.match(await page.locator('#metrics').innerText(),/222/);
  await page.selectOption('#station-select','s1');
  mode='short';await page.locator('#refresh-live-inputs').click();await page.waitForFunction(()=>document.querySelector('#live-input-window').textContent.includes('95 点'));assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'ready');assert.match(await page.locator('#live-source-grid').innerText(),/95 \/ 95/);
  mode='partial';const [partialResponse]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/station-1/inputs')),page.locator('#refresh-live-inputs').click()]);await partialResponse.finished();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  assert.match(await page.locator('#live-input-status').innerText(),/部分柜/);
  assert.equal(await page.locator('#operating-strip').getAttribute('data-tone'),'warn');
  assert.match(await page.locator('#live-capability-summary').innerText(),/250 kWh/);
  assert.match(await page.locator('#live-capability-summary').innerText(),/40.*45/);
  assert.match(await page.locator('#live-soc-method').textContent(),/参与柜.*60/);
  assert.match(await page.locator('#live-soc-method').textContent(),/全站.*40/);
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/排除/);
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/告警上报：alert/);
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/采样有效/);
  assert.match(await page.locator('#live-cabinet-rows tr').nth(1).innerText(),/未上报告警/);
  assert.match(await page.locator('#live-cabinet-scope').innerText(),/不代表.*历史计划/);
  assert.match(await page.locator('#live-cabinet-rows tr').nth(1).innerText(),/参与/);
  assert.match(await page.locator('#metrics').innerText(),/储能电量 SOC/);
  for(const width of [1440,768,390,320]){await page.setViewportSize({width,height:1100});for(const theme of ['light','dark']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),true);if(process.env.M4_LIVE_SCREENSHOT_DIR){fs.mkdirSync(process.env.M4_LIVE_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.M4_LIVE_SCREENSHOT_DIR,`partial-${width}-${theme}.png`),fullPage:true,animations:'disabled'});await page.locator('#live-input-panel').screenshot({path:path.join(process.env.M4_LIVE_SCREENSHOT_DIR,`cabinet-${width}-${theme}.png`),animations:'disabled'});}}}
  mode='advisory';await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/告警上报：alert/);
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/仅提示，不影响输入准入/);
  assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/可参与新计划/);
  assert.match(await page.locator('#live-input-status').innerText(),/全部柜参与/);
  assert.doesNotMatch(await page.locator('#live-input-status').innerText(),/已排除异常柜/);
  if(process.env.M4_LIVE_SCREENSHOT_DIR){await page.setViewportSize({width:1440,height:1100});await page.locator('#live-input-panel').screenshot({path:path.join(process.env.M4_LIVE_SCREENSHOT_DIR,'emu11-advisory.png'),animations:'disabled'});}
  for(const scenario of ['missing-alert','unknown-alert']){mode=scenario;await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');assert.match(await page.locator('#live-cabinet-rows tr').first().innerText(),/告警未确认/);assert.doesNotMatch(await page.locator('#live-cabinet-rows tr').first().innerText(),/未上报告警/);}
  await page.setViewportSize({width:1440,height:1000});mode='normal';await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  await page.locator('#open-settings').click();controlVersions['station-1']='controls-2';
  await page.locator('#reload-settings').click();
  await page.waitForFunction(()=>document.querySelector('#control-sources').textContent.includes('777 kW'));
  await page.waitForLoadState('networkidle');await page.locator('#close-settings').click();
  assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'blocked');
  assert.match(await page.locator('#live-input-status').innerText(),/控制配置版本已变化/);
  assert.doesNotMatch(await page.locator('#metrics').innerText(),/111/);
  assert.equal(await page.locator('#demand-limit').textContent(),'777');
  await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  mode='hold';const held=new Promise(resolve=>{onHeldInput=resolve;});
  await page.locator('#refresh-live-inputs').click();await held;
  await page.locator('#open-settings').click();controlVersions['station-1']='controls-3';
  await page.locator('#reload-settings').click();
  await page.waitForFunction(()=>document.querySelector('#control-sources').textContent.includes('888 kW'));
  await page.locator('#close-settings').click();mode='normal';heldInput();heldInput=null;
  await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state!=='loading');
  assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'blocked');
  assert.match(await page.locator('#live-input-status').innerText(),/控制配置版本已变化/);
  assert.equal(await page.locator('#demand-limit').textContent(),'888');
  await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  mode='control-error';await page.locator('#open-settings').click();await page.locator('#reload-settings').click();
  await page.waitForFunction(()=>document.querySelector('#control-sources').dataset.state==='error');
  await page.locator('#close-settings').click();
  assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'blocked');
  assert.match(await page.locator('#live-input-status').innerText(),/控制配置读取失败/);
  assert.equal(await page.locator('#demand-limit').textContent(),'—');
  mode='normal';await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  mode='error';await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='error');
  assert.doesNotMatch(await page.locator('#metrics').innerText(),/111/);assert.equal(await page.locator('#live-cabinet-rows tr').count(),0);
  assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  mode='normal';await page.locator('#refresh-live-inputs').click();await page.waitForLoadState('networkidle');await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  await page.evaluate(()=>{const original=Date.now;Date.now=()=>original()+86400000;});
  await page.selectOption('#station-select','s2');await page.selectOption('#station-select','s1');
  assert.equal(await page.locator('#live-input-status').getAttribute('data-state'),'blocked');assert.match(await page.locator('#live-input-status').innerText(),/过期/);
  assert.match(await page.locator('#live-cabinet-rows').innerText(),/快照过期/);assert.match(await page.locator('#live-cabinet-rows').innerText(),/采样已过期/);
  assert.doesNotMatch(await page.locator('#metrics').innerText(),/123/);
  await page.selectOption('#station-select','all');assert.equal(await page.locator('#live-input-panel').isVisible(),false);
  assert.equal(await page.locator('#daily-panel-devices').isVisible(),false);
  await page.locator('[data-station-summary="s1"] button').click();
  assert.equal(await page.locator('#daily-tab-overview').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#daily-panel-devices').isVisible(),false);
  assert.deepEqual(errors,[]);assert.equal(requests.every(r=>r.startsWith('GET ')),true);
  console.log('PASS: partial cabinet isolation, scaled capability, participating SOC, live input readiness, station isolation, weighted SOC display, changed control version blocking, concurrent control refresh preservation, refresh failure clearing, expired snapshot blocking, no simulated candidates and read-only requests');
 }finally{if(heldInput)heldInput();if(browser)await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
