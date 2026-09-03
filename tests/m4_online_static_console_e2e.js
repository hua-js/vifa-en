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

async function openPage(viewport) {
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
  await page.goto(`http://127.0.0.1:${port}/M4%E4%BC%98%E5%8C%96%E8%B0%83%E5%BA%A6%E6%8E%A7%E5%88%B6%E5%8F%B0-%E7%BA%BF%E4%B8%8A%E7%89%88.html`);
  await page.waitForLoadState("networkidle");
  return { page, externalRequests, consoleErrors, pageErrors };
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
  assert.doesNotMatch(await page.locator("body").textContent(), /验收看板|附件\s*1|沟通用|评审稿|口径待确认/);

  const screenshotDir = process.env.M4_ONLINE_SCREENSHOT_DIR;
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-overview-desktop.png"), fullPage: true });
  }

  await page.getByRole("tab", { name: "策略工作台" }).click();
  assert.strictEqual(await page.locator("#strategy-panel").isVisible(), true);
  assert.match(await page.locator("#strategy-output").textContent(), /两套储能各 96 点/);
  assert.match(await page.locator("#dispatch-contract").textContent(), /2 × 96 点.*人工确认/s);
  if (screenshotDir) {
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-strategy-desktop.png"), fullPage: true });
  }

  await page.getByRole("button", { name: "编辑第一个策略片段" }).click();
  await page.locator("#edit-power").fill("410");
  await page.getByRole("button", { name: "保存调整" }).click();
  assert.strictEqual(await page.locator("#precheck-state").getAttribute("data-state"), "pending");
  assert.strictEqual(await page.locator("#send-plan").isDisabled(), true);
  assert.strictEqual((await page.locator("#dispatch-version").textContent()).trim(), "V2");

  await page.getByRole("button", { name: "执行安全预检" }).click();
  assert.strictEqual(await page.locator("#precheck-state").getAttribute("data-state"), "passed");
  assert.strictEqual(await page.locator("#send-plan").isEnabled(), true);

  const recordsBefore = await page.locator("#execution-list .execution-item").count();
  await page.getByRole("button", { name: "发送至 EMS" }).click();
  assert.match(await page.locator("#ems-feedback").textContent(), /EMS 已接收/);
  assert.strictEqual(await page.locator("#execution-list .execution-item").count(), recordsBefore + 1);

  await page.getByRole("tab", { name: "执行记录" }).click();
  assert.strictEqual(await page.locator("#records-panel").isVisible(), true);
  assert.match(await page.locator("#execution-list").textContent(), /EMS 已接收/);

  const originalTheme = await page.locator("html").getAttribute("data-theme");
  await page.locator("#theme-toggle").click();
  assert.notStrictEqual(await page.locator("html").getAttribute("data-theme"), originalTheme);

  if (screenshotDir) {
    await page.screenshot({ path: path.join(screenshotDir, "m4-online-records-desktop.png"), fullPage: true });
  }

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

  for (const result of [desktop, mobile]) {
    assert.deepStrictEqual(result.externalRequests, []);
    assert.deepStrictEqual(result.consoleErrors, []);
    assert.deepStrictEqual(result.pageErrors, []);
  }

  await mobile.page.close();
  await page.close();
  await browser.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  console.log("M4 online static console e2e passed");
})().catch(async (error) => {
  console.error(error);
  if (browser) await browser.close().catch(() => {});
  if (server) await new Promise((resolve) => server.close(() => resolve()));
  process.exitCode = 1;
});
