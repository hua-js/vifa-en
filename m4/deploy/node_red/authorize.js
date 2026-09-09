// Two outputs: authorized request, customer-safe error response.
const fail = (status, detail) => {
    msg.statusCode = status;
    msg.headers = {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'};
    msg.payload = {detail};
    return [null, msg];
};
const expected = env.get('M4_IFRAME_TOKEN');
const publicOrigin = env.get('M4_PUBLIC_ORIGIN');
const frameOrigin = env.get('M4_FRAME_ORIGIN');
const validOrigin = value => typeof value === 'string' && /^https:\/\/[a-zA-Z0-9.-]+(?::\d{1,5})?$/.test(value);
if (typeof expected !== 'string' || !/^[A-Za-z0-9._~-]{32,512}$/.test(expected) || !validOrigin(publicOrigin) || !validOrigin(frameOrigin)) {
    return fail(503, 'M4 访问配置尚未完成，请联系管理员');
}
const pageRequest = !msg.req.params.stationId;
const auth = msg.req.headers.authorization;
const provided = pageRequest ? msg.req.query.token : (typeof auth === 'string' && auth.startsWith('Bearer ') ? auth.slice(7) : '');
if (typeof provided !== 'string' || provided.length !== expected.length) return fail(401, '访问凭据无效或已过期，请从平台重新打开页面');
let mismatch = 0;
for (let index = 0; index < expected.length; index++) mismatch |= expected.charCodeAt(index) ^ provided.charCodeAt(index);
if (mismatch) return fail(401, '访问凭据无效或已过期，请从平台重新打开页面');
// Browser writes must originate in the M4 iframe itself. API authentication is
// an explicit Bearer header; cookies and URL tokens do not authorize API calls.
if (!['GET','HEAD'].includes(msg.req.method) && msg.req.headers.origin && msg.req.headers.origin !== publicOrigin) {
    return fail(403, '只允许从 M4 页面保存参数或启动计算');
}
msg.headers = {'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer', 'X-Content-Type-Options':'nosniff'};
if (pageRequest) msg.headers['Content-Security-Policy'] = "frame-ancestors 'self' " + frameOrigin;
return [msg, null];
