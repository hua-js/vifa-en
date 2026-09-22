'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/M4优化调度控制台-线上版.html'), 'utf8');
const script = html.match(/<script>\s*('use strict';[\s\S]*?)<\/script>/)[1];
function fixture(search = '', full = false) {
  const elements = new Map(), calls = [], storage = new Map();
  function element() { return {children: [], value: '', dataset: {}, style: {}, classList: {toggle(){}}, addEventListener(){}, disabled: true, textContent: '', append(...nodes) { this.children.push(...nodes); }, replaceChildren(...nodes) {this.children = nodes;}, setAttribute() {}}; }
  const document = {getElementById(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);}, documentElement: element(), addEventListener(){}, createElement: element, querySelector() {return document.getElementById('toggle');}, querySelectorAll() {return [];}};
  const sandbox = {document, location: {search, protocol: 'http:', origin: 'http://localhost', hostname: 'localhost'}, URL, URLSearchParams, Headers, AbortSignal, console, setTimeout:()=>0, clearTimeout(){}, setInterval:()=>0, localStorage: {getItem(){},setItem(){}}, sessionStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v)}, fetch: async (url, options) => {calls.push({url: String(url), options});return {ok:true, status:200, json:async()=>sandbox.response};}};
  const ctx = vm.createContext(sandbox);
  vm.runInContext(full ? script.slice(0,script.lastIndexOf('initializeCurrent().then(')) : script.slice(0, script.indexOf('const header=')) + '\nfunction assert(c,m){if(!c)throw Error(m)}',ctx);
  return {ctx, calls, elements, storage, run: code => vm.runInContext(code, ctx)};
}
const project = () => ({schema_version:1, project_id:'factory-a', configuration_version:'cfg-a', stations:[{id:'station-3', source_code:'ES03', name:'第三站 <img>', has_pv:true, policy:'cost_first', grid_import_limit_kw:720}]});
test('full script startup waits for metadata before any business reads', async () => {
 new vm.Script(script);
 const f=fixture('?station=station-3',true);
 let release;
 f.ctx.fetch=async(url)=>{f.calls.push(String(url));if(String(url).endsWith('/project'))return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>project()});});return {ok:true,status:200,json:async()=>({station_id:'station-3',status:'empty'})};};
 const startup=f.run('initializeCurrent()');
 await f.run('loadCurrent()');await f.run('loadRecords()');await f.run('loadRecentLogs()');await f.run('recalculatePlan()');
 assert.deepEqual(f.calls,['http://localhost/m4-api/project']);
 release();await startup;
 assert.equal(f.calls.length,5);
 assert.ok(f.calls.slice(1).every(url=>url.includes('/stations/station-3/')));
});
test('history reads every configured station, including a third station', async () => {
 const f=fixture('',true);const p=project();p.stations=[1,2,3].map(n=>({...p.stations[0],id:'station-'+n,source_code:'ES0'+n}));f.ctx.response=p;await f.run('loadProject()');
 const stations=new Set();f.ctx.fetch=async(url)=>{const u=new URL(url),station=u.pathname.split('/')[3];stations.add(station);return {ok:true,status:200,json:async()=>u.pathname.endsWith('/daily-plan')?{station_id:station,status:'empty'}:{station_id:station,schema_version:'m4-decision-history-v1',dispatch_status:'not_dispatched',items:[],next_offset:null}};};
 await f.run('loadRecords()');assert.deepEqual([...stations].sort(),['station-1','station-2','station-3']);
 assert.equal(f.elements.get('record-station').children.length,4);
});
test('metadata is required before API reads; third station is loaded from project endpoint', async () => {
  const f = fixture('?station=station-3');
  await assert.rejects(f.run("api('station-3','daily-plan')"));
  assert.equal(f.calls.length,0);
  f.ctx.response=project();
  assert.equal(f.run('typeof loadProject'), 'function');
  await f.run('loadProject()');
  assert.equal(f.calls[0].url,'http://localhost/m4-api/project');
  assert.equal(f.run("stationName('station-3')"),'第三站 <img>');
  assert.equal(f.elements.get('station').value,'station-3');
  assert.equal(f.elements.get('station').children[0].textContent,'第三站 <img>');
  f.ctx.response={station_id:'station-3'};
  await f.run("api('station-3','daily-plan')");
  assert.equal(f.calls[1].url,'http://localhost/m4-api/stations/station-3/daily-plan');
});

test('station names accept surrounding whitespace and the 100-character boundary unchanged', async () => {
 for(const name of [' 电站 3', '电站 3 ', ' '+ '站'.repeat(98)+' ']){
  const f=fixture(),p=project();p.stations[0].name=name;f.ctx.response=p;
  await f.run('loadProject()');
  assert.equal(f.run("stationName('station-3')"),name);
  assert.equal(f.elements.get('station').children[0].textContent,name);
  f.ctx.response={station_id:'station-3'};
  await f.run("api('station-3','daily-plan')");
  assert.equal(f.calls.length,2);
 }
});
test('invalid names and whitespace in identifiers or configuration versions fail closed', async () => {
 const mutations=[...[null,42,'',' \t ','站'.repeat(101)].map(name=>p=>p.stations[0].name=name),
  p=>p.project_id=' factory-a',p=>p.stations[0].id='station-3 ',
  p=>p.stations[0].source_code=' ES03',p=>p.configuration_version=' cfg-a '];
 for(const mutate of mutations){
  const f=fixture(),p=project();mutate(p);f.ctx.response=p;
  await assert.rejects(f.run('loadProject()'));
  await assert.rejects(f.run("api('station-3','daily-plan')"));
  assert.equal(f.calls.length,1);
 }
});

test('unknown explicit station and unavailable aliases fail closed', async () => {
 for(const station of ['unknown','s1','s2','']){
  const f=fixture('?station='+station);f.ctx.response=project();
  await assert.rejects(f.run('loadProject()'));
  await assert.rejects(f.run("api('station-3','daily-plan')"));assert.equal(f.calls.length,1);
 }
 const f=fixture('?station=s1');const p=project();p.stations[0].id='station-1';f.ctx.response=p;
 await f.run('loadProject()');assert.equal(f.elements.get('station').value,'station-1');
});
test('invalid metadata is rejected without business requests', async () => {
 const mutations=[p=>p.schema_version='1',p=>p.stations=[],p=>p.configuration_version='',p=>p.stations.push({...p.stations[0]}),p=>p.stations[0].id='../x',p=>delete p.stations[0].has_pv,p=>p.stations[0].has_pv=1,p=>p.stations[0].policy='other',p=>p.stations[0].grid_import_limit_kw='720',p=>p.stations[0].grid_import_limit_kw=-1,p=>delete p.stations[0].source_code];
 for(const mutate of mutations){const f=fixture();const p=project();mutate(p);f.ctx.response=p;await assert.rejects(f.run('loadProject()'));await assert.rejects(f.run("api('station-3','daily-plan')"));assert.equal(f.calls.length,1);}
 const f=fixture();f.ctx.fetch=async()=>{throw Error('offline')};await assert.rejects(f.run('loadProject()'));await assert.rejects(f.run("api('station-3','daily-plan')"));
});
test('cache keys and station selection are partitioned by project and configuration', async () => {
 const f=fixture();f.ctx.response=project();await f.run('loadProject()');
 vm.runInContext(script.slice(script.indexOf('const viewCache='),script.indexOf('function schedulePlanStatusPoll(')),f.ctx);
 const first=f.run("viewKey('station-3','2026-09-16')");
 f.ctx.response={...project(),project_id:'factory-b'};await f.run('loadProject()');
 assert.notEqual(f.run("viewKey('station-3','2026-09-16')"),first);
 const second=f.run("viewKey('station-3','2026-09-16')");
 f.ctx.response={...project(),project_id:'factory-b',configuration_version:'cfg-b'};await f.run('loadProject()');
 assert.notEqual(f.run("viewKey('station-3','2026-09-16')"),second);
});
function planFixture(station) {
 const date='2026-09-16',start=Date.parse(date+'T00:00:00+08:00');
 const points=Array.from({length:96},(_,i)=>({timestamp:new Date(start+i*900000).toISOString(),mode:'idle',target_power_kw:0,grid_import_kw:100,expected_soc_pct:50,load_forecast_kw:100,pv_forecast_kw:0,buy_price_per_kwh:1,tariff_period:'flat'}));
 const candidate={plan:points,plan_version:'v1',metrics:{energy_cost:100,cycle_cost:0}};
 const request={station_id:station,plan_start_at:points[0].timestamp,points,source_versions:{controls:'controls',baseline_policy:'ems-demand-soc-duration-v7-pv-export-idle'},capability:{energy_capacity_kwh:100,max_charge_kw:10,max_discharge_kw:10,initial_soc_pct:50},constraints:{soc_min_pct:10,soc_max_pct:90,grid_import_limit_kw:720,demand_limit_kw:500}};
 const comparison={schema_version:'m4-daily-comparison-v1',station_id:station,usage:'preview_only',dispatch_status:'not_dispatched',status:'ems',date,start_at:points[0].timestamp,end_at:new Date(start+86400000).toISOString(),controls_version:'controls',baseline_policy_version:'ems-demand-soc-duration-v7-pv-export-idle',recommended:candidate,baseline:candidate,baseline_cost_yuan:100,recommended_source:'ems'};
 return [{station_id:station,input_summary:{configuration_version:'settings'},daily_comparison:comparison},request,station,date,{version:'settings'},{version:'controls'},{station_id:station,controls_version:'controls',controller_version:'ems-demand-soc-duration-v7-pv-export-idle',basis:'ems_rule_simulation',schedule:[{start_time:'00:00',end_time:'24:00',repeat:'daily',mode:'charge',power_kw:0}]}];
}
test('PV validation and reserve policy follow metadata independently of station IDs', async () => {
 const f=fixture('',true),p=project();p.stations[0].has_pv=false;f.ctx.response=p;await f.run('loadProject()');
 f.ctx.args=planFixture('station-3');assert.equal(f.run('mapPlan(...args).station'),'station-3');
 p.stations[0].has_pv=true;await f.run('loadProject()');assert.throws(()=>f.run('mapPlan(...args)'),/光伏/);
 f.ctx.args[1].source_versions.pv_gap_policy='pv-night-zero-20-07-v1';assert.equal(f.run('mapPlan(...args).station'),'station-3');
 p.stations[0].has_pv=false;p.stations[0].policy='peak_reserve';await f.run('loadProject()');assert.throws(()=>f.run('mapPlan(...args)'),/策略/);
 f.ctx.args[0].daily_comparison.daily_policy_version='m4-daily-peak-reserve-v3';f.ctx.args[0].daily_comparison.terminal_energy_rule='not_less_than_baseline';f.ctx.args[1].source_versions.daily_policy='m4-daily-peak-reserve-v3';f.ctx.args[1].peak_reserve_policy={version:'peak-reserve-v3'};
 assert.equal(f.run('mapPlan(...args).station'),'station-3');
});
test('soft reserve v4 is accepted only with matching daily and request versions', async () => {
 const f=fixture('',true),p=project();p.stations[0].has_pv=false;p.stations[0].policy='peak_reserve';f.ctx.response=p;await f.run('loadProject()');
 f.ctx.args=planFixture('station-3');
 f.ctx.args[0].daily_comparison.daily_policy_version='m4-daily-peak-reserve-v4';
 f.ctx.args[0].daily_comparison.terminal_energy_rule='not_less_than_baseline';
 f.ctx.args[1].source_versions.daily_policy='m4-daily-peak-reserve-v4';
 f.ctx.args[1].peak_reserve_policy={version:'peak-reserve-v4'};
 assert.equal(f.run('mapPlan(...args).station'),'station-3');
 f.ctx.args[1].peak_reserve_policy.version='peak-reserve-v3';
 assert.throws(()=>f.run('mapPlan(...args)'),/策略/);
});
test('cached views cannot cross project/configuration boundaries and query wins over saved station', async () => {
 const f=fixture('',true);f.ctx.response=project();await f.run('loadProject()');
 f.run("rememberView('station-3',today())");assert.equal(f.run("restoreView('station-3',today())"),true);
 f.ctx.response={...project(),configuration_version:'changed'};await f.run('loadProject()');assert.equal(f.run("restoreView('station-3',today())"),false);
 f.ctx.response={...project(),project_id:'other'};await f.run('loadProject()');assert.equal(f.run("restoreView('station-3',today())"),false);
 const q=fixture('?station=station-3'),p=project();p.stations.unshift({...p.stations[0],id:'station-1',source_code:'ES01'});q.ctx.response=p;
 q.storage.set('m4-selected-station:'+JSON.stringify([p.project_id,p.configuration_version]),'station-1');await q.run('loadProject()');assert.equal(q.elements.get('station').value,'station-3');
});
test('metadata accepts zero import limits and a station named all', async () => {
 const f=fixture();const p=project();p.stations[0].id='all';p.stations[0].grid_import_limit_kw=0;f.ctx.response=p;
 await f.run('loadProject()');assert.equal(f.run("validatedImportLimit('all',{grid_import_limit_kw:0})"),0);
 assert.notEqual(f.elements.get('record-station').children[0].value,'all');
});
test('cost-first import hard limit uses metadata and rejects mismatched request limits', async () => {
 const f=fixture();f.ctx.response=project();await f.run('loadProject()');
 assert.equal(f.run('typeof validatedImportLimit'),'function');
 assert.equal(f.run("validatedImportLimit('station-3',{grid_import_limit_kw:720,demand_limit_kw:500})"),720);
 assert.throws(()=>f.run("validatedImportLimit('station-3',{grid_import_limit_kw:550,demand_limit_kw:500})"));
});

test('operating-floor plans validate inventory-adjusted savings without requiring EMS terminal SOC', async () => {
 const f=fixture('',true),p=project();p.stations[0].has_pv=false;p.stations[0].policy='peak_reserve';f.ctx.response=p;await f.run('loadProject()');
 const args=planFixture('station-3'),[record,request]=args,c=record.daily_comparison;
 request.constraints.soc_min_pct=1;request.constraints.preferred_soc_min_pct=2;
 request.capability.charge_efficiency=1;
 request.points.forEach(x=>{x.tariff_period='gu';x.buy_price_per_kwh=.3;});
 request.source_versions.daily_policy='m4-daily-operating-floor-v1';request.source_versions.terminal_policy='daily-operating-floor-v1';request.source_versions.terminal_inventory_price='0.3';
 request.peak_reserve_policy={version:'peak-reserve-v4',terminal_soc_min_pct:2};
 c.baseline=structuredClone(c.baseline);c.baseline.metrics.energy_cost=300;
 c.recommended=structuredClone(c.recommended);c.recommended.plan.at(-1).expected_soc_pct=2;c.recommended.profile_id='cost';
 record.selected={profile_id:'cost',plan_version:'v1'};
 Object.assign(c,{status:'optimized',recommended_source:'optimized',candidate_plan_version:'v1',daily_policy_version:'m4-daily-operating-floor-v1',terminal_energy_rule:'operating_floor_inventory_adjusted',revenue_gate_version:'daily-net-savings-100-v1',baseline_cost_yuan:300,optimized_cost_yuan:100,terminal_inventory_adjustment_yuan:14.4,savings_yuan:185.6,net_savings_yuan:185.6});
 f.ctx.args=args;
 assert.ok(Math.abs(f.run('mapPlan(...args).saving')-185.6)<1e-5);
 c.net_savings_yuan=200;assert.throws(()=>f.run('mapPlan(...args)'),/净节省/);c.net_savings_yuan=185.6;
 c.terminal_inventory_adjustment_yuan=0;assert.throws(()=>f.run('mapPlan(...args)'),/库存/);c.terminal_inventory_adjustment_yuan=14.4;
 request.peak_reserve_policy.terminal_soc_min_pct=1;assert.throws(()=>f.run('mapPlan(...args)'),/目标/);request.peak_reserve_policy.terminal_soc_min_pct=2;
 c.terminal_energy_rule='not_less_than_baseline';assert.throws(()=>f.run('mapPlan(...args)'),/策略/);
});

test('daily-only chart never overlays saved rolling points', () => {
 const f=fixture('',true);
 f.run("plan={points:[{power:50,soc:40}]};rollingAdvice={status:'completed',result:{plan:[{target_power_kw:600}]}};");
 assert.equal(f.run('displayedPlanPoints()[0].power'),50);
 assert.equal(f.run('currentRollingPayload()'),null);
});

test('confirmed daily snapshot remains readable when live inputs fail', async () => {
 const f=fixture('',true);f.ctx.response=project();await f.run('loadProject()');
 const saved={request:{source_versions:{configuration:'saved-config',controls:'saved-controls'}}};
 f.ctx.fetch=async(url)=>{
  if(String(url).endsWith('/daily-plan'))return {ok:true,status:200,json:async()=>({station_id:'station-3',active_daily:saved})};
  throw Error('offline');
 };
 const bundle=await f.run("readCurrent('station-3','2026-09-22')");
 assert.equal(bundle[1].active_daily,saved);
 assert.equal(bundle[0].can_compare,false);
 assert.equal(bundle[2].version,'saved-config');
 assert.equal(bundle[3].version,'saved-controls');
});
