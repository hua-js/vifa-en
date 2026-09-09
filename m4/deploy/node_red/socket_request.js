// Node.js built-in http module is supplied by settings.js functionGlobalContext.
// No shell commands, TCP fallback, redirects, retries or browser credentials.
const client = global.get('m4Http');
if (!client || typeof client.request !== 'function') {
    msg.statusCode = 503;
    msg.payload = JSON.stringify({detail:'M4 Socket 转发尚未配置，请联系管理员'});
    return msg;
}
let settled = false, request, deadline;
const complete = (status, body, failed = false) => {
    if (settled) return;
    settled = true;
    clearTimeout(deadline);
    if (msg.res && typeof msg.res.removeListener === 'function') msg.res.removeListener('close', disconnected);
    msg.statusCode = status;
    msg.payload = body;
    if (failed) msg.error = {message:'socket request failed'};
    node.send(msg);
    node.done();
};
const fail = () => complete(502, '', true);
const disconnected = () => {
    if (request) request.destroy();
    if (!settled) {
        settled = true;
        clearTimeout(deadline);
        node.done();
    }
};
try {
    request = client.request({socketPath:msg.socketPath, path:msg.requestPath,
        method:msg.method, headers:msg.headers, agent:false}, response => {
        let bytes = 0;
        const chunks = [];
        response.on('data', chunk => {
            bytes += chunk.length;
            if (bytes > 16 * 1024 * 1024) {
                fail();
                response.destroy();
                request.destroy();
            } else chunks.push(chunk);
        });
        response.on('end', () => complete(response.statusCode, Buffer.concat(chunks).toString('utf8')));
        response.on('aborted', fail);
        response.on('error', fail);
    });
    request.on('error', fail);
    deadline = setTimeout(() => { fail(); request.destroy(); }, 180000);
    if (msg.res && typeof msg.res.once === 'function') msg.res.once('close', disconnected);
    request.end(msg.method === 'GET' ? undefined : msg.payload);
} catch (_) {
    fail();
    if (request) request.destroy();
}
return;
