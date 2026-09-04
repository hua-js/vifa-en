"use strict";

const assert = require("assert");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { once } = require("events");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const HTML_PATH = path.join(ROOT, "m4", "M4优化调度控制台-线上版.html");

let browser;
let server;

async function openPage(viewport, scenario = "normal") {
  const page = await browser.newPage({ viewport });
  const externalRequests = [];
  const consoleErrors = [];
  const pageErrors = [];

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.hostname !== "127.0.0.1") externalRequests.push(request.url());
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  const port = server.address().port;
  const query = new URLSearchParams({ scenario });
  await page.goto(`http://127.0.0.1:${port}/M4%E4%BC%98%E5%8C%96%E8%B0%83%E5%BA%A6%E6%8E%A7%E5%88%B6%E5%8F%B0-%E7%BA%BF%E4%B8%8A%E7%89%88.html?${query}`);
  await page.waitForLoadState("networkidle");
  return { page, externalRequests, consoleErrors, pageErrors };
}

async function openStrategy(result) {
  await result.page.getByRole("tab", { name: "策略工作台" }).click();
  assert.strictEqual(await result.page.locator("#strategy-panel").isVisible(), true);
}

(async () => {
  const html = fs.readFileSync(HTML_PATH);
  server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
    if (pathname === "/M4优化调度控制台-线上版.html") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(html);
      return;
    }
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("not found");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");

  browser = await chromium.launch({ headless: true });
  const desktop = await openPage({ width: 1440, height: 1000 });
  const { page } = desktop;

  assert.strictEqual((await page.locator("#mock-data-badge").textContent()).trim(), "Mock 数据");
  assert.strictEqual(await page.locator('[role="tab"]').count(), 3);
  assert.strictEqual(await page.getByRole("tab", { name: "调度总览" }).getAttribute("aria-selected"), "true");
  assert.strictEqual(await page.locator("[data-overview-metric]").count(), 6);
  assert.strictEqual(await page.locator("#overview-timeline .schedule-lane").count(), 2);
  assert.match(await page.locator("#automatic-operation-banner").textContent(), /全自动调度运行中.*15分钟/s);
  assert.doesNotMatch(
    await page.locator("body").textContent(),
    /验收看板|附件\s*1|沟通用|评审稿|口径待确认|AI生成新策略|保存调整|执行安全预检|发送至\s*EMS|人工确认|操作员发送/,
  );

  const screenshotDir = process.env.M4_ONLINE_SCREENSHOT_DIR;
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-overview-desktop.png"), fullPage: true });
  }

  await openStrategy(desktop);
  assert.match(await page.locator("#auto-cycle-summary").textContent(), /每15分钟.*下一轮/s);
  assert.strictEqual(await page.locator("#candidate-grid [data-candidate]").count(), 3);
  assert.match(await page.locator('#candidate-grid [data-selected="true"]').textContent(), /节费优先/);
  assert.match(await page.locator("#ai-selection").textContent(), /大语言模型.*节费优先.*0\.91/s);
  assert.match(await page.locator("#ai-selection").textContent(), /不生成或修改96点/);
  assert.strictEqual(await page.locator("#pipeline .pipeline-step").count(), 7);
  assert.match(await page.locator("#change-gate").textContent(), /未来4小时.*10%.*1%.*达到阈值/s);
  assert.strictEqual(await page.locator("#change-gate").getAttribute("data-result"), "dispatch");
  assert.match(await page.locator("#auto-dispatch-contract").textContent(), /2 × 96 点.*自动下发.*EMS已接收/s);
  assert.strictEqual(await page.locator("#validation-status").getAttribute("data-state"), "passed");
  if (screenshotDir) {
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-strategy-desktop.png"), fullPage: true });
  }

  await page.getByRole("tab", { name: "执行记录" }).click();
  assert.strictEqual(await page.locator("#records-panel").isVisible(), true);
  assert.match(await page.locator("#execution-list").textContent(), /MILP生成3个可行候选.*大语言模型完成选择.*确定性校验通过.*EMS已接收/s);
  assert.doesNotMatch(await page.locator("#execution-list").textContent(), /人工|操作员/);

  const originalTheme = await page.locator("html").getAttribute("data-theme");
  await page.locator("#theme-toggle").click();
  assert.notStrictEqual(await page.locator("html").getAttribute("data-theme"), originalTheme);

  if (screenshotDir) {
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-records-desktop.png"), fullPage: true });
  }

  const aiFallback = await openPage({ width: 1280, height: 900 }, "ai-fallback");
  await openStrategy(aiFallback);
  assert.strictEqual(await aiFallback.page.locator("#ai-selection").getAttribute("data-mode"), "fallback");
  assert.match(await aiFallback.page.locator('#candidate-grid [data-selected="true"]').textContent(), /均衡方案/);
  assert.match(await aiFallback.page.locator("#ai-selection").textContent(), /大语言模型超时.*自动降级.*均衡方案/s);
  assert.strictEqual(await aiFallback.page.locator("#validation-status").getAttribute("data-state"), "passed");
  assert.match(await aiFallback.page.locator("#auto-dispatch-contract").textContent(), /EMS已接收/);

  const validationFailed = await openPage({ width: 1280, height: 900 }, "validation-failed");
  await openStrategy(validationFailed);
  assert.strictEqual(await validationFailed.page.locator("#validation-status").getAttribute("data-state"), "blocked");
  assert.strictEqual(await validationFailed.page.locator("#dispatch-status").getAttribute("data-state"), "not-sent");
  assert.match(await validationFailed.page.locator("#auto-dispatch-contract").textContent(), /未发送.*保留EMS当前有效计划/s);

  const emsRetry = await openPage({ width: 1280, height: 900 }, "ems-retry");
  await openStrategy(emsRetry);
  assert.strictEqual(await emsRetry.page.locator("#ems-recovery").getAttribute("data-state"), "recovered");
  assert.match(await emsRetry.page.locator("#auto-dispatch-contract").textContent(), /EMS首次拒绝.*立即重算1次.*已恢复/s);

  const mobile = await openPage({ width: 320, height: 900 });
  const overflow = await mobile.page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  assert.ok(overflow.scroll <= overflow.client, `mobile page overflow: ${JSON.stringify(overflow)}`);
  const timelineOverflow = await mobile.page.locator("#overview-timeline").evaluate((node) => ({
    scroll: node.scrollWidth,
    client: node.clientWidth,
  }));
  assert.ok(
    timelineOverflow.scroll > timelineOverflow.client,
    `mobile timeline should scroll locally: ${JSON.stringify(timelineOverflow)}`,
  );
  if (screenshotDir) {
    await mobile.page.screenshot({ path: path.join(screenshotDir, "m4-online-overview-mobile.png"), fullPage: true });
  }

  for (const result of [desktop, aiFallback, validationFailed, emsRetry, mobile]) {
    assert.deepStrictEqual(result.externalRequests, []);
    assert.deepStrictEqual(result.consoleErrors, []);
    assert.deepStrictEqual(result.pageErrors, []);
  }

  await Promise.all([desktop, aiFallback, validationFailed, emsRetry, mobile].map((result) => result.page.close()));
  await browser.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  console.log("M4 online automatic scheduling console e2e passed");
})().catch(async (error) => {
  console.error(error);
  if (browser) await browser.close().catch(() => {});
  if (server) await new Promise((resolve) => server.close(() => resolve()));
  process.exitCode = 1;
});
