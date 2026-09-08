'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const host = spawn(path.join(root, '.venv/bin/python'), ['tests/m4_selection_demo_server.py', '--ai-results'], {cwd:root});
  let browser; const errors = [], writes = [];
  try {
    const port = await new Promise((resolve, reject) => {
      let output=''; const timer=setTimeout(()=>reject(Error('startup timeout')),20000);
      host.stdout.on('data', chunk=>{output+=chunk; const match=output.match(/PORT=(\d+)/); if(match){clearTimeout(timer);resolve(Number(match[1]));}});
      host.once('exit',code=>reject(Error('host exit '+code)));
      host.stderr.on('data',chunk=>process.stderr.write(chunk));
    });
    const base=`http://127.0.0.1:${port}`;
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()!=='GET')writes.push(r.url());});
    await page.goto(base+'/m4'); await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('#ai-result-panel').count(),1,'AI result panel should exist');
    await page.waitForFunction(()=>document.querySelector('#ai-result-status')?.dataset.state==='blocked_post_ai_validation');
    assert.match(await page.locator('#ai-result-choice').innerText(),/balanced/);
    assert.match(await page.locator('#ai-result-check').innerText(),/通过/);
    assert.match(await page.locator('#ai-result-summary').innerText(),/复核未通过/);
    assert.equal(await page.locator('#ai-result-metrics tbody tr').count(),3);
    await page.locator('#ai-result-evidence').evaluate(n=>n.open=true);
    await page.locator('[data-ai-plan="balanced"]').click();
    assert.equal(await page.locator('#ai-plan-table tbody tr').count(),96);
    assert.match(await page.locator('#ai-plan-caption').innerText(),/历史/);
    assert.equal(await page.locator('#ai-plan-chart rect').count(),96);
    await page.locator('#station-select').selectOption('s2');
    await page.waitForFunction(()=>document.querySelector('#ai-result-status').dataset.state==='empty');
    assert.equal(await page.locator('#ai-result-body').isVisible(),false);
    await page.locator('#station-select').selectOption('s1');
    await page.waitForFunction(()=>document.querySelector('#ai-result-status').dataset.state==='blocked_post_ai_validation');
    const screenshots='/tmp/m4-ai-results-screens'; fs.mkdirSync(screenshots,{recursive:true});
    for(const width of [1440,768,390,320]) for(const theme of ['light','dark']) {
      await page.setViewportSize({width,height:1100});
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      await page.locator('#ai-result-panel').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(screenshots,`ai-${width}-${theme}.png`),animations:'disabled',fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width} ${theme}`);
    }
    // Failed refresh hides old success; an older station response cannot bleed into another station.
    await page.route('**/station-1/ai-selection',route=>route.fulfill({status:503,json:{detail:'测试读取失败'}}));
    await page.locator('#refresh-ai-result').click();
    await page.waitForFunction(()=>document.querySelector('#ai-result-status').dataset.state==='error');
    assert.equal(await page.locator('#ai-result-body').isVisible(),false);
    await page.unroute('**/station-1/ai-selection');
    let release, entered, delivered;
    const held=new Promise(resolve=>{release=resolve;});
    const fetched=new Promise(resolve=>{entered=resolve;}),done=new Promise(resolve=>{delivered=resolve;});
    await page.route('**/station-1/ai-selection',async route=>{const response=await route.fetch();entered();await held;await route.fulfill({response});delivered();});
    await page.locator('#refresh-ai-result').click();
    await fetched;
    await page.locator('#station-select').selectOption('s2'); release();
    await done;
    await page.waitForFunction(()=>document.querySelector('#ai-result-status').dataset.state==='empty');
    assert.equal(await page.locator('#ai-result-body').isVisible(),false);
    assert.deepEqual(errors,[]); assert.deepEqual(writes,[]);
    console.log('AI UI: proposal/blocked check, 96-point preview, station isolation, failed refresh, late response, 8 layouts passed; no writes.');
  } finally {if(browser)await browser.close();host.kill('SIGTERM');}
})().catch(error=>{console.error(error);process.exitCode=1;});
