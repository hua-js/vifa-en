const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { once } = require('node:events');
const { chromium } = require('playwright');

const site = '363738052231168';
const now = new Date().toISOString();
const nodes = [];
const realtime = [];
for (const [station, sns] of [['ES01', ['emu11','emu12']], ['ES02', ['emu21','emu22','emu23','emu24','emu25','emu26']]]) {
  nodes.push({id:station+'ess', name:'储能系统', sn:station, node_type:'ess', is_aggregate:true, fk_es:station, fk_site_id:site});
  sns.forEach((sn, i) => {
    nodes.push({id:sn, name:sn+' 储能柜', sn, node_type:'ess', fk_es:station, fk_site_id:site, parentId:station+'ess'});
    realtime.push({fk_en:sn, timestamp:sn==='emu26'?'2026-09-07T08:12:05+08:00':now, cabinet_timestamp:sn==='emu26'?'':`2026-09-07T08:12:${String(i).padStart(2,'0')}+08:00`, power:25, soc:65, run_status:'charge', temperature_c:30});
  });
}
for (const station of ['ES01','ES02']) {
  nodes.push({id:station+'pv',name:'光伏系统',node_type:'pv_grid_ac',is_aggregate:true,fk_es:station,fk_site_id:site});
  nodes.push({id:station+'meter',name:'光伏计量表',node_type:'meter',parentId:station+'pv',fk_es:station,fk_site_id:site});
  realtime.push({fk_en:station+'meter',timestamp:'2026-09-07T08:10:06+08:00',run_status:'generate'});
}
const records = Array.from({length:25}, (_, i) => ({
  id:'derived:load_spike:'+i, category:i%2?'ess_self_loss':'load_spike',
  fk_site_id:site, fk_en_id:i%2?'ES02ess':'ES01ess', device_sn:i%2?'ES02':'ES01',
  txt:i%2?'储能自损耗异常':'负载功率突增',
  start_time:`2026-09-0${i<20?'7':'6'}T08:00:${String(i).padStart(2,'0')}+08:00`, value:1200, threshold:1000, unit:'kW',
}));
const fixture = {status:'ok', data:{
  sites:[{id:site,name:'威法能源场站'}],
  es_list:[{id:'ES01',station_code:'ES01',fk_site:site}, {id:'ES02',station_code:'ES02',fk_site:site}],
  nodes, realtime, alerts:[records[0]], stats:{},
  pv_inverters: [
    {sn:'emu1',timestamp:'2026-09-07T08:11:01+08:00',online:true,data_quality:'good',input_power_kw:50,output_power_kw:48},
    {sn:'emu2',timestamp:'2026-09-07T00:11:05Z',online:true,data_quality:'good',input_power_kw:50,output_power_kw:48},
    {sn:'emu3',timestamp:'invalid',online:false,data_quality:'missing'},
    {sn:'outside',timestamp:'2099-09-07T08:00:00+08:00'},
  ],
  alert_history:{status:'ok',records:[...records, {...records[0],fk_site_id:'other',txt:'其他客户'}],total:25,limit:1000},
}};
let payload = structuredClone(fixture);
let browser;
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/dashboard_energy_api')) {
    res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(payload));
  } else if (['/m1/dashboard_energy.html','/front/dashboard_energy.html'].includes(req.url)) {
    res.setHeader('Content-Type','text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(__dirname,'../..', req.url)));
  } else { res.writeHead(404); res.end(); }
});

(async () => {
  server.listen(0,'127.0.0.1'); await once(server,'listening');
  browser = await chromium.launch({headless:true});
  const errors = [];
  const entries = ['m1','front'].filter(entry => fs.existsSync(path.join(__dirname,'../..',entry,'dashboard_energy.html')));
  assert.ok(entries.includes('m1'));
  for (const entry of entries) {
    payload = structuredClone(fixture);
    const page = await browser.newPage({viewport:{width:1440,height:1100}, timezoneId:'America/Los_Angeles'});
    await page.clock.setFixedTime(new Date('2026-09-07T06:36:00Z'));
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/${entry}/dashboard_energy.html`);
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.node-time').count(), 10);
    assert.match(await page.locator('[data-node-id="emu11"] .node-time').textContent(), /08:12:00/);
    assert.match(await page.locator('[data-node-id="emu12"] .node-time').textContent(), /08:12:01/);
    assert.match(await page.locator('[data-node-id="emu26"] .node-time').textContent(), /^采集时间/);
    assert.match(await page.locator('[data-node-id="ES01meter"] .node-time').textContent(), /采集时间.*08:10:06/);
    assert.match(await page.locator('[data-node-id="ES02meter"] .node-time').textContent(), /采集时间.*08:12:05/);
    const station2Times = structuredClone(payload.data.realtime);
    payload.data.realtime.forEach(row => {
      if (/^emu2[1-6]$/.test(row.fk_en)) { row.cabinet_timestamp=''; row.timestamp=''; }
    });
    await page.evaluate(() => fetchData());
    assert.match(await page.locator('[data-node-id="ES02meter"] .node-time').textContent(), /暂无采集时间/);
    payload.data.realtime = station2Times;
    await page.evaluate(() => fetchData());
    assert.doesNotMatch((await page.locator('.node-time').allTextContents()).join(''), /柜体上报时间|实时采集时间|北京时间/);
    assert.equal(await page.locator('.alert-item').count(), 1);
    assert.equal(await page.locator('#resetAlertFilters').isVisible(), false);
    await page.getByRole('button',{name:'历史告警',exact:true}).click();
    assert.equal(await page.locator('#historyStart').inputValue(), '2026-09-01');
    assert.equal(await page.locator('#historyEnd').inputValue(), '2026-09-07');
    assert.equal(await page.locator('#resetAlertFilters').isVisible(), false);
    assert.doesNotMatch(await page.locator('#historyNote').textContent(), /启用后随看板刷新积累|不代表恢复状态/);
    assert.equal(await page.locator('.alert-item').count(), 20);
    assert.match(await page.locator('#historyPageInfo').textContent(), /1 \/ 2/);
    await page.getByRole('button',{name:'下一页'}).click();
    assert.equal(await page.locator('.alert-item').count(), 5);
    await page.locator('#historyStart').fill('2026-09-06');
    await page.locator('#historyEnd').fill('2026-09-06');
    assert.equal(await page.locator('.alert-item').count(), 5);
    await page.evaluate(() => fetchData());
    assert.equal(await page.locator('#historyStart').inputValue(), '2026-09-06');
    assert.equal(await page.locator('#historyEnd').inputValue(), '2026-09-06');
    assert.equal(await page.locator('.alert-item').count(), 5);
    await page.locator('#historyStart').fill('2026-09-07');
    assert.match(await page.locator('#alertList').textContent(), /开始日期不能晚于/);
    await page.getByRole('button',{name:'清除筛选'}).click();
    assert.equal(await page.locator('#historyStart').inputValue(), '2026-09-01');
    assert.equal(await page.locator('#historyEnd').inputValue(), '2026-09-07');
    await page.locator('#alertSearch').fill('ES02');
    assert.equal(await page.locator('.alert-item').count(), 12);
    await page.getByRole('button',{name:'清除筛选'}).click();
    await page.locator('.es-header[data-es-id="ES01"]').click();
    assert.equal(await page.locator('.alert-item').count(), 13);
    await page.getByRole('button',{name:'清除筛选'}).click();
    await page.locator('#alertTabs [data-tab="ess_self_loss"]').click();
    assert.equal(await page.locator('.alert-item').count(), 12);
    assert.equal(await page.locator('.site-card').count(), 1); // History survives no current category alert.
    await page.getByRole('button',{name:'清除筛选'}).click();
    assert.equal(await page.getByText('其他客户',{exact:true}).count(), 0);
    await page.screenshot({path:`/tmp/m1-${entry}-desktop.png`,fullPage:true});
    await page.locator('#themeBtn').click();
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.screenshot({path:`/tmp/m1-${entry}-dark.png`,fullPage:true,animations:'disabled'});
    for (const width of [768,390,320]) {
      await page.setViewportSize({width,height:1100});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth<=innerWidth), `overflow at ${width}`);
      assert.ok(await page.locator('#alertList').evaluate(el => el.clientHeight >= 200), `history list compressed at ${width}`);
      assert.ok(await page.locator('#historyPager').evaluate(el => {
        const panel=el.closest('.alert-panel').getBoundingClientRect();
        return el.getBoundingClientRect().bottom <= panel.bottom;
      }), `pager clipped at ${width}`);
      await page.screenshot({path:`/tmp/m1-${entry}-${width}.png`,fullPage:true});
    }
    payload.data.alerts=[];
    await page.evaluate(() => fetchData());
    assert.equal(await page.locator('.alert-item').count(),20);
    await page.getByRole('button',{name:'当前告警',exact:true}).click();
    assert.equal(await page.locator('.alert-item').count(),0);
    delete payload.data.alert_history;
    await page.evaluate(() => fetchData());
    await page.getByRole('button',{name:'历史告警',exact:true}).click();
    assert.match(await page.locator('#historyNote').textContent(), /尚未接入/);
    payload.data.alert_history={status:'error',records:[],message:'历史告警读写失败'};
    await page.evaluate(() => fetchData());
    assert.match(await page.locator('#historyNote').textContent(), /读写失败/);
    payload.data.alert_history={status:'ok',records:[],total:0,limit:1000};
    payload.data.realtime=[];
    payload.data.pv_inverters=[];
    await page.evaluate(() => fetchData());
    assert.equal(await page.getByText('暂无采集时间',{exact:true}).count(),10);
    assert.match(await page.locator('#alertList').textContent(), /暂无符合条件的历史记录/);
    await page.close();
  }
  assert.deepEqual(errors,[]);
  console.log(`M1 已检查入口 ${entries.join(', ')}：柜体时间、历史分页/日期/搜索/电站/类别筛选、空态/错误态、主题与 1440/768/390/320px 布局通过。`);
})().catch(error => {console.error(error);process.exitCode=1;}).finally(async () => {
  if(browser) await browser.close();
  server.close();
});
