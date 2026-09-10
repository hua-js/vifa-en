#!/usr/bin/env python3
"""Build the separate, manual-only PV Node-RED import artifact."""
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).parent
TAB = 'pv_manual_tab'
nodes = [{'id': TAB, 'type': 'tab', 'label': '电站2 · 光伏手动预测', 'disabled': False,
          'info': '手动采集天气、生成一次预测、查询结果。无定时器、无启动执行。需配套 pv.sock 服务，详见 README.md。'}]


def add(identifier, kind, name, x, y, wires=None, **properties):
    node = {'id': 'pv_'+identifier, 'type': kind, 'z': TAB, 'name': name, 'x': x, 'y': y,
            'wires': wires or [], **properties}
    nodes.append(node)
    return node


def function(identifier, name, code, x, y, outputs=1, wires=None):
    return add(identifier, 'function', name, x, y, wires, func=code, outputs=outputs,
               noerr=0, initialize='', finalize='', libs=[])


for name, label, y in [('weather', '1 · 手动采集并保存天气', 140),
                       ('forecast', '2 · 手动生成一次预测（自动取新天气）', 280),
                       ('job', '查看最近一次手动任务', 420),
                       ('latest', '3 · 查询最新已完成预测', 560)]:
    add(name+'_inject', 'inject', label, 260, y, [['pv_'+name+'_prepare']],
        props=[{'p':'payload'}], repeat='', crontab='', once=False, onceDelay=0.1,
        payload='', payloadType='str', topic='')
    function(name+'_prepare', '固定操作 · '+name,
        "msg.payload = ''; delete msg.headers; msg._pvKind = "+json.dumps(name)+"; return msg;",
        600, y, wires=[['pv_'+name+'_exec']])
    method = 'POST' if name in ('weather', 'forecast') else 'GET'
    endpoint = 'runs' if name == 'forecast' else name
    script = ('set -eu; pv_service_token="$(/bin/cat /userdata/holo/pyfiles/vifa-pv/run/.raw-source.token)"; '
        '[ "${#pv_service_token}" -ge 32 ]; '
        'printf "Authorization: Bearer %s\\n" "$pv_service_token" | '
        '/usr/bin/curl --silent --show-error --connect-timeout 2 --max-time 90 '
        '--unix-socket /userdata/holo/pyfiles/vifa-pv/run/pv.sock '
        '--header @- --header "Accept: application/json" --request '+method+
        ' --write-out "\\n%{http_code}" http://localhost/api/pv/ES02/'+endpoint)
    add(name+'_exec', 'exec', 'Socket · '+method+' '+endpoint, 930, y,
        [['pv_stdout'], [], ['pv_rc']], command='/bin/sh -c '+shlex.quote(script),
        addpay=False, append='', useSpawn='false', timer='95', oldrc='false', winHide=False)

add('note', 'comment', '不含定时触发；预测完成后再点击“查询最新已完成预测”', 410, 60,
    info='WeatherRidge，固定已核验训练快照；未来24小时，96个15分钟点。查询不会触发计算。')

add('http_in', 'http in', 'GET /pv-forecast-api/ES02/latest', 290, 740,
    [['pv_auth_prepare']], url='/pv-forecast-api/ES02/latest', method='get', upload=False, swaggerDoc='')
function('auth_prepare', '校验用户 Bearer · 固定认证地址', r"""
function reject(statusCode, code) {
    if (msg.req && msg.req.headers) delete msg.req.headers.authorization;
    msg.statusCode = statusCode;
    msg.headers = {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'};
    msg.payload = {status:'error',data:null,code}; return [null,msg];
}
const req = msg.req || {};
if (Object.keys(req.query || {}).length || (req.headers && (req.headers['transfer-encoding'] || Number(req.headers['content-length'] || 0) > 0))) return reject(400,'invalid_request');
const auth = req.headers && req.headers.authorization;
if (typeof auth !== 'string' || !/^Bearer [\x21-\x7e]{1,4096}$/.test(auth)) return reject(401,'unauthorized');
const base = env.get('M3_AUTH_BASE_URL') || 'https://ems.lvkpower.com';
if (typeof base !== 'string' || !/^https:\/\/[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?$/.test(base)) return reject(503,'auth_unavailable');
msg.url = base + '/api/auth:check';
msg.headers = {Authorization:auth,Accept:'application/json'};
msg.payload = ''; msg._pvKind = 'latest';
return [msg,null];
""".strip(), 650, 740, 2, [['pv_auth_request'], ['pv_response']])
add('auth_request', 'http request', '验证 EMS 当前用户', 960, 740,
    [['pv_auth_validate']], method='GET', ret='obj', paytoqs='ignore', url='', tls='', persist=False,
    proxy='', insecureHTTPParser=False, authType='', senderr=True, headers=[],
    followRedirects=False, requestTimeout='10000')
function('auth_validate', '认证完成后清除用户令牌', """
const status = Number(msg.statusCode);
const user = msg.payload && msg.payload.data;
let size = Infinity;
try { size = Buffer.byteLength(JSON.stringify(msg.payload)); } catch (_) {}
if (msg.req && msg.req.headers) { delete msg.req.headers.authorization; delete msg.req.headers.cookie; }
delete msg.headers; delete msg.url; delete msg.responseUrl; delete msg.cookies;
if (status !== 200 || size > 65536 || !user || !Number.isInteger(user.id) || user.id < 1) {
    msg.statusCode = status === 401 || status === 403 ? 401 : 503;
    msg.headers = {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'};
    msg.payload = {status:'error',data:null,code:msg.statusCode===401?'unauthorized':'auth_unavailable'};
    return [null,msg];
}
delete msg.statusCode; msg.payload = '';
return [msg,null];
""".strip(), 1260, 740, 2, [['pv_latest_exec'], ['pv_response']])

function('stdout', '响应体 · 按请求分组', """
const text = typeof msg.payload === 'string' ? msg.payload : '';
msg.parts = {id:msg._msgid,type:'object',key:'stdout',index:0,count:2};
msg.payload = Buffer.byteLength(text) <= 1048576 ? text : '';
return msg;
""".strip(), 1240, 280, wires=[['pv_join']])
function('rc', '退出码 · 按请求分组', """
const value = msg.payload;
msg.parts = {id:msg._msgid,type:'object',key:'rc',index:1,count:2};
msg.payload = Number.isInteger(value) ? {code:value} :
    {code:value && value.code,signaled:Boolean(value && value.signal)};
return msg;
""".strip(), 1240, 400, wires=[['pv_join']])
add('join', 'join', '同一请求的响应与退出码', 1520, 340, [['pv_map']], mode='auto',
    build='object', property='payload', propertyType='msg', key='topic', joiner='\\n',
    joinerType='str', useparts=True, accumulate=False, timeout='100', count='2')
function('map', '核验 HTTP 状态与业务响应', """
function error(statusCode,code) {
    msg.statusCode=statusCode; msg.payload={status:'error',data:null,code}; return finish();
}
function finish() {
    delete msg.parts;
    msg.headers={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'};
    if (msg.res) return [msg,null];
    return [null,{payload:msg.payload,statusCode:msg.statusCode}];
}
const parts=msg.payload || {};
if (!parts.rc || parts.rc.code!==0 || parts.rc.signaled) return error(parts.rc && parts.rc.code===28?504:502,'pv_service_unavailable');
if (typeof parts.stdout!=='string') return error(502,'invalid_service_response');
const cut=parts.stdout.lastIndexOf('\\n');
const status=Number(parts.stdout.slice(cut+1));
if (cut<1 || !Number.isInteger(status) || status<200 || status>599) return error(502,'invalid_service_response');
let value;
try { value=JSON.parse(parts.stdout.slice(0,cut)); } catch (_) { return error(502,'invalid_service_response'); }
if (!value || typeof value!=='object' || Array.isArray(value)) return error(502,'invalid_service_response');
if (status>=400) {
    if (status===409 && value.status==='busy') { msg.statusCode=409; msg.payload=value; return finish(); }
    return error(status===401?503:status,'pv_service_unavailable');
}
if (msg._pvKind==='latest') {
    if (status!==200 || !((value.status==='empty' && value.data===null) ||
        (value.status==='ok' && value.data && value.data.es_sn==='ES02' && Array.isArray(value.data.points) && value.data.points.length===96))) return error(502,'invalid_forecast_response');
} else if (msg._pvKind==='job') {
    if (status!==200 || !['ok','empty'].includes(value.status)) return error(502,'invalid_job_response');
} else if (status!==202 || value.status!=='accepted' || !value.data || !value.data.job_id) return error(502,'invalid_job_response');
msg.statusCode=status; msg.payload=value; return finish();
""".strip(), 1830, 340, 2, [['pv_response'], ['pv_debug']])
add('response', 'http response', '返回只读预测', 2120, 600, statusCode='', headers={})
add('debug', 'debug', '手动操作结果（仅 payload）', 2130, 340, active=True,
    tosidebar=True, console=False, tostatus=False, complete='payload', targetType='msg', statusVal='', statusType='auto')
add('catch', 'catch', '捕获此 Flow 异常', 1550, 900, [['pv_error']], scope=[n['id'] for n in nodes if n['type'] in ('function','http request','exec')], uncaught=False)
function('error', '清除异常细节并返回固定错误', """
if (msg.req && msg.req.headers) delete msg.req.headers.authorization;
delete msg.error; delete msg.headers; delete msg.url;
msg.statusCode=502; msg.headers={'Content-Type':'application/json','Cache-Control':'no-store'};
msg.payload={status:'error',data:null,code:'pv_gateway_unavailable'};
return msg.res ? [msg,null] : [null,{payload:msg.payload}];
""".strip(), 1840, 900, 2, [['pv_response'], ['pv_debug']])

(ROOT/'pv_manual_production_flow.json').write_text(json.dumps(nodes, ensure_ascii=False, indent=2)+'\n')
print(f'{len(nodes)} nodes; manual-only; no credentials')
