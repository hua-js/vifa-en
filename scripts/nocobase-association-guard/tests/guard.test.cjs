const test=require('node:test');const assert=require('node:assert/strict');
const {createGuard}=require('../server/guard');const policy=require('../server/policy.json');const guard=createGuard(policy);
function context(name){return {state:{currentUser:{id:1},currentRoles:policy.roles},action:{resourceName:name,params:{}},path:'/api/'+name+':list',query:{},request:{},throw(status,detail){let e=new Error(detail.message||detail);e.status=status;throw e;}};}
for(const name of Object.keys(policy.collections)) {
 test(name+' keeps scalar fields and row filters',async()=>{const c=context(name);c.action.params={fields:policy.collections[name].slice(0,2),filter:{run:{station_id:'ES02'}}};const before=JSON.stringify(c);let n=0;await guard(c,()=>n++);assert.equal(n,1);assert.equal(JSON.stringify(c),before);});
 for(const [variant,change] of [['appends',c=>c.query.appends='run'],['array',c=>c.action.params.appends=['createdBy']],['bracket',c=>c.query['appends[0]']='updatedBy'],['nested_fields',c=>c.query.fields='run.requested_by'],['POST',c=>c.request.body={appends:['run']}],['association_path',c=>{c.path='/api/'+name+'/1/createdBy:get';c.action.resourceName=name+'.createdBy';}],['encoded_path',c=>{c.path='/api/'+name+'%2F1%2Frun:get';c.action.resourceName='';}]])
 test(name+' blocks '+variant,async()=>{const c=context(name);change(c);await assert.rejects(()=>guard(c,()=>assert.fail('forwarded forbidden request')),e=>e.status===403);});
}
test('header alone cannot activate policy',async()=>{let c=context(Object.keys(policy.collections)[0]);c.state.currentRoles=['admin'];c.headers={'x-role':policy.roles[0]};c.query.appends='run';let n=0;await guard(c,()=>n++);assert.equal(n,1);});
test('unrelated collection unchanged',async()=>{let c=context('t_es_data');c.query.appends='anything';let n=0;await guard(c,()=>n++);assert.equal(n,1);});
test('union containing restricted role stays guarded',async()=>{let c=context(Object.keys(policy.collections)[0]);c.state.currentRoles=['member',...policy.roles];c.query.appends='run';await assert.rejects(()=>guard(c,()=>assert.fail()),e=>e.status===403);});
