'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {once}=require('node:events');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'m4/web/M4优化调度控制台-线上版.html'),'utf8');
const token='m4-browser-test-token',accessMessage='访问凭据无效或已过期，请从平台重新打开页面';
const parameters={energy_capacity_kwh:500,max_charge_kw:80,max_discharge_kw:90,charge_efficiency:.94,discharge_efficiency:.93,soc_min_pct:15,soc_max_pct:85,preferred_soc_min_pct:25,preferred_soc_max_pct:75,terminal_soc_tolerance_pct:4,grid_import_limit_kw:200,cycle_cost_per_kwh:.02,max_input_age_seconds:1800};
const historyId='11111111-1111-4111-8111-111111111111';

(async()=>{
 let browser,deniedStatus=0,redirectHistory=false,job=null;
 const requests=[],externalRequests=[],errors=[],configs={};
 const outside=http.createServer((req,res)=>{externalRequests.push(req.url);res.end('unexpected redirect');});
 outside.listen(0,'127.0.0.1');await once(outside,'listening');
 const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');let body='';for await(const chunk of req)body+=chunk;
  requests.push({method:req.method,path:url.pathname,query:url.search,headers:req.headers,body});
  res.setHeader('Cache-Control','no-store');
  if(url.pathname==='/m4'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(html);}
  if(url.pathname==='/referrer-probe'){res.setHeader('Content-Type','image/svg+xml');return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>');}
  res.setHeader('Content-Type','application/json');
  const send=value=>res.end(JSON.stringify(value));
  if(deniedStatus){res.statusCode=deniedStatus;return send({detail:'upstream-internal-id-do-not-display'});}
  const station=url.pathname.includes('station-2')?'station-2':'station-1',now=new Date().toISOString();
  configs[station]??={station_id:station,revision:1,version:'settings-v1',updated_at:now,capability_source:'manual_limits',parameters:{...parameters}};
  if(url.pathname.endsWith('/settings')){
   if(req.method==='PUT'){const saved=JSON.parse(body);configs[station]={...configs[station],revision:2,version:'settings-v2',parameters:saved.parameters};}
   return send(configs[station]);
  }
  if(url.pathname.endsWith('/control-sources'))return send({station_id:station,source_station_id:station==='station-1'?'ES01':'ES02',status:'ready',version:'controls-v1',fetched_at:now,demand:{need_kw:504,reserved_kw:5,rated_capacity:630},reverse_flow:{re_kw:40,enabled:false,effective_limit_kw:null},schedule:[],source_health:{schedule:'ready'},issues:[],warnings:[]});
  if(url.pathname.endsWith('/inputs'))return send({station_id:station,configuration_version:configs[station].version,status:'blocked',fetched_at:now,issues:['Mock 输入未就绪'],warnings:[],sources:{},points:[]});
  if(url.pathname.endsWith('/selection-policy'))return send({station_id:station,revision:1,version:'policy-v1',policy:{station_id:station,version:'policy-v1',metric:'profile_priority',metric_tolerance:0,demand_peak_tolerance_kw:.000001,demand_energy_tolerance_kwh:.000001,tie_order:['balanced','cost','pv']}});
  if(url.pathname.endsWith('/decision-result'))return send({schema_version:'m4-decision-results-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',status:'empty',record:null});
  if(url.pathname.endsWith('/decision-runs')){
   if(req.method==='POST'){res.statusCode=202;job={run_id:'mock-job',station_id:station,status:'running',stage:'model_solver',started_at:now,finished_at:null,message:'正在生成测试计划'};}
   return send({station_id:station,usage:'preview_only',dispatch_status:'not_dispatched',job:job?.station_id===station?job:null});
  }
  if(url.pathname.endsWith('/decision-history')){
   if(redirectHistory){res.statusCode=302;res.setHeader('Location',`http://127.0.0.1:${outside.address().port}/outside-api`);return res.end();}
   return send({schema_version:'m4-decision-history-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',offset:0,next_offset:null,items:[{run_id:historyId,status:'blocked_configuration',finished_at:now,selected:null,metrics:null}]});
  }
  if(url.pathname.endsWith('/decision-results/'+historyId))return send({schema_version:'m4-decision-results-v1',station_id:station,usage:'historical_preview_only',dispatch_status:'not_dispatched',status:'available',record:{run_id:historyId,station_id:station,status:'blocked_configuration',finished_at:now,selected:null,reason:'参数需要完善',issues:[],candidates:[],comparison:[],plan_start_at:null}});
  res.statusCode=404;send({});
 });
 try{
  server.listen(0,'127.0.0.1');await once(server,'listening');const origin=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({headless:true});const context=await browser.newContext(),page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>assert.equal(new URL(request.url()).origin,origin,'application requests stay on the Mock origin'));
  await page.goto(origin+'/m4?token='+encodeURIComponent(token));await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('meta[name="referrer"]').getAttribute('content'),'no-referrer');
  await page.locator('#open-settings').click();await page.locator('[name="max_charge_kw"]').fill('88');await page.locator('#save-settings').click();
  await page.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='saved');await page.locator('#close-settings').click();
  await page.locator('#start-decision-run').click();await page.waitForFunction(()=>document.querySelector('#start-decision-run').textContent==='计划生成中…');
  await page.locator('#daily-tab-history').click();await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===1);
  await page.locator(`[data-history-run="${historyId}"]`).click();await page.waitForFunction(()=>!document.querySelector('#history-output').hidden);
  assert.match(await page.locator('#history-output').innerText(),/参数需要完善/);
  const apiRequests=requests.filter(request=>request.path.startsWith('/m4-api/'));
  assert(apiRequests.some(request=>request.method==='GET'&&request.path.endsWith('/inputs')));
  assert(apiRequests.some(request=>request.method==='GET'&&request.path.endsWith('/decision-history')));
  assert(apiRequests.some(request=>request.method==='GET'&&request.path.endsWith('/decision-results/'+historyId)));
  const saved=apiRequests.find(request=>request.method==='PUT'),started=apiRequests.find(request=>request.method==='POST');
  assert.equal(JSON.parse(saved.body).parameters.max_charge_kw,88);assert.equal(saved.headers['content-type'],'application/json');
  assert.match(JSON.parse(started.body).request_id,/^[0-9a-f-]{36}$/);assert.equal(started.headers['content-type'],'application/json');
  for(const request of apiRequests){assert.equal(request.headers.authorization,'Bearer '+token);assert.equal(request.headers.referer,undefined);assert.equal(request.query.includes('token'),false);assert.equal(request.body.includes(token),false);}
  await Promise.all([page.waitForResponse(response=>response.url().endsWith('/referrer-probe')),page.evaluate(()=>{const image=document.createElement('img');image.src='/referrer-probe';document.body.append(image);})]);
  const probe=requests.find(request=>request.path==='/referrer-probe');assert.equal(probe.headers.authorization,undefined);assert.equal(probe.headers.referer,undefined);
  assert.equal(await page.evaluate(()=>JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)]).includes(new URLSearchParams(location.search).get('token'))),false);
  assert.equal(new URL(page.url()).searchParams.get('token'),token);
  await page.reload();await page.waitForLoadState('networkidle');assert.equal(new URL(page.url()).searchParams.get('token'),token);
  assert.equal(requests.filter(request=>request.path.startsWith('/m4-api/')).at(-1).headers.authorization,'Bearer '+token);
  await page.locator('#daily-tab-history').click();await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===1);
  deniedStatus=401;await page.locator('#history-refresh').click();await page.waitForFunction(()=>document.querySelector('#history-status').dataset.error==='true');
  assert.match(await page.locator('#history-status').innerText(),new RegExp(accessMessage));assert.doesNotMatch(await page.locator('#history-status').innerText(),/upstream-internal-id/);
  deniedStatus=403;await page.locator('#open-settings').click();await page.locator('#save-settings').click();await page.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='error');
  assert.equal(await page.locator('#settings-status').innerText(),accessMessage);await page.locator('#close-settings').click();
  deniedStatus=0;redirectHistory=true;await page.locator('#history-refresh').click();await page.waitForFunction(()=>document.querySelector('#history-status').dataset.error==='true');
  assert.deepEqual(externalRequests,[],'authenticated requests do not follow redirects outside the API origin');
  await page.close();redirectHistory=false;
  const beforeLocal=requests.length,local=await context.newPage();local.on('pageerror',error=>errors.push(error.message));
  await local.goto(origin+'/m4');await local.waitForLoadState('networkidle');
  const localRequests=requests.slice(beforeLocal).filter(request=>request.path.startsWith('/m4-api/'));assert(localRequests.length>0);
  assert(localRequests.every(request=>request.headers.authorization===undefined),'token is not inherited by a URL without a token');
  assert.deepEqual(errors,[]);await context.close();
  console.log('PASS: iframe Bearer auth for GET/PUT/POST/history, preserved JSON bodies, no token in API URLs/Referer/storage, refresh persistence, 401/403 customer messages, redirect isolation and token-free local use; Mock only.');
 }finally{if(browser)await browser.close();server.close();outside.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
