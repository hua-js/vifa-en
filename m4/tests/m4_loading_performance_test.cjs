'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/M4优化调度控制台-线上版.html'), 'utf8');
const script = html.match(/<script>\s*('use strict';[\s\S]*?)<\/script>/)[1];
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const {snapshot} = require('./m4_loading_test_fixture.cjs');
function fixture() {
  const elements = new Map(), calls = [], rendered = [];
  function element() { return {children:[], value:'', dataset:{}, style:{}, hidden:false, disabled:true,
    textContent:'', innerHTML:'', classList:{toggle(){}}, addEventListener(){}, setAttribute(){},
    append(...nodes){this.children.push(...nodes);}, replaceChildren(...nodes){this.children=nodes;}}; }
  const document = {getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
    documentElement:element(), addEventListener(){}, createElement:element,
    querySelector(selector){return document.getElementById(selector);}, querySelectorAll(){return [];}, hidden:false};
  const sandbox = {document, location:{search:'', protocol:'http:', origin:'http://localhost', hostname:'localhost'},
    URL, URLSearchParams, Headers, AbortSignal, console, setTimeout:()=>0, clearTimeout(){}, setInterval:()=>0,
    localStorage:{getItem(){},setItem(){}}, sessionStorage:{getItem(){},setItem(){}}, rendered,
    fetch:async(url,options)=>{
      const endpoint=new URL(url).pathname.split('/').at(-1);calls.push({endpoint, method:options.method||'GET'});
      const body=endpoint==='project'?{schema_version:1,project_id:'fixture',configuration_version:'fixture-config',
        stations:[1,2].map(n=>({id:'station-'+n,source_code:'ES0'+n,name:'电站 '+n,has_pv:false,policy:'cost_first',grid_import_limit_kw:720}))}
        :await sandbox.read(endpoint,new URL(url).pathname.split('/')[3]);
      return {ok:true,status:200,json:async()=>body};
    }};
  const ctx = vm.createContext(sandbox);
  vm.runInContext(script.slice(0,script.lastIndexOf('initializeCurrent().then(')),ctx);
  // Keep the actual loading, identity/version validation and button guards;
  // replace unrelated chart/DOM drawing, not the read pipeline under test.
  vm.runInContext("renderRollingAdvice=()=>{};clearReferenceOverview=()=>{};renderPlan=p=>{plan=p;rendered.push(p);};",ctx);
  const run=code=>vm.runInContext(code,ctx);
  const date=run('today()'), job=snapshot('station-2',date);
  job.active_daily=snapshot('station-2',date);
  job.daily_dispatch={station_id:'station-2',date,status:'completed',confirmed_daily_run_id:job.run_id};
  job.ems_table_write={enabled:true};
  sandbox.read=async(endpoint,station)=>endpoint==='daily-plan'?job:endpoint==='daily-inputs'
    ?{schema_version:'m4-daily-inputs-v1',station_id:station,date,configuration_version:'live-config',
      usage:'retrospective_comparison_only',dispatch_status:'not_dispatched',can_compare:false,sources:{}}
    :{station_id:station,version:endpoint==='settings'?'live-config':'live-controls'};
  return {ctx, sandbox, elements, calls, rendered, run, date, job};
}
test('confirmed plan renders before slow daily inputs without enabling recalculation', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:read(endpoint,station);
  const loading=f.run('loadCurrent()');
  try {
    await settle();
    assert.equal(f.rendered.length,1,'an already confirmed plan must not wait for daily-inputs');
    assert.equal(f.rendered[0].station,'station-2');
    assert.equal(f.elements.get('loading-mask').hidden,true);
    assert.equal(f.run('context'),null,'display-only snapshot must not become fresh live context');
    assert.equal(f.elements.get('recalculate').disabled,true);
  } finally {
    slow.resolve(await read('daily-inputs','station-2'));await loading;
  }
  assert.equal(f.rendered.length,2,'live completion still runs the original validated render');
  assert.equal(f.run('context.settings.version'),'live-config');
  assert.ok(f.calls.every(c=>c.method==='GET'),'loading must never write or start a task');
});
test('sidebar logs reuse the completed main read without fetching inputs again', async () => {
  const f=fixture(), read=f.sandbox.read;
  f.sandbox.read=(endpoint,station)=>endpoint==='decision-history'
    ?{station_id:station,items:[]}:read(endpoint,station);
  await f.run('loadProject()');await f.run('loadCurrent()');await f.run('loadRecentLogs()');
  for(const endpoint of ['daily-inputs','daily-plan','settings','control-sources']) {
    assert.equal(f.calls.filter(c=>c.endpoint===endpoint).length,1,endpoint+' must not be fetched again for logs');
  }
  assert.equal(f.calls.filter(c=>c.endpoint==='decision-history').length,1);
  assert.match(f.elements.get('recent-logs').innerHTML,/fixture-run/);
});
test('retained plan cannot be dispatched while a new read is still validating', async () => {
  const f=fixture(), slowPlan=deferred(), slowInputs=deferred(), read=f.sandbox.read;
  delete f.job.active_daily;f.job.daily_dispatch.confirmed_daily_run_id='older-confirmed-run';
  await f.run('loadProject()');await f.run('loadCurrent()');
  assert.equal(f.elements.get('dispatch-daily').disabled,false,'replacement is initially available');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-plan'?slowPlan.promise:endpoint==='daily-inputs'?slowInputs.promise:read(endpoint,station);
  const loading=f.run('loadCurrent()');
  try {
    assert.equal(f.elements.get('dispatch-daily').disabled,true,'disable writes synchronously when fresh context is revoked');
  } finally {
    slowPlan.resolve(f.job);slowInputs.resolve(await read('daily-inputs','station-2'));await loading;
  }
});
test('sidebar can finish while inputs are pending and shares the in-flight plan read', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;let finished=false;
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:endpoint==='decision-history'?{station_id:station,items:[]}:read(endpoint,station);
  const loading=f.run('loadCurrent()').then(()=>{finished=true;});
  try {
    await f.run('loadRecentLogs()');
    assert.equal(finished,false,'logs must not depend on daily-inputs completing');
    assert.match(f.elements.get('recent-logs').innerHTML,/fixture-run/);
    for(const endpoint of ['daily-inputs','daily-plan','settings','control-sources'])assert.equal(f.calls.filter(c=>c.endpoint===endpoint).length,1);
  } finally {slow.resolve(await read('daily-inputs','station-2'));await loading;}
});
test('unconfirmed or failed plans keep the loading gate until inputs finish', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;
  delete f.job.active_daily;f.job.status='failed';f.job.result=null;
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:read(endpoint,station);
  const loading=f.run('loadCurrent()');
  try {await settle();assert.equal(f.rendered.length,0);assert.equal(f.elements.get('loading-mask').hidden,false);}
  finally {slow.resolve(await read('daily-inputs','station-2'));await loading;}
});
test('an invalid saved date is never shown by the fast path', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;
  f.job.active_daily.result.record.daily_comparison.date='2020-01-01';
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:read(endpoint,station);
  const loading=f.run('loadCurrent()');
  try {await settle();assert.equal(f.rendered.length,0);assert.equal(f.elements.get('loading-mask').hidden,false);}
  finally {slow.resolve(await read('daily-inputs','station-2'));await loading;}
  assert.equal(f.rendered.length,0);assert.equal(f.run('plan'),null);
  assert.equal(f.run('header.textContent'),'数据暂不可用');
});
test('late responses from a previous station cannot overwrite the current station', async () => {
  const f=fixture(), slowPlan=deferred(), slowInputs=deferred(), read=f.sandbox.read;
  const old=snapshot('station-1',f.date);old.active_daily=snapshot('station-1',f.date);
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>station==='station-1'&&endpoint==='daily-plan'?slowPlan.promise
    :station==='station-1'&&endpoint==='daily-inputs'?slowInputs.promise:read(endpoint,station);
  f.run("$('station').value='station-1'");const previous=f.run('loadCurrent()');
  f.run("$('station').value='station-2'");await f.run('loadCurrent()');
  slowPlan.resolve(old);slowInputs.resolve(await read('daily-inputs','station-1'));await previous;
  assert.ok(f.rendered.every(p=>p.station==='station-2'));
  assert.equal(f.run('context.station'),'station-2');assert.equal(f.run('plan.station'),'station-2');
});
test('reentering the same pending station read keeps one set of network requests', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;
  await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:read(endpoint,station);
  const first=f.run('loadCurrent()');await settle();const second=f.run('loadCurrent()');await settle();
  slow.resolve(await read('daily-inputs','station-2'));await Promise.all([first,second]);
  for(const endpoint of ['daily-inputs','daily-plan','settings','control-sources'])assert.equal(f.calls.filter(c=>c.endpoint===endpoint).length,1);
  assert.equal(f.run('context.station'),'station-2');
});
test('failed live input refresh preserves the verified saved plan and marks live data unavailable', async () => {
  const f=fixture(), read=f.sandbox.read;await f.run('loadProject()');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?Promise.reject(Error('live source offline')):read(endpoint,station);
  await f.run('loadCurrent()');assert.equal(f.run('plan.record.run_id'),'fixture-run');
  assert.equal(f.run('context.inputs.can_compare'),false);assert.equal(f.elements.get('loading-mask').hidden,true);
});
test('previewing a new candidate does not fast-render the old confirmed snapshot', async () => {
  const f=fixture(), slow=deferred(), read=f.sandbox.read;await f.run('loadProject()');
  f.run('previewDaily=true');
  f.sandbox.read=(endpoint,station)=>endpoint==='daily-inputs'?slow.promise:read(endpoint,station);
  const loading=f.run('loadCurrent()');
  try {await settle();assert.equal(f.rendered.length,0);}
  finally {slow.resolve(await read('daily-inputs','station-2'));await loading;}
});
