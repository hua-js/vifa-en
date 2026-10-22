'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {fixture,script} = require('./availability_harness.cjs');
const {payload,fail,scenarios,copy} = require('./availability_fixture.cjs');
const rt = (data,id) => data.realtime.find(row => row.fk_en === id);
const plain = value => JSON.parse(JSON.stringify(value));

for (const kind of scenarios) {
  test(`${kind}: retain affected data silently, update healthy sources, then recover`, async () => {
    let body = fail(payload(2),kind);
    const {sandbox,previous,notices} = fixture(async () => ({ok:true,json:async()=>copy(body)}));
    const untouched = plain(previous);
    await sandbox.fetchData();
    const data = sandbox.allData;
    assert.deepEqual(notices,[]);
    assert.deepEqual(plain(previous),untouched,'retention must not mutate its last good snapshot');
    for (const row of data.realtime) assert.equal(sandbox.nodeOnline(row),true);
    assert.equal(sandbox.summarizePvInverters(data.pv_inverters).validCount,9);
    if (kind === 'growatt' || kind === 'all_sources') {
      assert.equal(data.pv_inverters[0].output_power_kw,10);
      assert.equal(data.alerts.filter(alert=>alert.category==='pv_efficiency').length,1);
    } else assert.equal(data.pv_inverters[0].output_power_kw,11);
    for (const [code,id] of [['ES01',1000],['ES02',1100]]) {
      const failed = kind === 'station_load:'+code || kind === 'all_sources';
      assert.equal(rt(data,id).power,failed?21:22);
    }
    for (const [code,parent,cabinet] of [['ES01',1003,1010],['ES02',1103,1110]]) {
      const failed = ['storage_cabinets','storage_cabinets:'+code,'all_sources'].includes(kind);
      assert.equal(rt(data,parent).power,failed?21:22);
      const missingCabinet = cabinet === 1010 && ['missing_realtime','missing_placeholder'].includes(kind);
      assert.equal(rt(data,cabinet).temperature_c,failed||missingCabinet?31:32);
      if (failed) assert.equal(rt(data,cabinet).soc,52,'a failed cabinet source must not freeze fresh EMS SOC');
    }
    if (['missing_realtime','missing_placeholder'].includes(kind)) {
      assert.equal(rt(data,1010).power,21);
      assert.equal(rt(data,1011).power,22);
    }
    if (['storage_cabinets','storage_cabinets:ES02','pv_meter:ES02','all_sources'].includes(kind)) {
      assert.equal(rt(data,1102).pv_meter_timestamp,rt(previous,1102).pv_meter_timestamp);
    }
    assert.equal(data.alert_history.status,'ok');
    body = payload(3);
    await sandbox.fetchData();
    assert.deepEqual(plain(sandbox.allData),plain(sandbox.isolateCustomerData(body.data)));
    assert.equal(rt(sandbox.allData,1010).power,23);
    assert.equal(sandbox.allData.pv_inverters[0].output_power_kw,12);
    assert.deepEqual(notices,[]);
  });
}

for (const kind of ['network','http','json','business','empty']) {
  test(`${kind}: keep whole visible snapshot and hint, release lock, recover`,async()=>{
    let failing = true;
    const {sandbox,previous,refreshInfo,notices,renders,timers} = fixture(async()=>{
      if (!failing) return {ok:true,json:async()=>payload(3)};
      if (kind === 'network') throw new Error('simulated network error');
      if (kind === 'http') return {ok:false,status:503};
      if (kind === 'json') return {ok:true,json:async()=>{throw new SyntaxError('bad JSON');}};
      return {ok:true,json:async()=>kind==='business'?{status:'error',message:'fixture'}:{status:'ok',data:{}}};
    });
    const hint = refreshInfo.textContent;
    await sandbox.fetchData();
    assert.equal(sandbox.allData,previous);
    assert.equal(refreshInfo.textContent,hint);
    assert.deepEqual(notices,[]);
    assert.equal(renders.length,0);
    assert.equal(timers.size,0);
    assert.equal(sandbox.fetchInFlight,false);
    failing = false;
    await sandbox.fetchData();
    assert.equal(rt(sandbox.allData,1010).power,23);
    assert.equal(renders.length,1);
  });
}

test('zero values and real inverter stop replace old data after a successful response',async()=>{
  const body=payload(2);
  Object.assign(rt(body.data,1010),{power:0,soc:0,temperature_c:0});
  Object.assign(rt(body.data,1000),{power:0});
  body.data.pv_inverters.forEach(row=>Object.assign(row,{online:false,data_quality:'invalid',input_power_kw:0,output_power_kw:0}));
  const {sandbox}=fixture(async()=>({ok:true,json:async()=>body}));
  await sandbox.fetchData();
  assert.equal(rt(sandbox.allData,1010).power,0);
  assert.equal(rt(sandbox.allData,1010).soc,0);
  assert.equal(rt(sandbox.allData,1010).temperature_c,0);
  assert.equal(rt(sandbox.allData,1000).power,0);
  assert.equal(sandbox.summarizePvInverters(sandbox.allData.pv_inverters).validCount,0);
});

test('consecutive source failures retain each source independently without poisoning recovery',async()=>{
  let body=fail(payload(2),'growatt');
  const {sandbox,notices}=fixture(async()=>({ok:true,json:async()=>copy(body)}));
  for(const version of [2,3,4]) {
    body=fail(payload(version),'growatt');
    await sandbox.fetchData();
    assert.equal(sandbox.allData.pv_inverters[0].output_power_kw,10);
    assert.equal(rt(sandbox.allData,1010).soc,50+version);
  }
  body=fail(payload(5),'station_load:ES01');
  await sandbox.fetchData();
  assert.equal(sandbox.allData.pv_inverters[0].output_power_kw,14);
  assert.equal(rt(sandbox.allData,1000).power,24,'retain latest successful load, not the first snapshot');
  assert.equal(rt(sandbox.allData,1100).power,25);
  body=payload(6);
  await sandbox.fetchData();
  assert.equal(rt(sandbox.allData,1000).power,26);
  assert.equal(sandbox.allData.pv_inverters[0].output_power_kw,15);
  assert.deepEqual(notices,[]);
});

test('partial cabinet failure preserves unavailable fields without overwriting valid zero readings',async()=>{
  const body=fail(payload(2),'storage_cabinets:ES01');
  Object.assign(rt(body.data,1011),{temperature_c:0,cabinet_timestamp:'2026-10-12T04:02:30Z'});
  const {sandbox}=fixture(async()=>({ok:true,json:async()=>body}));
  await sandbox.fetchData();
  assert.equal(rt(sandbox.allData,1010).temperature_c,31);
  assert.equal(rt(sandbox.allData,1011).temperature_c,0);
  assert.equal(rt(sandbox.allData,1011).cabinet_timestamp,'2026-10-12T04:02:30Z');
});

test('retention follows current authorized topology and never resurrects removed devices',async()=>{
  const body=fail(payload(2),'growatt');
  body.data.nodes=body.data.nodes.filter(node=>![1010,1102].includes(node.id));
  const {sandbox}=fixture(async()=>({ok:true,json:async()=>body}));
  await sandbox.fetchData();
  assert.equal(rt(sandbox.allData,1010),undefined);
  assert.equal(sandbox.allData.alerts.some(row=>row.fk_en_id===1102),false);
});

test('first failed load has no fabricated fallback; later success populates data',async()=>{
  let failing=true;
  const {sandbox,notices}=fixture(async()=>{
    if (failing) throw new Error('fixture');
    return {ok:true,json:async()=>payload(2)};
  },{cold:true});
  await sandbox.fetchData();
  assert.equal(sandbox.allData,null);
  assert.equal(notices.length,1);
  failing=false;
  await sandbox.fetchData();
  assert.equal(rt(sandbox.allData,1010).power,22);
});

test('refresh registration uses a single shared 30-second interval',()=>{
  assert.match(script,/const REFRESH_INTERVAL_MS = 30000;/);
  assert.equal([...script.matchAll(/setInterval\(fetchData,\s*REFRESH_INTERVAL_MS\)/g)].length,2);
});
