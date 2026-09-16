'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.resolve(__dirname, '../..');
function run(name, msg) {
  const source = fs.readFileSync(path.join(root, 'm4/deploy/node_red', name + '.js'), 'utf8');
  const values = {M4_PUBLIC_ORIGIN:'https://dashboard.example.test', M4_FRAME_ORIGIN:'https://identity.example.test', M4_BACKEND_URL:'http://m4-api:8844'};
  return vm.runInNewContext('(function(){' + source + '\n})()', {msg, Buffer, env:{get:key=>values[key]}});
}
function request(params={}, route='/m4-api/stations/:stationId/:resource') {
  return {req:{method:'GET', params, route:{path:route}, headers:{authorization:'Bearer test-only-credential'},query:{}}};
}
test('project metadata uses API Bearer authentication, not page query authentication', ()=>{
  const msg=request({}, '/m4-api/project');
  const result=run('authorize',msg);
  assert.equal(result[0],msg);
  assert.equal(msg.m4AuthState.pageRequest,false);
});
test('metadata proxy is an explicit read-only endpoint', ()=>{
  const msg=request({}, '/m4-api/project');
  assert.equal(run('prepare_proxy',msg)[0],msg);
  assert.equal(msg.url,'http://m4-api:8844/m4-api/project');
  assert.equal(msg.headers.Authorization,undefined);
  const write=request({}, '/m4-api/project'); write.req.method='POST'; write.payload={};
  assert.equal(run('prepare_proxy',write)[1].statusCode,404);
});
test('safe project-defined station IDs are forwarded for authoritative backend validation', ()=>{
  const msg=request({stationId:'factory-west',resource:'settings'});
  assert.equal(run('prepare_proxy',msg)[0],msg);
  assert.equal(msg.requestPath,'/m4-api/stations/factory-west/settings');
});
test('dynamic station support does not permit traversal, encoded IDs or arbitrary routes', ()=>{
  for(const stationId of ['../x','x/y','x%2fy','__proto__','', ['factory-west']]) {
    const msg=request({stationId,resource:'settings'});
    assert.equal(run('prepare_proxy',msg)[1].statusCode,404);
  }
  const msg=request({}, '/m4-api/admin');
  assert.equal(run('prepare_proxy',msg)[1].statusCode,404);
});
test('gateway identifier grammar matches project configuration grammar', ()=>{
  for(const stationId of ['Factory_West','3rd-station','constructor','a'.repeat(80)]) {
    const msg=request({stationId,resource:'settings'});
    assert.equal(run('prepare_proxy',msg)[0],msg);
  }
  const msg=request({stationId:'a'.repeat(81),resource:'settings'});
  assert.equal(run('prepare_proxy',msg)[1].statusCode,404);
});
