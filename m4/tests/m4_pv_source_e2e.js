'use strict';
const {fixture,html}=require('./m4_reference_live_e2e'),{chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:'chrome'});try{
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 await page.addInitScript(()=>sessionStorage.setItem('m4-selected-station','station-2'));
 await page.route('https://m4.test/**',async route=>{
  assert.equal(route.request().method(),'GET');
  const endpoint=new URL(route.request().url()).pathname.split('/').at(-1);
  if(endpoint==='m4')return route.fulfill({contentType:'text/html',body:html});
  const f=fixture('station-2');
  if(endpoint==='daily-inputs'){
   f.inputs.sources.pv.version='pv/v2';
   f.inputs.request=structuredClone(f.job.request);f.inputs.request.source_versions.pv='pv/v2';
   const c=structuredClone(f.job.result.record.daily_comparison);
   f.inputs.comparison={...c,status:'ems',recommended_source:'ems',recommended:c.baseline,reason:'当前输入的 EMS 基线'};
   return route.fulfill({json:f.inputs});
  }
  if(endpoint==='daily-plan')return route.fulfill({json:f.job});
  return route.fulfill({json:{station_id:'station-2',version:endpoint==='settings'?'config/v1':'controls/v1'}});
 });
 await page.goto('https://m4.test/m4?token=fixture-only-token');
 await page.waitForFunction(()=>!document.querySelector('#simulate').disabled);
 assert.equal(await page.locator('.decision h2').innerText(),'沿用 EMS 原计划');
 assert.equal(await page.locator('#saving').innerText(),'¥ 0.00');
 assert.equal(await page.locator('#chart-panel').isVisible(),true);
 await page.screenshot({path:'/tmp/m4-pv-current-baseline.png',fullPage:true});
 console.log('PASS: changed PV source shows current EMS baseline instead of previous optimization; no POST.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
