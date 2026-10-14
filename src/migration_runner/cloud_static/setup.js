'use strict';
(() => {
  const model=globalThis.CustomerSetupModel,form=document.getElementById('setup-form');
  const el=id=>document.getElementById(id),fmt=n=>Number.isFinite(n)?n.toLocaleString(undefined,{maximumFractionDigits:3}):'—';
  function read() {
    const p=Object.fromEntries(new FormData(form));
    for(const key of model.numericFields) p[key]=p[key].trim()===''?NaN:Number(p[key]);
    p.workerGbps=p.workerGbps.trim()===''?null:Number(p.workerGbps);
    for(const key of ['awsRegion','ociRegion','sourceBucket','destinationBucket']) p[key]=p[key].trim();
    return p;
  }
  function list(target,items) {target.replaceChildren(...items.map(text=>{const li=document.createElement('li');li.textContent=text;return li;}));}
  function update() {
    const p=read(),r=model.evaluate(p),valid=!r.errors.length;
    el('export-status').textContent='';
    el('budget-value').textContent=valid?`${fmt(r.budgetGbps)} Gbps`:'Check settings';
    el('payload-value').textContent=valid?`${fmt(r.payloadGBps)} GB/s`:'—';
    el('parallel-objects').textContent=valid?fmt(r.parallelObjects):'—';
    el('buffer-estimate').textContent=valid?`${fmt(r.buffersGiB)} GiB`:'—';
    el('compute-estimate').textContent=valid?`${fmt(r.startingOcpus)} OCPUs`:'—';
    el('worker-recommendation').textContent=!valid?'—':r.recommended===null?'Needs calibration':`${r.recommended} workers`;
    el('recommendation-note').textContent=!valid?'Correct the draft settings to see a recommendation.':r.recommended===null?'Enter a measured per-worker source-read rate to estimate a worker count. The initial worker count remains your chosen starting point.':`Planning estimate: ${r.needed} workers to approach the bandwidth budget, assuming linear scaling at your entered rate. ${r.capped?'The maximum worker limit caps the recommendation; the plan may leave bandwidth unused.':'Your minimum and maximum limits are applied.'} This does not resize the active deployment or predict achieved throughput.`;
    el('route-note').textContent=p.route==='private'?'Private route prerequisite: confirm the interconnect and regions, then validate an S3 interface endpoint, routing, and DNS from OCI workers. Cloud access has not been tested.':p.route==='internet'?'Public internet selected for this draft. Rates and transfer charges must be validated for the selected regions.':'Connection status is unknown. Confirm regions, private connectivity, and available capacity before validating access.';
    el('verification-note').textContent=p.verification==='full'?'Full verification checks object content and provides separate copied and verified milestones. The active verification workflow reads source data for hashing, copying, and rechecking, plus destination verification; total network traffic is higher than copied bytes.':'Copy only skips full content verification. A completed copy is not proof of verified content. This policy requires runtime integration; selecting it here does not change the active deployment or its evidence.';
    const validation=el('validation');validation.className=valid?'validation-ok':'validation-errors';validation.replaceChildren();
    if(valid)validation.textContent='Local planning checks pass. Cloud readiness has not been checked.';
    else {const title=document.createElement('strong');title.textContent='Resolve these settings before exporting:';const ul=document.createElement('ul');list(ul,r.errors);validation.append(title,ul);}
    el('download-draft').disabled=!valid;
    el('plan-summary').textContent=`Source: ${p.sourceBucket||'bucket to be confirmed'}${p.sourcePrefix?'/'+p.sourcePrefix:''} (${p.awsRegion||'region to be confirmed'}) → OCI: ${p.destinationBucket||'bucket to be confirmed'}/${p.destinationPrefix} (${p.ociRegion||'region to be confirmed'}). ${p.shape}, ${fmt(p.ocpus)} OCPUs / ${fmt(p.memoryGiB)} GiB per worker. Start ${fmt(p.initialWorkers)}, range ${fmt(p.minWorkers)}–${fmt(p.maxWorkers)} active workers. ${p.verification==='full'?'Full verification':'Copy only (proposed)'}. Stop admitting new work at ${fmt(p.maxDataGB)} GB scheduled copy data or ${fmt(p.maxHours)} hours; active work must drain under the implemented runtime policy.`;
    list(el('deployment-checks'),r.checks);
  }
  form.addEventListener('input',update);form.addEventListener('change',update);
  document.querySelectorAll('[data-pipe]').forEach(button=>button.addEventListener('click',()=>{form.elements.pipeGbps.value=button.dataset.pipe;update();}));
  el('reset-draft').addEventListener('click',()=>{form.reset();update();});
  form.addEventListener('submit',event=>{event.preventDefault();if(!form.reportValidity())return;try {const blob=new Blob([JSON.stringify(model.draft(read()),null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='migration-configuration-draft.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);el('export-status').textContent='Configuration draft downloaded. Nothing was applied to the active deployment.';}catch(error){el('export-status').textContent=error.message;}});
  update();
})();
