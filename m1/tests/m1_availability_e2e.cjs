'use strict';
// Local Firefox only. Every non-local request is fulfilled by a fixture or blocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const crypto = require('node:crypto');
const {once} = require('node:events');
const {firefox} = require('playwright');
const {payload,fail,scenarios,copy} = require('./availability_fixture.cjs');
const root = path.resolve(__dirname,'../..');
const htmlPath = path.join(root,'m1/web/dashboard_energy.html');
const html = fs.readFileSync(htmlPath);
const output = path.join(root,'outputs/m1/tests',new Date().toISOString().replace(/[:.]/g,'-'));
fs.mkdirSync(output,{recursive:true});
const report = {browser:'Firefox',scope:'Local fixture fault injection; no production requests',
  html_sha256:crypto.createHash('sha256').update(html).digest('hex'),cases:[],page_errors:[],blocked_requests:[]};
let mode = 'ok',body = payload(1),requests = 0,browser;
const server = http.createServer((req,res)=>{
  if (req.url === '/' || req.url === '/m1') {
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});res.end(html);
  } else {res.writeHead(404);res.end();}
});

(async()=>{
  server.listen(0,'127.0.0.1');await once(server,'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await firefox.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'});
  await context.route('**/*',async route=>{
    const url = route.request().url();
    if (url.startsWith('https://aivifa.hlszh.com/dashboard_energy_api')) {
      requests++;
      if (mode === 'network') return route.abort('failed');
      const headers = {'Content-Type':'application/json','Access-Control-Allow-Origin':'*'};
      if (mode === 'http') return route.fulfill({status:503,headers,body:'{"status":"error"}'});
      if (mode === 'json') return route.fulfill({status:200,headers,body:'{broken'});
      const value = mode === 'business' ? {status:'error',message:'fixture'} :
        mode === 'empty' ? {status:'ok',data:{}} : body;
      return route.fulfill({status:200,headers,body:JSON.stringify(value)});
    }
    if (url.startsWith(origin+'/')) return route.continue();
    report.blocked_requests.push(url);return route.abort('blockedbyclient');
  });
  const page = await context.newPage();
  page.on('pageerror',error=>report.page_errors.push(error.message));
  await page.goto(origin+'/m1');
  await page.waitForFunction(()=>typeof allData !== 'undefined' && allData && !fetchInFlight);
  const cards = await page.locator('.node-card').count();
  assert.ok(cards >= 9);
  const getState = ()=>page.evaluate(()=>({
    data:allData,
    hint:document.querySelector('#refreshInfo').textContent,
    cards:document.querySelectorAll('.node-card').length,
    offline:document.querySelectorAll('.node-offline').length,
    toast:document.querySelector('#toast').classList.contains('show'),
    cabinet:document.querySelector('.node-card[data-node-id="1010"]').textContent,
    pv:document.querySelector('.node-card[data-node-id="1102"]').textContent,
  }));
  const row = (state,id)=>state.data.realtime.find(row=>row.fk_en===id);
  const monitor = ()=>page.evaluate(()=>{
    window.availabilityMutations=[];
    window.availabilityObserver?.disconnect();
    window.availabilityObserver=new MutationObserver(()=>{
      const invalid=document.querySelectorAll('.node-offline').length ||
        document.querySelector('#toast').classList.contains('show') ||
        !document.querySelectorAll('.node-card').length ||
        /刷新失败|刷新超时|数据为空/.test(document.querySelector('#refreshInfo').textContent);
      if(invalid)window.availabilityMutations.push(document.querySelector('#sitesPanel').textContent.slice(0,100));
    });
    window.availabilityObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
  });
  const read = async()=>{
    await page.evaluate(()=>fetchData());
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    return getState();
  };
  const assertQuiet = async state=>{
    assert.equal(state.cards,cards,'device cards must remain on screen');
    assert.equal(state.offline,0,'fixture source failure must not mark devices offline');
    assert.equal(state.toast,false,'no visible toast');
    assert.deepEqual(await page.evaluate(()=>window.availabilityMutations),[],'no transient empty/offline/error DOM state');
  };
  for(const kind of scenarios) {
    mode='ok';body=payload(1);await read();await monitor();
    body=fail(payload(2),kind);
    const failed = await read();await assertQuiet(failed);
    if(kind==='growatt'||kind==='all_sources') {
      assert.equal(failed.data.pv_inverters[0].output_power_kw,10);
      assert.match(failed.pv,/90 kW/);
      assert.equal(row(failed,1011).soc,52,'healthy EMS source continues updating');
    }
    if(kind==='missing_realtime'||kind==='missing_placeholder') assert.equal(row(failed,1010).soc,51);
    if(kind==='station_load:ES01') assert.equal(row(failed,1000).power,21);
    if(kind==='station_load:ES02') assert.equal(row(failed,1100).power,21);
    if(kind.startsWith('storage_cabinets')||kind==='all_sources') {
      const id=kind==='storage_cabinets:ES02'?1110:1010;
      assert.equal(row(failed,id).temperature_c,31);
    }
    if(kind==='history'||kind==='all_sources') {
      assert.equal(failed.data.alert_history.status,'ok');
      await page.getByRole('button',{name:'历史告警',exact:true}).click();
      assert.doesNotMatch(await page.locator('#historyNote').textContent(),/读取失败|读写失败|尚未接入/);
      await page.getByRole('button',{name:'当前告警',exact:true}).click();
    }
    if(kind==='all_sources') await page.screenshot({path:path.join(output,'sources-failed.png'),fullPage:true});
    body=payload(3);
    const recovered = await read();await assertQuiet(recovered);
    assert.equal(row(recovered,1010).power,23);
    assert.equal(row(recovered,1010).temperature_c,33);
    assert.equal(recovered.data.pv_inverters[0].output_power_kw,12);
    assert.match(recovered.pv,/108 kW/);
    report.cases.push({name:kind,result:'PASS',retained:true,recovered:true,dom_glitches:0});
  }
  for(const kind of ['network','http','json','business','empty']) {
    mode='ok';body=payload(1);const before=await read();await monitor();
    mode=kind;
    const failed=await read();await assertQuiet(failed);
    assert.deepEqual(failed,before,'whole read failure must keep all visible data and refresh hint');
    mode='ok';body=payload(3);
    const recovered=await read();await assertQuiet(recovered);
    assert.equal(row(recovered,1010).power,23);
    report.cases.push({name:kind,result:'PASS',retained:true,recovered:true,dom_glitches:0});
  }
  await page.clock.install();
  mode='ok';body=fail(payload(4),'growatt');const beforeRequests=requests;
  await page.clock.runFor(30001);
  await page.waitForFunction(()=>!fetchInFlight && allData.realtime.some(row=>row.fk_en===1010 && row.power===24));
  assert.equal(requests,beforeRequests+1,'one automatic read at the 30-second tick');
  const automaticFailure=await getState();await assertQuiet(automaticFailure);
  assert.equal(automaticFailure.data.pv_inverters[0].output_power_kw,12,'automatic source failure retains last success');
  body=payload(5);
  await page.clock.runFor(30000);
  await page.waitForFunction(()=>!fetchInFlight && allData.realtime.some(row=>row.fk_en===1010 && row.power===25));
  assert.equal(requests,beforeRequests+2,'a recovered source updates at the next 30-second tick');
  const automaticRecovery=await getState();await assertQuiet(automaticRecovery);
  assert.equal(automaticRecovery.data.pv_inverters[0].output_power_kw,14);
  report.cases.push({name:'30-second automatic failure and recovery',result:'PASS',retained:true,recovered:true,dom_glitches:0});
  await page.screenshot({path:path.join(output,'recovered.png'),fullPage:true});
  assert.deepEqual(report.page_errors,[]);
  assert.deepEqual(report.blocked_requests,[]);
  report.status='PASS';report.requests=requests;
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({status:report.status,cases:report.cases.length,requests,report:path.join(output,'report.json')}));
})().catch(error=>{
  report.status='FAIL';report.failure=error.stack;
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));
  console.error(error);process.exitCode=1;
}).finally(async()=>{
  if(browser)await browser.close();
  await new Promise(resolve=>server.close(resolve));
});
