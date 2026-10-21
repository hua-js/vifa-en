'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webkit}=require('playwright');
const root=path.resolve(__dirname,'../..');
const fixtures=process.env.M4_FALLBACK_FIXTURE_DIR;
assert(fixtures,'Set M4_FALLBACK_FIXTURE_DIR to the saved-input validation directory');
const read=name=>JSON.parse(fs.readFileSync(path.join(fixtures,name),'utf8'));
const saved=read('station-2-validated-fallback-snapshot.json');
const input=read('station-2-daily-inputs.json');
const settings=read('station-2-settings.json'),project=read('ui-project.json');
const html=fs.readFileSync(path.join(root,'m4/web/M4优化调度控制台-线上版.html'),'utf8');
const output=path.join(fixtures,'fallback-layouts');fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await webkit.launch({headless:true});const results=[];
 try{
  for(const scenario of ['pending','confirmed','frozen-after-failure','blocked']){
   const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
   await page.clock.setFixedTime(new Date('2026-10-14T13:50:00+08:00'));
   page.on('pageerror',e=>errors.push(e.message));
   const job=structuredClone(saved);job.automation={enabled:true,next_check_at:'2026-10-14T14:00:00+08:00'};
   job.rolling_history={station_id:'station-2',status:'empty',rows:[]};job.rolling={status:'disabled'};
   job.ems_table_write={enabled:true};job.daily_dispatch={station_id:'station-2',date:'2026-10-14',status:'waiting'};
   if(scenario==='confirmed'||scenario==='frozen-after-failure'){
    job.active_daily=structuredClone(saved);job.daily_dispatch={station_id:'station-2',date:'2026-10-14',status:'completed',confirmed_daily_run_id:saved.run_id,
     active_plan:{kind:'daily',source:'ems_fallback',status:'plan_table_readback_verified',effective_at:'2026-10-14T01:45:00+08:00',end_at:'2026-10-15T00:00:00+08:00'}};
   }
   if(scenario==='frozen-after-failure'){job.status='failed';job.message='新计算失败';job.result=null;}
   if(scenario==='blocked'){Object.assign(job.result.record.daily_comparison,{status:'blocked',recommended:null,recommended_source:null,fallback_status:'blocked',fallback_reason:'保守计划不满足安全要求。',reason:'全天预计净节省未达到100元。保守计划不满足安全要求。'});}
   await page.route('https://m4.test/**',async route=>{
    const req=route.request(),url=new URL(req.url());assert.equal(req.method(),'GET','No write or task request during UI validation');
    if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
    const endpoint=url.pathname.split('/').at(-1);
    if(endpoint==='project')return route.fulfill({json:project});
    if(endpoint==='daily-plan')return route.fulfill({json:job});
    if(endpoint==='daily-inputs')return route.fulfill({json:{...input,can_compare:scenario==='blocked',comparison:null}});
    if(endpoint==='settings')return route.fulfill({json:settings});
    if(endpoint==='control-sources')return route.fulfill({json:input.sources.controls});
    if(endpoint==='decision-history')return route.fulfill({json:{station_id:url.pathname.split('/')[3],items:[]}});
    throw Error('Unexpected fixture route '+url.pathname);
   });
   await page.goto('https://m4.test/m4?token=fixture-only-token');await page.waitForLoadState('networkidle');
   console.log(JSON.stringify({scenario,badge:await page.locator('.decision .badge').first().innerText(),copy:await page.locator('#decision-copy').innerText(),errors}));
   if(scenario==='blocked'){
    await page.waitForFunction(()=>document.querySelector('.decision h2').textContent.includes('优化未采用'));
    assert.equal(await page.locator('#chart-panel').isVisible(),false);
    assert.equal(await page.locator('#dispatch-daily').isVisible(),false);
   }else{
    await page.waitForFunction(()=>document.querySelector('#chart-panel').hidden===false);
    assert.match(await page.locator('#decision-copy').innerText(),/72\.97/);
    assert.match(await page.locator('#decision-copy').innerText(),/100元/);
    assert.match(await page.locator('#decision-copy').innerText(),/保守 EMS/);
    assert.match(await page.locator('#plan-cost').innerText(),/1,?406\.88/);
    assert.match(await page.locator('#baseline-cost').innerText(),/1,?369\.38/);
    assert.equal(await page.evaluate(()=>plan.comparison.recommended_source),'ems_fallback');
    const state=await page.locator('#daily-dispatch-status').innerText();
    assert.match(state,scenario==='pending'?/尚未确认写表/:/保守日计划已确认写表/);
    if(scenario==='pending')for(const width of [1440,834,390])for(const theme of ['light','dark']){
     await page.setViewportSize({width,height:1000});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
     assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),width+' '+theme+' overflow');
     const file=path.join(output,width+'-'+theme+'.png');await page.screenshot({path:file,fullPage:true,animations:'disabled'});
     results.push({width,theme,screenshot:file});
    }
   }
   assert.deepEqual(errors,[]);results.push({scenario,status:'passed'});await page.close();
  }
  fs.writeFileSync(path.join(fixtures,'frontend-fallback-e2e.json'),JSON.stringify({browser:'WebKit',results},null,2));
  console.log('4 fallback states and 6 layouts passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
