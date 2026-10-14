'use strict';
// Pure planning calculations. This module has no access to the live API or storage.
globalThis.CustomerSetupModel = (() => {
  const limits = {pipeGbps:[0.1,1000],headroomPct:[10,40],otherTrafficGbps:[0,1000],ceilingGbps:[0.1,1000],ocpus:[1,64],memoryGiB:[8,512],minWorkers:[1,64],initialWorkers:[1,64],maxWorkers:[1,64],objectSlots:[1,16],batchSlots:[1,2],multipartStreams:[1,8],maxDataGB:[1,1000000],maxHours:[1,168]};
  const integers = new Set(['headroomPct','ocpus','memoryGiB','minWorkers','initialWorkers','maxWorkers','objectSlots','batchSlots','multipartStreams','maxDataGB','maxHours']);
  function evaluate(p) {
    const errors = [];
    for (const [key,[min,max]] of Object.entries(limits)) {
      if (!Number.isFinite(p[key]) || p[key]<min || p[key]>max || (integers.has(key) && !Number.isInteger(p[key]))) errors.push(`${key}: enter ${integers.has(key)?'a whole number':'a number'} from ${min} to ${max}.`);
    }
    if (![16,32,64,128].includes(p.chunkMiB)) errors.push('Choose a listed multipart chunk size.');
    if (!['undecided','private','internet'].includes(p.route)) errors.push('Choose a listed payload route.');
    if (!['full','copy-only'].includes(p.verification)) errors.push('Choose a listed verification policy.');
    if (!['VM.Standard.E4.Flex','VM.Standard.A1.Flex','VM.Optimized3.Flex'].includes(p.shape)) errors.push('Choose a listed worker shape.');
    if (!(p.minWorkers<=p.initialWorkers && p.initialWorkers<=p.maxWorkers)) errors.push('Workers must satisfy minimum ≤ initial ≤ maximum.');
    if (p.workerGbps!==null && (!Number.isFinite(p.workerGbps) || p.workerGbps<0.01 || p.workerGbps>1000)) errors.push('Measured worker rate must be between 0.01 and 1000 Gbps, or blank.');
    for (const key of ['awsRegion','ociRegion']) if (p[key] && !/^[a-z][a-z0-9-]{2,63}$/.test(p[key])) errors.push(`${key}: use a region identifier such as us-east-1 or us-ashburn-1.`);
    if (p.sourceBucket && (!/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(p.sourceBucket) || p.sourceBucket.includes('..'))) errors.push('Source bucket must use 3–63 lowercase letters, numbers, periods, or hyphens.');
    if (!p.destinationPrefix.trim()) errors.push('Enter a dedicated destination prefix.');
    for (const key of ['sourcePrefix','destinationPrefix','destinationBucket']) if (/[\x00-\x1f\x7f]/.test(p[key])) errors.push(`${key}: control characters are not allowed.`);
    const referenceFields=['awsAccountId','awsRoleArn','trustAnchorArn','rolesProfileArn','certificateRef','sourceKmsArn','tenancyOcid','compartmentOcid','namespace','destinationKeyOcid','connectionRef','awsVpcId','s3EndpointId','s3EndpointUrl','awsSecurityGroup','vcnOcid','subnetOcid','drgOcid','routePolicyRef'];
    for(const key of referenceFields) {
      const value=p[key]||'';
      if (/[\x00-\x1f\x7f]/.test(value) || /-----BEGIN |\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/.test(value)) errors.push(`${key}: enter a resource reference, never a credential or private key.`);
    }
    if(p.awsAccountId && !/^\d{12}$/.test(p.awsAccountId)) errors.push('AWS account ID must contain 12 digits.');
    for(const [key,service,resource] of [['awsRoleArn','iam','role/'],['trustAnchorArn','rolesanywhere','trust-anchor/'],['rolesProfileArn','rolesanywhere','profile/'],['sourceKmsArn','kms','(?:key|alias)/']]) {
      if(!p[key]) continue;
      const match=new RegExp(`^arn:aws(?:-us-gov|-cn)?:${service}:([^:]*):(\\d{12}):${resource}[^\\s]+$`).exec(p[key]);
      if(!match) errors.push(`${key}: enter a valid resource ARN.`);
      else {
        if(p.awsAccountId && match[2]!==p.awsAccountId && key!=='sourceKmsArn') errors.push(`${key}: account does not match the AWS account ID.`);
      }
    }
    for(const [key,kind] of [['tenancyOcid','tenancy'],['compartmentOcid','compartment'],['destinationKeyOcid','key'],['vcnOcid','vcn'],['subnetOcid','subnet'],['drgOcid','drg']]) if(p[key] && !new RegExp(`^ocid1\\.${kind}\\.[a-zA-Z0-9._-]+$`).test(p[key])) errors.push(`${key}: enter an OCID for ${kind}.`);
    if(p.s3EndpointUrl) {
      try {const url=new URL(p.s3EndpointUrl);if(url.protocol!=='https:' || url.username || url.password || url.search || url.hash || url.pathname!=='/') throw new Error();}
      catch {errors.push('S3 endpoint must be an HTTPS host URL without credentials, a bucket path, query parameters, or fragments.');}
    }
    if(p.route==='private' && p.transport==='internet') errors.push('A private payload route cannot use public internet transport.');
    if(p.route==='internet' && p.transport && !['undecided','internet'].includes(p.transport)) errors.push('Select public internet transport or change the payload route.');
    const budgetGbps = Math.max(0,Math.min(p.ceilingGbps,p.pipeGbps*(1-p.headroomPct/100)-p.otherTrafficGbps));
    if (!(budgetGbps>0)) errors.push('Other traffic leaves no migration bandwidth after headroom.');
    const buffersGiB = p.objectSlots*p.multipartStreams*p.chunkMiB/1024;
    if (buffersGiB>p.memoryGiB/2) errors.push('Upload buffers exceed half of worker memory. Reduce parallelism/chunk size or increase memory.');
    const needed = p.workerGbps===null ? null : Math.ceil(budgetGbps/p.workerGbps);
    const recommended = needed===null ? null : Math.max(p.minWorkers,Math.min(p.maxWorkers,needed));
    const checks = [];
    if (!p.awsRegion || !p.ociRegion) checks.push('Specify both cloud regions.');
    if (!p.sourceBucket || !p.destinationBucket) checks.push('Supply source and destination bucket names.');
    const referenceLabels={awsAccountId:'AWS account ID',awsRoleArn:'reader role',trustAnchorArn:'trust anchor',rolesProfileArn:'Roles Anywhere profile',certificateRef:'certificate secret',tenancyOcid:'OCI tenancy',compartmentOcid:'compartment',namespace:'storage namespace',connectionRef:'connection',awsVpcId:'AWS VPC',s3EndpointId:'S3 endpoint',awsSecurityGroup:'endpoint security group',vcnOcid:'OCI VCN',subnetOcid:'worker subnet',drgOcid:'routing gateway',routePolicyRef:'routing and security policy'};
    const missingIdentity=referenceFields.filter(key=>['awsAccountId','awsRoleArn','trustAnchorArn','rolesProfileArn','certificateRef','tenancyOcid','compartmentOcid','namespace'].includes(key)&&!p[key]);
    if(missingIdentity.length) checks.push('Complete identity references: '+missingIdentity.map(key=>referenceLabels[key]).join(', ')+'.');
    if(p.route==='private') {
      const missingPath=['connectionRef','awsVpcId','s3EndpointId','awsSecurityGroup','vcnOcid','subnetOcid','drgOcid','routePolicyRef'].filter(key=>!p[key]);
      if(missingPath.length) checks.push('Complete private path references: '+missingPath.map(key=>referenceLabels[key]).join(', ')+'.');
      if(!p.dnsMode || p.dnsMode==='undecided') checks.push('Select a DNS resolution plan and verify it from the worker subnet.');
      if(p.dnsMode==='endpoint-specific'&&!p.s3EndpointUrl) checks.push('Supply the endpoint-specific S3 URL.');
    }
    if(!p.controlPlaneAccess || p.controlPlaneAccess==='undecided') checks.push('Define access to authentication, secret retrieval, and monitoring APIs.');
    if(!p.transport || p.transport==='undecided') checks.push('Confirm the network transport and its actual capacity.');
    if (p.route==='undecided') checks.push('Confirm whether a private connection exists and select the intended payload route.');
    if (p.route==='private') checks.push('Validate interconnect provisioning, an S3 interface endpoint, private DNS and routing, and a test that blocks public payload fallback.');
    if (p.route==='internet') checks.push('Approve internet transfer and validate security rules, bandwidth, and transfer costs.');
    checks.push('Provision OCI tenancy resources, IAM policies and AWS temporary-credential trust; test scoped source read and destination write permissions.');
    checks.push('Check shape availability, architecture-compatible images, OCPU/memory sizing, quotas, and deployment cost.');
    checks.push('Inventory object sizes, encryption, versions, and source consistency; validate prefixes and destination exclusivity.');
    checks.push('Integrate and test the draft limits in the deployment installer and worker runtime before enabling jobs.');
    checks.push('Calibrate the selected shape and policy on the actual route; validate network metering, retry behavior, certificate renewal, and run limits.');
    if (p.verification==='copy-only') checks.push('Obtain acceptance of copy-only evidence and implement that policy in the deployment runtime; the active environment still requires full verification.');
    return {errors,budgetGbps,payloadGBps:budgetGbps/8,buffersGiB,needed,recommended,capped:needed!==null&&needed>p.maxWorkers,parallelObjects:p.initialWorkers*p.objectSlots,startingOcpus:p.initialWorkers*p.ocpus,checks};
  }
  function draft(p,now=new Date().toISOString()) {
    const r=evaluate(p);
    if (r.errors.length) throw new Error('Resolve local validation errors before exporting.');
    return {schema:'migration-planning-draft/v2',created_at:now,preview_only:true,execution_binding:null,cloud_validation:'not_performed',installer_supported:false,configuration:p,planning:{budget_gbps:r.budgetGbps,payload_ceiling_GBps:r.payloadGBps,recommended_workers:r.recommended,workers_needed_before_cap:r.needed,upload_buffers_gib_per_worker:r.buffersGiB},deployment_checks:r.checks};
  }
  return {evaluate,draft,numericFields:[...Object.keys(limits),'chunkMiB']};
})();
