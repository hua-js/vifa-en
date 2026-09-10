'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'../..');
function payload(station,month){
 const days=Array.from({length:month==='2026-09'?9:new Date(Number(month.slice(0,4)),Number(month.slice(5)),0).getDate()},(_,i)=>({date:month+'-'+String(i+1).padStart(2,'0'),updated_at:month+'-'+String(i+1).padStart(2,'0')+'T23:45:00+08:00',day_charge:100,day_discharge:80,charge_cost:40+Math.round(15*Math.sin(i*Math.PI/15)),discharge_earnings:30+Math.round(15*Math.sin(i*Math.PI/15))/2,day_earnings:-10-Math.round(15*Math.sin(i*Math.PI/15))/2}));
 const count=days.length;
 return {schema_version:'m4-billing-v1',station_id:station,station_code:station==='station-1'?'ES01':'ES02',month,status:'ready',period_status:month==='2026-08'?'historical':'ongoing',settlement_status:'unverified',fetched_at:'2026-09-09T10:00:00+08:00',monthly:{updated_at:month+'-09T09:30:00+08:00',month_charge:count*100,month_discharge:count*80,month_earnings:count*-10,month_costs:0,month_income:0,profit:0},daily:days,daily_totals:{day_charge:count*100,day_discharge:count*80,charge_cost:count*40,discharge_earnings:count*30,day_earnings:count*-10},differences:{month_charge:0,month_discharge:0,month_earnings:0},coverage:{recorded_days:count,expected_days:count,missing_dates:[]},sources:{monthly:{status:'ready',table:'t_es_count_stat'},daily:{status:'ready',table:'t_es_stat'}},issues:[]};
}
(async()=>{
 const host=spawn(path.join(root,'.venv/bin/python'),['m4/tests/m4_selection_demo_server.py'],{cwd:root});let browser,release;
 try{
  const port=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('startup timeout')),20000);host.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/PORT=(\d+)/);if(match){clearTimeout(timer);resolve(+match[1]);}});host.once('exit',c=>reject(Error('host exit '+c)));host.stderr.on('data',c=>process.stderr.write(c));});
  browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1100},locale:'zh-CN'}),errors=[],writes=[],requests=[];let mode='normal',markPrevious=null;
  page.on('pageerror',e=>errors.push(e.stack));page.on('request',r=>{requests.push(r.url());if(r.method()!=='GET')writes.push(r.url());});
  await page.route('**/bills?*',async route=>{
   assert.equal(route.request().headers().authorization,'Bearer billing-test-token');
   const url=new URL(route.request().url()),station=url.pathname.includes('station-1')?'station-1':'station-2',month=url.searchParams.get('month');
   if(mode==='previous-error'&&month==='2026-07')return route.fulfill({status:502,json:{detail:'上月统计暂不可用'}});
   if(mode==='http-error')return route.fulfill({status:502,json:{detail:'账单统计暂不可用'}});
   let data=payload(station,month);
   if(mode==='empty'||station==='station-2'){data.daily=[];data.monthly=null;data.status='empty';data.sources.daily.status='empty';data.sources.monthly.status='empty';data.coverage.recorded_days=0;data.differences={month_charge:null,month_discharge:null,month_earnings:null};data.daily_totals=Object.fromEntries(Object.keys(data.daily_totals).map(k=>[k,null]));}
   if(mode==='partial'){data.monthly=null;data.sources.monthly.status='error';data.status='partial';data.issues=['月统计接口读取失败','<img src=x onerror=alert(1)>'];data.differences={};}
   if(mode==='wrong')data.station_id='station-2';
   if(mode==='hold-previous'&&station==='station-1'&&month==='2026-07')await new Promise(r=>{release=r;markPrevious?.();});
   if(mode==='hold'&&station==='station-1'&&month==='2026-08')await new Promise(r=>release=r);
   await route.fulfill({json:data}).catch(()=>{});
  });
  await page.goto(`http://127.0.0.1:${port}/m4?token=billing-test-token`);await page.waitForLoadState('networkidle');
  await page.locator('#daily-tab-billing').click();await page.waitForFunction(()=>document.querySelector('#billing-output .billing-metrics'));
  assert.equal(await page.locator('#daily-panel-billing').isVisible(),true);assert.equal(await page.locator('#daily-panel-overview').isVisible(),false);
  await page.locator('#billing-month').fill('2026-08');await page.locator('#billing-month').dispatchEvent('change');await page.waitForFunction(()=>document.querySelectorAll('.billing-days tbody tr').length===31);
  await page.waitForFunction(()=>document.querySelector('[data-comparison-field="day_charge"]'));
  assert.match(await page.locator('#billing-month-comparison').innerText(),/历史整月对比/);
  assert.match(await page.locator('#billing-month-comparison').innerText(),/31\/31 天/);
  assert.match(await page.locator('[data-comparison-field="day_earnings"]').innerText(),/-310\.00.*-310\.00.*0\.00.*—/);
  assert.equal(await page.locator('#billing-trend-chart polyline').count(),3);
  await page.locator('[data-billing-series="charge_cost"]').uncheck();assert.equal(await page.locator('#billing-trend-chart polyline').count(),2);
  await page.locator('#billing-trend-day').fill('30');await page.locator('#billing-trend-day').dispatchEvent('input');assert.match(await page.locator('#billing-trend-readout').innerText(),/2026-08-31.*净收益.*-10\.00/);
  await page.locator('[data-billing-series="charge_cost"]').check();
  assert.match(await page.locator('#billing-output').innerText(),/历史月统计/);assert.equal(await page.locator('.billing-negative').count(),31);
  assert.match(await page.locator('.billing-metrics').innerText(),/-310\.00/);
  const downloadPromise=page.waitForEvent('download');await page.locator('#billing-export').click();const download=await downloadPromise;
  assert.equal(download.suggestedFilename(),'M4-ES01-2026-08-每日统计.csv');const csv=fs.readFileSync(await download.path(),'utf8');assert.match(csv,/2026-08-31/);assert.equal(csv.trim().split('\r\n').length,32);
  const screens='/tmp/m4-billing-screens';fs.mkdirSync(screens,{recursive:true});
  for(const width of [1440,768,390,320])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:1100});await page.evaluate(theme=>document.documentElement.setAttribute('data-theme',theme),theme);
   assert(await page.locator('#daily-navigation').evaluate(e=>e.scrollWidth<=e.clientWidth+1),`navigation overflow ${width} ${theme}`);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),`page overflow ${width} ${theme}`);
   assert(await page.locator('#daily-panel-billing').evaluate(e=>e.scrollWidth<=e.clientWidth+1),`billing panel overflow ${width} ${theme}`);
   await page.screenshot({animations:'disabled',path:path.join(screens,`billing-${width}-${theme}.png`),fullPage:true});
  }
  await page.setViewportSize({width:1440,height:1100});
  mode='previous-error';await page.locator('#billing-refresh').click();await page.waitForFunction(()=>document.querySelector('#billing-month-comparison')?.textContent.includes('上月统计暂不可用'));
  assert.equal(await page.locator('.billing-days tbody tr').count(),31);assert.equal(await page.locator('#billing-trend-chart polyline').count(),3);
  mode='partial';await page.locator('#billing-refresh').click();await page.waitForFunction(()=>document.querySelector('#billing-status').dataset.state==='partial');
  assert.equal(await page.locator('.billing-days tbody tr').count(),31);assert.equal(await page.locator('#billing-output img').count(),0);assert.match(await page.locator('.billing-metrics').innerText(),/—/);
  mode='http-error';await page.locator('#billing-refresh').click();await page.waitForFunction(()=>document.querySelector('#billing-status').dataset.state==='error');assert.equal(await page.locator('#billing-output').innerText(),'');assert(await page.locator('#billing-export').isDisabled());
  mode='wrong';await page.locator('#billing-refresh').click();await page.waitForFunction(()=>document.querySelector('#billing-status').textContent.includes('不匹配'));assert.equal(await page.locator('#billing-output').innerText(),'');
  mode='hold';await page.locator('#billing-refresh').click();await page.waitForFunction(()=>document.querySelector('#billing-status').dataset.state==='loading');
  await page.locator('#station-select').selectOption('s2');await page.waitForFunction(()=>document.querySelector('#billing-status').dataset.state==='empty'&&document.querySelector('#billing-output .billing-metrics'));
  if(release){release();release=null;}await page.waitForLoadState('networkidle');assert.match(await page.locator('#billing-output').innerText(),/电站\s*2/);assert.equal(await page.locator('#daily-tab-billing').getAttribute('aria-selected'),'true');
  mode='normal';await page.locator('#station-select').selectOption('s1');await page.waitForFunction(()=>document.querySelectorAll('.billing-days tbody tr').length===31);assert.equal(await page.locator('#billing-month').inputValue(),'2026-08');
  await page.locator('#daily-tab-billing').focus();await page.keyboard.press('Home');assert.equal(await page.locator('#daily-tab-overview').getAttribute('aria-selected'),'true');await page.keyboard.press('End');assert.equal(await page.locator('#daily-tab-billing').getAttribute('aria-selected'),'true');
  mode='hold-previous';const previousStarted=new Promise(resolve=>markPrevious=resolve);await page.locator('#billing-refresh').click();await previousStarted;
  await page.waitForFunction(()=>document.querySelector('#billing-month-comparison')?.textContent.includes('正在读取'));
  assert.equal(await page.locator('.billing-days tbody tr').count(),31);
  await page.locator('#station-select').selectOption('s2');release();release=null;
  await page.waitForFunction(()=>document.querySelector('#billing-month-comparison')?.textContent.includes('2026-09'));
  assert.match(await page.locator('#billing-output').innerText(),/电站\s*2/);assert.doesNotMatch(await page.locator('#billing-month-comparison').innerText(),/2026-07/);
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);assert(requests.every(url=>new URL(url).hostname==='127.0.0.1'));
  console.log('PASS: trend series/date controls, full-month comparison, prior-source failure isolation; billing tab, monthly/day totals, historical month, negative earnings, CSV, partial/empty/error states, escaped source text, wrong-station rejection, late-response isolation, month memory, keyboard tabs, 8 light/dark layout screenshots; Mock GET only.');
 }finally{if(release)release();if(browser)await browser.close();host.kill('SIGTERM');}
})().catch(e=>{console.error(e);process.exitCode=1;});
