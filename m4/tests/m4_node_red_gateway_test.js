'use strict';

// Offline security/contract checks for the importable Node-RED gateway functions.
// Run: node m4/tests/m4_node_red_gateway_test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const sourceRoot = path.join(root, 'm4/deploy/node_red');
const token = 'm4-gateway-test-only-0123456789abcdefgh';
const wrongToken = 'x'.repeat(token.length);
const origin = 'https://m4.example.test';
const frameOrigin = 'https://nocobase.example.test';
const runId = '11111111-2222-4333-8444-555555555555';
const maximumBodyBytes = 1048576;
const defaults = {
    M4_IFRAME_TOKEN: token,
    M4_PUBLIC_ORIGIN: origin,
    M4_FRAME_ORIGIN: frameOrigin,
    M4_BACKEND_URL: 'http://127.0.0.1:8844',
};
const sources = Object.fromEntries(['authorize', 'finish_auth', 'prepare_proxy', 'finish_proxy'].map(name =>
    [name, fs.readFileSync(path.join(sourceRoot, `${name}.js`), 'utf8')]));
const scripts = Object.fromEntries(Object.entries(sources).map(([name, source]) =>
    [name, new vm.Script(`(function () {\n${source}\n})()`, {filename: `${name}.js`})]));

function run(name, msg, overrides = {}) {
    const values = {...defaults, ...overrides};
    return scripts[name].runInNewContext({msg, env: {get: name => values[name]}, Buffer}, {timeout: 1000});
}

function request({method = 'GET', station = 'station-1', resource = 'settings', record,
    headers = {}, query = {}, payload, page = false} = {}) {
    const params = page ? {} : {stationId: station, resource};
    if (record !== undefined) params.runId = record;
    return {
        req: {method, params, query: {...query}, headers: {...headers}},
        payload,
    };
}

function loginRequest(msg, overrides = {}) {
    const result = run('authorize', msg, overrides);
    assert.equal(result[1], null);
    assert.equal(result[0], msg);
    return msg;
}

function authenticatedReply(msg, status = 200, payload = JSON.stringify({data: {id: 1}})) {
    msg.statusCode = status;
    msg.payload = payload;
    return run('finish_auth', msg);
}

function authorized(msg, overrides = {}) {
    loginRequest(msg, overrides);
    const page = msg.m4AuthState.pageRequest;
    const result = authenticatedReply(msg);
    assert.equal(result[2], null);
    assert.equal(result[page ? 0 : 1], msg);
    return msg;
}

function prepared(msg, overrides = {}) {
    const result = run('prepare_proxy', msg, overrides);
    assert.equal(result[1], null);
    assert.equal(result[0], msg);
    return result[0];
}

function rejected(name, msg, status, overrides = {}) {
    const result = run(name, msg, overrides);
    assert.equal(result[0], null);
    assert.equal(result[1].statusCode, status);
    assert.match(result[1].headers['Content-Type'], /^application\/json/);
    assert.equal(result[1].headers['Cache-Control'], 'no-store');
    assert.equal(result[1].headers['Referrer-Policy'], 'no-referrer');
    assert.equal(typeof result[1].payload.detail, 'string');
    assert.ok(!JSON.stringify(result[1].payload).includes(token));
    return result[1];
}

function jsonValue(value) {
    return JSON.parse(JSON.stringify(value));
}

test('HTML requires a query token validated by EMS and emits iframe protections', () => {
    const msg = authorized(request({page: true, query: {token}}));
    assert.equal(msg.headers['Content-Security-Policy'], `frame-ancestors 'self' ${frameOrigin}`);
    assert.equal(msg.headers['Referrer-Policy'], 'no-referrer');
    assert.equal(msg.headers['X-Content-Type-Options'], 'nosniff');
    for (const provided of [undefined, '', '{{ ctx.token }}', 'x'.repeat(4097), `${token} `,
        [token], [token, token], {value: token}, 123]) {
        rejected('authorize', request({page: true, query: {token: provided}}), 401);
    }
    rejected('authorize', request({page: true, headers: {authorization: `Bearer ${token}`}}), 401);
});

test('API authentication requires Bearer and ignores URL tokens/cookies', () => {
    authorized(request({headers: {authorization: `Bearer ${token}`}}));
    authorized(request({headers: {authorization: `Bearer ${token}`}, query: {token: wrongToken}}));
    for (const authorization of [undefined, '', `Basic ${token}`, `bearer ${token}`,
        `Bearer {{ ctx.token }}`, `Bearer  ${token}`, [`Bearer ${token}`]]) {
        rejected('authorize', request({headers: {authorization, cookie: `token=${token}`}, query: {token}}), 401);
    }
    const msg = authorized(request({headers: {authorization: `Bearer ${token}`}}));
    assert.equal(msg.headers['Content-Security-Policy'], undefined);
});

test('no shared token is required; invalid origin configuration still fails closed', () => {
    authorized(request({page: true, query: {token}}), {M4_IFRAME_TOKEN: undefined});
    for (const name of ['M4_PUBLIC_ORIGIN', 'M4_FRAME_ORIGIN']) {
        for (const value of [undefined, '', 'http://m4.example.test', 'https://m4.example.test/path',
            'https://user:pass@m4.example.test', 'https://m4.example.test?q=1',
            "https://m4.example.test; frame-ancestors *", 123]) {
            rejected('authorize', request({page: true, query: {token}}), 503, {[name]: value});
        }
    }
});

test('login check uses only fixed EMS URL and a clean GET with the supplied Bearer', () => {
    const jwt = 'eyJ.' + 'x'.repeat(1500) + '.signature';
    const msg = loginRequest(request({method: 'POST', resource: 'decision-runs', headers: {authorization: 'Bearer ' + jwt,
        cookie: 'private', host: 'attacker.test', origin}, payload: {request_id: 'example'},
        query: {url: 'https://attacker.test', token: wrongToken}}));
    assert.equal(msg.url, frameOrigin + '/api/auth:check');
    assert.equal(msg.method, 'GET');
    assert.equal(msg.followRedirects, false);
    assert.equal(msg.requestTimeout, 10000);
    assert.equal(msg.payload, undefined);
    assert.deepEqual(jsonValue(msg.headers), {Authorization: 'Bearer ' + jwt, Accept: 'application/json'});
    const result = authenticatedReply(msg);
    assert.equal(result[1], msg);
    assert.equal(msg.method, 'POST');
    assert.deepEqual(jsonValue(msg.payload), {request_id: 'example'});
    assert.equal(msg.req.headers.authorization, undefined);
    assert.equal(msg.req.query.token, undefined);
    assert.equal(msg.m4AuthState, undefined);
    assert.equal(msg.url, undefined);
    const api = prepared(msg);
    assert.equal(api.payload, '{"request_id":"example"}');
    assert(!JSON.stringify(api.headers).includes(jwt));
});

test('expired login, redirects, outages and invalid auth replies never reach HTML or API', () => {
    const cases = [[401, '{}', 401], [403, '{}', 401], [302, '{}', 503],
        [500, '{}', 503], [NaN, '{}', 503], [200, 'not json', 503],
        [200, 'x'.repeat(65537), 503], [200, '{}', 401],
        [200, '{"data":{"id":0}}', 401], [200, '{"data":{"id":"1"}}', 401],
        [200, '{"data":[]}', 401]];
    for (const page of [true, false]) for (const [status, payload, expected] of cases) {
        const msg = loginRequest(request({page, query: {token}, headers: {authorization: 'Bearer ' + token}}));
        msg.headers = {'Set-Cookie': 'ems-private'};
        msg.responseCookies = {session: 'secret'};
        const result = authenticatedReply(msg, status, payload);
        assert.equal(result[0], null); assert.equal(result[1], null);
        assert.equal(result[2].statusCode, expected);
        assert.equal(msg.responseCookies, undefined);
        assert.equal(msg.m4AuthState, undefined);
        assert.equal(msg.req.headers.authorization, undefined);
        assert.equal(msg.headers['Set-Cookie'], undefined);
        assert(!JSON.stringify(msg.payload).includes(token));
    }
    const msg = loginRequest(request({page: true, query: {token}}));
    msg.error = {message: 'connection failed containing sensitive upstream context'};
    const result = authenticatedReply(msg);
    assert.equal(result[2].statusCode, 503);
    assert(!JSON.stringify(msg).includes('sensitive upstream context'));
});

test('browser writes check iframe Origin and permit authenticated nonbrowser requests', () => {
    for (const method of ['PUT', 'POST']) {
        for (const invalidOrigin of ['https://other.example.test', frameOrigin, 'null', 'http://m4.example.test']) {
            rejected('authorize', request({method, headers: {authorization: `Bearer ${token}`, origin: invalidOrigin}}), 403);
        }
        authorized(request({method, headers: {authorization: `Bearer ${token}`, origin}}));
        authorized(request({method, headers: {authorization: `Bearer ${token}`}}));
    }
    authorized(request({headers: {authorization: `Bearer ${token}`, origin: 'https://other.example.test'}}));
});

test('all expected GET, PUT and POST station routes reach the fixed backend', () => {
    const routes = {
        GET: ['settings', 'control-sources', 'inputs', 'candidates', 'selection-policy',
            'decision-result', 'decision-history', 'decision-runs'],
        PUT: ['settings', 'selection-policy'],
        POST: ['candidates', 'selection', 'decision-runs'],
    };
    for (const [method, resources] of Object.entries(routes)) {
        for (const station of ['station-1', 'station-2']) {
            for (const resource of resources) {
                const msg = prepared(request({method, station, resource, payload: {expected_revision: 0}}));
                assert.equal(msg.method, method);
                assert.equal(msg.requestPath, `/m4-api/stations/${station}/${resource}` +
                    (resource === 'decision-history' ? '?limit=10&offset=0' : ''));
                assert.equal(msg.followRedirects, false);
                assert.equal(msg.requestTimeout, 180000);
            }
        }
    }
});

test('unknown stations, methods and paths are rejected before proxying', () => {
    for (const station of ['', 'station-3', '../station-1', 'station-1/../../admin',
        'https://attacker.example.test', ['station-1'], undefined]) {
        const msg = request();
        msg.req.params.stationId = station;
        rejected('prepare_proxy', msg, 404);
    }
    for (const resource of ['admin', '', '../settings', 'settings?url=http://attacker.example.test',
        '%2e%2e/settings', 'decision-results', '__proto__', 'constructor']) {
        rejected('prepare_proxy', request({resource}), 404);
    }
    for (const method of ['DELETE', 'PATCH', 'OPTIONS', 'HEAD', 'TRACE', 'get']) {
        rejected('prepare_proxy', request({method}), 404);
    }
    rejected('prepare_proxy', request({method: 'PUT', resource: 'inputs', payload: {}}), 404);
    rejected('prepare_proxy', request({method: 'POST', resource: 'settings', payload: {}}), 404);
});

test('query and browser-controlled message fields cannot select a target or forward credentials', () => {
    const msg = request({query: {url: 'http://169.254.169.254/latest/meta-data/', host: 'attacker.example.test',
        token, method: 'DELETE', limit: '20'}, headers: {authorization: `Bearer ${token}`,
        cookie: 'session=synthetic-secret', origin, host: 'attacker.example.test',
        'x-forwarded-host': 'attacker.example.test', 'x-forwarded-proto': 'https'}, payload: {ignored: true}});
    Object.assign(msg, {url: 'http://attacker.example.test', method: 'DELETE', socketPath: '/var/run/docker.sock', requestPath: '/containers/json',
        headers: {Authorization: `Bearer ${token}`, Cookie: 'private', Origin: origin}, cookies: {private: true}});
    const result = prepared(msg);
    assert.equal(result.requestPath, '/m4-api/stations/station-1/settings');
    assert.equal(result.socketPath, undefined);
    assert.equal(result.url, 'http://127.0.0.1:8844' + result.requestPath);
    assert.equal(result.method, 'GET');
    assert.deepEqual(jsonValue(result.headers), {Host: '127.0.0.1:8844', Accept: 'application/json'});
    assert.equal(result.cookies, undefined);
    assert.equal(result.payload, undefined);
    assert.equal(result.followRedirects, false);
});

test('backend URL comes only from server configuration and malformed addresses are rejected', () => {
    const msg = prepared(request(), {M4_BACKEND_URL: 'http://m4-api:8844'});
    assert.equal(msg.url, 'http://m4-api:8844/m4-api/stations/station-1/settings');
    assert.equal(msg.socketPath, undefined);
    for (const backend of ['file:///etc/passwd', 'http://user:pass@127.0.0.1:8844',
        'http://127.0.0.1:8844/path', 'http://127.0.0.1:8844?url=x',
        'http://127.0.0.1:8844#fragment', 'http://127.0.0.1:8844\r\nHost: attacker', 123]) {
        rejected('prepare_proxy', request(), 503, {M4_BACKEND_URL: backend});
    }
});

test('history forwards only bounded pagination matching the backend API contract', () => {
    for (const query of [{}, {limit: '1', offset: '0'}, {limit: '20', offset: '100000'}]) {
        const msg = prepared(request({resource: 'decision-history', query: {...query, token,
            url: 'http://attacker.example.test', sort: 'secret'}}));
        assert.equal(msg.requestPath, '/m4-api/stations/station-1/decision-history' +
            `?limit=${query.limit || '10'}&offset=${query.offset || '0'}`);
    }
    for (const query of [{limit: '0'}, {limit: '21'}, {limit: '50'}, {limit: '100'},
        {limit: '-1'}, {limit: '1.5'}, {limit: ['10']}, {limit: 10}, {limit: '10&url=x'},
        {offset: '-1'}, {offset: '1.5'}, {offset: ['0']}, {offset: 0}, {offset: '100001'},
        {offset: '999999999'}, {offset: '0&url=x'}]) {
        rejected('prepare_proxy', request({resource: 'decision-history', query}), 400);
    }
});

test('historical details accept only a canonical UUID4 record identifier', () => {
    const msg = prepared(request({resource: undefined, record: runId, query: {token, url: 'http://attacker.example.test'}}));
    assert.equal(msg.requestPath, `/m4-api/stations/station-1/decision-results/${runId}`);
    for (const record of ['', '../settings', `${runId}?url=x`, `${runId}/../../inputs`,
        '11111111-2222-1333-8444-555555555555', '11111111-2222-4333-1444-555555555555',
        'not-a-uuid', [runId], {id: runId}]) {
        rejected('prepare_proxy', request({record}), 404);
    }
    rejected('prepare_proxy', request({method: 'PUT', record: runId, payload: {}}), 404);
});

test('write payloads must be JSON objects and are serialized with safe headers', () => {
    for (const method of ['PUT', 'POST']) {
        const resource = method === 'PUT' ? 'settings' : 'decision-runs';
        const data = {request_id: runId, text: '客户参数'};
        const msg = prepared(request({method, resource, payload: data}));
        assert.equal(msg.payload, JSON.stringify(data));
        assert.deepEqual(jsonValue(msg.headers), {
            Host: '127.0.0.1:8844', Accept: 'application/json', 'Content-Type': 'application/json',
        });
        for (const payload of [undefined, null, false, 0, 'text', '{}', [], Buffer.from('{}')]) {
            rejected('prepare_proxy', request({method, resource, payload}), 400);
        }
    }
});

test('request size limit uses UTF-8 bytes and permits the exact limit', () => {
    const exact = {text: 'x'.repeat(maximumBodyBytes - Buffer.byteLength(JSON.stringify({text: ''})))};
    const valid = prepared(request({method: 'PUT', payload: exact}));
    assert.equal(Buffer.byteLength(valid.payload), maximumBodyBytes);
    rejected('prepare_proxy', request({method: 'PUT', payload: {text: exact.text + 'x'}}), 413);
    rejected('prepare_proxy', request({method: 'PUT', payload: {text: '中'.repeat(350000)}}), 413);
});

test('JSON API responses preserve legitimate statuses and remove unsafe headers/cookies', () => {
    for (const [statusCode, payload] of [[200, {station_id: 'station-1'}], [202, {job: {status: 'running'}}],
        [400, {detail: 'bad request'}], [409, {detail: 'conflict'}], [422, {detail: [{msg: 'invalid'}]}],
        [503, {detail: '暂时不可用'}], [200, null], [200, []]]) {
        const msg = {statusCode, payload: JSON.stringify(payload), headers: {
            'set-cookie': 'private=synthetic', location: 'https://private.example.test', server: 'internal'},
            cookies: {private: true}, responseCookies: {session: true}};
        const result = run('finish_proxy', msg);
        assert.equal(result.statusCode, statusCode);
        assert.deepEqual(jsonValue(result.payload), payload);
        assert.deepEqual(jsonValue(result.headers), {
            'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
        });
        assert.equal(result.cookies, undefined);
        assert.equal(result.responseCookies, undefined);
        assert.equal(result.error, undefined);
    }
});

test('redirects, connection failures and non-JSON responses become sanitized 502 errors', () => {
    const internal = 'synthetic-upstream-private-diagnostic';
    for (const input of [{statusCode: 301, payload: '{"redirect":"private"}'},
        {statusCode: 302, payload: '{}'}, {statusCode: 307, payload: '{}'},
        {statusCode: 308, payload: '{}'}, {statusCode: 200, payload: `<html>${internal}</html>`},
        {statusCode: 500, payload: internal}, {statusCode: 200, payload: undefined},
        {statusCode: 'ECONNREFUSED', payload: internal}, {statusCode: 199, payload: '{}'},
        {statusCode: 600, payload: '{}'}, {statusCode: 200, payload: '{}', error: {message: internal}},
        {statusCode: 200, payload: '{invalid json'}]) {
        const result = run('finish_proxy', {...input, headers: {location: internal, 'set-cookie': internal},
            cookies: {internal}, responseCookies: {internal}});
        assert.equal(result.statusCode, 502);
        assert.equal(result.payload.detail, 'M4 后端暂时不可用，请稍后重试');
        assert.ok(!JSON.stringify({payload: result.payload, headers: result.headers}).includes(internal));
        assert.equal(result.cookies, undefined);
        assert.equal(result.responseCookies, undefined);
        assert.equal(result.error, undefined);
    }
});

test('generated Flow connects every ingress through authorization and safe proxy handling', () => {
    const python = process.env.M4_PYTHON || path.join(root, '.venv/bin/python');
    const code = [
        'import importlib.util,json,sys',
        'spec=importlib.util.spec_from_file_location("m4_build",sys.argv[1])',
        'module=importlib.util.module_from_spec(spec)',
        'spec.loader.exec_module(module)',
        'print(json.dumps(module.flow("<!doctype html><title>M4 test</title>")))',
    ].join('\n');
    const result = spawnSync(python, ['-c', code, path.join(root, 'm4/deploy/build_package.py')], {
        cwd: root, encoding: 'utf8', timeout: 15000,
        env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stderr || String(result.error || 'Python flow import failed'));
    const nodes = JSON.parse(result.stdout);
    const byId = Object.fromEntries(nodes.map(node => [node.id, node]));
    assert.equal(Object.keys(byId).length, nodes.length, 'Node ids must be unique');
    for (const node of nodes) {
        for (const output of node.wires || []) {
            for (const target of output) assert.ok(byId[target], `Missing wire target ${target}`);
        }
    }
    const inputs = nodes.filter(node => node.type === 'http in');
    assert.deepEqual(inputs.map(node => `${node.method} ${node.url}`).sort(), [
        'get /m4', 'get /m4-api/stations/:stationId/:resource',
        'get /m4-api/stations/:stationId/decision-results/:runId',
        'post /m4-api/stations/:stationId/:resource', 'put /m4-api/stations/:stationId/:resource',
    ].sort());
    for (const input of inputs) {
        assert.deepEqual(input.wires, [[input.url === '/m4' ? 'm4-page-authorize' : 'm4-api-authorize']]);
    }
    assert.deepEqual(byId['m4-page-authorize'].wires, [['m4-login-request'], ['m4-api-response']]);
    assert.deepEqual(byId['m4-api-authorize'].wires, [['m4-login-request'], ['m4-api-response']]);
    assert.deepEqual(byId['m4-login-request'].wires, [['m4-login-finish']]);
    assert.deepEqual(byId['m4-login-finish'].wires, [['m4-page-template'], ['m4-api-prepare'], ['m4-api-response']]);
    assert.equal(byId['m4-login-finish'].func, sources.finish_auth);
    assert.equal(byId['m4-login-request'].type, 'http request');
    assert.equal(byId['m4-login-request'].ret, 'txt');
    assert.deepEqual(byId['m4-login-catch'].scope, ['m4-login-request']);
    assert.deepEqual(byId['m4-login-catch'].wires, [['m4-login-finish']]);
    const settings = Object.fromEntries(byId['m4-customer-page'].env.map(item => [item.name, item.value]));
    assert.equal(settings.M4_FRAME_ORIGIN, 'https://ems.lvkpower.com');
    assert.equal(settings.M4_PUBLIC_ORIGIN, 'https://opdash.lvkpower.com');
    assert.equal(settings.M4_IFRAME_TOKEN, undefined);
    assert.deepEqual(byId['m4-api-prepare'].wires, [['m4-backend-request'], ['m4-api-response']]);
    assert.deepEqual(byId['m4-backend-request'].wires, [['m4-api-finish']]);
    assert.deepEqual(byId['m4-proxy-catch'].scope, ['m4-backend-request']);
    assert.deepEqual(byId['m4-proxy-catch'].wires, [['m4-api-finish']]);
    assert.deepEqual(byId['m4-api-finish'].wires, [['m4-api-response']]);
    assert.equal(byId['m4-page-authorize'].func, sources.authorize);
    assert.equal(byId['m4-api-authorize'].func, sources.authorize);
    assert.equal(byId['m4-api-prepare'].func, sources.prepare_proxy);
    assert.equal(byId['m4-api-finish'].func, sources.finish_proxy);
    assert.equal(byId['m4-page-template'].syntax, 'plain');
    assert.equal(byId['m4-page-template'].template, '<!doctype html><title>M4 test</title>');
    assert.deepEqual(byId['m4-page-template'].wires, [['m4-page-response']]);
    assert.equal(byId['m4-page-response'].headers['Content-Type'], 'text/html; charset=utf-8');
    assert.equal(byId['m4-page-response'].headers['Cache-Control'], 'no-store');
    assert.equal(byId['m4-api-response'].statusCode, '');
    assert.deepEqual(byId['m4-api-response'].headers, {});
    assert.equal(byId['m4-backend-request'].type, 'http request');
    assert.equal(byId['m4-backend-request'].method, 'use');
    assert.equal(byId['m4-backend-request'].ret, 'txt');
    assert.equal(byId['m4-backend-request'].url, '');
    assert.equal(byId['m4-backend-request'].senderr, true);
    assert(!nodes.some(node => node.type === 'exec'));
});

test('billing forwards only a validated month and remains GET only', () => {
    const msg = prepared(request({resource: 'bills', query: {month: '2026-09', token, url: 'https://other.test'}}));
    assert.equal(msg.requestPath, '/m4-api/stations/station-1/bills?month=2026-09');
    for (const month of [undefined, '2026-13', '2026-00', '2026-9', ['2026-09'], 202609, '2026-09&url=x', '1999-12']) {
        rejected('prepare_proxy', request({resource: 'bills', query: {month}}), 400);
    }
    for (const method of ['PUT', 'POST']) rejected('prepare_proxy', request({method, resource: 'bills', query: {month: '2026-09'}, payload: {}}), 404);
});

test('cabinet allocation routes forward only validated query identifiers', () => {
    let [forward, error] = run('prepare_proxy', request({resource:'allocations',query:{run_id:runId,limit:'2',offset:'0'}}));
    assert.equal(error,null);assert.equal(forward.requestPath,`/m4-api/stations/station-1/allocations?limit=2&offset=0&run_id=${runId}`);
    [forward,error]=run('prepare_proxy',request({resource:'allocation-result',query:{allocation_id:runId}}));
    assert.equal(error,null);assert.ok(forward.requestPath.endsWith(`allocation_id=${runId}`));
    [forward,error]=run('prepare_proxy',request({method:'POST',resource:'allocations',payload:{request_id:runId,plan_run_id:runId}}));
    assert.equal(error,null);assert.equal(forward.method,'POST');
    [forward,error]=run('prepare_proxy',request({resource:'allocation-result',query:{allocation_id:'../../secret'}}));
    assert.equal(forward,null);assert.equal(error.statusCode,400);
});
