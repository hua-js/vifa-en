// Display confirmed table rows, never synthesize dispatch rows from a candidate.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('m4/web/M4优化调度控制台-线上版.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const extract=(a,b)=>script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a)));
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:''});return nodes.get(id);};
$('station').value='station-2';$('plan-date').value='2026-09-22';
const stamp=slot=>new Date(Date.parse('2026-09-22T00:00:00+08:00')+slot*900000).toISOString();
const row=(id,start,end,type,explain)=>({record_id:id,start_at:stamp(start),end_at:stamp(end),type,kw:600,explain});
const first={status:'plan_table_readback_verified',effective_at:stamp(1),schedule:[row(1,1,5,'discharge','高价时段放电供负荷，减少高价购电。')]};
const second={status:'plan_table_readback_verified',effective_at:stamp(3),schedule:[row(2,3,4,'charge','利用预计光伏余电充电，供后续用电。')]};
const plan={station:'station-2',date:'2026-09-22',record:{run_id:'new-unsent-candidate'},points:[{power:999}]};
class Clock extends Date{static now(){return Date.parse(stamp(3.5));}}
const state={station_id:'station-2',date:'2026-09-22',status:'completed',active_plan:second,versions:[first,second]};
const ctx=vm.createContext({$,Date:Clock,Map,Set,Infinity,plan,dailyDispatch:state,stationName:String,esc:s=>String(s).replaceAll('<','&lt;'),clock:String,emsStateBadge:String,renderStrategyExecutionLogs(){},renderActionTimeline(){}});
vm.runInContext(extract('function currentDailyDispatch(){','function renderDailyDispatch(){'),ctx);
vm.runInContext(extract('function renderScheduleTable(){','function renderActualOverview(){'),ctx);
const render=()=>{vm.runInContext('renderScheduleTable()',ctx);return $('schedule-rows').innerHTML;};
let content=render();
assert.match(content,/1–3<\/td><td>放电/);
assert.match(content,/3–4<\/td><td>充电/);
assert.match(content,/高价时段放电供负荷，减少高价购电。/);
assert.match(content,/table_ended/);assert.match(content,/table_active/);
assert.equal((content.match(/<tr>/g)||[]).length,2);
assert.doesNotMatch(content,/待机|999/);
state.status='blocked';state.pending_effective_at=stamp(3);
assert.match(render(),/table_unconfirmed/);
state.status='completed';
// Carrying the same physical row keeps its original start and avoids duplicates.
second.schedule=[row(1,1,5,'discharge','原记录承接说明')];
content=render();assert.equal((content.match(/<tr>/g)||[]).length,1);assert.match(content,/1–5/);
second.schedule[0].explain='<script>untrusted</script>';
assert.match(render(),/&lt;script>/);
state.versions=[];state.active_plan=null;
assert.match(render(),/今日暂无已下发的充放计划/);
$('station').value='station-1';assert.equal(vm.runInContext('currentDailyDispatch()',ctx),null);
console.log('Confirmed schedule rows preserve boundaries, explanations, ownership and replacement state.');
