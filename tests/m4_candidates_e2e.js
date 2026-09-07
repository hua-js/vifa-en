'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {once}=require('node:events');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const parameters={energy_capacity_kwh:500,max_charge_kw:80,max_discharge_kw:90,charge_efficiency:.94,discharge_efficiency:.93,soc_min_pct:15,soc_max_pct:85,preferred_soc_min_pct:25,preferred_soc_max_pct:75,terminal_soc_tolerance_pct:4,grid_import_limit_kw:500,cycle_cost_per_kwh:.02,max_input_age_seconds:1800};
function inputs(id,version='v1'){
 const now=Date.now(),start=Math.ceil(now/900000)*900000,blocked=id==='station-2';
 return {station_id:id,configuration_version:version,status:blocked?'blocked':'ready',fetched_at:new Date(now).toISOString(),plan_start_at:new Date(start).toISOString(),plan_end_at:new Date(start+86400000).toISOString(),issues:blocked?['明日负荷预测不完整']:[],warnings:[],sources:{
  load:{status:blocked?'incomplete':'ready',values:Array(96).fill(321),coverage_points:blocked?12:96},pv:{status:'ready',values:Array(96).fill(0),method:'station_without_pv'},tariff:{status:'ready',values:Array(96).fill(.6)},controls:{station_id:id,source_station_id:blocked?'ES02':'ES01',status:'ready',version:'controls-v1',demand:{need_kw:504,reserved_kw:5,rated_capacity:630},reverse_flow:{re_kw:40,enabled:false,effective_limit_kw:null},schedule:[],issues:[],warnings:[],fetched_at:new Date(now).toISOString()},
  realtime:{status:'ready',available:true,energy_capacity_kwh:500,initial_soc_pct:50,full_station_soc_pct:50,participation_status:'full',participating_cabinet_ids:['emu11','emu12'],excluded_cabinet_ids:[],available_energy_capacity_kwh:500,available_max_charge_kw:80,available_max_discharge_kw:90,observed_at:new Date(now).toISOString(),sampled_grid_power_kw:321,sampled_pv_power_kw:0,observed_station_soc_pct:50,cabinets:['emu11','emu12'].map(emu_sn=>({emu_sn,capacity_kwh:250,soc_pct:50,observed_at:new Date(now).toISOString(),status:'wait',available:true,issues:[]}))}
 },points:[]};
}
function envelope(id,version='v1'){
 const bundle=inputs(id,version),start=Date.parse(bundle.plan_start_at),now=new Date().toISOString(),request_id=`actual-${id}-${version}`,source_versions={capability:'real-capability',constraints:'real-constraints',load_forecast:'real-load',pv_forecast:'real-pv',tariff:'real-tariff'};
 const summary={...parameters,horizon_points:96,initial_soc_pct:50,plan_start_at:bundle.plan_start_at,input_observed_at:now,demand_limit_kw:504,source_versions};
 const request={station_id:id,request_id,plan_start_at:bundle.plan_start_at,input_observed_at:now,source_versions,capability:{...parameters,initial_soc_pct:50},constraints:parameters,points:Array.from({length:96},(_,i)=>({timestamp:new Date(start+i*900000).toISOString(),load_forecast_kw:321,pv_forecast_kw:0,buy_price_per_kwh:.6,sell_price_per_kwh:0}))};
 const candidates=['balanced','cost','pv'].map((profile_id,index)=>({profile_id,profile_version:`debug-${profile_id}`,plan_version:`${request_id}/${profile_id}`,status:'optimal',solve_seconds:.01,risk_messages:[],metrics:{import_cost:100+index,energy_cost:100+index,export_revenue:0,cycle_cost:1,max_grid_import_kw:321,pv_self_use_kwh:0,pv_self_use_rate:0,pv_unabsorbed_energy_kwh:0,throughput_energy_kwh:100,peak_demand_exceed_kw:0},plan:Array.from({length:96},(_,i)=>({timestamp:request.points[i].timestamp,mode:i%2?'discharge':'charge',target_power_kw:10+index,expected_soc_pct:51+index,grid_import_kw:321,grid_export_kw:0,pv_unabsorbed_kw:0,demand_exceed_kw:0}))}));
 const result={schema_version:'m4-orchestration-v1',run_id:`run-${id}`,started_at:now,finished_at:now,model_version:'real-optimizer',orchestrator_version:'b1',overall_status:'completed',errors:[],stations:[{station_id:id,input_ref:'live-input',request_id,status:'optimized',input_summary:summary,optimization_result:{station_id:id,request_id,candidates},error:null,selection_status:'pending_ai',selected_candidate_id:null,dispatch_status:'not_dispatched',ems_task_id:null}]};
 return {schema_version:'m4-live-candidates-v1',station_id:id,configuration_version:version,generated_at:now,expires_at:bundle.plan_start_at,usage:'preview_only',profile_metadata:{usage:'candidate_debug',production_approved:false,configuration_version:'debug-v1',description:'仅用于候选调试的目标配置说明',normalization:{cost_reference_cny:1000,pv_reference_kwh:1000,cost_weight:1,pv_weight:1},tolerances:{absolute:1e-6,relative:0}},inputs:bundle,request,result,stale:false,stale_reason:''};
}
(async()=>{
 let browser,held=null,onHeld,mode='normal',version='v1',recent=null;const errors=[],requests=[];
 const server=http.createServer(async(req,res)=>{
  let raw='';for await(const chunk of req)raw+=chunk;requests.push({method:req.method,url:req.url,body:raw});res.setHeader('Cache-Control','no-store');
  if(req.url==='/m4'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(fs.readFileSync(path.join(root,'m4/M4优化调度控制台-线上版.html')));}
  res.setHeader('Content-Type','application/json');const id=req.url.includes('station-2')?'station-2':'station-1',send=(body,status=200)=>{res.statusCode=status;res.end(JSON.stringify(body));};
  if(req.url.endsWith('/settings'))return send({station_id:id,revision:version==='v1'?1:2,version,updated_at:new Date().toISOString(),capability_source:'manual_limits',parameters});
  if(req.url.endsWith('/control-sources'))return mode==='control-error'?send({detail:'控制配置读取失败'},502):send(inputs(id,version).sources.controls);
  if(req.url.endsWith('/inputs')){const bundle=inputs(id,version);if(mode==='partial-inputs'&&id==='station-1')Object.assign(bundle.sources.realtime,{initial_soc_pct:60,participation_status:'partial',participating_cabinet_ids:['emu12'],excluded_cabinet_ids:['emu11'],available_energy_capacity_kwh:250,available_max_charge_kw:40,available_max_discharge_kw:45});return mode==='input-error'?send({detail:'真实输入读取失败'},502):send(bundle);}
  if(req.url.endsWith('/candidates')){
   if(req.method==='GET')return send(id==='station-1'?recent:null);
   if(mode==='busy')return send({detail:{message:'本站已有候选计算进行中'}},429);
   const body=envelope(id,version);if(mode==='wrong-station')body.station_id='station-2';
   if(mode==='no-candidate'){body.result.overall_status='failed';const station=body.result.stations[0];station.status='no_usable_candidate';station.error={code:'NO_USABLE_CANDIDATE',message:'三套候选均求解超时'};station.optimization_result.candidates.forEach(c=>Object.assign(c,{status:'timeout',plan:[],metrics:null}));}
   if(mode==='hold'){held=()=>send(body);onHeld();return;}return send(body);
  }
  send({},404);
 });
 try{
  server.listen(0,'127.0.0.1');await once(server,'listening');browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1100}});page.on('pageerror',e=>errors.push(e.message));
  const url=`http://127.0.0.1:${server.address().port}/m4`,button=page.locator('#start-live-candidates'),status=page.locator('#live-candidate-status');
  const ready=()=>page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='ready');
  const refresh=async()=>{const response=page.waitForResponse(r=>r.url().endsWith('/station-1/inputs'));await page.locator('#refresh-live-inputs').click();await(await response).finished();await ready();};
  const start=async()=>{await button.click();await page.waitForFunction(()=>document.querySelector('#live-candidate-status').dataset.state!=='loading');};
  await page.goto(url);await page.waitForLoadState('networkidle');await ready();assert.equal(await button.isEnabled(),true);assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  mode='hold';const holdReady=new Promise(resolve=>onHeld=resolve);await button.click();await holdReady;assert.equal(await button.isDisabled(),true);assert.match(await status.innerText(),/计算/);
  await page.selectOption('#station-select','s2');assert.equal(await button.isDisabled(),true);assert.equal(await page.locator('#plan-chart [data-point]').count(),0);mode='normal';held();held=null;await page.waitForLoadState('networkidle');await page.selectOption('#station-select','s1');
  await page.waitForFunction(()=>document.querySelectorAll('#plan-chart [data-point]').length===96);
  assert.deepEqual(JSON.parse(requests.find(r=>r.method==='POST').body),{configuration_version:'v1'});
  assert.match(await status.innerText(),/真实.*候选/);assert.match(await page.locator('#decision-confidence').innerText(),/AI.*待接入/);assert.equal(await page.locator('#actual-power-trace').count(),0);
  assert.match(await page.locator('#execution-stats').innerText(),/候选预览/);assert.doesNotMatch(await page.locator('#execution-stats').innerText(),/未生成/);
  await page.locator('#candidate-comparison').evaluate(e=>e.open=true);
  for(const [profile,power,soc] of [['balanced','10','51%'],['cost','11','52%'],['pv','12','53%']]){await page.locator(`[data-candidate="${profile}"]`).click();const values=await page.locator('#point-readout b').allTextContents();assert.deepEqual(values,[power,soc]);assert.equal(await page.locator('#plan-chart [data-point]').count(),96);}
  assert.match(await page.locator('#metrics').innerText(),/321/);assert.doesNotMatch(await page.locator('#metrics').innerText(),/离线|Mock/);
  assert.match(await page.locator('#candidate-grid').innerText(),/不适用/);assert.match(await page.locator('#flow-caption').innerText(),/光伏.*0/);assert.match(await page.locator('#flow-breakdown').innerText(),/不适用/);
  await page.locator('#open-plan').click();assert.match(await page.locator('#drawer-body').innerText(),/actual-station-1-v1/);assert.doesNotMatch(await page.locator('#drawer-kicker').innerText(),/离线|Mock/);assert.match(await page.locator('#drawer-body').innerText(),/仅用于候选调试的目标配置说明/);assert.match(await page.locator('#drawer-body').innerText(),/容差/);await page.keyboard.press('Escape');
  if(process.env.M4_CANDIDATE_SCREENSHOT_DIR){fs.mkdirSync(process.env.M4_CANDIDATE_SCREENSHOT_DIR,{recursive:true});for(const width of [1440,768,390,320]){await page.setViewportSize({width,height:1100});for(const theme of ['light','dark']){await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),true);await page.screenshot({path:path.join(process.env.M4_CANDIDATE_SCREENSHOT_DIR,`${width}-${theme}.png`),fullPage:true,animations:'disabled'});}}await page.setViewportSize({width:1440,height:1100});}
  await refresh();assert.equal(await page.locator('#plan-chart [data-point]').count(),0);mode='busy';await start();assert.match(await status.innerText(),/已有候选计算/);assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  mode='hold';const again=new Promise(resolve=>onHeld=resolve);await button.click();await again;mode='normal';await refresh();held();held=null;await page.waitForLoadState('networkidle');assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  mode='hold';const controlHold=new Promise(resolve=>onHeld=resolve);await button.click();await controlHold;mode='control-error';await page.locator('#open-settings').click();await page.locator('#reload-settings').click();await page.waitForFunction(()=>document.querySelector('#control-sources').dataset.state==='error');await page.locator('#close-settings').click();held();held=null;await page.waitForLoadState('networkidle');assert.equal(await page.locator('#plan-chart [data-point]').count(),0);assert.equal(await button.isDisabled(),true);mode='normal';await refresh();
  await start();assert.equal(await page.locator('#plan-chart [data-point]').count(),96);mode='input-error';await page.locator('#refresh-live-inputs').click();await page.waitForFunction(()=>document.querySelector('#live-input-status').dataset.state==='error');assert.equal(await page.locator('#plan-chart [data-point]').count(),0);assert.equal(await button.isDisabled(),true);
  mode='normal';await refresh();mode='hold';const last=new Promise(resolve=>onHeld=resolve);await button.click();await last;version='v2';await page.locator('#open-settings').click();await page.locator('#reload-settings').click();await page.waitForFunction(()=>document.querySelector('#settings-version').textContent.includes('v2'));await page.locator('#close-settings').click();mode='normal';held();held=null;await page.waitForLoadState('networkidle');assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  recent=envelope('station-1',version);mode='partial-inputs';await page.reload();await page.waitForLoadState('networkidle');assert.equal(await status.getAttribute('data-state'),'stale');assert.match(await status.innerText(),/历史.*输入.*变化/);assert.equal(await page.locator('#plan-chart [data-point]').count(),96);
  mode='normal';recent=envelope('station-1',version);recent.stale=true;recent.stale_reason='候选已过期';recent.expires_at=new Date(Date.now()-1000).toISOString();await page.reload();await page.waitForLoadState('networkidle');assert.match(await status.innerText(),/历史|过期/);assert.equal(await page.locator('#plan-chart [data-point]').count(),96);
  mode='wrong-station';await start();assert.equal(await page.locator('#plan-chart [data-point]').count(),0);assert.match(await status.innerText(),/失败|不匹配|无效/);
  mode='no-candidate';await start();assert.equal(await page.locator('#plan-chart [data-point]').count(),0);assert.equal(await status.getAttribute('data-state'),'error');assert.match(await status.innerText(),/没有可用候选/);assert.match(await page.locator('#execution-stats').innerText(),/无可用候选/);
  assert.deepEqual(errors,[]);assert.equal(requests.filter(r=>r.method==='POST').every(r=>r.url==='/m4-api/stations/station-1/candidates'),true);
  console.log('PASS: real candidate POST, independent stations, 3 candidates/96 points, preview-only AI/EMS state, late response invalidation, input failure clearing, stale recovery and malformed result rejection');
 }finally{if(held)held();if(browser)await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
