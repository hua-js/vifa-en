const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('m4/web/M4优化调度控制台-线上版.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const extract=(a,b)=>script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a)));
const context=vm.createContext({assert:(v,m)=>assert.ok(v,m)});
vm.runInContext(extract('function comparisonControls(', 'function mapPlan('),context);
const fixed={station_id:'station-2',status:'ready',version:'fixed-controls',baseline_source:'fixed_configuration',baseline_version:'reference-v1'};
const inputs={sources:{controls:fixed},request:{source_versions:{ems_baseline:'reference-v1'}},baseline:{reference_version:'reference-v1'}};
assert.equal(context.comparisonControls(inputs,{version:'changed-execution'},'station-2'),fixed);
assert.throws(()=>context.comparisonControls(inputs,{},'station-1'));
assert.throws(()=>context.comparisonControls({...inputs,baseline:{reference_version:'other'}},{},'station-2'));
const live={version:'legacy'};
assert.equal(context.comparisonControls({sources:{controls:{}}},live,'station-2'),live);
console.log('Fixed baseline source selection: execution edits, station identity, reference version and legacy passed');

// Optional read-only production response replay, without browsers or API writes.
if(process.argv[2]){
 const root=process.argv[2],read=name=>JSON.parse(fs.readFileSync(root+'/'+name+'.json','utf8'));
 const input=read('daily-inputs'),job=read('daily-plan'),settings=read('settings');
 const ctx=vm.createContext({assert:(v,m)=>assert.ok(v,m),finite:Number.isFinite,
  requireStation:()=>({has_pv:true,policy:'peak_reserve'}),clock:i=>String(i),
  validatedImportLimit:(_,b)=>Math.min(b.demand_limit_kw,b.grid_import_limit_kw??Infinity)});
 vm.runInContext(extract('function validSeries(', 'function dailySnapshotNote('),ctx);
 const control=ctx.comparisonControls(input,{version:'live-execution'},input.station_id);
 const mapped=ctx.mapPlan(job.result.record,job.request,input.station_id,input.date,settings,control,job.baseline);
 assert.equal(mapped.points.length,96);
 assert.throws(()=>ctx.mapPlan(job.result.record,job.request,input.station_id,input.date,settings,{...control,version:'changed-safety'},job.baseline));
 const baselineRecord={station_id:input.station_id,input_summary:{configuration_version:input.configuration_version},daily_comparison:input.comparison};
 assert.equal(ctx.mapPlan(baselineRecord,input.request,input.station_id,input.date,settings,control,input.baseline).points.length,96);
 console.log('Saved production responses: optimized plan and EMS fallback both map 96 points; safety changes still rejected');
}
