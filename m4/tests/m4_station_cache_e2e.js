'use strict';
const {fixture,html}=require('./m4_reference_live_e2e'),{chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'chrome'});try{
 const page=await browser.newPage(),errors=[],held=new Map(),counts=new Map();let hold=null,fail=false;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('https://m4.test/**',async route=>{
  const u=new URL(route.request().url());if(u.pathname==='/m4')return route.fulfill({contentType:'text/html',body:html});
  assert.equal(route.request().method(),'GET');const station=u.pathname.includes('station-2')?'station-2':'station-1',endpoint=u.pathname.split('/').at(-1),f=fixture(station);
  counts.set(station+'/'+endpoint,(counts.get(station+'/'+endpoint)||0)+1);
  if(endpoint==='daily-inputs'){
   if(hold===station)await new Promise(resolve=>held.set(station,resolve));
   if(fail)return route.fulfill({status:502,json:{detail:'读取失败'}});
   return route.fulfill({json:f.inputs});
  }
  return route.fulfill({json:endpoint==='daily-plan'?f.job:{station_id:station,version:endpoint==='settings'?'config/v1':'controls/v1'}});
 });
 const ready=()=>page.waitForFunction(()=>!document.querySelector('#simulate').disabled&&!document.querySelector('.decision .badge').textContent.includes('缓存'));
 await page.goto('https://m4.test/m4?token=fixture-only-token');await ready();await page.locator('#station').selectOption('station-2');await ready();
 hold='station-2';await page.reload();await page.waitForFunction(()=>document.querySelector('.decision .badge').textContent.includes('缓存'));
 assert.equal(await page.locator('#loading-mask').isVisible(),false);assert.equal(await page.locator('#station').inputValue(),'station-2');assert.equal(await page.locator('#chart-panel').isVisible(),true);assert.equal(await page.locator('#simulate').isDisabled(),true);
 await page.waitForFunction(()=>document.querySelector('#decision-copy').textContent.includes('电站 2'));
 while(!held.has('station-2'))await new Promise(r=>setTimeout(r,10));held.get('station-2')();held.delete('station-2');hold=null;await ready();
 hold='station-1';const before=counts.get('station-1/daily-inputs');await page.locator('#station').selectOption('station-1');assert.equal(await page.locator('#chart-panel').isVisible(),true);
 assert.match(await page.locator('#decision-copy').innerText(),/电站 1/);assert.equal(await page.locator('#simulate').isDisabled(),true);
 await page.locator('#station').selectOption('station-2');await page.locator('#station').selectOption('station-1');
 while(!held.has('station-1'))await new Promise(r=>setTimeout(r,10));assert.equal(counts.get('station-1/daily-inputs'),before+1,'switching back reuses the in-flight request');
 await page.locator('#station').selectOption('station-2');held.get('station-1')();held.delete('station-1');hold=null;await ready();assert.match(await page.locator('#decision-copy').innerText(),/电站 2/);
 const stored=await page.evaluate(()=>Object.entries(sessionStorage));assert(stored.some(([k])=>k.startsWith('m4-view-v1:')));assert(stored.every(([,v])=>!v.includes('fixture-only-token')));
 // Expired data cannot be used as a current view on reload.
 await page.evaluate(()=>{for(const k of Object.keys(sessionStorage))if(k.startsWith('m4-view-v1:')){const v=JSON.parse(sessionStorage.getItem(k));v.at=0;sessionStorage.setItem(k,JSON.stringify(v));}});
 hold='station-2';await page.reload();await page.waitForFunction(()=>document.querySelector('.decision h2').textContent==='正在读取当天数据');assert.equal(await page.locator('#chart-panel').isVisible(),false);assert.equal(await page.locator('#loading-mask').isVisible(),true);await page.waitForFunction(()=>document.querySelector('#loading-message').textContent.includes('较慢'));
 await page.screenshot({path:'/tmp/m4-loading-light.png'});await page.evaluate(()=>document.documentElement.dataset.theme='dark');await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/m4-loading-dark-mobile.png'});
 while(!held.has('station-2'))await new Promise(r=>setTimeout(r,10));held.get('station-2')();held.delete('station-2');hold=null;await ready();
 fail=true;await page.locator('#station').dispatchEvent('change');await page.waitForFunction(()=>document.querySelector('.decision h2').textContent==='数据暂不可用');assert.equal(await page.locator('#chart-panel').isVisible(),false);
 assert.equal(await page.locator('#loading-mask').isVisible(),false);assert.deepEqual(errors,[]);console.log('PASS: refresh restores station/snapshot, instant station switch, pending request deduplication, stale-response isolation, disabled generation during revalidation, no stored token, expiry and failed-refresh invalidation.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
