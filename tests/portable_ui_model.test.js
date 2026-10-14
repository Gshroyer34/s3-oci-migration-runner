/* Offline DOM contract test; no browser profile, cloud calls or live credentials. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element {
  constructor(){this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.children=[];this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;if(nodes[0]?.value)this.value=String(nodes[0].value);}
  set innerHTML(_){throw Error('HTML injection is prohibited');}
  click(){}
}
const ids=new Map();const get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);};
const controls=['pause','drain','resume','cancel'].map(action=>{const el=new Element();el.dataset.action=action;return el;});
const calls=[];let created=false,bound=false,control='active';
const jobId='11111111-2222-3333-4444-555555555555';
const deployment={deployment_name:'Example deployment',source:{bucket:'<img src=x>',prefix:'approved/',region:'us-west-2'},destination:{bucket:'example-target',prefix:'incoming/',region:'us-phoenix-1'},limits:{max_objects:100,max_dataset_bytes:1e10,max_object_bytes:1e8,max_seconds:3600},route:'internet',renewal_mode:'oci-managed-issuer'};
const context={document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>controls},
  console,confirm:()=>true,setInterval:()=>1,clearInterval:()=>{},setTimeout:()=>1,
  fetch:async(path,options)=>{calls.push({path,options});let value;
    if(path==='/api/session')value={ready:true,remaining_runs:3,max_runs:3,max_workers:4,certificate_expires_at:'2030-01-01T00:00:00Z',deployment,renewal:{activation:'current'},workers:[{node_id:'worker-1',ready:true,status:'idle'}]};
    else if(path==='/api/jobs'&&options.method==='POST'){created=true;value={job_id:jobId};}
    else if(path==='/api/jobs')value=created?[{id:jobId,created_at:'2026-10-14T12:00:00Z'}]:[];
    else if(path.endsWith('/start')){bound=true;value={};}
    else if(path.endsWith('/pause')){control='paused';value={};}
    else value={job_id:jobId,bound,control,ui_managed:true,queue_complete:false,exceptions:0,running:0,content_verified_objects:0,content_verified_bytes:0,destination_prefix:'incoming/ui-example/',objects:[{source_key:'approved/object.bin',size:100,batch_state:'planned'}]};
    return {ok:true,json:async()=>value};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/migration_runner/cloud_static/portable.js','utf8'),context);
(async()=>{
  assert.equal(calls.length,0);
  get('token').value='synthetic-only';await get('sign-in').onclick();
  assert.equal(get('workspace').hidden,false);assert.equal(get('token').value,'');
  assert.match(get('source').textContent,/<img src=x>/);assert.match(get('renewal').textContent,/current/);
  assert.equal(get('workers').children.length,4);assert.equal(get('start').disabled,true);
  assert(controls.every(el=>el.disabled));
  await get('create').onclick();assert.equal(get('start').disabled,false);
  get('workers').value='1';await get('start').onclick();assert.equal(get('start').disabled,true);
  await controls[0].onclick();assert.equal(get('state').textContent,'paused');
  assert(calls.every(call=>call.options.headers.Authorization==='Bearer synthetic-only'));
  get('logout').onclick();assert.equal(get('workspace').hidden,true);assert.equal(get('objects').children.length,0);
  console.log('Portable UI DOM workflow: 14 assertions passed; no cloud requests.');
})().catch(error=>{console.error(error);process.exitCode=1;});
