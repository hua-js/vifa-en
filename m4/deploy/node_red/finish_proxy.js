// Forward API status and JSON only; never leak upstream cookies or redirect URLs.
const status = Number(msg.statusCode);
let body;
try { body = typeof msg.payload === 'string' ? JSON.parse(msg.payload) : msg.payload; } catch (_) { body = undefined; }
if (msg.error || !Number.isInteger(status) || status < 200 || status >= 600 || (status >= 300 && status < 400) || body === undefined) {
    msg.statusCode = 502;
    msg.payload = {detail:'M4 后端暂时不可用，请稍后重试'};
} else {
    msg.statusCode = status;
    msg.payload = body;
}
msg.headers = {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer', 'X-Content-Type-Options':'nosniff'};
delete msg.cookies;
delete msg.responseCookies;
delete msg.error;
return msg;
