// Only explicit M4 operations reach the fixed, server-configured backend.
const fail = (status, detail) => {
    msg.statusCode = status;
    msg.headers = {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'};
    msg.payload = {detail};
    return [null, msg];
};
const {stationId, resource, runId} = msg.req.params;
const method = msg.req.method;
const allowed = {
    GET: ['settings','control-sources','inputs','candidates','selection-policy','decision-result','decision-history','decision-runs'],
    PUT: ['settings','selection-policy'],
    POST: ['candidates','selection','decision-runs']
};
if (!['station-1','station-2'].includes(stationId)) return fail(404, '未知电站');
let route;
if (runId !== undefined) {
    if (method !== 'GET' || typeof runId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(runId)) return fail(404, '未知记录');
    route = 'decision-results/' + runId;
} else {
    if (!(allowed[method] || []).includes(resource)) return fail(404, '不支持的 M4 操作');
    route = resource;
}
let query = '';
if (resource === 'decision-history') {
    const limit = msg.req.query.limit === undefined ? '10' : msg.req.query.limit;
    const offset = msg.req.query.offset === undefined ? '0' : msg.req.query.offset;
    if (typeof limit !== 'string' || !/^\d{1,2}$/.test(limit) || Number(limit) < 1 || Number(limit) > 20 || typeof offset !== 'string' || !/^\d{1,6}$/.test(offset) || Number(offset) > 100000) return fail(400, '运行记录分页参数不正确');
    query = '?limit=' + limit + '&offset=' + offset;
}
const socketPath = env.get('M4_SOCKET_PATH') || '/run/vifa-m4/api.sock';
if (typeof socketPath !== 'string' || socketPath.length > 100 || !/^\/[A-Za-z0-9_./-]+\.sock$/.test(socketPath) || socketPath.split('/').some(part => part === '..' || part === '.')) return fail(503, 'M4 Socket 路径配置不正确');
msg.socketPath = socketPath;
msg.requestPath = '/m4-api/stations/' + stationId + '/' + route + query;
delete msg.url;
msg.method = method;
msg.followRedirects = false;
msg.requestTimeout = 180000;
// Do not forward browser Token, Cookie, Origin, arbitrary query, or proxy headers.
msg.headers = {'Host':'localhost', 'Accept':'application/json'};
delete msg.cookies;
if (method === 'GET') {
    delete msg.payload;
} else {
    if (!msg.payload || typeof msg.payload !== 'object' || Array.isArray(msg.payload) || Buffer.isBuffer(msg.payload)) return fail(400, '请使用 JSON 对象提交参数');
    const body = JSON.stringify(msg.payload);
    if (Buffer.byteLength(body, 'utf8') > 1048576) return fail(413, '提交参数过大');
    msg.headers['Content-Type'] = 'application/json';
    msg.payload = body;
}
return [msg, null];
