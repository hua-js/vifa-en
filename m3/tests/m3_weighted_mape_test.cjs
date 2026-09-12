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

// Both midnight and evening ranges are local-time intervals, with exclusive ends.
for (const [time, expected] of [
  ["2026-09-11T16:00:00Z", 28], // 00:00
  ["2026-09-12T06:59:59+08:00", 28],
  ["2026-09-12T07:00:00+08:00", 55],
  ["2026-09-12T20:59:59+08:00", 55],
  ["2026-09-12T13:00:00Z", 28], // 21:00
  ["2026-09-12T23:59:59+08:00", 28],
  ["2026-09-13T00:00:00+08:00", 28],
]) {
  const input = load([point(time, 10, 20), point("2026-09-12T12:00:00+08:00", 100, 110)]);
  const before = JSON.stringify(input);
  near(score(input).displayed, expected);
  near(score(input).standard, 55);
  assert.equal(JSON.stringify(input), before, "Scoring must not alter forecasts");
  near(score({ ...input, unique_id: "storage_soc" }).displayed, 55);
}

// Zero actuals count toward fill status but never toward either MAPE denominator.
const mixed = score(load([
  point("2026-09-12T01:00:00+08:00", 10, 20),
  point("2026-09-12T12:00:00+08:00", 100, 110),
  point("2026-09-12T12:15:00+08:00", 0, 100),
  point("2026-09-12T12:30:00+08:00", 1, 100, "imputed"),
  point("2026-09-12T12:45:00+08:00", null, 100),
  point("2026-09-12T13:00:00+08:00", 1, NaN),
]));
near(mixed.displayed, 28);
assert.equal(mixed.actualCount, 3);
assert.equal(score(load([])).displayed, null);
assert.equal(score(load([point("2026-09-12T12:00:00Z", 0, 10)])).displayed, null);
near(score(load([point("2026-09-12T01:00:00+08:00", 10, 20)])).displayed, 100);
console.log("weighted MAPE: time boundaries, quality, zero values, SOC and immutable predictions passed");

const threshold = score(load([
  point("2026-09-12T01:00:00+08:00", 100, 109.999),
  point("2026-09-12T01:15:00+08:00", 100, 110),
  point("2026-09-12T01:30:00+08:00", 100, 90),
]));
assert.equal(threshold.scoredCount, 2);
near(threshold.displayed, 20 / 3);
const smallOnly = score(load([point("2026-09-12T01:00:00+08:00", 1, 9)]));
assert.equal(smallOnly.displayed, 0);
near(smallOnly.standard, 800);
console.log("10 kW inclusive threshold, negative residual and small-load exclusion passed");

// Daytime errors below 10 kW remain fully scored, even at the 07:00 boundary.
for (const time of ["2026-09-12T07:00:00+08:00", "2026-09-12T20:59:59+08:00"]) {
  const day = score(load([point(time, 100, 105)]));
  near(day.displayed, 5);
  assert.equal(day.toleratedCount, 0);
}
const sharedDenominator = score(load([
  point("2026-09-12T01:00:00+08:00", 1, 9),
  point("2026-09-12T21:00:00+08:00", 1, 9),
  point("2026-09-12T12:00:00+08:00", 100, 105),
]));
near(sharedDenominator.displayed, 5 / 1.5);
assert.equal(sharedDenominator.toleratedCount, 2);
console.log("Night-only tolerance and retained weighted denominator passed");
