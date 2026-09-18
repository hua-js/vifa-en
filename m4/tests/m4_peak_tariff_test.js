const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/M4优化调度控制台-线上版.html'),'utf8');
const context=vm.createContext({finite:v=>typeof v==='number'&&Number.isFinite(v)});
const statsStart=source.indexOf('function peakWindowStats('),statsEnd=source.indexOf('\n}\n',statsStart);
assert.ok(statsStart>=0&&statsEnd>statsStart,'peakWindowStats function must exist');
vm.runInContext(source.slice(statsStart,statsEnd+2),context);
function data(){const inputs=Array.from({length:96},(_,i)=>({timestamp:new Date(Date.UTC(2026,8,10,0,i*15)).toISOString(),tariff_period:i===40?'feng':i===72?'sharp':'gu'}));const base=inputs.map(p=>({timestamp:p.timestamp,grid_import_kw:900})),plan=structuredClone(base);base[40].grid_import_kw=300;base[72].grid_import_kw=280;plan[40].grid_import_kw=90;plan[72].grid_import_kw=200;return [inputs,base,plan];}
test('excludes a shared overnight peak; compares two peak-window maxima, not largest point reduction',()=>{const r=context.peakWindowStats(...data());assert.equal(r.peakBase,300);assert.equal(r.peakPlan,200);assert.equal(r.cut,100);});
test('peak alias and sharp are both included',()=>{const d=data();d[0][40].tariff_period='peak';assert.equal(context.peakWindowStats(...d).cut,100);});
test('no peak or unknown tariff hides metric instead of falling back to whole day',()=>{const d=data();d[0].forEach(p=>p.tariff_period='flat');assert.equal(context.peakWindowStats(...d).cut,null);d[0][0].tariff_period=null;assert.equal(context.peakWindowStats(...d).cut,null);});
test('misaligned snapshots are rejected',()=>{const d=data();d[2][40].timestamp=d[0][41].timestamp;assert.equal(context.peakWindowStats(...d).cut,null);});
test('increase and zero baseline remain finite and are not clamped to a fake benefit',()=>{const d=data();d[1][40].grid_import_kw=d[1][72].grid_import_kw=0;assert.equal(context.peakWindowStats(...d).cut,-200);});

test('jian is displayed as sharp and included in peak-window statistics',()=>{
 const mapping=source.match(/const periods=(\{[^;]+\});/)[1];
 const periods=vm.runInNewContext('('+mapping+')');
 assert.equal(periods.jian,'尖');
 const d=data();d[0][72].tariff_period='jian';d[1][72].grid_import_kw=500;
 const result=context.peakWindowStats(...d);
 assert.equal(result.peakBase,500);assert.equal(result.peakPlan,200);assert.equal(result.cut,300);
});
