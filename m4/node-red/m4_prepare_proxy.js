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
    GET: ['candidate-jobs','settings','control-sources','inputs','daily-inputs','daily-plan','candidates','selection-policy','decision-result','decision-history','decision-runs','bills'],
    PUT: ['settings','selection-policy'],
    POST: ['candidate-jobs','daily-plan','candidates','selection','decision-runs']
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
if (resource === 'candidate-jobs' && method === 'GET') {
    const requestId = msg.req.query.request_id;
    if (typeof requestId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(requestId)) return fail(400, '候选任务标识无效');
    query = '?request_id=' + encodeURIComponent(requestId);
}

if (resource === 'bills') {
    const month = msg.req.query.month;
    if (typeof month !== 'string' || !/^20[0-9]{2}-(0[1-9]|1[0-2])$/.test(month)) return fail(400, '账单月份格式不正确');
    query = '?month=' + month;
}
if (resource === 'decision-history') {
    const limit = msg.req.query.limit === undefined ? '10' : msg.req.query.limit;
    const offset = msg.req.query.offset === undefined ? '0' : msg.req.query.offset;
    if (typeof limit !== 'string' || !/^\d{1,2}$/.test(limit) || Number(limit) < 1 || Number(limit) > 20 || typeof offset !== 'string' || !/^\d{1,6}$/.test(offset) || Number(offset) > 100000) return fail(400, '运行记录分页参数不正确');
    query = '?limit=' + limit + '&offset=' + offset;
}
const backend = env.get('M4_BACKEND_URL') || 'http://127.0.0.1:8844';
if (typeof backend !== 'string' || !/^https?:\/\/[a-zA-Z0-9.-]+:\d{1,5}$/.test(backend)) return fail(503, 'M4 后端地址配置不正确');
msg.requestPath = '/m4-api/stations/' + stationId + '/' + route + query;
msg.url = backend + msg.requestPath;
delete msg.socketPath;
msg.method = method;
msg.followRedirects = false;
msg.requestTimeout = 180000;
// Do not forward browser Token, Cookie, Origin, arbitrary query, or proxy headers.
msg.headers = {'Host':'127.0.0.1:8844', 'Accept':'application/json'};
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
