// The former rolling-refresh surface now retains daily forecasts and table history.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('m4/web/M4优化调度控制台-线上版.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const extract=(a,b)=>script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a)));
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:''});return nodes.get(id);};
$('station').value='station-2';$('plan-date').value='2026-09-22';
const stamp=slot=>new Date(Date.parse('2026-09-22T00:00:00+08:00')+slot*900000).toISOString();
const point=(slot,power,mode='discharge')=>({timestamp:stamp(slot),mode,target_power_kw:power});
const first={status:'plan_table_readback_verified',effective_at:stamp(1),daily_run_id:'first',
 plan:[point(1,20),point(2,25),point(3,30)],dispatch_plan:[point(1,20),point(2,25),point(3,30)]};
const second={status:'plan_table_readback_verified',effective_at:stamp(3),daily_run_id:'second',
 plan:[point(3,40,'charge')],dispatch_plan:[point(3,40,'charge')]};
const plan={station:'station-2',date:'2026-09-22',record:{run_id:'second'},
 points:[0,1,2,3].map(i=>({simulatedPower:-40,power:-40,soc:50}))};
const ctx=vm.createContext({$,Date,Map,Infinity,plan,rollingAdvice:{result:{plan:[point(2,999)]}},
 dailyDispatch:{station_id:'station-2',date:'2026-09-22',confirmed_daily_run_id:'second',active_plan:second,versions:[first,second]},
 stationName:String,fmt:String,esc:String,clock:slot=>String(slot),renderStrategyExecutionLogs(){},renderActionTimeline(){}});
vm.runInContext(extract('function currentRollingPayload(){','function refreshChartContext(){'),ctx);
assert.equal(vm.runInContext('currentRollingPayload()',ctx),null);
assert.equal(vm.runInContext('displayedPlanPoints()[2].power',ctx),-40);
vm.runInContext(extract('function currentDailyDispatch(){','function renderDailyDispatch(){'),ctx);
vm.runInContext(extract('function renderScheduleTable(){','function renderActualOverview(){'),ctx);
vm.runInContext('renderScheduleTable()',ctx);
assert.match($('schedule-rows').innerHTML,/<td>1–2<\/td><td>放电<\/td><td class="number">20<\/td><td class="number">600<\/td><td>下发成功/);
assert.match($('schedule-rows').innerHTML,/<td>3–4<\/td><td>充电<\/td><td class="number">40<\/td><td class="number">600<\/td><td>下发成功/);
$('station').value='station-1';
assert.equal(vm.runInContext('currentDailyDispatch()',ctx),null);
console.log('Daily forecasts remain separate from rolling evidence; confirmed history follows cutover versions.');
$('station').value='station-2';
vm.runInContext("dailyDispatch.status='blocked';dailyDispatch.pending_effective_at='2026-09-22T00:45:00+08:00';renderScheduleTable()",ctx);
assert.match($('schedule-rows').innerHTML,/<td>3–4<\/td>[\s\S]*?<td class="number">待核对<\/td><td>写表待核对/);
assert.match($('schedule-rows').innerHTML,/<td>1–2<\/td><td>放电<\/td><td class="number">20<\/td><td class="number">600<\/td><td>下发成功/);

// Confirmed table intervals remain successful while previewing another daily version.
vm.runInContext("dailyDispatch.status='completed';plan.record.run_id='new-candidate';dailyDispatch.versions[1].schedule=[{start_at:'2026-09-22T00:45:00+08:00',end_at:'2026-09-22T01:00:00+08:00',type:'charge',kw:600}];dailyDispatch.versions[1].dispatch_plan=[];renderScheduleTable()",ctx);
assert.match($('schedule-rows').innerHTML,/<td>3–4<\/td><td>充电<\/td><td class="number">40<\/td><td class="number">600<\/td><td>下发成功/);
assert.match($('schedule-rows').innerHTML,/<td>0–1<\/td>[\s\S]*?<td>未下发/);
