"""Surgical production M1 gateway transition; no templates or other nodes change."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

INPUT_ID = "3549591af9c54c05"
PREPARE_ID = "4c9e7d4986a30167"
REQUEST_ID = "a2622c9c110dcb05"
WRAP_ID = "1a99b852d2815bcc"
RESPONSE_ID = "96c0fb90378cbe03"

PREPARE_JS = r"""var bearer = String((msg.req.headers || {}).authorization || '').replace(/^Bearer\s+/i, '');
var token = String(bearer || (msg.req.query || {}).token || '');
if (!/^[A-Za-z0-9._~-]{1,4096}$/.test(token)) {
    msg.res.status(401).json({status: 'error', message: '请先登录后再查看。'});
    return null;
}
delete msg.method;
msg.requestTimeout = 115000;
msg.followRedirects = false;
msg.headers = {Accept: 'application/json', Authorization: 'Bearer ' + token};
msg.payload = '';
return msg;"""

WRAP_JS = r"""var value = msg.payload;
if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch (_error) { value = null; }
}
var valid = value && ((value.status === 'ok' && value.data && typeof value.data === 'object' && !Array.isArray(value.data)) ||
    (value.status === 'error' && typeof value.message === 'string'));
msg.payload = valid ? value : {status: 'error', message: '数据暂不可用，请稍后重试。'};
if (!Number.isInteger(msg.statusCode) || msg.statusCode < 100 || msg.statusCode > 599) msg.statusCode = 502;
msg.headers = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    Vary: 'Accept-Encoding'};
node.log('m1_dashboard_completed status=' + msg.payload.status + ' source_failures=' +
    Object.keys((msg.payload.data || {}).source_errors || {}).length);
// Compress the complete response only when the client accepts gzip.
var accepted = String(((msg.req || {}).headers || {})['accept-encoding'] || '').toLowerCase();
var qualities = {};
accepted.split(',').forEach(function(entry) {
    var parts = entry.trim().split(';');
    var coding = parts.shift().trim();
    if (coding !== 'gzip' && coding !== '*') return;
    var quality = 1;
    parts.forEach(function(parameter) {
        var match = parameter.trim().match(/^q\s*=\s*(.*)$/);
        if (match) quality = /^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/.test(match[1]) ? Number(match[1]) : 0;
    });
    qualities[coding] = quality;
});
var gzipQuality = Object.prototype.hasOwnProperty.call(qualities, 'gzip') ? qualities.gzip : qualities['*'];
if (gzipQuality > 0) {
    try {
        var json = Buffer.from(JSON.stringify(msg.payload), 'utf8');
        if (json.length >= 1024) {
            var compressed = m1Zlib.gzipSync(json, {level: 1});
            if (compressed.length < json.length) {
                msg.payload = compressed;
                msg.headers['Content-Encoding'] = 'gzip';
            }
        }
    } catch (_compressionError) {
        node.warn('m1_response_compression_failed');
    }
}
return msg;"""


def _configure_response(node: dict[str, Any]) -> None:
    libs = node.get("libs") or []
    if not isinstance(libs, list) or any(not isinstance(lib, dict) for lib in libs):
        raise ValueError("M1 response module configuration is invalid")
    existing = [lib for lib in libs if lib.get("var") == "m1Zlib"]
    if len(existing) > 1 or any(lib.get("module") != "zlib" for lib in existing):
        raise ValueError("M1 compression module alias is already in use")
    node["libs"] = libs if existing else [*libs, {"var": "m1Zlib", "module": "zlib"}]
    node["func"] = WRAP_JS


def update_response_compression(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Update the existing M1 response node; preserve every other node verbatim."""
    output = deepcopy(nodes)
    by_id = {node["id"]: node for node in output}
    if (
        by_id.get(INPUT_ID, {}).get("url") != "/dashboard_energy_api"
        or by_id.get(REQUEST_ID, {}).get("url") != "http://127.0.0.1:8811/dashboard_energy_api"
        or by_id.get(WRAP_ID, {}).get("type") != "function"
    ):
        raise ValueError("M1 resident gateway identity does not match")
    _configure_response(by_id[WRAP_ID])
    return output


def update_gateway(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = deepcopy(nodes)
    by_id = {node["id"]: node for node in output}
    expected = {INPUT_ID, PREPARE_ID, REQUEST_ID, WRAP_ID, RESPONSE_ID}
    if not expected.issubset(by_id) or by_id[INPUT_ID].get("url") != "/dashboard_energy_api":
        raise ValueError("M1 production gateway identity does not match")
    if by_id[REQUEST_ID].get("type") not in {"exec", "http request"}:
        raise ValueError("M1 production request node does not match")
    by_id[PREPARE_ID]["func"] = PREPARE_JS
    by_id[PREPARE_ID]["name"] = "校验凭据并转发M1"
    original = by_id[REQUEST_ID]
    request = {key: original[key] for key in ("id", "z", "g", "x", "y", "d") if key in original}
    request.update({
        "type": "http request", "name": "M1常驻数据接口", "method": "GET", "ret": "obj",
        "paytoqs": "ignore", "url": "http://127.0.0.1:8811/dashboard_energy_api",
        "tls": "", "persist": False, "proxy": "", "insecureHTTPParser": False,
        "authType": "", "senderr": False, "headers": [], "followRedirects": False,
        "requestTimeout": "115000", "wires": [[WRAP_ID]],
    })
    output[output.index(original)] = request
    _configure_response(by_id[WRAP_ID])
    by_id[WRAP_ID]["name"] = "返回M1业务结果"
    allowed = {PREPARE_ID, REQUEST_ID, WRAP_ID}
    if {a["id"] for a, b in zip(nodes, output) if a != b} - allowed:
        raise ValueError("Unrelated production nodes changed")
    return output
