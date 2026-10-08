'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../web/M4优化调度控制台-线上版.html'),'utf8');
function extract(name){const start=html.indexOf('function '+name+'('),end=html.indexOf('\n}',start);assert.ok(start>=0&&end>start);return html.slice(start,end+2);}
function fixture(version='ems-demand-soc-duration-v9-initial-soc'){
 const date='2026-10-08',start=Date.parse(date+'T00:00:00+08:00');
 const points=Array.from({length:96},(_,i)=>({timestamp:new Date(start+i*900000).toISOString(),load_forecast_kw:100,pv_forecast_kw:0,buy_price_per_kwh:1,tariff_period:'ping'}));
 const plan=points.map(p=>({timestamp:p.timestamp,mode:'idle',target_power_kw:0,grid_import_kw:100,grid_export_kw:0,expected_soc_pct:50}));
 const baseline={plan_version:'ems',plan,metrics:{energy_cost:2400,cycle_cost:0}};
 const comparison={schema_version:'m4-daily-comparison-v1',station_id:'station-1',usage:'preview_only',dispatch_status:'not_dispatched',status:'ems',date,start_at:points[0].timestamp,end_at:new Date(start+86400000).toISOString(),controls_version:'ctrl',baseline_policy_version:version,baseline,recommended:baseline,recommended_source:'ems',baseline_cost_yuan:2400};
 const request={station_id:'station-1',plan_start_at:points[0].timestamp,source_versions:{baseline_policy:version,controls:'ctrl',ems_baseline:'ref'},points,capability:{energy_capacity_kwh:1000,max_charge_kw:100,max_discharge_kw:100,initial_soc_pct:50},constraints:{soc_min_pct:2,soc_max_pct:98}};
 return [{station_id:'station-1',input_summary:{configuration_version:'cfg'},daily_comparison:comparison},request,'station-1',date,{version:'cfg'},{version:'ctrl',baseline_source:'fixed_configuration',baseline_version:'ref'},{controller_version:version,reference_version:'ref'}];
}
const ctx=vm.createContext({assert:(ok,msg)=>assert.ok(ok,msg),finite:Number.isFinite,validSeries:a=>a.length===96,requireStation:()=>({has_pv:false,policy:'cost_first'}),emsSchedulePowers:()=>Array(96).fill(0),storageDisplay:()=>({simulatedPower:0}),clock:String,peakWindowStats:()=>({})});
vm.runInContext(extract('mapPlan'),ctx);
test('v9 fixed EMS baseline renders and v8 history remains readable',()=>{
 for(const version of ['ems-demand-soc-duration-v9-initial-soc','ems-demand-soc-duration-v8-reference-soc'])assert.equal(ctx.mapPlan(...fixture(version)).points.length,96);
});
test('mixed or unsupported baseline versions remain rejected',()=>{
 const args=fixture();args[6].controller_version='ems-demand-soc-duration-v8-reference-soc';
 assert.throws(()=>ctx.mapPlan(...args),/基线版本/);
 assert.throws(()=>ctx.mapPlan(...fixture('unsupported')),/基线版本/);
});
test('optimized plans below the retained 100 yuan threshold remain rejected',()=>{
 const args=fixture(),c=args[0].daily_comparison;
 c.status='optimized';c.revenue_gate_version='daily-net-savings-100-v1';
 c.recommended=structuredClone(c.baseline);c.recommended.metrics.energy_cost=2301;
 assert.throws(()=>ctx.mapPlan(...args),/100元/);
});
test('saved planning snapshot survives unavailable live input services',async()=>{
 const job={status:'completed',request:{source_versions:{load_admission_policy:'complete-forecast-v1',configuration:'cfg',controls:'ctrl'}},result:{record:{daily_comparison:{recommended:{plan:[1]}}}}};
 const c=vm.createContext({pendingReads:new Map(),viewKey:(s,d)=>s+d,api:async(s,path)=>{if(path==='daily-plan')return job;throw Error('upstream unavailable');}});
 vm.runInContext(extract('savedPlanningSnapshot')+'\n'+extract('readCurrent'),c);
 const bundle=await c.readCurrent('station-1','2026-10-08');
 assert.equal(bundle[1],job);assert.equal(bundle[0].can_compare,false);
 job.status='failed';
 await assert.rejects(c.readCurrent('station-1','2026-10-09'),/upstream unavailable/);
});
