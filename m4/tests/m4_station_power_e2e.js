'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const {fixture,html}=require('./m4_reference_live_e2e');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://m4.test/**',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
   requests.push({method:request.method(),path:url.pathname});
   const station=url.pathname.includes('station-2')?'station-2':'station-1',f=fixture(station),endpoint=url.pathname.split('/').at(-1);
   f.job.request.peak_reserve_policy={version:'peak-reserve-v1'};
   const responses={
    'daily-inputs':f.inputs,'daily-plan':f.job,
    'settings':{station_id:station,version:'config/v1'},
    'control-sources':{station_id:station,version:'controls/v1'},
    'decision-history':{station_id:station,schema_version:'m4-decision-history-v1',dispatch_status:'not_dispatched',items:[],next_offset:null}
   };
   if(request.method()!=='GET'||!responses[endpoint])return route.fulfill({status:404,json:{detail:'not available'}});
   return route.fulfill({json:responses[endpoint]});
  });
  await page.goto('http://m4.test/m4?token=fixture-only-token');
  await page.waitForFunction(()=>!document.querySelector('#chart-panel').hidden);
  assert.equal(await page.locator('#cabinet-body,#allocation-dialog,#save-allocation').count(),0);
  assert.match(await page.locator('.reference-hero').innerText(),/柜间分配由 EMS 负责/);
  for(const station of ['station-1','station-2']){
   await page.locator('[data-station-id="'+station+'"]').click();
   await page.waitForFunction(s=>plan?.station===s,station);
   const expected=fixture(station).job.result.record.daily_comparison.recommended.plan.map(p=>p.mode==='charge'?-p.target_power_kw:p.mode==='discharge'?p.target_power_kw:0);
   assert.deepEqual(await page.evaluate(()=>plan.points.map(p=>p.power)),expected);
   await page.locator('#time').fill('76');await page.locator('#time').dispatchEvent('input');
   assert.match(await page.locator('#inspector').innerText(),/放电/);
  }
  await page.locator('#all-logs').click();
  await page.waitForFunction(()=>document.querySelectorAll('#records tr').length===2);
  await page.locator('[data-record]').first().click();
  await page.locator('#detail').waitFor({state:'visible'});
  assert.doesNotMatch(await page.locator('#detail').innerText(),/柜级分配/);
  await page.locator('#close-detail').click();await page.locator('#back-console').click();
  const dir=path.resolve(__dirname,'../../outputs/m4/evaluations/2026-09-12-station-power');fs.mkdirSync(dir,{recursive:true});
  for(const width of [1440,834,390])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:1000});
   if(await page.locator('html').getAttribute('data-theme')!==theme)await page.locator('#theme').click();
   assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   assert(await page.locator('.log-reference').evaluate(node=>
    node.getBoundingClientRect().width >= node.parentElement.getBoundingClientRect().width-2));
   await page.screenshot({path:path.join(dir,width+'-'+theme+'.png'),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  assert(requests.every(r=>r.method==='GET'));
  assert(!requests.some(r=>/allocations|allocation-result/.test(r.path)));
  console.log('PASS: station powers unchanged, retired allocation absent, two stations, history, timeline, six theme/viewport renders, no writes');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
