const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('m4/deploy/node_red/authorize.js','utf8');
function run(origin,allow){
 const values={M4_PUBLIC_ORIGIN:origin+':1880',M4_FRAME_ORIGIN:origin,M4_ALLOW_HTTP_ORIGIN:allow};
 const msg={req:{params:{},query:{token:'test-token'},headers:{},method:'GET'}};
 return vm.runInNewContext('(function(){'+source+'})()', {msg,env:{get:k=>values[k]}});
}
test('local HTTP requires opt-in and still calls issuer authentication',()=>{
 assert.equal(run('http://192.168.1.53','')[1].statusCode,503);
 const result=run('http://192.168.1.53','1');
 assert.equal(result[0].url,'http://192.168.1.53/api/auth:check');
 assert.equal(result[0].headers.Authorization,'Bearer test-token');
 assert.equal(result[1],null);
});
test('opt-in does not allow public HTTP, hostnames or malformed IPs',()=>{
 for(const origin of ['http://8.8.8.8','http://example.com','http://192.168.999.1','http://192.168.1.53@evil.com'])
  assert.equal(run(origin,'1')[1].statusCode,503);
});
