'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const nodes = JSON.parse(fs.readFileSync(path.join(__dirname, '../m3/pv/pv_manual_production_flow.json')));
const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
function run(id, msg, settings={}) {
    return new Function('msg','env','Buffer',byId['pv_'+id].func)(msg,{get:key=>settings[key]},Buffer);
}
test('valid isolated graph; manual injects only; fixed commands never append input', () => {
    assert.equal(Object.keys(byId).length, nodes.length);
    for (const node of nodes) {
        for (const wires of node.wires || []) for (const id of wires) assert(byId[id]);
        if (node.type==='inject') { assert.equal(node.once,false); assert.equal(node.repeat,''); assert.equal(node.crontab,''); }
        if (node.type==='exec') { assert.equal(node.addpay,false); assert.equal(node.append,''); assert(node.command.includes('--header @-')); assert(!node.command.includes('python')); assert(node.command.includes('/etc/vifa-m3/raw-source.token')); assert(!node.command.includes('.pv-service.token')); }
        if (node.type==='function') new Function('msg','env','Buffer',node.func);
    }
    assert.equal(nodes.filter(n=>n.type==='http in').length,1);
    assert.equal(byId.pv_http_in.method,'get');
    assert.equal(byId.pv_join.mode,'auto');
});
test('public query requires Bearer and never accepts custom URL or body', () => {
    let result=run('auth_prepare',{req:{headers:{},query:{}}});
    assert.equal(result[1].statusCode,401);
    const auth='Bearer test-user-token';
    result=run('auth_prepare',{req:{headers:{authorization:auth},query:{}}});
    assert.equal(result[0].url,'https://ems.lvkpower.com/api/auth:check');
    assert.equal(result[0].headers.Authorization,auth);
    for (const query of [{url:'http://attacker'},{token:'test'}]) {
        assert.equal(run('auth_prepare',{req:{headers:{authorization:auth},query}})[1].statusCode,400);
    }
    assert.equal(run('auth_prepare',{req:{headers:{authorization:auth,'content-length':'3'}}})[1].statusCode,400);
});
test('auth result must identify real user and strips credentials before transport', () => {
    const msg={req:{headers:{authorization:'Bearer test',cookie:'private'}},headers:{Authorization:'Bearer test'},
        statusCode:200,payload:{data:{id:1}},url:'https://ems.lvkpower.com/api/auth:check'};
    const result=run('auth_validate',msg)[0];
    assert.equal(result.headers,undefined); assert.equal(result.req.headers.authorization,undefined);
    assert.equal(result.req.headers.cookie,undefined); assert.equal(result.payload,'');
    assert.equal(run('auth_validate',{statusCode:200,payload:{data:{}}})[1].statusCode,503);
});
test('concurrent stdout and rc are joined only within their original request', () => {
    const a=run('stdout',{_msgid:'request-a',payload:'{}\n200'});
    const b=run('rc',{_msgid:'request-b',payload:{code:0}});
    assert.notEqual(a.parts.id,b.parts.id);
    assert.equal(a.parts.key,'stdout'); assert.equal(b.parts.key,'rc');
    assert.equal(a.parts.count,2);
});
function response(kind,value,status=200,extra={}) {
    return run('map',{_pvKind:kind,payload:{stdout:JSON.stringify(value)+'\n'+status,rc:{code:0}},...extra});
}
test('query contract, empty result, task acceptance and errors map correctly', () => {
    assert.equal(response('latest',{status:'empty',data:null})[1].statusCode,200);
    assert.equal(response('latest',{status:'ok',data:{es_sn:'ES02',points:Array(96).fill({})}})[1].statusCode,200);
    assert.equal(response('latest',{status:'ok',data:{es_sn:'ES02',points:[]}})[1].statusCode,502);
    assert.equal(response('forecast',{status:'accepted',data:{job_id:'one'}},202)[1].statusCode,202);
    const result=response('latest',{private_token:'never reveal'},500)[1];
    assert(!JSON.stringify(result).includes('never reveal'));
    assert.equal(response('latest',{status:'empty',data:null},200,{res:{}})[0].headers['Cache-Control'],'no-store');
    assert.equal(run('map',{payload:{rc:{code:28}}})[1].statusCode,504);
});
