'use strict';
// Native Node.js Unix Socket checks; no Node-RED runtime or Flow import.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const script = new vm.Script('(function(){\n' + fs.readFileSync(path.join(root, 'm4/deploy/node_red/socket_request.js'), 'utf8') + '\n})()');
function send(socketPath, requestPath, method='GET', payload, client=http) {
    return new Promise((resolve, reject) => {
        const timer=setTimeout(()=>reject(new Error('proxy did not finish')),5000);
        const msg={socketPath,requestPath,method,payload,headers:{Host:'localhost',Accept:'application/json',...(method==='GET'?{}:{'Content-Type':'application/json'})}};
        let sent, count=0;
        const context={msg,global:{get:()=>client},Buffer,setTimeout,clearTimeout,node:{send:value=>{sent=value;count++;},done:()=>{clearTimeout(timer);assert.equal(count,1);resolve(sent);}}};
        const immediate=script.runInNewContext(context);
        if(immediate){clearTimeout(timer);resolve(immediate);}
    });
}
(async()=>{
    if(process.env.M4_SMOKE_SOCKET){
        const socket=process.env.M4_SMOKE_SOCKET;
        let response=await send(socket,'/m4-api/stations/station-1/settings');
        assert.equal(response.statusCode,200);assert.equal(JSON.parse(response.payload).station_id,'station-1');
        response=await send(socket,'/m4-api/stations/station-1/selection-policy');
        const revision=JSON.parse(response.payload).revision;
        response=await send(socket,'/m4-api/stations/station-1/selection-policy','PUT',JSON.stringify({expected_revision:revision,preferences:null}));
        assert.equal(response.statusCode,200);
        response=await send(socket,'/m4-api/stations/station-1/decision-runs','POST','{}');
        assert.equal(response.statusCode,422,'invalid POST must not start computation');
        console.log('PASS: isolated backend GET, local preference PUT, invalid POST via native Unix Socket proxy.');
        return;
    }
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'m4-uds-'));
    const socket=path.join(directory,'mock.sock');
    let count=0;
    const server=http.createServer((req,res)=>{
        count++;
        assert.equal(req.headers.host,'localhost');
        assert.equal(req.headers.authorization,undefined);
        assert.equal(req.headers.cookie,undefined);
        assert.equal(req.headers.origin,undefined);
        if(req.url==='/redirect'){res.writeHead(302,{Location:'http://127.0.0.1:1/must-not-follow'});res.end('{}');return;}
        if(req.url==='/broken'){req.socket.destroy();return;}
        let body='';req.on('data',c=>body+=c);req.on('end',()=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({method:req.method,path:req.url,body}));});
    });
    try{
        await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(socket,resolve);});
        for(const method of ['GET','PUT','POST']){
            const payload=method==='GET'?undefined:JSON.stringify({text:'客户参数'});
            const response=await send(socket,'/mock?limit=10',method,payload);
            assert.equal(response.statusCode,200);
            assert.deepEqual(JSON.parse(response.payload),{method,path:'/mock?limit=10',body:payload||''});
        }
        let response=await send(socket,'/redirect');assert.equal(response.statusCode,302);assert.equal(count,4,'no redirect or retry');
        response=await send(socket,'/broken');assert.equal(response.statusCode,502);assert(response.error);
        response=await send(path.join(directory,'missing.sock'),'/mock');assert.equal(response.statusCode,502);
        response=await send(socket,'/mock','GET',undefined,null);assert.equal(response.statusCode,503);
        console.log('PASS: native Socket GET/PUT/POST, JSON body and path, header isolation, no redirects/retries, disconnect/missing Socket/module failures. No Flow imported.');
    }finally{
        await new Promise(resolve=>server.close(resolve));
        fs.rmSync(directory,{recursive:true,force:true});
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
