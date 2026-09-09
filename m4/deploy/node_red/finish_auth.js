// Three outputs: HTML, M4 API, customer-safe error. Never pass EMS user data on.
const state = msg.m4AuthState;
const status = Number(msg.statusCode);
const raw = msg.payload;
const transportError = Boolean(msg.error);
delete msg.m4AuthState;
delete msg.url;
delete msg.headers;
delete msg.cookies;
delete msg.responseCookies;
delete msg.responseUrl;
delete msg.error;
delete msg.statusCode;
delete msg.payload;
if (msg.req && msg.req.headers) delete msg.req.headers.authorization;
if (msg.req && msg.req.query) delete msg.req.query.token;
const fail = (code, detail) => {
    msg.statusCode = code;
    msg.headers = {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'};
    msg.payload = {detail};
    return [null, null, msg];
};
if (!state || typeof state.pageRequest !== 'boolean') return fail(503, '登录校验暂不可用，请稍后重试');
if (status === 401 || status === 403) return fail(401, '登录已失效，请重新登录平台后打开页面');
if (transportError || status !== 200) return fail(503, '登录校验暂不可用，请稍后重试');
let result;
try {
    if (typeof raw !== 'string' || Buffer.byteLength(raw, 'utf8') > 65536) throw new Error();
    result = JSON.parse(raw);
} catch (_) {
    return fail(503, '登录校验返回异常，请联系管理员');
}
const user = result && result.data;
if (!user || Array.isArray(user) || !Number.isSafeInteger(user.id) || user.id < 1) return fail(401, '登录已失效，请重新登录平台后打开页面');
msg.method = state.method;
msg.payload = state.payload;
msg.headers = {'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer', 'X-Content-Type-Options':'nosniff'};
if (state.pageRequest) {
    msg.headers['Content-Security-Policy'] = "frame-ancestors 'self' " + state.frameOrigin;
    return [msg, null, null];
}
return [null, msg, null];
