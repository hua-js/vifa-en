'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {createHash}=require('node:crypto'),{webkit}=require('playwright');
const {snapshot}=require('./m4_loading_test_fixture.cjs');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'m4/web/M4优化调度控制台-线上版.html'),'utf8');
const output=path.resolve(process.env.M4_AI_OUTPUT_DIR||path.join(root,'outputs/m4/releases/2026-10-22-ai-comparison'));
fs.mkdirSync(output,{recursive:true});
const date='2026-10-22',stamp=date+'T19:00:00+08:00';
const project={schema_version:1,project_id:'fixture',configuration_version:'ai-comparison-fixture',stations:[1,2].map(n=>({id:'station-'+n,source_code:'ES0'+n,name:'电站 '+n,has_pv:false,policy:'cost_first',grid_import_limit_kw:720}))};
function fixture(state,station='station-2'){
 const job=snapshot(station,date),request=job.request,c=job.result.record.daily_comparison,record=job.result.record;
 request.capability.energy_capacity_kwh=2000;
 request.points.forEach((v,i)=>{v.tariff_period=i<24?'gu':i>=64&&i<88?'feng':'ping';v.buy_price_per_kwh=i<24?.3:i>=64&&i<88?1.2:.7;v.sell_price_per_kwh=.2;});
 const metrics=points=>({energy_cost:points.reduce((sum,p,i)=>sum+p.grid_import_kw*request.points[i].buy_price_per_kwh*.25,0),cycle_cost:points.reduce((sum,p)=>sum+p.target_power_kw*.25*.03,0)});
 c.baseline.metrics=metrics(c.baseline.plan);c.baseline_cost_yuan=c.baseline.metrics.energy_cost;
 const power=state==='rejected'||state==='unverified-candidate'?20:80;let soc=50;
 const plan=c.baseline.plan.map((v,i)=>{const kw=i<16?-power:i>=72&&i<88?power:0;soc-=kw*.25/request.capability.energy_capacity_kwh*100;return {...v,mode:kw<0?'charge':kw>0?'discharge':'idle',target_power_kw:Math.abs(kw),expected_soc_pct:soc,grid_import_kw:100-kw};});
 const candidate={profile_id:'balanced',plan_version:'ai-plan/v1',status:'optimal',plan,metrics:metrics(plan)};
 const saving=c.baseline.metrics.energy_cost-candidate.metrics.energy_cost,net=saving-candidate.metrics.cycle_cost;
 Object.assign(c,{status:'optimized',recommended_source:'optimized',recommended:candidate,candidate_plan_version:candidate.plan_version,optimized_cost_yuan:candidate.metrics.energy_cost,savings_yuan:saving,net_savings_yuan:net,revenue_gate_version:'daily-net-savings-100-v1',optimization_status:'adopted',reason:'优化方案满足安全及收益条件。'});
 record.selected={profile_id:candidate.profile_id,plan_version:candidate.plan_version};record.candidates=[candidate];
 if(state==='rejected'||state==='unverified-candidate')Object.assign(c,{status:'ems',recommended_source:'ems',recommended:c.baseline,optimization_status:'not_adopted',optimization_reason:'预计净节省未达到采用条件。',reason:'沿用 EMS 原规则。'});
 if(state==='unverified-candidate')c.net_savings_yuan+=50;
 const inputs={station_id:station,schema_version:'m4-daily-inputs-v1',date,usage:'retrospective_comparison_only',dispatch_status:'not_dispatched',configuration_version:'saved-config',can_compare:false,sources:{},checks:[{status:'missing',detail:'预测数据尚未就绪'}]};
 if(state==='pending'){
  Object.assign(inputs,{can_compare:true,request:structuredClone(request),baseline:structuredClone(job.baseline),comparison:structuredClone({...c,status:'ems',recommended_source:'ems',recommended:c.baseline})});
  job.status='empty';job.result=null;
 }
 if(state==='missing'){job.status='empty';job.result=null;}
 if(state==='invalid')c.recommended.plan=c.recommended.plan.slice(1);
 job.automation={enabled:true,next_check_at:stamp};
 return {job,inputs,expected:{base:c.baseline.metrics.energy_cost,cost:candidate.metrics.energy_cost+candidate.metrics.cycle_cost,net}};
}
const money=v=>'¥ '+v.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
(async()=>{
 const browser=await webkit.launch({headless:true}),results=[];
 try{
  for(const state of ['adopted','rejected','unverified-candidate','pending','missing','invalid']){
   const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[],writes=[];
   await page.clock.setFixedTime(new Date(stamp));page.on('pageerror',e=>errors.push(e.message));
   await page.route('https://m4.test/**',async route=>{
    const req=route.request(),url=new URL(req.url());if(req.method()!=='GET')writes.push({method:req.method(),path:url.pathname});
    assert.equal(req.method(),'GET');if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
    const endpoint=url.pathname.split('/').at(-1),station=url.pathname.split('/')[3],f=fixture(state,station);
    if(endpoint==='project')return route.fulfill({json:project});
    if(endpoint==='daily-plan')return route.fulfill({json:f.job});
    if(endpoint==='daily-inputs')return route.fulfill({json:f.inputs});
    if(endpoint==='settings')return route.fulfill({json:{station_id:station,version:'saved-config'}});
    if(endpoint==='control-sources')return route.fulfill({json:{station_id:station,version:'saved-controls'}});
    if(endpoint==='decision-history')return route.fulfill({json:{station_id:station,items:[]}});
    if(endpoint==='today-revenue')return route.fulfill({status:502,json:{detail:'实测收益暂不可用'}});
    if(endpoint==='storage-log')return route.fulfill({json:{schema_version:'m4-storage-log-v1',station_id:station,date,timezone:'Asia/Shanghai',items:[]}});
    throw Error('Unexpected read '+url.pathname);
   });
   await page.goto('https://m4.test/m4?station=station-2');await page.waitForLoadState('networkidle');
   if(state==='missing'||state==='invalid'){
    assert.equal(await page.locator('#ai-comparison-content').isVisible(),false,state);
    assert.equal(await page.locator('#ai-comparison-empty').isVisible(),true,state);
    assert.doesNotMatch(await page.locator('#ai-comparison').innerText(),/AI优化成功|¥ 0\.00/);
   }else{
    assert.equal(await page.locator('#ai-comparison-content').isVisible(),true,state+' '+await page.locator('#decision-copy').innerText());
    const expected=fixture(state).expected;
    assert.equal(await page.locator('#ai-baseline-cost').innerText(),money(expected.base));
    if(state==='pending'){
     assert.match(await page.locator('#ai-comparison-status').innerText(),/待生成/);
     assert.equal(await page.locator('#ai-net-saving').innerText(),'—');
    }else{
     assert.equal(await page.locator('#ai-comparison-status').innerText(),'AI优化成功');
     assert.match(await page.locator('#ai-adoption-status').innerText(),state==='adopted'?/^已采用$/:/未采用/);
     assert.equal(await page.locator('#ai-current-cost').innerText(),money(state==='unverified-candidate'?expected.base:expected.cost));
     assert.equal(await page.locator('#ai-net-saving').innerText(),money(state==='unverified-candidate'?0:expected.net));
     if(state==='unverified-candidate')assert.equal(await page.locator('#ai-chart-current-label').innerText(),'EMS 原方案');
     else{
      assert.equal(await page.locator('#ai-power-chart path').count(),2);
      await page.locator('#ai-time').fill('74');await page.locator('#ai-time').dispatchEvent('input');
      assert.equal(await page.locator('#time').inputValue(),'74');assert.match(await page.locator('#ai-plan-reading').innerText(),/放电/);
      await page.locator('[data-ai-slot]').first().click();assert.equal(await page.locator('#ai-time').inputValue(),'0');
      await page.locator('.ai-difference-details summary').click();assert.ok(await page.locator('#ai-difference-rows tr').count()>0);
      await page.locator('.ai-difference-details summary').click();
      if(state==='rejected')assert.match(await page.locator('#ai-comparison-note').innerText(),/未采用/);
     }
    }
    if(state==='adopted'){
     for(const width of [1440,834,390,320])for(const theme of ['light','dark']){
      await page.setViewportSize({width,height:1050});await page.evaluate(t=>applyTheme(t),theme);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),width+' '+theme+' page overflow');
      const clipped=await page.locator('#ai-comparison strong,#ai-comparison h2,#ai-comparison h3,#ai-comparison p,#ai-comparison .ai-effect-value').evaluateAll(nodes=>nodes.filter(e=>e.getClientRects().length&&e.scrollWidth>e.clientWidth+2).map(e=>({id:e.id,text:e.textContent})));assert.deepEqual(clipped,[],width+' '+theme+' clipped text');
      await page.locator('#ai-comparison').screenshot({path:path.join(output,`ai-${width}-${theme}.png`),animations:'disabled'});
      results.push({layout:{width,theme},status:'passed'});
     }
     await page.locator('#theme').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
     await page.locator('#ai-time').focus();await page.keyboard.press('ArrowRight');assert.equal(await page.locator('#ai-time').inputValue(),'1');
    }
   }
   assert.deepEqual(errors,[],state+' script errors');assert.deepEqual(writes,[],state+' writes');
   results.push({state,status:'passed',errors,writes});await page.close();
  }
  fs.writeFileSync(path.join(output,'ai-comparison-e2e.json'),JSON.stringify({browser:'WebKit',html_sha256:createHash('sha256').update(html).digest('hex'),results},null,2));
  console.log('PASS: 6 comparison states, candidate/adoption identity, costs, slider/highlights/keyboard/theme, 8 layouts; GET-only mocked transport.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
