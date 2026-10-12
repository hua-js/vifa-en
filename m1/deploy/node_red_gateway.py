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
msg.headers = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'};
node.log('m1_dashboard_completed status=' + msg.payload.status + ' source_failures=' +
    Object.keys((msg.payload.data || {}).source_errors || {}).length);
return msg;"""


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
    by_id[WRAP_ID]["func"] = WRAP_JS
    by_id[WRAP_ID]["name"] = "返回M1业务结果"
    allowed = {PREPARE_ID, REQUEST_ID, WRAP_ID}
    if {a["id"] for a, b in zip(nodes, output) if a != b} - allowed:
        raise ValueError("Unrelated production nodes changed")
    return output
