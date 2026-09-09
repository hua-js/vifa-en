"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { task5Fixtures, weeklyEvidenceFixture } = require("./m3_dashboard_e2e");

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(4000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.clock.install({ time: new Date("2026-08-31T15:00:00+08:00") });
    const dashboard = task5Fixtures().ready;
    let current = weeklyEvidenceFixture();
    current.result.series[1].points.forEach(point => {
      point.actual_value = point.forecast_value;
      point.actual_quality = "valid";
      point.absolute_percentage_error = 0;
    });
    let latest = current;
    let storedOverride = null;
    let posts = 0;
    let expired = false;
    const authMode = process.env.M3_TEST_AUTH_MODE || "server_token";
    const queryToken = "test-user.token-123";
    const html = fs.readFileSync(process.env.M3_TEST_HTML || path.join(__dirname, "../m3/node_red/m3_production_gateway_template.html"), "utf8");
    await page.route("http://m3.test/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/ett") {
        return route.fulfill({ contentType: "text/html", body: html.replace("{{{m3DashboardAuthModeJson}}}", JSON.stringify(authMode)).replace("{{{m3NocobaseParentOriginJson}}}", JSON.stringify("http://m3.test")) });
      }
      if (authMode === "query_token") {
        assert.equal(route.request().headers().authorization, `Bearer ${queryToken}`);
        assert.equal(url.searchParams.has("token"), false);
      }
      if (expired) return route.fulfill({ status: 401, json: { status: "error", error: { code: "unauthorized" } } });
      let data;
      if (url.pathname.endsWith("/latest")) {
        if (!latest || url.searchParams.get("interval_seconds") !== String(latest.run.interval_seconds)) {
          return route.fulfill({ status: 404, json: { status: "error", error: { code: "not_found" } } });
        }
        data = latest.run;
      } else if (url.pathname.endsWith("/result")) data = current.result;
      else if (url.pathname.includes("custom-performance")) data = current.performance;
      else if (url.pathname.includes("custom-runs")) {
        if (route.request().method() === "POST") posts++;
        else if (!latest && !storedOverride) return route.fulfill({ status: 404, json: { status: "error", error: { code: "not_found" } } });
        data = route.request().method() === "POST" ? current.run : storedOverride || current.run;
      } else return route.fulfill({ json: dashboard });
      return route.fulfill({ json: { status: "ok", data } });
    });
    await page.goto("http://m3.test/ett" + (authMode === "query_token" ? `?token=${queryToken}` : ""));
    assert.equal(new URL(page.url()).searchParams.has("token"), false);
    await page.waitForFunction(() => document.querySelector("#result-model-meta").textContent === "3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度");
    assert.match(await page.locator("#result-date-note").innerText(), /覆盖今天.*2026\/08\/31.*2026\/09\/01/);
    assert.equal(await page.locator('path[data-kind="actual"]').count(), 2);

    // An invalid input must never submit an incomplete historical day.
    await page.locator("#history-end").fill("2026-08-31");
    await page.locator("#history-end").dispatchEvent("change");
    assert.equal(await page.locator("#run-button").isDisabled(), true);
    assert.match(await page.locator("#run-hint").innerText(), /昨天/);
    assert.equal(posts, 0);
    await page.locator("#history-end").fill("2026-08-30");
    await page.locator("#history-end").dispatchEvent("change");
    assert.equal(await page.locator("#run-button").isEnabled(), true);

    // A missing default must clear cached and legacy curves, including on refresh.
    const futureRun = { ...current.run, run_id: "future-run" };
    for (const key of ["history_start", "history_end", "forecast_start", "forecast_end"]) {
      futureRun[key] = new Date(Date.parse(current.run[key]) + 2 * 86400000 + 8 * 3600000).toISOString().replace(".000Z", "+08:00");
    }
    latest = { ...current, run: futureRun };
    await page.locator("#station-picker").selectOption("station_2");
    await page.getByText("今天暂无匹配预测结果", { exact: true }).waitFor();
    assert.equal(await page.locator('path[data-kind="forecast"]').count(), 0);
    latest = null;
    storedOverride = futureRun;
    await page.locator("#station-picker").selectOption("station_1");
    await page.getByText("今天暂无匹配预测结果", { exact: true }).waitFor();
    assert.equal(await page.locator('path[data-kind="forecast"]').count(), 0);
    storedOverride = null;
    await page.clock.runFor(60_000);
    assert.equal(await page.locator('path[data-kind="forecast"]').count(), 0);

    latest = current;
    await page.locator("#granularity").selectOption("300");
    await page.getByText("今天暂无匹配预测结果", { exact: true }).waitFor();
    await page.locator("#granularity").selectOption("900");
    await page.waitForFunction(() => document.querySelector("#result-model-meta").textContent === "3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度");

    // A page left open across the end of its forecast must query today's default.
    latest = null;
    await page.clock.setSystemTime(new Date("2026-09-02T00:01:00+08:00"));
    await page.clock.runFor(60_000);
    await page.getByText("今天暂无匹配预测结果", { exact: true }).waitFor();
    assert.equal(await page.locator('path[data-kind="forecast"]').count(), 0);

    // Explicit historical requests remain available and are visibly labelled.
    await page.locator("#run-button").click();
    await page.waitForFunction(() => document.querySelector("#result-model-meta").textContent === "3 个连续有效周 · 负载周期 7 天 · 15 分钟粒度");
    assert.match(await page.locator("#result-date-note").innerText(), /历史预测/);
    assert.equal(posts, 1);
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const theme of ["light", "dark"]) {
        await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({ path: `/tmp/m3-date-${width}-${theme}.png`, fullPage: true });
      }
    }
    if (authMode === "query_token") {
      expired = true;
      await page.evaluate(() => window.loadDashboard());
      assert.equal(await page.locator("#run-button").isDisabled(), true);
      assert.match(await page.locator("#error-state").innerText(), /登录状态已失效，请从 NocoBase 重新打开页面/);
      assert.equal(await page.evaluate(() => JSON.stringify(sessionStorage).includes("test-user.token-123")), false);
    }
    assert.deepEqual(errors, []);
    console.log("m3_current_day_e2e_ok");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
