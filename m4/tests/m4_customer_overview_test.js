'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'../../m4/web/M4优化调度控制台-线上版.html'),'utf8');
const pure=html.split('// Customer overview: plan interpretation only, never evidence of dispatch.')[1].split('// End customer plan interpretation.')[0];
const ctx=vm.createContext({});vm.runInContext(pure+';globalThis.summarize=customerPlanSummary;',ctx);
const start=Date.parse('2026-09-09T00:00:00+08:00');
function record(count=96){return {status:'completed',selected:{profile_id:'balanced',plan_version:'p1'},expires_at:'2026-09-09T00:05:00+08:00',candidates:[{profile_id:'balanced',plan_version:'p1',plan:Array.from({length:count},(_,i)=>({timestamp:new Date(start+i*900000).toISOString(),mode:i<4?'charge':i<8?'idle':'discharge',target_power_kw:i<4?20+i:i<8?0:10,expected_soc_pct:50}))}]};}
test('merges only adjacent modes, keeps variable power bounds and energy',()=>{
 const r=ctx.summarize(record(),start);assert.equal(r.segments.length,3);assert.equal(r.segments[0].count,4);assert.equal(r.segments[0].min,20);assert.equal(r.segments[0].max,23);assert.equal(r.charge,21.5);assert.equal(r.discharge,220);assert.equal(r.next.mode,'idle');
});
test('matches the current interval with an exclusive end and supports 95 points',()=>{
 for(const count of [95,96]){const r=record(count),s=ctx.summarize(r,start+4*900000);assert.equal(s.point.mode,'idle');assert.equal(s.index,4);assert.equal(s.next.mode,'discharge');assert.equal(s.end,start+count*900000);assert.equal(ctx.summarize(r,s.end).phase,'ended');assert.equal(ctx.summarize(r,s.end).point,null);assert.equal(ctx.summarize(r,s.end).next,null);}
});
test('future and expired snapshots do not become active execution claims',()=>{
 const before=ctx.summarize(record(),start-1);assert.equal(before.phase,'future');assert.equal(before.point,null);assert.equal(before.next.mode,'charge');const expired=ctx.summarize(record(),start+900000);assert.equal(expired.expired,true);assert.equal(expired.point.mode,'charge');assert.equal(expired.dispatch_status,undefined);
});
test('uses only the final selected plan, not another candidate or an incomplete run',()=>{
 const r=record();r.candidates.unshift({...r.candidates[0],profile_id:'cost',plan_version:'p2'});assert.equal(ctx.summarize(r,start).chosen.profile_id,'balanced');r.selected.plan_version='missing';assert.equal(ctx.summarize(r,start),null);r.status='blocked_inputs';assert.equal(ctx.summarize(r,start),null);
});
test('invalid timestamps, gaps, modes and numerical values fail without invented actions',()=>{
 for(const patch of [{timestamp:'invalid'},{timestamp:new Date(start+990000).toISOString()},{mode:'execute'},{target_power_kw:NaN},{target_power_kw:-1},{expected_soc_pct:null}]){const r=record();Object.assign(r.candidates[0].plan[1],patch);assert.equal(ctx.summarize(r,start),null);}
 assert.equal(ctx.summarize(record(94),start),null);
});
