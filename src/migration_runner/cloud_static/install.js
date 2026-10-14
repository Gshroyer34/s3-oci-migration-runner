'use strict';
(() => {
  const form=document.getElementById('installation-form'), el=id=>document.getElementById(id);
  let validated=null, busy=false, connectionsEnabled=false, connectionBusy=false, connectionReport=null;
  fetch('/api/installation/context').then(r=>r.ok?r.json():null).then(context=>{
    if(context?.mode==='local-stateless-assistant'&&context.operator_token_required===false) {
      el('installation-token').required=false;el('installation-token-label').hidden=true;
      el('installation-access-note').textContent='Enter resource identifiers and your SSH public key only. Package preparation needs no cloud login. Optional live checks use existing profiles on this computer; never enter cloud passwords or private keys here.';
      if(context.connections?.enabled) {
        connectionsEnabled=true;
        el('connection-context').textContent=`Runs from: ${context.connections.execution_location}. AWS profile: ${context.connections.aws_profile}. OCI profile: ${context.connections.oci_profile}. Uses local administrator credentials, not worker identities.`;
        const defaults=context.connections.defaults;
        if(defaults)for(const [group,values] of Object.entries(defaults))for(const [key,value] of Object.entries(values)){
          const field=form.elements.namedItem(group+'.'+key);if(field&&!field.value)field.value=value??'';
          if(field&&group==='network'&&key==='payload_route')field.value=value;
        }
        connectionButtons();
      }
    }
  }).catch(()=>{});
  const numeric=new Set(['ocpus','memory_gib','initial_workers','maximum_workers']);
  function configuration() {
    const value={schema_version:'migration-installation/v1',source:{},destination:{},network:{},compute:{}};
    for(const [key,raw] of new FormData(form)) {
      const parts=key.split('.'),text=raw.trim();
      if(parts.length===1)value[key]=text;
      else value[parts[0]][parts[1]]=numeric.has(parts[1])?Number(text):text;
    }
    for(const [group,key] of [['source','kms_key_arn'],['network','s3_endpoint_id']])if(!value[group][key])value[group][key]=null;
    return value;
  }
  function buttons() {el('installation-validate').disabled=busy;el('installation-download').disabled=busy||validated===null;}
  function connectionButtons() {el('connection-test').disabled=!connectionsEnabled||connectionBusy;el('connection-download').disabled=connectionBusy||connectionReport===null;}
  function connectionConfiguration() {
    const data=configuration();
    return {source:data.source,destination:data.destination,network:{payload_route:data.network.payload_route,s3_endpoint_id:data.network.s3_endpoint_id}};
  }
  function reset() {validated=null;connectionReport=null;el('connection-status').textContent='Settings changed. Run a new connection test.';el('connection-checks').replaceChildren();connectionButtons();el('installation-status').textContent='Configuration changed. Validate before downloading.';el('installation-checks').replaceChildren();buttons();}
  form.addEventListener('input',reset);form.addEventListener('change',reset);
  async function request(path,body) {
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+el('installation-token').value},body:JSON.stringify(body)});
    if(!response.ok) {
      const error=await response.json();
      throw Error((error.detail||'Request failed.')+(error.fields?' Check: '+error.fields.join(', '):''));
    }
    return response;
  }
  el('connection-test').addEventListener('click',async()=>{
    if(connectionBusy||!connectionsEnabled)return;
    for(const field of form.elements)if(field.name?.startsWith('source.')||field.name?.startsWith('destination.'))if(!field.reportValidity())return;
    const settings=connectionConfiguration(),snapshot=JSON.stringify(settings);
    connectionBusy=true;connectionReport=null;connectionButtons();el('connection-checks').replaceChildren();
    el('connection-status').textContent='Checking AWS and OCI access… This can take up to 60 seconds.';
    try {
      const report=await (await request('/api/installation/test-connections',settings)).json();
      if(JSON.stringify(connectionConfiguration())!==snapshot){el('connection-status').textContent='Settings changed during the test. Results discarded; test again.';return;}
      connectionReport=report;
      el('connection-status').textContent=`${report.read_checks_passed?'Read checks passed.':'Some checks need attention.'} Migration readiness is not yet verified. Tested ${new Date(report.checked_at).toLocaleString()} from ${report.execution_location} (${report.elapsed_seconds}s).`;
      const labels={passed:'Passed',failed:'Failed',not_tested:'Not tested'};
      el('connection-checks').replaceChildren(...report.checks.map(item=>{
        const li=document.createElement('li'),label=document.createElement('strong');li.dataset.status=item.status;
        label.textContent=`${labels[item.status]||item.status} — ${item.name}: `;li.append(label,document.createTextNode(item.detail));return li;
      }));
    }catch(error){el('connection-status').textContent=error.message;}
    finally{connectionBusy=false;connectionButtons();}
  });
  el('connection-download').addEventListener('click',()=>{
    if(!connectionReport||connectionBusy)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(connectionReport,null,2)],{type:'application/json'})),link=document.createElement('a');
    link.href=url;link.download='migration-connection-report.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!form.reportValidity())return;
    busy=true;validated=null;buttons();el('installation-checks').replaceChildren();
    const profile=configuration(),snapshot=JSON.stringify(profile);
    try {
      el('installation-status').textContent='Checking configuration…';
      const report=await (await request('/api/installation/validate',profile)).json();
      if(JSON.stringify(configuration())!==snapshot){reset();return;}
      validated=snapshot;
      el('installation-status').textContent=`Configuration valid: ${report.summary.initial_instances} private instances, ${report.summary.total_initial_ocpus} initial OCPUs. Cloud access has not been tested. Application activation is not included.`;
      el('installation-checks').replaceChildren(...report.required_checks.map(text=>{const li=document.createElement('li');li.textContent=text;return li;}));
    } catch(error) {el('installation-status').textContent=error.message;}
    finally {busy=false;buttons();}
  });
  el('installation-download').addEventListener('click',async()=>{
    if(busy||validated===null)return;
    const profile=configuration();if(JSON.stringify(profile)!==validated){reset();return;}
    busy=true;buttons();
    try {
      const response=await request('/api/installation/package',profile);
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
      link.href=url;link.download=profile.deployment_name+'-installation.zip';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      el('installation-status').textContent='Package downloaded. Read START_HERE.md before creating cloud resources. The running environment was not changed.';
    } catch(error) {el('installation-status').textContent=error.message;}
    finally {busy=false;buttons();}
  });
})();
