// Two outputs: EMS auth:check request, customer-safe error response.
// A supplied token is never accepted without validation by its NocoBase issuer.
const fail = (status, detail) => {
    msg.statusCode = status;
    msg.headers = {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'};
    msg.payload = {detail};
    return [null, msg];
};
const publicOrigin = env.get('M4_PUBLIC_ORIGIN');
const frameOrigin = env.get('M4_FRAME_ORIGIN');
const validOrigin = value => typeof value === 'string' && /^https:\/\/[a-zA-Z0-9.-]+(?::\d{1,5})?$/.test(value);
if (!validOrigin(publicOrigin) || !validOrigin(frameOrigin)) {
    return fail(503, 'M4 访问配置尚未完成，请联系管理员');
}
const pageRequest = !msg.req.params.stationId;
const auth = msg.req.headers.authorization;
const provided = pageRequest ? msg.req.query.token : (typeof auth === 'string' && auth.startsWith('Bearer ') ? auth.slice(7) : '');
if (typeof provided !== 'string' || !/^[A-Za-z0-9._~-]{1,4096}$/.test(provided)) return fail(401, '登录凭据缺失或格式不正确，请从平台重新打开页面');
// Browser writes must originate in the M4 iframe itself. API authentication is
// an explicit Bearer header; cookies and URL tokens do not authorize API calls.
if (!['GET','HEAD'].includes(msg.req.method) && msg.req.headers.origin && msg.req.headers.origin !== publicOrigin) {
    return fail(403, '只允许从 M4 页面保存参数或启动计算');
}
// Preserve the original operation while the HTTP Request node checks login.
msg.m4AuthState = {pageRequest, frameOrigin, payload: msg.payload, method: msg.req.method};
msg.url = frameOrigin + '/api/auth:check';
msg.method = 'GET';
msg.followRedirects = false;
msg.requestTimeout = 10000;
msg.headers = {'Authorization': 'Bearer ' + provided, 'Accept': 'application/json'};
delete msg.payload;
delete msg.cookies;
delete msg.error;
delete msg.statusCode;
return [msg, null];
