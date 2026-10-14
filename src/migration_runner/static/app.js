'use strict';
let token='';
const el=id=>document.getElementById(id);
async function api(path,body){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 if(!response.ok) throw Error((await response.json()).detail||'Request failed');
 return response;
}
function action(fn){return async()=>{try{el('message').textContent='';await fn();}catch(error){el('message').textContent=error.message;}};}
const job=()=>el('jobs').value;
async function refresh(){if(job())el('report').textContent=JSON.stringify(await (await api(`/api/jobs/${job()}`)).json(),null,2);}
el('sign-in').onclick=action(async()=>{token=el('token').value;el('token').value='';await api('/api/session');el('login').hidden=true;el('workspace').hidden=false;const jobs=await (await api('/api/jobs')).json();el('jobs').replaceChildren(...jobs.map(j=>{const option=document.createElement('option');option.value=j.id;option.textContent=`${j.id} · ${j.state} · ${j.control}`;return option;}));await refresh();});
el('logout').onclick=()=>{token='';el('workspace').hidden=true;el('login').hidden=false;el('report').textContent='';el('events').textContent='';el('jobs').replaceChildren();};
el('refresh').onclick=action(refresh);el('jobs').onchange=action(refresh);
el('start').onclick=action(async()=>{await api(`/api/jobs/${job()}/start`,{source_root:el('source').value,destination_root:el('destination').value,limits:{max_scheduled_bytes:Number(el('bytes').value),max_seconds:Number(el('seconds').value),max_workers:Number(el('workers').value)}});await refresh();});
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=action(async()=>{if(button.dataset.action==='cancel'&&!confirm('Cancel this migration permanently?'))return;await api(`/api/jobs/${job()}/${button.dataset.action}`,{});await refresh();}));
el('retry').onclick=action(async()=>{await api(`/api/jobs/${job()}/retry/${Number(el('batch').value)}`,{});await refresh();});
el('audit').onclick=action(async()=>{el('events').textContent=JSON.stringify(await (await api('/api/audit')).json(),null,2);});
for(const format of ['json','csv'])el(format).onclick=action(async()=>{const response=await api(`/api/jobs/${job()}/export?format=${format}`);const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=`migration-report.${format}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
