"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const PRODUCTION_FLOW = path.join(
  ROOT,
  "m3",
  "node_red",
  "m3_production_gateway_flow.json",
);

function nodeById(flowPath, id) {
  const nodes = JSON.parse(fs.readFileSync(flowPath, "utf8"));
  const node = nodes.find((item) => item.id === id);
  assert.ok(node, `missing Node-RED node ${id}`);
  return node;
}

function runFunction(node, msg, environment) {
  const sandbox = {
    Buffer,
    msg,
    env: { get: (name) => environment[name] },
  };
  return vm.runInNewContext(`(() => { ${node.func}\n })()`, sandbox);
}

const authPrepare = nodeById(PRODUCTION_FLOW, "m3_prod_auth_prepare");
const pagePrepare = nodeById(PRODUCTION_FLOW, "m3_prod_page_origin");

{
  const request = {
    req: { query: {}, headers: { authorization: "Bearer current-user-token" } },
  };
  const result = runFunction(authPrepare, request, {
    M3_AUTH_MODE: "postmessage",
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result.length, 3);
  assert.strictEqual(result[0], request);
  assert.strictEqual(result[1], null);
  assert.strictEqual(result[2], null);
  assert.strictEqual(request.url, "https://ems.lvkpower.com/api/auth:check");
  assert.strictEqual(request.headers.Authorization, "Bearer current-user-token");
}

{
  const request = { req: { query: {}, headers: {} } };
  const result = runFunction(authPrepare, request, {
    M3_AUTH_MODE: "server_token",
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result.length, 3);
  assert.strictEqual(result[0], null);
  assert.strictEqual(result[1], null);
  assert.strictEqual(result[2], request);
  assert.strictEqual(request.payload, "");
  assert.strictEqual(request.req.headers.authorization, undefined);
  assert.strictEqual(request.url, undefined);
}

for (const mode of [undefined, "", "SERVER_TOKEN", "server-token", "unknown"]) {
  const request = { req: { query: {}, headers: {} } };
  const result = runFunction(authPrepare, request, {
    M3_AUTH_MODE: mode,
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], null, String(mode));
  assert.strictEqual(result[2], null, String(mode));
  assert.strictEqual(result[1].statusCode, 503, String(mode));
  assert.strictEqual(result[1].payload.error.code, "auth_unavailable", String(mode));
}

for (const invalidOrigin of [
  "https://ems.lvkpower.com/",
  "https://ems.lvkpower.com/path",
  "https://ems.lvkpower.com?query=1",
  "https://ems.lvkpower.com#fragment",
  "https://user:secret@ems.lvkpower.com",
  "https://ems.lvkpower.com:65536",
]) {
  const request = {
    req: { query: {}, headers: { authorization: "Bearer current-user-token" } },
  };
  const result = runFunction(authPrepare, request, {
    M3_AUTH_MODE: "postmessage",
    M3_AUTH_BASE_URL: invalidOrigin,
  });
  assert.strictEqual(result[0], null, invalidOrigin);
  assert.strictEqual(result[2], null, invalidOrigin);
  assert.strictEqual(result[1].statusCode, 503, invalidOrigin);
  assert.strictEqual(result[1].payload.error.code, "auth_unavailable", invalidOrigin);
}

assert.strictEqual(
  fs.existsSync(PRODUCTION_FLOW),
  true,
  `missing production Node-RED flow: ${PRODUCTION_FLOW}`,
);
const productionAuth = nodeById(PRODUCTION_FLOW, "m3_prod_auth_prepare");
const productionPage = nodeById(PRODUCTION_FLOW, "m3_prod_page_origin");

{
  const request = {
    req: { query: {}, headers: { authorization: "Bearer current-user-token" } },
  };
  const result = runFunction(productionAuth, request, {
    M3_AUTH_MODE: "postmessage",
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], request);
  assert.strictEqual(result[1], null);
  assert.strictEqual(result[2], null);
  assert.strictEqual(request.url, "https://ems.lvkpower.com/api/auth:check");
}

{
  const request = { req: { query: {}, headers: {} } };
  const result = runFunction(productionAuth, request, {
    M3_AUTH_MODE: "server_token",
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], null);
  assert.strictEqual(result[1], null);
  assert.strictEqual(result[2], request);
}

for (const mode of [undefined, ""] ) {
  const request = { req: { query: {}, headers: {} } };
  const result = runFunction(productionAuth, request, {
    M3_AUTH_MODE: mode,
    M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], null, String(mode));
  assert.strictEqual(result[1].statusCode, 503, String(mode));
  assert.strictEqual(result[1].payload.error.code, "auth_unavailable", String(mode));
}

{
  const request = { req: { query: {} } };
  const result = runFunction(productionPage, request, {
    M3_AUTH_MODE: "server_token",
    M3_NOCOBASE_PAGE_ORIGIN: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], request);
  assert.strictEqual(result[1], null);
}

for (const mode of [undefined, ""] ) {
  const request = { req: { query: {} } };
  const result = runFunction(productionPage, request, {
    M3_AUTH_MODE: mode,
    M3_NOCOBASE_PAGE_ORIGIN: "https://ems.lvkpower.com",
  });
  assert.strictEqual(result[0], null, String(mode));
  assert.strictEqual(result[1].statusCode, 503, String(mode));
}

{
  const request = { req: { query: {} } };
  const result = runFunction(pagePrepare, request, {
    M3_AUTH_MODE: "server_token",
    M3_NOCOBASE_PAGE_ORIGIN: "http://192.168.1.53",
  });
  assert.strictEqual(result[0], request);
  assert.strictEqual(result[1], null);
  assert.strictEqual(request.m3DashboardAuthModeJson, '"server_token"');
  assert.strictEqual(request.m3NocobaseParentOriginJson, '"http://192.168.1.53"');
}

{
  const request = { req: { query: {} } };
  const result = runFunction(pagePrepare, request, {
    M3_AUTH_MODE: "bad-mode",
    M3_NOCOBASE_PAGE_ORIGIN: "http://192.168.1.53",
  });
  assert.strictEqual(result[0], null);
  assert.strictEqual(result[1].statusCode, 503);
}

process.stdout.write("m3_dashboard_auth_modes_ok\n");

// URL tokens only bootstrap the page. APIs always require a Bearer header.
const tokenEnv = {
  M3_AUTH_MODE: "query_token",
  M3_AUTH_BASE_URL: "https://ems.lvkpower.com",
  M3_NOCOBASE_PAGE_ORIGIN: "https://ems.lvkpower.com",
  M3_STATIONS_JSON: JSON.stringify([
    { station_id: "ES01", station_key: "station_1", station_name: "1# 电站" },
    { station_id: "ES02", station_key: "station_2", station_name: "2# 电站" },
  ]),
};
for (const [query, expected] of [
  [{ token: "test-user.token-123" }, 200],
  [{}, 401], [{ token: "" }, 401], [{ token: "{{ ctx.token }}" }, 401],
  [{ token: ["one", "two"] }, 401], [{ token: "a\r\nb" }, 401],
  [{ token: "x".repeat(4097) }, 401], [{ token: "<script>" }, 401],
  [{ token: "test", extra: "1" }, 400],
]) {
  const request = { req: { query } };
  const result = runFunction(pagePrepare, request, tokenEnv);
  assert.equal(request.statusCode, expected);
  assert.equal(result[expected === 200 ? 0 : 1], request);
  if (expected === 200) assert.equal(request.m3DashboardAuthModeJson, '"query_token"');
}
const customPrepare = nodeById(PRODUCTION_FLOW, "m3_prod_custom_prepare");
for (const node of [authPrepare, customPrepare]) {
  for (const valid of [true, false]) {
    const request = { req: {
      query: node === customPrepare ? { interval_seconds: "900", forecast_days: "1" } : {},
      headers: valid ? { authorization: "Bearer test-user.token-123" } : {},
      method: "GET", route: { path: "/energy-forecast-api/custom-runs/:station_key/latest" },
      params: { station_key: "station_1" },
    } };
    const result = runFunction(node, request, tokenEnv);
    assert.equal(result[2], null, "must never bypass authentication");
    if (valid) {
      assert.equal(result[0], request);
      assert.equal(request.url, "https://ems.lvkpower.com/api/auth:check");
      assert.equal(request.headers.Authorization, "Bearer test-user.token-123");
    } else assert.equal(result[1].statusCode, 401);
  }
}
for (const id of ["m3_prod_auth_validate", "m3_prod_custom_auth_validate"]) {
  for (const [statusCode, payload, expected] of [
    [401, {}, 401], [403, {}, 401], [500, {}, 503], [200, { data: null }, 401],
  ]) {
    const result = runFunction(nodeById(PRODUCTION_FLOW, id), {
      statusCode, payload, req: { headers: { authorization: "Bearer test" } },
    }, tokenEnv);
    assert.equal(result[0], null);
    assert.equal(result[1].statusCode, expected);
    assert.equal(result[1].req.headers.authorization, undefined);
  }
}
process.stdout.write("m3_query_token_gateway_ok\n");
