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

async function openPage(viewport, scenario = "normal", station = "s1") {
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
  const query = new URLSearchParams({ source: "demo", scenario, station });
  await page.goto(`http://127.0.0.1:${port}/M4%E4%BC%98%E5%8C%96%E8%B0%83%E5%BA%A6%E6%8E%A7%E5%88%B6%E5%8F%B0-%E7%BA%BF%E4%B8%8A%E7%89%88.html?${query}`);
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
    } else {
      response.writeHead(404);
      response.end("not found");
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  browser = await chromium.launch({ headless: true });
  const result = await openPage({ width: 1440, height: 1100 });
  const { page } = result;
  const allPages = [result];
  const screenshots = process.env.M4_ONLINE_SCREENSHOT_DIR;
  async function screenshot(name) {
    if (!screenshots) return;
    fs.mkdirSync(screenshots, { recursive: true });
    await page.screenshot({ path: path.join(screenshots, name + ".png"), fullPage: true, animations: "disabled" });
  }
  async function noOverflow() {
    const width = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
    assert.ok(width.scroll <= width.client, `Page overflow: ${JSON.stringify(width)}`);
  }
  async function closeDrawer() {
    await page.keyboard.press("Escape");
    assert.strictEqual(await page.locator("#detail-drawer").isVisible(), false);
  }

  // The primary workspace is continuous; comparing a candidate does not select or dispatch it.
  assert.strictEqual(await page.getByRole("tab").count(), 0);
  assert.match(await page.locator("#mock-data-badge").textContent(), /静态演示.*Mock/);
  assert.strictEqual(await page.locator("[data-overview-metric]").count(), 6);
  assert.strictEqual(await page.locator("#plan-chart [data-point]").count(), 96);
  assert.strictEqual(await page.locator("#candidate-grid button").count(), 3);
  assert.strictEqual(await page.locator("#decision-name").textContent(), "节费优先");
  assert.strictEqual(await page.locator("#candidate-comparison").getAttribute("open"), null);
  await page.locator("#candidate-comparison summary").click();
  const originalPath = await page.locator("#plan-chart .chart-soc").getAttribute("d");
  await page.getByRole("button", { name: "预览光伏消纳优先曲线" }).click();
  assert.notStrictEqual(await page.locator("#plan-chart .chart-soc").getAttribute("d"), originalPath);
  assert.strictEqual(await page.locator('[data-candidate="pv"]').getAttribute("aria-pressed"), "true");
  assert.strictEqual(await page.locator('[data-candidate="cost"]').getAttribute("data-selected"), "true");
  assert.strictEqual(await page.locator("#decision-name").textContent(), "节费优先");
  assert.strictEqual(await page.locator('[data-candidate="pv"]').evaluate(el => el === document.activeElement), true);
  await page.locator("#restore-preview").click();
  assert.strictEqual(await page.locator("#plan-chart .chart-soc").getAttribute("d"), originalPath);
  await page.locator("#point-slider").focus();
  await page.keyboard.press("End");
  assert.strictEqual(await page.locator("#point-time").textContent(), "23:45");
  await page.keyboard.press("Home");
  assert.strictEqual(await page.locator("#point-time").textContent(), "00:00");
  await page.keyboard.press("ArrowRight");
  assert.strictEqual(await page.locator("#point-time").textContent(), "00:15");
  await page.locator("#point-slider").fill("60");
  await page.locator("#candidate-comparison summary").click();
  await screenshot("m4-redesign-desktop");

  // Read-only details and keyboard dismissal preserve the user's context.
  await page.locator("#input-details summary").click();
  assert.match(await page.locator("#input-list").textContent(), /520 kW/);
  await page.locator("#input-details summary").click();
  await page.locator("#open-plan").click();
  assert.match(await page.locator("#drawer-body").textContent(), /96 点.*61% \/ 61%/s);
  await closeDrawer();
  assert.strictEqual(await page.locator("#open-plan").evaluate(el => el === document.activeElement), true);
  await page.locator("#open-records").click();
  assert.match(await page.locator("#drawer-events").textContent(), /MILP生成3个可行候选.*EMS已接收/s);
  await page.locator("#event-filter").selectOption("exceptions");
  assert.match(await page.locator("#drawer-events").textContent(), /没有异常/);
  await page.locator("#close-drawer").click();

  // Each station retains its own scenario and preview; a failure is visible in the site summary.
  await page.locator("#scenario-select").selectOption("validation-failed");
  assert.strictEqual(await page.locator("#decision-status").textContent(), "已阻断");
  assert.match(await page.locator("#chart-preview-status").textContent(), /校验阻断.*保持原计划/);
  await page.locator("#open-decision").click();
  assert.match(await page.locator("#drawer-body").textContent(), /未发送.*M4-S1-20260904-060/s);
  await screenshot("m4-redesign-blocked-drawer");
  await closeDrawer();
  await page.locator("#station-select").selectOption("s2");
  assert.strictEqual(await page.locator("#decision-name").textContent(), "光伏消纳优先");
  assert.strictEqual(await page.locator("#decision-status").textContent(), "执行中");
  await page.locator("#open-records").click();
  assert.match(await page.locator("#drawer-body").textContent(), /EMS-S2.*emu21–emu26/s);
  assert.doesNotMatch(await page.locator("#drawer-events").textContent(), /电站1|M4-S1/);
  await closeDrawer();
  await page.locator("#station-select").selectOption("all");
  assert.strictEqual(await page.locator("#station-workspace").isVisible(), false);
  assert.strictEqual(await page.locator("#open-records").isDisabled(), true);
  assert.strictEqual(await page.locator("#scenario-select").isDisabled(), true);
  assert.match(await page.locator('[data-station-summary="s1"]').textContent(), /已阻断/);
  assert.match(await page.locator('[data-station-summary="s2"]').textContent(), /执行中/);
  await screenshot("m4-redesign-all-stations");
  await page.locator('[data-open-station="s1"]').click();
  assert.strictEqual(await page.locator("#scenario-select").inputValue(), "validation-failed");
  assert.strictEqual(await page.locator("#station-select").evaluate(el => el === document.activeElement), true);

  for (const [scenario, name, status] of [["solver-unproven", "无最终方案", "已阻断"], ["ems-retry", "节费优先", "重算恢复"]]) {
    await page.locator("#scenario-select").selectOption(scenario);
    assert.strictEqual(await page.locator("#decision-name").textContent(), name);
    assert.strictEqual(await page.locator("#decision-status").textContent(), status);
    await page.locator("#open-records").click();
    await page.locator("#event-filter").selectOption("exceptions");
    assert.ok(await page.locator("#drawer-events .event").count() > 0);
    await closeDrawer();
  }
  await page.locator("#scenario-select").selectOption("normal");
  assert.strictEqual(await page.locator("#actual-power-trace").count(), 1);
  assert.match(await page.locator("#point-reason").textContent(), /元\/kWh/);
  assert.ok(Math.abs(Number(await page.locator("#energy-card").getAttribute("data-balance-error"))) < 1e-7);
  await page.locator("#simulate-demand").click();
  assert.strictEqual(await page.locator("#demand-card").getAttribute("data-triggered"), "true");
  assert.strictEqual(await page.locator("#demand-current").textContent(), "501.2");
  assert.strictEqual(await page.locator("#demand-margin").textContent(), "18.8");
  assert.match(await page.locator("#execution-stats").textContent(), /60.*58.8.*1.2/s);
  await screenshot("m4-operations-demand");
  await page.locator("#open-records").click();
  assert.match(await page.locator("#drawer-events").textContent(), /需量逼近.*削峰计划已接收.*EMS 实际功率已回读/s);
  await closeDrawer();
  await page.locator("#simulate-pv").click();
  assert.match(await page.locator("#operating-title").textContent(), /充电 230 kW.*光伏余电/);
  assert.strictEqual(await page.locator("#demand-current").textContent(), "0");
  assert.match(await page.locator("#flow-breakdown").textContent(), /220.*230.*0/s);
  assert.strictEqual(await page.locator("#point-time").textContent(), "12:00");
  assert.strictEqual(await page.locator("#decision-name").textContent(), "光伏消纳优先");
  assert.ok(Math.abs(Number(await page.locator("#energy-card").getAttribute("data-balance-error"))) < 1e-7);
  await screenshot("m4-operations-pv");
  await page.locator("#station-select").selectOption("s2");
  assert.strictEqual(await page.locator("#scenario-select").inputValue(), "normal");
  assert.strictEqual(await page.locator("#demand-card").getAttribute("data-triggered"), "false");
  await page.locator("#station-select").selectOption("s1");
  assert.strictEqual(await page.locator("#scenario-select").inputValue(), "pv-surplus");
  await page.locator("#open-evidence").click();
  assert.strictEqual(await page.locator("[data-evidence]").count(), 5);
  assert.match(await page.locator('[data-evidence="4.1"]').textContent(), /M4 MILP v1.0.*MOCK-M4-S1.*96 点/s);
  await page.locator('[data-evidence="4.4"] summary').click();
  await page.locator('[data-evidence-demo="demand-near"]').click();
  assert.match(await page.locator('[data-evidence="4.4"]').textContent(), /模拟已触发.*501.2/s);
  await screenshot("m4-operations-evidence");
  await closeDrawer();
  await page.locator("#open-bill").click();
  assert.strictEqual(await page.locator(".bill-hero .rate").textContent(), "6.2%");
  assert.match(await page.locator(".bill-table").textContent(), /300,000.*281,400/s);
  await screenshot("m4-operations-bill");
  await page.locator("#bill-period").selectOption("2026-09");
  assert.strictEqual(await page.locator(".bill-hero .rate").textContent(), "待验证");
  assert.doesNotMatch(await page.locator("#drawer-body").textContent(), /模拟达标|6.2%/);
  await closeDrawer();
  assert.strictEqual(await page.locator("#bill-summary-value").textContent(), "待验证");
  await page.locator("#station-select").selectOption("s2");
  assert.strictEqual(await page.locator("#bill-summary-rate").textContent(), "6.6%");
  await page.locator("#station-select").selectOption("s1");
  assert.strictEqual(await page.locator("#bill-summary-value").textContent(), "待验证");
  await page.locator("#open-bill").click();
  await page.locator("#bill-period").selectOption("2026-08");
  await closeDrawer();
  await page.locator("#scenario-select").selectOption("normal");
  await page.locator("#theme-toggle").click();
  assert.strictEqual(await page.locator("html").getAttribute("data-theme"), "dark");
  await screenshot("m4-redesign-dark");
  await page.locator("#theme-toggle").click();

  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await noOverflow();
    if (width === 320 || width === 768) await screenshot(`m4-redesign-${width}`);
    await page.locator("#open-records").click();
    const drawer = await page.locator("#detail-drawer").evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }));
    assert.ok(drawer.scroll <= drawer.client, `Drawer overflow at ${width}px`);
    await closeDrawer();
    await page.locator("#open-evidence").click();
    assert.ok(await page.locator("#detail-drawer").evaluate(el => el.scrollWidth <= el.clientWidth));
    await closeDrawer();
    await page.locator("#open-bill").click();
    assert.ok(await page.locator("#detail-drawer").evaluate(el => el.scrollWidth <= el.clientWidth));
    await closeDrawer();
  }
  const localChart = await page.locator("#plan-chart-scroll").evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  assert.ok(localChart.scroll > localChart.client, "Dense chart should scroll inside its card on mobile");
  await page.locator("#station-select").selectOption("all");
  await noOverflow();

  const direct = await openPage({ width: 1280, height: 900 }, "solver-unproven", "s2");
  allPages.push(direct);
  assert.match(await direct.page.locator("#station-context-title").textContent(), /电站2/);
  assert.strictEqual(await direct.page.locator("#decision-name").textContent(), "无最终方案");
  for (const item of allPages) {
    assert.deepStrictEqual(item.externalRequests, []);
    assert.deepStrictEqual(item.consoleErrors, []);
    assert.deepStrictEqual(item.pageErrors, []);
    await item.page.close();
  }
  await browser.close();
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  console.log("M4 single-page console e2e passed: preview, power balance, peak shaving, PV surplus, five evidence items, monthly bills, station isolation, keyboard, themes, responsive layouts, and no external requests");
})().catch(async error => {
  console.error(error);
  if (browser) await browser.close().catch(() => {});
  if (server) await new Promise(resolve => server.close(resolve));
  process.exitCode = 1;
});
