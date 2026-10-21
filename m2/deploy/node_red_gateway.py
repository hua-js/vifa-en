"""Prepare an M2 read gateway transition without touching scheduled write nodes."""
from copy import deepcopy


PREPARE_JS = r"""const query = msg.req.query || {};
function reject() {
    msg.statusCode = 400;
    msg.headers = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'};
    msg.payload = {status: 'error', error: {code: 'invalid_arguments', message: '查询参数不正确。'}};
    return [null, msg];
}
const operation = query.operation === undefined ? 'dashboard' : query.operation;
const dates = {dashboard: [], realtime: [], history: ['date'], curve: ['date'], events: ['start_date', 'end_date']};
if (!Object.prototype.hasOwnProperty.call(dates, operation) || typeof operation !== 'string') return reject();
const station = typeof query.station_id === 'string' ? query.station_id.trim().toUpperCase() : '';
if (!['ES01', 'ES02'].includes(station)) return reject();
const fields = dates[operation];
if (Object.keys(query).some(key => !['operation', 'station_id', ...fields].includes(key))) return reject();
function validDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value < '0001-01-01') return false;
    const parsed = new Date(value + 'T00:00:00Z');
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
if (fields.length) {
    const start = query[fields[0]], end = query[fields[fields.length - 1]];
    const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
    if (!validDate(start) || !validDate(end) || start > end || end > today ||
        (Date.parse(end) - Date.parse(start)) / 86400000 >= 31) return reject();
}
const pairs = [['station_id', station], ['operation', operation], ...fields.map(key => [key, query[key]])];
msg.url = 'http://127.0.0.1:8812/energy-efficiency-api?' + pairs.map(([key, value]) => key + '=' + encodeURIComponent(value)).join('&');
msg.method = 'GET';
msg.headers = {Accept: 'application/json'};
msg.followRedirects = false;
msg.requestTimeout = 40000;
msg.payload = '';
return [msg, null];"""

WRAP_JS = r"""let value = msg.payload;
if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch (_) { value = null; }
}
const codes = ['invalid_arguments', 'missing_config', 'source_error', 'store_error', 'calculation_error', 'internal_error', 'busy'];
const valid = value && ((value.status === 'ok' && value.data && typeof value.data === 'object' && !Array.isArray(value.data)) ||
    (value.status === 'error' && value.error && codes.includes(value.error.code) && typeof value.error.message === 'string'));
if (!valid) {
    msg.statusCode = 502;
    value = {status: 'error', error: {code: 'internal_error', message: '数据暂不可用，请稍后重试。'}};
}
if (!Number.isInteger(msg.statusCode) || msg.statusCode < 100 || msg.statusCode > 599) msg.statusCode = 502;
msg.payload = value;
msg.headers = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'};
return msg;"""


def update_gateway(nodes):
    """Discover the single read chain by route; refuse ambiguous or shared nodes."""
    output = deepcopy(nodes)
    by_id = {node['id']: node for node in output}
    inputs = [node for node in output if node.get('type') == 'http in' and node.get('url') == '/energy-efficiency-api']
    if len(inputs) != 1 or inputs[0].get('method', 'get').lower() != 'get':
        raise ValueError('M2 read route is ambiguous')

    def next_node(node, kind):
        wires = node.get('wires', [])
        targets = wires[0] if wires else []
        if len(targets) != 1 or targets[0] not in by_id or by_id[targets[0]].get('type') not in kind:
            raise ValueError('M2 read chain does not match')
        return by_id[targets[0]]

    prepare = next_node(inputs[0], {'function'})
    request = next_node(prepare, {'exec', 'http request'})
    wrap = next_node(request, {'function'})
    response = next_node(wrap, {'http response'})
    for target, parent in ((prepare, inputs[0]), (request, prepare), (wrap, request)):
        incoming = {node['id'] for node in output if any(target['id'] in wire for wire in node.get('wires', []))}
        if incoming != {parent['id']}:
            raise ValueError('M2 read chain contains a shared node')
    if len(prepare.get('wires', [])) != 2 or prepare['wires'][1] != [response['id']]:
        raise ValueError('M2 rejection route does not match')
    prepare.update(name='校验M2只读查询', func=PREPARE_JS)
    request_id = request['id']
    replacement = {key: request[key] for key in ('id', 'z', 'g', 'x', 'y', 'd') if key in request}
    replacement.update({
        'type': 'http request', 'name': 'M2常驻只读接口', 'method': 'GET', 'ret': 'obj',
        'paytoqs': 'ignore', 'url': '', 'tls': '', 'persist': False, 'proxy': '',
        'insecureHTTPParser': False, 'authType': '', 'senderr': False, 'headers': [],
        'followRedirects': False, 'requestTimeout': '40000', 'wires': [[wrap['id']]],
    })
    output[output.index(request)] = replacement
    wrap.update(name='返回M2业务结果', func=WRAP_JS)
    allowed = {prepare['id'], request_id, wrap['id']}
    if any(original != updated and original['id'] not in allowed for original, updated in zip(nodes, output)):
        raise ValueError('Unrelated nodes changed')
    return output
