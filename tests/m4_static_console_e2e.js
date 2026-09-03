"use strict";

const assert = require("assert");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { once } = require("events");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const HTML_PATH = path.join(ROOT, "m4", "M4优化调度控制台.html");

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
  await page.goto(`http://127.0.0.1:${port}/M4%E4%BC%98%E5%8C%96%E8%B0%83%E5%BA%A6%E6%8E%A7%E5%88%B6%E5%8F%B0.html`);
  await page.waitForLoadState("networkidle");
  return { page, externalRequests, consoleErrors, pageErrors };
}

(async () => {
  const html = fs.readFileSync(HTML_PATH);
  server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
    if (pathname === "/M4优化调度控制台.html") {
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

  assert.strictEqual(
    (await page.locator("#demo-mode").textContent()).trim(),
    "静态演示模式 · 不连接真实 AI/EMS",
  );
  assert.strictEqual(await page.locator('[role="tab"]').count(), 3);
  assert.strictEqual(await page.locator("#schedule-points").getAttribute("data-point-count"), "96");
  assert.strictEqual(await page.locator(".schedule-lane").count(), 2);

  const screenshotDir = process.env.M4_SCREENSHOT_DIR;
  if (screenshotDir) {
    fs.mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: path.join(screenshotDir, "m4-workbench-desktop.png"), fullPage: true });
  }

  const originalTheme = await page.locator("html").getAttribute("data-theme");
  await page.locator("#theme-toggle").click();
  assert.notStrictEqual(await page.locator("html").getAttribute("data-theme"), originalTheme);

  await page.getByRole("button", { name: "编辑策略" }).click();
  await page.locator("#edit-power").fill("1600");
  await page.getByRole("button", { name: "保存调整" }).click();
  await page.getByRole("button", { name: "执行安全预检" }).click();
  assert.strictEqual(await page.locator("#validation-state").getAttribute("data-state"), "blocked");
  assert.strictEqual(await page.locator("#send-ems").isDisabled(), true);
  assert.match(await page.locator("#validation-state").textContent(), /超过演示上限/);

  await page.locator("#edit-power").fill("420");
  await page.getByRole("button", { name: "保存调整" }).click();
  await page.getByRole("button", { name: "执行安全预检" }).click();
  assert.strictEqual(await page.locator("#validation-state").getAttribute("data-state"), "passed");
  assert.strictEqual(await page.locator("#send-ems").isEnabled(), true);

  const recordsBefore = await page.locator("#record-list .record-item").count();
  await page.getByRole("button", { name: "发送给 EMS" }).click();
  assert.match(await page.locator("#ems-result").textContent(), /EMS 已接收/);
  assert.strictEqual(await page.locator("#record-list .record-item").count(), recordsBefore + 1);

  await page.locator("#ems-scenario").selectOption("rejected");
  await page.getByRole("button", { name: "发送给 EMS" }).click();
  assert.match(await page.locator("#ems-result").textContent(), /EMS 已拒绝/);
  assert.match(await page.locator("#record-list .record-item").first().textContent(), /拒绝/);

  await page.getByRole("tab", { name: "策略与执行记录" }).click();
  assert.strictEqual(await page.locator("#records-panel").isVisible(), true);
  await page.getByRole("tab", { name: "验收看板" }).click();
  assert.strictEqual(await page.locator("#acceptance-panel").isVisible(), true);
  assert.strictEqual(await page.locator(".acceptance-item").count(), 5);
  assert.match(await page.locator('[data-acceptance="4.5"]').textContent(), /口径待确认/);
  assert.doesNotMatch(await page.locator("#acceptance-panel").textContent(), /正式通过/);

  if (screenshotDir) {
    await page.screenshot({ path: path.join(screenshotDir, "m4-desktop.png"), fullPage: true });
  }

  const mobile = await openPage({ width: 320, height: 900 });
  const overflow = await mobile.page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  assert.ok(overflow.scroll <= overflow.client, `mobile page overflow: ${JSON.stringify(overflow)}`);
  const timelineOverflow = await mobile.page.locator("#schedule-points").evaluate((node) => ({
    scroll: node.scrollWidth,
    client: node.clientWidth,
  }));
  assert.ok(
    timelineOverflow.scroll > timelineOverflow.client,
    `mobile timeline should scroll locally: ${JSON.stringify(timelineOverflow)}`,
  );
  if (screenshotDir) {
    await mobile.page.screenshot({ path: path.join(screenshotDir, "m4-workbench-mobile.png"), fullPage: true });
  }
  await mobile.page.getByRole("tab", { name: "验收看板" }).click();
  assert.strictEqual(await mobile.page.locator("#acceptance-panel").isVisible(), true);
  if (screenshotDir) {
    await mobile.page.screenshot({ path: path.join(screenshotDir, "m4-mobile.png"), fullPage: true });
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
  console.log("M4 static console e2e passed");
})().catch(async (error) => {
  console.error(error);
  if (browser) await browser.close().catch(() => {});
  if (server) await new Promise((resolve) => server.close(() => resolve()));
  process.exitCode = 1;
});
