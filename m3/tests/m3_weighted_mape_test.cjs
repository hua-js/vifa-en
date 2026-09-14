"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../node_red/m3_production_gateway_template.html"), "utf8");
const source = html.slice(html.indexOf("      function currentPredictionMape("), html.indexOf("      function renderCurrentPredictionMape("));
const score = vm.runInNewContext(`${source}; currentPredictionMape`, { finite: Number.isFinite });
const point = (time, actual, forecast, quality = "valid") => ({
  target_time: time, actual_value: actual, forecast_value: forecast, actual_quality: quality,
});
const load = points => ({ unique_id: "station_total_load", points });
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);

// The frontend must use the API score, even when the points imply another MAPE.
const points = [point("2026-09-12T01:00:00+08:00", 1, 9)];
const series = load(points);
series.current_score = { policy: "load-night-weighted-mape-v1", unique_id: "station_total_load",
  actual_count: 1, valid_count: 1, mape_percent: 12.5 };
near(score(series).displayed, 12.5);
assert.equal(score(load(points)).displayed, null);
series.current_score.policy = "old";
assert.equal(score(series).displayed, null);
series.current_score.policy = "load-night-weighted-mape-v1";
series.current_score.valid_count = 2;
assert.equal(score(series).displayed, null);
near(score({ unique_id: "storage_soc", points }).displayed, 800);
console.log("Backend score consumption, missing/mismatched score and unchanged SOC covered");
