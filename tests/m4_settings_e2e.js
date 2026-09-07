'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const values={energy_capacity_kwh:'500',max_charge_kw:'80',max_discharge_kw:'90',charge_efficiency:'94',discharge_efficiency:'93',soc_min_pct:'15',soc_max_pct:'85',preferred_soc_min_pct:'25',preferred_soc_max_pct:'75',terminal_soc_tolerance_pct:'4',grid_import_limit_kw:'200',cycle_cost_per_kwh:'0.02',max_input_age_seconds:'300'};
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'m4-settings-'));
 const server=spawn(path.join(root,'.venv/bin/python'),['-m','uvicorn','m4_settings.api:create_app','--factory','--host','127.0.0.1','--port','0'],{cwd:root,env:{...process.env,M4_SETTINGS_DB:path.join(temp,'settings.sqlite3'),M4_NOCOBASE_TOKEN:''},stdio:['ignore','pipe','pipe']});
 let browser;
 try {
  const port=await new Promise((resolve,reject)=>{let log='';const timer=setTimeout(()=>reject(new Error(log||'server timeout')),15000);server.stderr.on('data',chunk=>{log+=chunk;const match=log.match(/http:\/\/127\.0\.0\.1:(\d+)/);if(match){clearTimeout(timer);resolve(match[1]);}});server.once('exit',()=>{clearTimeout(timer);reject(new Error(log));});});
  const url=`http://127.0.0.1:${port}/m4`;
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const controlsRoute=async route=>{
    const station=route.request().url().includes('station-1')?1:2;
    await route.fulfill({json:{station_id:`station-${station}`,source_station_id:`ES0${station}`,status:'ready',version:`control-test-${station}`,fetched_at:'2026-09-07T08:55:00+00:00',power_scope:'station',demand:{need_kw:station===1?1062.5:504,reserved_kw:station===1?50:5,rated_capacity:station===1?1250:630,load_rate:.8},reverse_flow:{re_kw:40,enabled:false,effective_limit_kw:null},schedule:[{id:station,start_time:'00:00:00',end_time:'08:00:00',mode:'charge',power_kw:100,repeat:'daily'},{id:station+2,start_time:'08:00:00',end_time:'12:00:00',mode:'discharge',power_kw:station===1?60:90,repeat:'daily'}],issues:[],warnings:['防逆流配置未启用，40 kW 不参与约束。'],source_health:{schedule:'ready'}}});
  };
  await page.route('**/control-sources',controlsRoute);
  await page.goto(url);await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('#open-settings').count(),1);
  assert.equal(await page.locator('#offline-controls').isVisible(),false);
  assert.equal(await page.locator('#plan-chart [data-point]').count(),0);
  await page.locator('#open-settings').click();
  await page.waitForFunction(()=>!document.querySelector('#save-settings').disabled);
  assert.equal(await page.locator('[name="energy_capacity_kwh"]').inputValue(),'');
  assert.equal(await page.locator('[name="initial_soc_pct"]').count(),0);
  for(const [key,value] of Object.entries(values))await page.locator(`[name="${key}"]`).fill(value);
  await page.locator('[name="soc_max_pct"]').fill('10');await page.locator('#save-settings').click();
  assert.match(await page.locator('#settings-status').innerText(),/SOC/);
  let payload=await (await page.request.get(`http://127.0.0.1:${port}/m4-api/stations/station-1/settings`)).json();assert.equal(payload.revision,0);
  await page.locator('[name="soc_max_pct"]').fill('85');await page.locator('#save-settings').click();
  await page.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='saved');
  payload=await (await page.request.get(`http://127.0.0.1:${port}/m4-api/stations/station-1/settings`)).json();
  assert.equal(payload.parameters.charge_efficiency,.94);assert.equal(payload.parameters.max_charge_kw,80);assert.equal('demand_limit_kw' in payload.parameters,false);
  assert.equal(await page.locator('[name="demand_limit_kw"],[name="grid_export_enabled"],[name="grid_export_limit_kw"]').count(),0);
  assert.match(await page.locator('#control-sources').innerText(),/1062.5 kW/);
  assert.match(await page.locator('#control-sources').innerText(),/免调功率/);
  assert.match(await page.locator('#control-sources').innerText(),/40 kW · 未启用/);
  assert.match(await page.locator('#control-sources').innerText(),/60 kW/);
  assert.match(await page.locator('#control-sources').innerText(),/kVA/);
  await page.selectOption('#settings-station','s2');
  await page.waitForFunction(()=>document.querySelector('#control-sources').textContent.includes('504 kW'));
  assert.match(await page.locator('#control-sources').innerText(),/90 kW/);
  assert.equal(await page.locator('[name="energy_capacity_kwh"]').inputValue(),'');
  await page.selectOption('#settings-station','s1');
  assert.equal(await page.locator('[name="energy_capacity_kwh"]').inputValue(),'500');
  await page.keyboard.press('Escape');await page.reload();await page.waitForLoadState('networkidle');
  await page.locator('#open-settings').click();
  await page.waitForFunction(()=>document.querySelector('[name="energy_capacity_kwh"]').value==='500');
  assert.match(await page.locator('#settings-version').innerText(),/v1/);
  const second=await browser.newPage();await second.route('**/control-sources',controlsRoute);await second.goto(url);await second.waitForLoadState('networkidle');await second.locator('#open-settings').click();await second.waitForFunction(()=>document.querySelector('[name="energy_capacity_kwh"]').value==='500');
  await second.locator('[name="max_charge_kw"]').fill('130');await second.locator('#save-settings').click();await second.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='saved');
  await page.locator('[name="max_charge_kw"]').fill('120');await page.locator('#save-settings').click();
  await page.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='error');assert.match(await page.locator('#settings-status').innerText(),/其他窗口|重新读取/);
  assert.equal(await page.locator('[name="max_charge_kw"]').inputValue(),'120');
  await page.locator('#reload-settings').click();await page.waitForFunction(()=>document.querySelector('[name="max_charge_kw"]').value==='130');
  for(const width of [1440,768,390,320]){
   await page.setViewportSize({width,height:1000});await page.locator('#settings-drawer').evaluate(e=>e.scrollTop=0);
   for(const theme of ['light','dark']){
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    await page.evaluate(()=>Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{}))));
    assert.equal(await page.locator('#settings-drawer').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
    if(process.env.M4_SETTINGS_SCREENSHOT_DIR){fs.mkdirSync(process.env.M4_SETTINGS_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.M4_SETTINGS_SCREENSHOT_DIR,`${width}-${theme}.png`)});}
   }
  }
  let releaseControls;
  const heldControls=new Promise(resolve=>releaseControls=resolve);
  await page.route('**/control-sources',async route=>{await heldControls;await route.abort();});
  await page.locator('#reload-settings').click();
  assert.equal(await page.locator('#demand-limit').innerText(),'—');
  assert.equal(await page.locator('#control-sources').getAttribute('data-state'),'loading');
  releaseControls();
  await page.waitForFunction(()=>document.querySelector('#control-sources').dataset.state==='error');
  assert.doesNotMatch(await page.locator('#control-sources').innerText(),/1062.5 kW/);
  await page.waitForFunction(()=>!document.querySelector('#save-settings').disabled);
  await page.route('**/m4-api/stations/*/settings',route=>route.abort());await page.locator('#save-settings').click();await page.waitForFunction(()=>document.querySelector('#settings-status').dataset.state==='error');
  assert.match(await page.locator('#settings-status').innerText(),/未保存|失败|连接/);
  assert.deepEqual(errors,[]);
  console.log('PASS: read-only control sources, reduced fields, source failure isolation, per-station parameter editing, server persistence, percentage conversion, SOC validation, concurrency conflict, failure state and responsive light/dark drawer');
 }finally{if(browser)await browser.close();server.kill('SIGTERM');await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));fs.rmSync(temp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
