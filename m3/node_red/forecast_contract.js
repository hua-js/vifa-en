"use strict";

const crypto = require("crypto");

const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000;
const BUCKET_MS = 15 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_WINDOW_MS = 7 * DAY_MS;
const MAX_GAP_MS = 120000;
const MIN_LOAD_COVERAGE_MS = 12 * 60 * 1000;
const MAX_SOC_AGE_MS = 90000;
const SERIES_IDS = Object.freeze(["station_total_load", "storage_1_soc", "storage_2_soc"]);
const SERIES_SET = new Set(SERIES_IDS);
const STATION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const SAFE_NAME = /^[a-z][a-z0-9_]{0,63}$/;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const PATH_SEGMENT = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const FORBIDDEN_SEGMENTS = new Set(["__proto__", "prototype", "constructor"]);
const SHANGHAI_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?\+08:00$/;
const NOCOBASE_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?(Z|([+-])(\d{2}):(\d{2}))$/;
const MAPPING_REQUIRED = Object.freeze([
  "timestamp", "stationTotalLoad", "storage1Soc", "storage2Soc",
]);

class ContractError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ContractError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new ContractError(code, message);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requirePlainObject(value, code, label) {
  if (!isPlainObject(value)) fail(code, `${label} must be an exact object`);
  for (const key of Object.keys(value)) {
    if (FORBIDDEN_SEGMENTS.has(key)) fail(code, `${label} contains an unsafe key`);
  }
  return value;
}

function exactKeys(value, required, optional, code, label) {
  requirePlainObject(value, code, label);
  const allowed = new Set([...required, ...optional]);
  const keys = Object.keys(value);
  if (required.some((key) => !Object.prototype.hasOwnProperty.call(value, key)) || keys.some((key) => !allowed.has(key)))
    fail(code, `${label} has invalid fields`);
  return value;
}

function exactString(value, code, label, maximum = 4096) {
  if (typeof value !== "string" || value.length === 0 || value.length > maximum)
    fail(code, `${label} must be a bounded non-empty string`);
  return value;
}

function headerToken(value, code, label) {
  const token = exactString(value, code, label, 4096);
  if (/[^\x21-\x7e]/.test(token)) fail(code, `${label} contains invalid header characters`);
  return token;
}

function finiteNumber(value, code = "source_contract_invalid", label = "value") {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(code, `${label} must be a finite number`);
  return value;
}

function exactInteger(value, code, label, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum)
    fail(code, `${label} must be a bounded integer`);
  return value;
}

function validateStationId(value, code = "invalid_request") {
  if (typeof value !== "string" || value === "." || value === ".." || !STATION_ID.test(value))
    fail(code, "station_id is invalid");
  return value;
}

function isoShanghai(milliseconds) {
  if (!Number.isFinite(milliseconds)) fail("invalid_request", "timestamp is invalid");
  return new Date(milliseconds + SHANGHAI_OFFSET_MS).toISOString().replace(".000Z", "+08:00").replace("Z", "+08:00");
}

function parseShanghaiTimestamp(value, options = {}) {
  const code = options.code || "invalid_request";
  if (typeof value !== "string") fail(code, "timestamp must be an exact string");
  const match = SHANGHAI_TIMESTAMP.exec(value);
  if (!match) fail(code, "timestamp must use explicit Asia/Shanghai +08:00");
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const fractional = match[7] || "";
  const nonzeroFraction = /[1-9]/.test(fractional);
  if (options.wholeSecond && nonzeroFraction)
    fail(code, "timestamp must be on a whole-second boundary");
  if (options.quarterHour && (minute % 15 !== 0 || second !== 0 || nonzeroFraction))
    fail(code, "timestamp must be on a quarter-hour boundary");
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) fail(code, "timestamp is invalid");
  const local = new Date(milliseconds + SHANGHAI_OFFSET_MS);
  if (
    local.getUTCFullYear() !== year || local.getUTCMonth() + 1 !== month || local.getUTCDate() !== day ||
    local.getUTCHours() !== hour || local.getUTCMinutes() !== minute || local.getUTCSeconds() !== second
  ) fail(code, "timestamp has an invalid calendar value");
  return milliseconds;
}

function parseNocoBaseTimestamp(value, options = {}) {
  const code = options.code || "dashboard_contract_invalid";
  if (typeof value !== "string") fail(code, "NocoBase timestamp must be an exact string");
  const match = NOCOBASE_TIMESTAMP.exec(value);
  if (!match) fail(code, "NocoBase timestamp must be an explicit timezone-aware ISO instant");
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const fractional = match[7] || "";
  const zone = match[8];
  let offsetMinutes = 0;
  if (zone !== "Z") {
    const offsetHours = Number(match[10]);
    const offsetRemainder = Number(match[11]);
    if (offsetHours > 14 || offsetRemainder > 59 || (offsetHours === 14 && offsetRemainder !== 0))
      fail(code, "NocoBase timestamp offset is invalid");
    offsetMinutes = (offsetHours * 60 + offsetRemainder) * (match[9] === "+" ? 1 : -1);
  }
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) fail(code, "NocoBase timestamp is invalid");
  const representedLocal = new Date(milliseconds + offsetMinutes * 60000);
  const fractionalMilliseconds = Number(`${fractional.slice(1)}000`.slice(0, 3));
  if (
    representedLocal.getUTCFullYear() !== year || representedLocal.getUTCMonth() + 1 !== month || representedLocal.getUTCDate() !== day ||
    representedLocal.getUTCHours() !== hour || representedLocal.getUTCMinutes() !== minute || representedLocal.getUTCSeconds() !== second ||
    representedLocal.getUTCMilliseconds() !== fractionalMilliseconds
  ) fail(code, "NocoBase timestamp has an invalid calendar value");
  const nonzeroFraction = /[1-9]/.test(fractional);
  if ((options.wholeSecond || options.quarterHour) && nonzeroFraction)
    fail(code, "NocoBase boundary timestamp has nonzero fractional precision");
  const shanghai = new Date(milliseconds + SHANGHAI_OFFSET_MS);
  if (options.wholeSecond && shanghai.getUTCMilliseconds() !== 0)
    fail(code, "NocoBase timestamp must be a whole-second instant");
  if (
    options.quarterHour &&
    (shanghai.getUTCMinutes() % 15 !== 0 || shanghai.getUTCSeconds() !== 0 || shanghai.getUTCMilliseconds() !== 0)
  ) fail(code, "NocoBase timestamp must be a Shanghai quarter-hour instant");
  return milliseconds;
}

function bucketStart(value) {
  const milliseconds = typeof value === "number" ? value : parseShanghaiTimestamp(value, { code: "invalid_request" });
  if (!Number.isFinite(milliseconds)) fail("invalid_request", "invalid timestamp");
  return new Date(Math.floor(milliseconds / BUCKET_MS) * BUCKET_MS);
}

function validateRange(start, end, code = "invalid_request") {
  const startMs = parseShanghaiTimestamp(start, { quarterHour: true, code });
  const endMs = parseShanghaiTimestamp(end, { quarterHour: true, code });
  if (endMs <= startMs || endMs - startMs > MAX_WINDOW_MS) fail(code, "range must be in (0, 7 days]");
  return { startMs, endMs };
}

function authorizeBearer(header, configuredToken) {
  const token = headerToken(configuredToken, "source_not_configured", "configured bearer");
  if (typeof header !== "string" || !header.startsWith("Bearer ") || header.length !== token.length + 7)
    fail("unauthorized", "authorization failed");
  const supplied = Buffer.from(header.slice(7));
  const expected = Buffer.from(token);
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected))
    fail("unauthorized", "authorization failed");
  return true;
}

function parsePath(value, code = "source_not_configured") {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) fail(code, "mapping path is invalid");
  const segments = value.split(".");
  if (segments.length > 8 || segments.some((segment) => !PATH_SEGMENT.test(segment) || FORBIDDEN_SEGMENTS.has(segment)))
    fail(code, "mapping path is unsafe");
  return segments;
}

function validateMapping(mapping) {
  exactKeys(mapping, MAPPING_REQUIRED, ["updatedAt"], "source_not_configured", "field mapping");
  const output = {};
  for (const key of MAPPING_REQUIRED) {
    parsePath(mapping[key]);
    output[key] = mapping[key];
  }
  if (Object.prototype.hasOwnProperty.call(mapping, "updatedAt")) {
    parsePath(mapping.updatedAt);
    output.updatedAt = mapping.updatedAt;
  }
  return output;
}

function getOwnPath(record, path, code = "source_contract_invalid") {
  let value = record;
  for (const segment of parsePath(path, code)) {
    if (!isPlainObject(value) || !Object.prototype.hasOwnProperty.call(value, segment))
      fail(code, "mapped source field is missing");
    value = value[segment];
  }
  return value;
}

function nullableMappedNumber(record, path, label) {
  const value = getOwnPath(record, path);
  if (value === null) return null;
  return finiteNumber(value, "source_contract_invalid", label);
}

function recordIdentity(record, mapping) {
  requirePlainObject(record, "source_contract_invalid", "source record");
  const timestamp = getOwnPath(record, mapping.timestamp);
  const time = parseShanghaiTimestamp(timestamp, { code: "source_contract_invalid" });
  let revision = 1;
  if (mapping.updatedAt) {
    revision = parseShanghaiTimestamp(getOwnPath(record, mapping.updatedAt), { code: "source_contract_invalid" });
    if (!Number.isSafeInteger(revision) || revision <= 0) fail("source_contract_invalid", "source revision is invalid");
  }
  const values = {
    load: nullableMappedNumber(record, mapping.stationTotalLoad, "load"),
    soc1: nullableMappedNumber(record, mapping.storage1Soc, "storage_1_soc"),
    soc2: nullableMappedNumber(record, mapping.storage2Soc, "storage_2_soc"),
  };
  return { time, revision, values };
}

function sameValues(left, right) {
  return left.load === right.load && left.soc1 === right.soc1 && left.soc2 === right.soc2;
}

function normalizeRecords(records, mapping, startMs, endMs) {
  if (!Array.isArray(records)) fail("source_contract_invalid", "records must be an exact array");
  const byTime = new Map();
  for (const record of records) {
    const normalized = recordIdentity(record, mapping);
    if (normalized.time < startMs || normalized.time >= endMs) continue;
    const previous = byTime.get(normalized.time);
    if (!previous || normalized.revision > previous.revision) byTime.set(normalized.time, normalized);
    else if (normalized.revision === previous.revision && !sameValues(normalized.values, previous.values))
      fail("source_contract_invalid", "duplicate source revision conflicts");
  }
  return [...byTime.values()].sort((left, right) => left.time - right.time);
}

function timeWeightedMean(samples, bucketStartMs, bucketEndMs) {
  if (bucketEndMs === undefined) {
    bucketEndMs = bucketStartMs;
    bucketStartMs = bucketEndMs - BUCKET_MS;
  }
  if (!Array.isArray(samples) || samples.length < 2) return null;
  const ordered = [...samples].sort((left, right) => left.time - right.time);
  if (ordered[0].time < bucketStartMs || ordered[ordered.length - 1].time >= bucketEndMs) return null;
  const head = ordered[0].time - bucketStartMs;
  const tail = bucketEndMs - ordered[ordered.length - 1].time;
  if (head > MAX_GAP_MS || tail > MAX_GAP_MS) return null;
  let weighted = 0;
  let covered = 0;
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const span = ordered[index + 1].time - ordered[index].time;
    if (span <= 0 || span > MAX_GAP_MS) return null;
    weighted += ordered[index].value * span;
    covered += span;
  }
  weighted += ordered[ordered.length - 1].value * tail;
  covered += tail;
  return covered >= MIN_LOAD_COVERAGE_MS ? weighted / covered : null;
}

function lastFreshSoc(samples, bucketEndMs) {
  if (!Array.isArray(samples) || samples.length === 0) return null;
  const ordered = [...samples].sort((left, right) => left.time - right.time);
  const last = ordered[ordered.length - 1];
  const age = bucketEndMs - last.time;
  return age >= 0 && age <= MAX_SOC_AGE_MS ? last.value : null;
}

function aggregateObservations({ stationId, records, mapping, start, end }) {
  validateStationId(stationId, "source_contract_invalid");
  const safeMapping = validateMapping(mapping);
  const { startMs, endMs } = validateRange(start, end, "source_contract_invalid");
  const normalized = normalizeRecords(records, safeMapping, startMs, endMs);
  const buckets = new Map();
  for (let cursor = startMs; cursor < endMs; cursor += BUCKET_MS)
    buckets.set(cursor, { load: [], soc1: [], soc2: [], revision: 1 });
  for (const record of normalized) {
    const bucket = Math.floor(record.time / BUCKET_MS) * BUCKET_MS;
    const target = buckets.get(bucket);
    if (!target) continue;
    if (record.values.load !== null && record.values.load >= 0) target.load.push({ time: record.time, value: record.values.load });
    if (record.values.soc1 !== null && record.values.soc1 >= 0 && record.values.soc1 <= 100) target.soc1.push({ time: record.time, value: record.values.soc1 });
    if (record.values.soc2 !== null && record.values.soc2 >= 0 && record.values.soc2 <= 100) target.soc2.push({ time: record.time, value: record.values.soc2 });
    target.revision = Math.max(target.revision, record.revision);
  }
  const points = [];
  for (const [bucket, values] of buckets) {
    const bucketEndMs = bucket + BUCKET_MS;
    const output = [
      ["station_total_load", timeWeightedMean(values.load, bucket, bucketEndMs)],
      ["storage_1_soc", lastFreshSoc(values.soc1, bucketEndMs)],
      ["storage_2_soc", lastFreshSoc(values.soc2, bucketEndMs)],
    ];
    for (const [uniqueId, value] of output) {
      points.push({
        unique_id: uniqueId,
        ds: isoShanghai(bucket),
        y: value,
        quality: value === null ? "invalid" : "valid",
        source_revision: values.revision,
      });
    }
  }
  return points;
}

function cursorSecret(value) {
  if (typeof value !== "string" || Buffer.byteLength(value) < 32 || Buffer.byteLength(value) > 4096)
    fail("source_not_configured", "cursor secret must contain at least 32 bytes");
  return value;
}

function cursorPayload(binding, offset) {
  validateStationId(binding.stationId);
  validateRange(binding.start, binding.end);
  exactInteger(offset, "invalid_request", "cursor offset", 1, 4096);
  return { v: 1, station_id: binding.stationId, start: binding.start, end: binding.end, offset };
}

function encodeCursor(binding, offset, secret) {
  const payload = Buffer.from(JSON.stringify(cursorPayload(binding, offset))).toString("base64url");
  const signature = crypto.createHmac("sha256", cursorSecret(secret)).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function decodeCursor(cursor, binding, secret) {
  if (typeof cursor !== "string" || cursor.length === 0 || cursor.length > 1024 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(cursor))
    fail("invalid_request", "cursor is invalid");
  const [payload, suppliedSignature] = cursor.split(".");
  const expectedSignature = crypto.createHmac("sha256", cursorSecret(secret)).update(payload).digest("base64url");
  const supplied = Buffer.from(suppliedSignature);
  const expected = Buffer.from(expectedSignature);
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) fail("invalid_request", "cursor is invalid");
  let decoded;
  try {
    decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  } catch (_error) {
    fail("invalid_request", "cursor is invalid");
  }
  exactKeys(decoded, ["v", "station_id", "start", "end", "offset"], [], "invalid_request", "cursor");
  if (decoded.v !== 1 || decoded.station_id !== binding.stationId || decoded.start !== binding.start || decoded.end !== binding.end)
    fail("invalid_request", "cursor binding is invalid");
  exactInteger(decoded.offset, "invalid_request", "cursor offset", 1, 4096);
  return decoded.offset;
}

function validateObservationRequest({ stationId, query, cursorSecret: secret }) {
  validateStationId(stationId);
  exactKeys(query, ["start", "end"], ["cursor"], "invalid_request", "observation query");
  const { startMs, endMs } = validateRange(query.start, query.end);
  const offset = Object.prototype.hasOwnProperty.call(query, "cursor")
    ? decodeCursor(query.cursor, { stationId, start: query.start, end: query.end }, secret)
    : 0;
  return { stationId, start: query.start, end: query.end, startMs, endMs, offset };
}

function paginatePoints({ points, stationId, start, end, offset, pageSize = 512, cursorSecret: secret }) {
  if (!Array.isArray(points)) fail("source_contract_invalid", "points must be an array");
  exactInteger(offset, "invalid_request", "page offset", 0, 4096);
  exactInteger(pageSize, "source_not_configured", "page size", 1, 512);
  if (offset > points.length) fail("invalid_request", "cursor offset exceeds result size");
  const page = points.slice(offset, offset + pageSize);
  const nextOffset = offset + page.length;
  return {
    points: page,
    next_cursor: nextOffset < points.length ? encodeCursor({ stationId, start, end }, nextOffset, secret) : null,
  };
}

function serializedSize(value, code) {
  let serialized;
  try {
    serialized = JSON.stringify(value);
  } catch (_error) {
    fail(code, "payload is not JSON serializable");
  }
  if (serialized === undefined) fail(code, "payload is not JSON serializable");
  return Buffer.byteLength(serialized);
}

function validatePaginationMeta(meta, { page, pageSize, code }) {
  exactKeys(meta, ["count", "page", "pageSize", "totalPage"], [], code, "response metadata");
  exactInteger(meta.count, code, "response count", 0);
  exactInteger(meta.page, code, "response page", 1);
  exactInteger(meta.pageSize, code, "response page size", 1);
  exactInteger(meta.totalPage, code, "response total pages", 0);
  if (meta.page !== page || meta.pageSize !== pageSize) fail(code, "response pagination request mismatch");
  const expectedTotalPage = meta.count === 0 ? 0 : Math.ceil(meta.count / meta.pageSize);
  if (meta.totalPage !== expectedTotalPage || (meta.totalPage > 0 && meta.page > meta.totalPage))
    fail(code, "response pagination is inconsistent");
  return meta;
}

function extractRecordEnvelope(payload, limits = {}) {
  const maxBytes = limits.maxBytes === undefined ? 4 * 1024 * 1024 : limits.maxBytes;
  const maxRecords = limits.maxRecords === undefined ? 50000 : limits.maxRecords;
  exactInteger(maxBytes, "source_not_configured", "maximum source bytes", 1, 16 * 1024 * 1024);
  exactInteger(maxRecords, "source_not_configured", "maximum source records", 1, 100000);
  if (serializedSize(payload, "source_contract_invalid") > maxBytes) fail("payload_too_large", "source response is too large");
  exactKeys(payload, ["data"], ["meta"], "source_contract_invalid", "source response");
  if (!Array.isArray(payload.data) || payload.data.length > maxRecords) fail("source_contract_invalid", "source response records are invalid");
  if (Object.prototype.hasOwnProperty.call(payload, "meta")) {
    requirePlainObject(payload.meta, "source_contract_invalid", "response metadata");
    exactInteger(payload.meta.pageSize, "source_contract_invalid", "response page size", 1, maxRecords);
    const meta = validatePaginationMeta(payload.meta, { page: 1, pageSize: payload.meta.pageSize, code: "source_contract_invalid" });
    if (meta.totalPage > 1 || meta.count !== payload.data.length || (meta.totalPage === 0 ? payload.data.length !== 0 : meta.totalPage !== 1))
      fail("source_contract_invalid", "source response must contain the complete request window");
  }
  return payload.data;
}

function validateEmptyQuery(query) {
  exactKeys(query, [], [], "invalid_request", "query");
  return {};
}

function extractAcceptanceEnvelope(payload, limits = {}) {
  const maxBytes = limits.maxBytes === undefined ? 65536 : limits.maxBytes;
  exactInteger(maxBytes, "source_not_configured", "maximum acceptance bytes", 1, 1024 * 1024);
  if (serializedSize(payload, "source_contract_invalid") > maxBytes) fail("payload_too_large", "acceptance response is too large");
  exactKeys(payload, ["data"], [], "source_contract_invalid", "acceptance response");
  return validateAcceptanceContext(payload.data);
}

function validateConfiguredUrl(value, expectedPath) {
  if (typeof value !== "string" || value.length === 0 || value.length > 2048)
    fail("source_not_configured", "configured URL is invalid");
  let parsed;
  try { parsed = new URL(value); } catch (_error) { fail("source_not_configured", "configured URL is invalid"); }
  if (
    !["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password ||
    parsed.search || parsed.hash || (expectedPath !== undefined && parsed.pathname !== expectedPath)
  ) fail("source_not_configured", "configured URL is not an exact HTTP target");
  return value;
}

function extractNocoBasePage(payload, limits = {}) {
  const maxBytes = limits.maxBytes === undefined ? 4 * 1024 * 1024 : limits.maxBytes;
  const maxRows = limits.maxRows === undefined ? 4096 : limits.maxRows;
  const requestedPage = limits.page === undefined ? 1 : limits.page;
  const requestedPageSize = limits.pageSize;
  const mode = limits.mode || "complete";
  exactInteger(maxBytes, "source_not_configured", "maximum dashboard bytes", 1, 16 * 1024 * 1024);
  exactInteger(maxRows, "source_not_configured", "maximum dashboard rows", 1, 100000);
  exactInteger(requestedPage, "source_not_configured", "requested page", 1);
  exactInteger(requestedPageSize, "source_not_configured", "requested page size", 1, 100000);
  if (!["complete", "head"].includes(mode)) fail("source_not_configured", "pagination mode is invalid");
  if (serializedSize(payload, "dashboard_contract_invalid") > maxBytes) fail("payload_too_large", "dashboard response is too large");
  exactKeys(payload, ["data", "meta"], [], "dashboard_contract_invalid", "NocoBase response");
  if (!Array.isArray(payload.data) || payload.data.length > maxRows || payload.data.length > requestedPageSize)
    fail("dashboard_contract_invalid", "NocoBase rows are invalid");
  const meta = validatePaginationMeta(payload.meta, { page: requestedPage, pageSize: requestedPageSize, code: "dashboard_contract_invalid" });
  if (mode === "complete") {
    if (meta.totalPage > 1 || meta.count > maxRows || payload.data.length !== meta.count)
      fail("dashboard_contract_invalid", "NocoBase response is incomplete");
  } else {
    const expectedRows = meta.totalPage > requestedPage ? requestedPageSize : meta.count - (requestedPage - 1) * requestedPageSize;
    if (payload.data.length !== expectedRows) fail("dashboard_contract_invalid", "NocoBase head page is incomplete");
  }
  return payload.data;
}

function validateAcceptanceContext(value) {
  exactKeys(value, ["active", "acceptance_run_id", "window_start", "window_end"], [], "source_contract_invalid", "acceptance context");
  if (typeof value.active !== "boolean") fail("source_contract_invalid", "acceptance active must be a boolean");
  if (!value.active) {
    if (value.acceptance_run_id !== null || value.window_start !== null || value.window_end !== null)
      fail("source_contract_invalid", "inactive acceptance context must not expose a run");
  } else {
    if (typeof value.acceptance_run_id !== "string" || !RUN_ID.test(value.acceptance_run_id))
      fail("source_contract_invalid", "acceptance run is invalid");
    const startMs = parseShanghaiTimestamp(value.window_start, { quarterHour: true, code: "source_contract_invalid" });
    const endMs = parseShanghaiTimestamp(value.window_end, { quarterHour: true, code: "source_contract_invalid" });
    if (endMs - startMs !== MAX_WINDOW_MS) fail("source_contract_invalid", "acceptance window must be exactly seven days");
  }
  return { active: value.active, acceptance_run_id: value.acceptance_run_id, window_start: value.window_start, window_end: value.window_end };
}

function validateAlertRequest(value) {
  exactKeys(value, ["station_id", "task", "error_code", "at"], [], "invalid_request", "alert body");
  validateStationId(value.station_id);
  if (typeof value.task !== "string" || !SAFE_NAME.test(value.task) || typeof value.error_code !== "string" || !SAFE_NAME.test(value.error_code))
    fail("invalid_request", "alert names are invalid");
  parseShanghaiTimestamp(value.at, { wholeSecond: true, code: "invalid_request" });
  return { station_id: value.station_id, task: value.task, error_code: value.error_code, at: value.at };
}

function alertAcknowledgement() {
  return { status: "ok" };
}

function validateAlertSinkAcknowledgement(value) {
  exactKeys(value, ["status"], [], "upstream_contract_invalid", "alert sink acknowledgement");
  if (value.status !== "ok") fail("upstream_contract_invalid", "alert sink acknowledgement is invalid");
  return true;
}

function validateJsonValue(value, depth = 0) {
  if (depth > 16) fail("dashboard_contract_invalid", "JSON value is too deeply nested");
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("dashboard_contract_invalid", "JSON number must be finite");
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 4096) fail("dashboard_contract_invalid", "JSON array is too large");
    for (const item of value) validateJsonValue(item, depth + 1);
    return;
  }
  requirePlainObject(value, "dashboard_contract_invalid", "JSON object");
  if (Object.keys(value).length > 256) fail("dashboard_contract_invalid", "JSON object is too large");
  for (const [key, item] of Object.entries(value)) {
    if (FORBIDDEN_SEGMENTS.has(key)) fail("dashboard_contract_invalid", "JSON object contains unsafe keys");
    validateJsonValue(item, depth + 1);
  }
}

function validateForecastPoint(point, uniqueId, index, previousTarget) {
  exactKeys(point, ["data_time", "target_time", "horizon_step", "raw_forecast", "forecast_value", "is_clipped"], [], "dashboard_contract_invalid", "forecast point");
  const dataMs = parseNocoBaseTimestamp(point.data_time, { quarterHour: true, code: "dashboard_contract_invalid" });
  const targetMs = parseNocoBaseTimestamp(point.target_time, { quarterHour: true, code: "dashboard_contract_invalid" });
  exactInteger(point.horizon_step, "dashboard_contract_invalid", "horizon step", 1, 96);
  if (point.horizon_step !== index + 1 || targetMs - dataMs !== BUCKET_MS || (previousTarget !== null && dataMs !== previousTarget))
    fail("dashboard_contract_invalid", "forecast timeline is invalid");
  finiteNumber(point.raw_forecast, "dashboard_contract_invalid", "raw forecast");
  finiteNumber(point.forecast_value, "dashboard_contract_invalid", "forecast value");
  if (typeof point.is_clipped !== "boolean" || point.is_clipped !== (point.raw_forecast !== point.forecast_value))
    fail("dashboard_contract_invalid", "clip flag is invalid");
  if (uniqueId === "station_total_load" && point.forecast_value < 0) fail("dashboard_contract_invalid", "load forecast is invalid");
  if (uniqueId !== "station_total_load" && (point.forecast_value < 0 || point.forecast_value > 100)) fail("dashboard_contract_invalid", "SOC forecast is invalid");
  return targetMs;
}

function validateForecastSeries(series) {
  exactKeys(series, ["unique_id", "unit", "model_name", "status", "points", "fallback_reason"], [], "dashboard_contract_invalid", "forecast series");
  if (!SERIES_SET.has(series.unique_id)) fail("dashboard_contract_invalid", "forecast series ID is invalid");
  const expectedUnit = series.unique_id === "station_total_load" ? "kW" : "%";
  if (series.unit !== expectedUnit || typeof series.model_name !== "string" || series.model_name.length === 0 || series.model_name.length > 128)
    fail("dashboard_contract_invalid", "forecast series metadata is invalid");
  if (!["ok", "warming_up", "degraded", "insufficient_history", "error"].includes(series.status))
    fail("dashboard_contract_invalid", "forecast series status is invalid");
  if (series.fallback_reason !== null && (typeof series.fallback_reason !== "string" || !SAFE_NAME.test(series.fallback_reason)))
    fail("dashboard_contract_invalid", "forecast fallback reason is invalid");
  if (!Array.isArray(series.points)) fail("dashboard_contract_invalid", "forecast points must be an array");
  const expectedLength = ["insufficient_history", "error"].includes(series.status) ? 0 : 96;
  if (series.points.length !== expectedLength) fail("dashboard_contract_invalid", "forecast point count is invalid");
  let previousTarget = null;
  for (let index = 0; index < series.points.length; index += 1)
    previousTarget = validateForecastPoint(series.points[index], series.unique_id, index, previousTarget);
  return series;
}

function validateLatest(latest, stationId) {
  exactKeys(latest, ["station_id", "as_of", "generated_at", "source_data_end", "status", "series_payload", "model_manifest", "content_hash", "updated_at"], [], "dashboard_contract_invalid", "latest snapshot");
  validateStationId(latest.station_id, "dashboard_contract_invalid");
  if (stationId !== undefined && latest.station_id !== stationId) fail("dashboard_contract_invalid", "latest station mismatch");
  const asOfMs = parseNocoBaseTimestamp(latest.as_of, { wholeSecond: true, code: "dashboard_contract_invalid" });
  parseNocoBaseTimestamp(latest.generated_at, { code: "dashboard_contract_invalid" });
  parseNocoBaseTimestamp(latest.source_data_end, { quarterHour: true, code: "dashboard_contract_invalid" });
  parseNocoBaseTimestamp(latest.updated_at, { code: "dashboard_contract_invalid" });
  if (!["ok", "warming_up", "degraded"].includes(latest.status)) fail("dashboard_contract_invalid", "latest status is invalid");
  if (!Array.isArray(latest.series_payload) || latest.series_payload.length !== 3) fail("dashboard_contract_invalid", "latest requires three series");
  const series = latest.series_payload.map(validateForecastSeries);
  if (series.some((item, index) => item.unique_id !== SERIES_IDS[index])) fail("dashboard_contract_invalid", "latest series order is invalid");
  const predictable = series.filter((item) => item.points.length > 0);
  if (predictable.length) {
    const reference = predictable[0].points;
    const expectedStartMs = Math.floor(asOfMs / BUCKET_MS) * BUCKET_MS;
    if (parseNocoBaseTimestamp(reference[0].data_time, { quarterHour: true, code: "dashboard_contract_invalid" }) !== expectedStartMs)
      fail("dashboard_contract_invalid", "forecast horizon must start at the as_of bucket");
    for (const item of predictable.slice(1)) {
      if (
        parseNocoBaseTimestamp(item.points[0].data_time, { quarterHour: true, code: "dashboard_contract_invalid" }) !== expectedStartMs ||
        item.points.some((point, index) =>
          parseNocoBaseTimestamp(point.data_time, { quarterHour: true, code: "dashboard_contract_invalid" }) !==
            parseNocoBaseTimestamp(reference[index].data_time, { quarterHour: true, code: "dashboard_contract_invalid" }) ||
          parseNocoBaseTimestamp(point.target_time, { quarterHour: true, code: "dashboard_contract_invalid" }) !==
            parseNocoBaseTimestamp(reference[index].target_time, { quarterHour: true, code: "dashboard_contract_invalid" })
        )
      )
        fail("dashboard_contract_invalid", "forecast series horizons do not align");
    }
  }
  validateJsonValue(latest.model_manifest);
  if (typeof latest.content_hash !== "string" || !/^[a-f0-9]{64}$/.test(latest.content_hash)) fail("dashboard_contract_invalid", "content hash is invalid");
  return latest;
}

function validateActual(point) {
  exactKeys(point, ["unique_id", "ds", "y", "quality", "source_revision"], [], "dashboard_contract_invalid", "actual point");
  if (!SERIES_SET.has(point.unique_id)) fail("dashboard_contract_invalid", "actual series is invalid");
  parseShanghaiTimestamp(point.ds, { quarterHour: true, code: "dashboard_contract_invalid" });
  exactInteger(point.source_revision, "dashboard_contract_invalid", "actual source revision", 0);
  if (!['valid', 'invalid'].includes(point.quality)) fail("dashboard_contract_invalid", "actual quality is invalid");
  if (point.quality === "invalid") {
    if (point.y !== null) fail("dashboard_contract_invalid", "invalid actual requires null");
  } else {
    finiteNumber(point.y, "dashboard_contract_invalid", "actual value");
    if (point.unique_id === "station_total_load" && point.y < 0) fail("dashboard_contract_invalid", "actual load is invalid");
    if (point.unique_id !== "station_total_load" && (point.y < 0 || point.y > 100)) fail("dashboard_contract_invalid", "actual SOC is invalid");
  }
  return point;
}

function validateMetric(value, label) {
  if (value === null) return null;
  const number = finiteNumber(value, "dashboard_contract_invalid", label);
  if (number < 0) fail("dashboard_contract_invalid", `${label} is invalid`);
  return number;
}

function validateAcceptanceResult(result) {
  exactKeys(result, ["unique_id", "expected_count", "valid_count", "zero_actual_count", "mape_percent", "mae", "smape_percent", "outcome"], [], "dashboard_contract_invalid", "acceptance result");
  if (!SERIES_SET.has(result.unique_id)) fail("dashboard_contract_invalid", "acceptance series is invalid");
  exactInteger(result.expected_count, "dashboard_contract_invalid", "expected count", 672, 672);
  exactInteger(result.valid_count, "dashboard_contract_invalid", "valid count", 0, 672);
  exactInteger(result.zero_actual_count, "dashboard_contract_invalid", "zero count", 0, 672);
  if (result.valid_count + result.zero_actual_count > result.expected_count)
    fail("dashboard_contract_invalid", "acceptance counts overlap");
  const metrics = [
    validateMetric(result.mape_percent, "MAPE"),
    validateMetric(result.mae, "MAE"),
    validateMetric(result.smape_percent, "SMAPE"),
  ];
  if (result.valid_count === 0 ? metrics.some((value) => value !== null) : metrics.some((value) => value === null))
    fail("dashboard_contract_invalid", "acceptance metrics do not match the scored count");
  const expectedOutcome = result.valid_count < 605
    ? "insufficient_data"
    : result.mape_percent <= 30 ? "passed" : "failed";
  if (result.outcome !== expectedOutcome) fail("dashboard_contract_invalid", "acceptance outcome is inconsistent");
  return result;
}

function validateAcceptanceSummary(value) {
  if (value === null) return null;
  exactKeys(value, ["acceptance_run_id", "status", "completed_days", "expected_days", "results"], [], "dashboard_contract_invalid", "acceptance summary");
  if (typeof value.acceptance_run_id !== "string" || !RUN_ID.test(value.acceptance_run_id)) fail("dashboard_contract_invalid", "acceptance run is invalid");
  if (!["in_progress", "passed", "failed", "insufficient_data"].includes(value.status)) fail("dashboard_contract_invalid", "acceptance status is invalid");
  exactInteger(value.completed_days, "dashboard_contract_invalid", "completed days", 1, 7);
  exactInteger(value.expected_days, "dashboard_contract_invalid", "expected days", 7, 7);
  if (!Array.isArray(value.results)) fail("dashboard_contract_invalid", "acceptance results are invalid");
  if (value.status === "in_progress") {
    if (value.results.length !== 0) fail("dashboard_contract_invalid", "in-progress acceptance cannot expose evaluations");
    return value;
  }
  if (value.completed_days !== 7 || value.results.length !== 3)
    fail("dashboard_contract_invalid", "final acceptance results are incomplete");
  const results = value.results.map(validateAcceptanceResult);
  if (results.some((item, index) => item.unique_id !== SERIES_IDS[index])) fail("dashboard_contract_invalid", "acceptance series order is invalid");
  const expectedStatus = results.some((item) => item.outcome === "insufficient_data")
    ? "insufficient_data"
    : results.some((item) => item.outcome === "failed") ? "failed" : "passed";
  if (value.status !== expectedStatus) fail("dashboard_contract_invalid", "acceptance status is inconsistent");
  return value;
}

function deriveDashboardActualWindow({ latest, stationId, nowMs = Date.now() }) {
  validateStationId(stationId, "dashboard_contract_invalid");
  let endMs;
  if (latest === null) {
    if (!Number.isFinite(nowMs)) fail("dashboard_contract_invalid", "now is invalid");
    endMs = Math.floor(nowMs / BUCKET_MS) * BUCKET_MS;
  } else {
    validateLatest(latest, stationId);
    const predictable = latest.series_payload.find((item) => item.points.length > 0);
    endMs = predictable
      ? parseNocoBaseTimestamp(predictable.points[0].data_time, { quarterHour: true, code: "dashboard_contract_invalid" })
      : Math.floor(parseNocoBaseTimestamp(latest.as_of, { wholeSecond: true, code: "dashboard_contract_invalid" }) / BUCKET_MS) * BUCKET_MS;
  }
  return { start: isoShanghai(endMs - DAY_MS), end: isoShanghai(endMs) };
}

function emptySeries(state) {
  return SERIES_IDS.map((uniqueId) => ({
    unique_id: uniqueId,
    unit: uniqueId === "station_total_load" ? "kW" : "%",
    model_name: null,
    status: state,
    fallback_reason: null,
    actual: [],
    forecast: [],
  }));
}

function emptyDashboard(stationId, state, nowMs) {
  validateStationId(stationId, "dashboard_contract_invalid");
  if (!["initializing", "error"].includes(state)) fail("dashboard_contract_invalid", "empty dashboard state is invalid");
  const now = isoShanghai(nowMs);
  return {
    operation: "forecast_dashboard",
    range: { timezone: "Asia/Shanghai", history_start: null, history_end: null, actual_latest: null, forecast_start: null, forecast_end: null, interval_seconds: 900, history_hours: 24, forecast_hours: 24, display_hours: 48, now_separator: now },
    system: { state, mode: state, generated_at: null, stale: false },
    series: emptySeries(state),
    acceptance: null,
  };
}

function buildDashboard({ latest, actuals, acceptance, nowMs = Date.now(), stationId, emptyState = "initializing" }) {
  if (!Number.isFinite(nowMs)) fail("dashboard_contract_invalid", "now is invalid");
  if (latest === null) return emptyDashboard(stationId, emptyState, nowMs);
  const actualWindow = deriveDashboardActualWindow({ latest, stationId: stationId === undefined ? latest.station_id : stationId, nowMs });
  if (!Array.isArray(actuals)) fail("dashboard_contract_invalid", "actuals must be an array");
  validateAcceptanceSummary(acceptance);
  const forecastStart = actualWindow.end;
  const forecastStartMs = parseShanghaiTimestamp(forecastStart, { quarterHour: true, code: "dashboard_contract_invalid" });
  const historyStartMs = parseShanghaiTimestamp(actualWindow.start, { quarterHour: true, code: "dashboard_contract_invalid" });
  const actualByKey = new Map();
  for (const rawPoint of actuals) {
    const point = validateActual(rawPoint);
    const time = parseShanghaiTimestamp(point.ds, { quarterHour: true, code: "dashboard_contract_invalid" });
    if (time < historyStartMs || time >= forecastStartMs) continue;
    const normalizedPoint = {
      unique_id: point.unique_id,
      ds: isoShanghai(time),
      y: point.y,
      quality: point.quality,
      source_revision: point.source_revision,
    };
    const key = `${time}\u0000${point.unique_id}`;
    const previous = actualByKey.get(key);
    if (!previous || normalizedPoint.source_revision > previous.source_revision) actualByKey.set(key, normalizedPoint);
    else if (
      normalizedPoint.source_revision === previous.source_revision &&
      (normalizedPoint.y !== previous.y || normalizedPoint.quality !== previous.quality)
    )
      fail("dashboard_contract_invalid", "actual revision conflicts");
  }
  const orderedActuals = [...actualByKey.values()].sort((left, right) => {
    const time = Date.parse(left.ds) - Date.parse(right.ds);
    return time || left.unique_id.localeCompare(right.unique_id);
  });
  const series = latest.series_payload.map((item) => ({
    unique_id: item.unique_id,
    unit: item.unit,
    model_name: item.model_name,
    status: item.status,
    fallback_reason: item.fallback_reason,
    actual: orderedActuals.filter((point) => point.unique_id === item.unique_id).map((point) => ({ data_time: point.ds, value: point.y, quality: point.quality, source_revision: point.source_revision })),
    forecast: item.points.map((point) => ({
      data_time: isoShanghai(parseNocoBaseTimestamp(point.data_time, { quarterHour: true, code: "dashboard_contract_invalid" })),
      target_time: isoShanghai(parseNocoBaseTimestamp(point.target_time, { quarterHour: true, code: "dashboard_contract_invalid" })),
      value: point.forecast_value,
      raw_value: point.raw_forecast,
      is_clipped: point.is_clipped,
    })),
  }));
  const predictableOutput = series.find((item) => item.forecast.length > 0);
  const forecastEnd = predictableOutput ? predictableOutput.forecast[predictableOutput.forecast.length - 1].target_time : forecastStart;
  const generatedMs = parseNocoBaseTimestamp(latest.generated_at, { code: "dashboard_contract_invalid" });
  const stale = nowMs - generatedMs > 30 * 60 * 1000;
  let state = latest.status === "ok" ? "ready" : latest.status === "warming_up" ? "initializing" : "degraded";
  if (stale) state = "stale";
  return {
    operation: "forecast_dashboard",
    range: {
      timezone: "Asia/Shanghai",
      history_start: isoShanghai(historyStartMs),
      history_end: forecastStart,
      actual_latest: orderedActuals.length ? orderedActuals[orderedActuals.length - 1].ds : null,
      forecast_start: forecastStart,
      forecast_end: forecastEnd,
      interval_seconds: 900,
      history_hours: 24,
      forecast_hours: 24,
      display_hours: 48,
      now_separator: isoShanghai(nowMs),
    },
    system: { state, mode: latest.status === "degraded" ? "degraded" : "normal", generated_at: isoShanghai(generatedMs), stale },
    series,
    acceptance,
  };
}

function validateBatchRow(row, stationId) {
  exactKeys(row, ["id", "station_id", "acceptance_run_id", "issued_at", "write_state"], [], "dashboard_contract_invalid", "acceptance batch");
  exactInteger(row.id, "dashboard_contract_invalid", "batch id", 1);
  if (row.station_id !== stationId || typeof row.acceptance_run_id !== "string" || !RUN_ID.test(row.acceptance_run_id) || row.write_state !== "complete")
    fail("dashboard_contract_invalid", "acceptance batch is invalid");
  const issuedMs = parseNocoBaseTimestamp(row.issued_at, { wholeSecond: true, code: "dashboard_contract_invalid" });
  const local = new Date(issuedMs + SHANGHAI_OFFSET_MS);
  if (
    local.getUTCHours() !== 1 || local.getUTCMinutes() !== 2 || local.getUTCSeconds() !== 0 || local.getUTCMilliseconds() !== 0
  )
    fail("dashboard_contract_invalid", "acceptance batch is outside the formal issuance slot");
  return row;
}

function selectAcceptanceBatches(rows, stationId) {
  validateStationId(stationId, "dashboard_contract_invalid");
  if (!Array.isArray(rows) || rows.length > 7) fail("dashboard_contract_invalid", "acceptance batch head is invalid");
  const safeRows = rows.map((row) => validateBatchRow(row, stationId));
  for (let index = 1; index < safeRows.length; index += 1) {
    if (
      parseNocoBaseTimestamp(safeRows[index - 1].issued_at, { wholeSecond: true }) <=
      parseNocoBaseTimestamp(safeRows[index].issued_at, { wholeSecond: true })
    )
      fail("dashboard_contract_invalid", "acceptance batch order is invalid");
  }
  if (safeRows.length === 0) return { runId: null, batches: [] };
  const runId = safeRows[0].acceptance_run_id;
  const selected = [];
  let leftCurrentRun = false;
  for (const row of safeRows) {
    if (row.acceptance_run_id === runId) {
      if (leftCurrentRun) fail("dashboard_contract_invalid", "acceptance run is not a contiguous head");
      selected.push(row);
    } else {
      leftCurrentRun = true;
    }
  }
  for (let index = 1; index < selected.length; index += 1) {
    if (
      parseNocoBaseTimestamp(selected[index - 1].issued_at, { wholeSecond: true }) -
        parseNocoBaseTimestamp(selected[index].issued_at, { wholeSecond: true }) !== DAY_MS
    )
      fail("dashboard_contract_invalid", "acceptance days are not consecutive");
  }
  return { runId, batches: selected };
}

function validateAcceptancePointRow(row, batches) {
  exactKeys(row, ["batch_id", "unique_id", "data_time", "actual_quality"], [], "dashboard_contract_invalid", "acceptance point");
  exactInteger(row.batch_id, "dashboard_contract_invalid", "batch id", 1);
  if (!batches.has(row.batch_id) || !SERIES_SET.has(row.unique_id)) fail("dashboard_contract_invalid", "acceptance point identity is invalid");
  parseNocoBaseTimestamp(row.data_time, { quarterHour: true, code: "dashboard_contract_invalid" });
  if (![null, "valid", "invalid"].includes(row.actual_quality)) fail("dashboard_contract_invalid", "actual quality is invalid");
  return row;
}

function validateAcceptancePointSeriesPage(rows, uniqueId) {
  if (!SERIES_SET.has(uniqueId) || !Array.isArray(rows)) fail("dashboard_contract_invalid", "acceptance point page is invalid");
  for (const row of rows) {
    exactKeys(row, ["batch_id", "unique_id", "data_time", "actual_quality"], [], "dashboard_contract_invalid", "acceptance point");
    exactInteger(row.batch_id, "dashboard_contract_invalid", "batch id", 1);
    if (row.unique_id !== uniqueId) fail("dashboard_contract_invalid", "acceptance point series does not match its fixed query");
    parseNocoBaseTimestamp(row.data_time, { quarterHour: true, code: "dashboard_contract_invalid" });
    if (![null, "valid", "invalid"].includes(row.actual_quality)) fail("dashboard_contract_invalid", "actual quality is invalid");
  }
  return rows;
}

function validateEvaluationRow(row, stationId, runId) {
  exactKeys(row, ["station_id", "acceptance_run_id", "evaluation_key", "expected_count", "valid_count", "zero_actual_count", "mape_percent", "mae", "smape_percent", "outcome"], [], "dashboard_contract_invalid", "evaluation row");
  if (row.station_id !== stationId || row.acceptance_run_id !== runId || ![...SERIES_IDS, "overall"].includes(row.evaluation_key))
    fail("dashboard_contract_invalid", "evaluation identity is invalid");
  if (row.evaluation_key === "overall") {
    exactInteger(row.expected_count, "dashboard_contract_invalid", "overall expected count", 2016, 2016);
    exactInteger(row.valid_count, "dashboard_contract_invalid", "overall valid count", 0, 2016);
    exactInteger(row.zero_actual_count, "dashboard_contract_invalid", "overall zero count", 0, 2016);
    if (row.valid_count + row.zero_actual_count > row.expected_count || row.mape_percent !== null || row.mae !== null || row.smape_percent !== null)
      fail("dashboard_contract_invalid", "overall evaluation is invalid");
    if (!["passed", "failed", "insufficient_data"].includes(row.outcome)) fail("dashboard_contract_invalid", "overall outcome is invalid");
    return row;
  }
  return validateAcceptanceResult({ unique_id: row.evaluation_key, expected_count: row.expected_count, valid_count: row.valid_count, zero_actual_count: row.zero_actual_count, mape_percent: row.mape_percent, mae: row.mae, smape_percent: row.smape_percent, outcome: row.outcome });
}

function buildAcceptanceSummary({ stationId, batches, points, evaluations }) {
  validateStationId(stationId, "dashboard_contract_invalid");
  if (![batches, points, evaluations].every(Array.isArray)) fail("dashboard_contract_invalid", "acceptance inputs must be arrays");
  if (batches.length === 0 && points.length === 0 && evaluations.length === 0) return null;
  const selection = selectAcceptanceBatches(batches, stationId);
  const safeBatches = selection.batches;
  if (safeBatches.length === 0) fail("dashboard_contract_invalid", "acceptance points exist without batches");
  const batchById = new Map(safeBatches.map((row) => [row.id, row]));
  const keys = new Set();
  const counts = new Map();
  const times = new Map();
  for (const raw of points) {
    const row = validateAcceptancePointRow(raw, batchById);
    const dataTimeMs = parseNocoBaseTimestamp(row.data_time, { quarterHour: true, code: "dashboard_contract_invalid" });
    const key = `${row.batch_id}\u0000${row.unique_id}\u0000${dataTimeMs}`;
    if (keys.has(key)) fail("dashboard_contract_invalid", "acceptance point is duplicated");
    keys.add(key);
    counts.set(row.batch_id, (counts.get(row.batch_id) || 0) + 1);
    const seriesKey = `${row.batch_id}\u0000${row.unique_id}`;
    if (!times.has(seriesKey)) times.set(seriesKey, []);
    times.get(seriesKey).push(dataTimeMs);
  }
  for (const batch of safeBatches) {
    if (counts.get(batch.id) !== 288) fail("dashboard_contract_invalid", "complete batch must expose 288 points");
    let reference = null;
    for (const uniqueId of SERIES_IDS) {
      const seriesTimes = (times.get(`${batch.id}\u0000${uniqueId}`) || []).sort((left, right) => left - right);
      if (seriesTimes.length !== 96 || seriesTimes.some((time, index) => index > 0 && time - seriesTimes[index - 1] !== BUCKET_MS))
        fail("dashboard_contract_invalid", "complete batch series must expose 96 contiguous points");
      if (seriesTimes[0] !== parseNocoBaseTimestamp(batch.issued_at, { wholeSecond: true, code: "dashboard_contract_invalid" }) - 120000)
        fail("dashboard_contract_invalid", "complete batch series is not anchored to its formal issuance");
      if (reference === null) reference = seriesTimes;
      else if (seriesTimes.some((time, index) => time !== reference[index])) fail("dashboard_contract_invalid", "complete batch series timelines do not align");
    }
  }
  const runId = selection.runId;
  if (evaluations.length === 0)
    return { acceptance_run_id: runId, status: "in_progress", completed_days: safeBatches.length, expected_days: 7, results: [] };
  if (safeBatches.length !== 7 || evaluations.length !== 4)
    fail("dashboard_contract_invalid", "final acceptance set is incomplete");
  const byKey = new Map();
  for (const row of evaluations) {
    const result = validateEvaluationRow(row, stationId, runId);
    if (byKey.has(row.evaluation_key)) fail("dashboard_contract_invalid", "evaluation row is duplicated");
    byKey.set(row.evaluation_key, result);
  }
  if ([...SERIES_IDS, "overall"].some((key) => !byKey.has(key)))
    fail("dashboard_contract_invalid", "final acceptance set is incomplete");
  const results = SERIES_IDS.map((uniqueId) => byKey.get(uniqueId));
  const overall = byKey.get("overall");
  const validCount = results.reduce((sum, item) => sum + item.valid_count, 0);
  const zeroCount = results.reduce((sum, item) => sum + item.zero_actual_count, 0);
  const expectedStatus = results.some((item) => item.outcome === "insufficient_data")
    ? "insufficient_data"
    : results.some((item) => item.outcome === "failed") ? "failed" : "passed";
  if (overall.valid_count !== validCount || overall.zero_actual_count !== zeroCount || overall.outcome !== expectedStatus)
    fail("dashboard_contract_invalid", "overall evaluation is inconsistent");
  return { acceptance_run_id: runId, status: overall.outcome, completed_days: 7, expected_days: 7, results };
}

function parseConfigJson(value, label) {
  if (typeof value !== "string" || value.length === 0 || value.length > 1024 * 1024) fail("source_not_configured", `${label} is missing`);
  let parsed;
  try { parsed = JSON.parse(value); } catch (_error) { fail("source_not_configured", `${label} is invalid`); }
  requirePlainObject(parsed, "source_not_configured", label);
  return parsed;
}

function configuredStation(stationId, stationsJson) {
  validateStationId(stationId);
  const stations = parseConfigJson(stationsJson, "station configuration");
  if (!Object.prototype.hasOwnProperty.call(stations, stationId)) fail("not_found", "station is not configured");
  const station = requirePlainObject(stations[stationId], "source_not_configured", "station configuration");
  return station;
}

function mappingFromJson(value) {
  return validateMapping(parseConfigJson(value, "field mapping"));
}

function validateDashboardAuthInput(query, authorizationHeader) {
  requirePlainObject(query, "invalid_request", "dashboard query");
  const keys = Object.keys(query);
  if (keys.some((key) => key !== "token") || keys.length > 1) fail("invalid_request", "dashboard query is invalid");
  const queryToken = Object.prototype.hasOwnProperty.call(query, "token") ? query.token : null;
  if (queryToken !== null && typeof queryToken !== "string") fail("invalid_request", "dashboard token is invalid");
  const headerToken = typeof authorizationHeader === "string" && authorizationHeader.startsWith("Bearer ") ? authorizationHeader.slice(7) : null;
  if ((queryToken && headerToken) || (!queryToken && !headerToken)) fail("unauthorized", "dashboard authorization failed");
  return headerTokenValue(queryToken || headerToken);
}

function headerTokenValue(value) {
  return headerToken(value, "unauthorized", "dashboard token");
}

function validateDashboardAuthorization(value) {
  exactKeys(value, ["authorized", "station_id"], [], "unauthorized", "dashboard authorization");
  if (value.authorized !== true) fail("unauthorized", "dashboard authorization failed");
  return validateStationId(value.station_id, "unauthorized");
}

function fixedRequestMessage(msg, { token, payload, contentType = false }) {
  if (!isPlainObject(msg)) fail("internal_error", "message is invalid");
  delete msg.url;
  delete msg.requestUrl;
  delete msg.method;
  delete msg.query;
  const headers = { Authorization: `Bearer ${headerToken(token, "source_not_configured", "outbound token")}` };
  if (contentType) headers["Content-Type"] = "application/json";
  msg.headers = headers;
  msg.followRedirects = false;
  if (payload !== undefined) msg.payload = payload;
  return msg;
}

function jsonResponse(msg, payload, statusCode = 200) {
  msg.statusCode = statusCode;
  msg.headers = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" };
  msg.payload = payload;
  delete msg.url;
  delete msg.requestUrl;
  delete msg.method;
  return msg;
}

const SAFE_ERROR_STATUS = Object.freeze({
  unauthorized: 401,
  not_found: 404,
  invalid_request: 400,
  payload_too_large: 413,
  source_not_configured: 503,
  source_contract_invalid: 502,
  dashboard_contract_invalid: 502,
  upstream_contract_invalid: 502,
  upstream_failed: 502,
  internal_error: 500,
});

function toErrorMessage(msg, error) {
  const code = error && typeof error.code === "string" && Object.prototype.hasOwnProperty.call(SAFE_ERROR_STATUS, error.code)
    ? error.code : "internal_error";
  if (!isPlainObject(msg)) msg = {};
  delete msg.url;
  delete msg.requestUrl;
  delete msg.method;
  delete msg.query;
  delete msg.m3;
  delete msg.req;
  return jsonResponse(msg, { status: "error", error: { code, message: "request rejected" } }, SAFE_ERROR_STATUS[code]);
}

function requireHttpSuccess(statusCode, code = "upstream_failed") {
  if (!Number.isSafeInteger(statusCode) || statusCode < 200 || statusCode >= 300) fail(code, "upstream request failed");
}

module.exports = Object.freeze({
  BUCKET_MS,
  DAY_MS,
  SERIES_IDS,
  ContractError,
  aggregateObservations,
  alertAcknowledgement,
  authorizeBearer,
  bucketStart,
  buildAcceptanceSummary,
  buildDashboard,
  configuredStation,
  encodeCursor,
  extractAcceptanceEnvelope,
  deriveDashboardActualWindow,
  extractNocoBasePage,
  extractRecordEnvelope,
  fixedRequestMessage,
  isoShanghai,
  jsonResponse,
  lastFreshSoc,
  mappingFromJson,
  paginatePoints,
  parseConfigJson,
  parseNocoBaseTimestamp,
  parseShanghaiTimestamp,
  requireHttpSuccess,
  selectAcceptanceBatches,
  timeWeightedMean,
  toErrorMessage,
  validateAcceptanceContext,
  validateAlertRequest,
  validateAlertSinkAcknowledgement,
  validateAcceptancePointSeriesPage,
  validateDashboardAuthInput,
  validateDashboardAuthorization,
  validateConfiguredUrl,
  validateEmptyQuery,
  validateMapping,
  validateObservationRequest,
  validateStationId,
});
