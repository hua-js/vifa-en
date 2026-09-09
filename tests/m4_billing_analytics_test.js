'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'../m4/M4优化调度控制台-线上版.html'),'utf8');
const pure=html.split('// Billing analytics: pure calculations, also exercised by the local contract tests.')[1].split('// End pure billing analytics.')[0];
const context=vm.createContext({});vm.runInContext(pure+';globalThis.api={billingDays,previousBillingMonth,billingComparison,billingTrendModel};',context);const api=context.api;
function bill(month,{today='2026-09-09T10:00:00+08:00',count=api.billingDays(month),value=10,station='station-1'}={}){
 return {station_id:station,month,fetched_at:today,coverage:{expected_days:count},sources:{daily:{status:'ready'}},daily:Array.from({length:count},(_,i)=>({date:month+'-'+String(i+1).padStart(2,'0'),day_charge:value,day_discharge:value,charge_cost:value,discharge_earnings:value,day_earnings:value}))};
}
test('current month compares equal date ranges and excludes later prior days',()=>{
 const a=bill('2026-09',{count:9,value:20}),b=bill('2026-08');b.daily[20].day_earnings=99999;
 const result=api.billingComparison(a,b);assert.equal(result.mode,'same_days');assert.equal(result.current.end,'2026-09-09');assert.equal(result.previous.end,'2026-08-09');
 assert.equal(result.metrics[4].current,180);assert.equal(result.metrics[4].previous,90);assert.equal(result.metrics[4].delta,90);assert.equal(result.metrics[4].percent,100);
});
test('shorter previous month clamps both periods including leap day',()=>{
 for(const [year,cutoff] of [[2024,29],[2025,28]]){
  const now=year+'-03-31T12:00:00+08:00',r=api.billingComparison(bill(year+'-03',{today:now}),bill(year+'-02'));
  assert.equal(r.cutoff,cutoff);assert.equal(r.current.expected,cutoff);assert.equal(r.previous.expected,cutoff);assert.equal(r.clamped,true);
 }
});
test('historical full months use their actual unequal lengths and never monthly replacement',()=>{
 const a=bill('2026-08'),b=bill('2026-07');a.monthly={month_earnings:99999};const r=api.billingComparison(a,b);
 assert.equal(r.mode,'full_month');assert.equal(r.metrics[4].current,310);
 const shorter=api.billingComparison(bill('2026-07'),bill('2026-06'));assert.equal(shorter.current.expected,31);assert.equal(shorter.previous.expected,30);
});
test('timezone and year transitions are based on Shanghai date',()=>{
 assert.equal(api.previousBillingMonth('2026-01'),'2025-12');assert.equal(api.previousBillingMonth('2000-01'),'1999-12');
 const r=api.billingComparison(bill('2026-09',{today:'2026-08-31T16:01:00Z',count:1}),bill('2026-08'));assert.equal(r.mode,'same_days');assert.equal(r.cutoff,1);
});
test('missing dates, failed sources and null values suppress affected comparisons',()=>{
 const a=bill('2026-08'),b=bill('2026-07');a.daily.splice(5,1);let r=api.billingComparison(a,b);assert.equal(r.current.recorded,30);assert.equal(r.current.missing[0],'2026-08-06');assert.equal(r.metrics[0].delta,null);
 const c=bill('2026-08');c.daily[0].charge_cost=null;r=api.billingComparison(c,b);assert.equal(r.metrics[2].current,null);assert.equal(r.metrics[4].delta,0);
 b.sources.daily.status='error';r=api.billingComparison(c,b);assert.equal(r.metrics[4].previous,null);assert.equal(r.metrics[4].percent,null);
});
test('zero and negative baselines retain amount deltas without misleading percentages',()=>{
 for(const value of [0,-10]){const r=api.billingComparison(bill('2026-08',{value:5}),bill('2026-07',{value}));assert.equal(r.metrics[4].delta,(5-value)*31);assert.equal(r.metrics[4].percent,null);}
 const r=api.billingComparison(bill('2026-08',{value:-5}),bill('2026-07',{value:10}));assert.equal(r.metrics[4].percent,-150);
});
test('station/month mismatch and future periods cannot produce comparisons',()=>{
 assert.throws(()=>api.billingComparison(bill('2026-08'),bill('2026-07',{station:'station-2'})),/不匹配/);
 assert.throws(()=>api.billingComparison(bill('2026-08'),bill('2026-06')),/不匹配/);
 const r=api.billingComparison(bill('2026-10'),bill('2026-09'));assert.equal(r.mode,'future');assert.equal(r.metrics[4].delta,null);
});
test('trend retains negative values and splits gaps instead of joining or zero-filling',()=>{
 const a=bill('2026-09',{count:5,value:-10});a.daily.splice(2,1);a.daily[1].charge_cost=null;const r=api.billingTrendModel(a);
 assert.equal(r.days.length,5);assert.equal(r.days[2].row,null);assert(r.min<0);assert(r.zero<r.y(-10));
 assert.equal(r.series.find(s=>s.field==='day_earnings').segments.length,2);
 assert.equal(r.series.find(s=>s.field==='charge_cost').segments[0].length,1);
 assert.equal(api.billingTrendModel(a,[]).hasValues,false);
 const one=api.billingTrendModel(bill('2026-09',{count:1,value:0}));assert(Number.isFinite(one.series[0].segments[0][0].x));assert(Number.isFinite(one.zero));
});
