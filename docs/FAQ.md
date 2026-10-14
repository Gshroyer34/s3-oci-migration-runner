# Frequently asked questions

## Do I need Codex or the original developer's laptop?

No. Use Git, Python, cloud consoles/CLIs and the instructions. Manual signing needs your own protected signing workstation. Managed mode uses your own OCI issuers after setup.

## Is this one click?

No. The setup assistant generates reviewed templates. An administrator still applies the stacks, configures remaining IAM/network/certificate resources, builds the image and installs/enrolls each host. This is an engineering preview with a documented acceptance checklist.

## What runs in OCI and AWS?

OCI hosts the coordinator, database and workers and receives the copied objects. AWS retains S3 and supplies a read-only role through Roles Anywhere. The AWS stack is authorization, not a second migration compute cluster.

## Does uploading both stacks connect the clouds?

No. Authentication, network connectivity and migration correctness are separate checks. This pilot uses approved internet HTTPS. A future private deployment also needs interconnect/VPN routing, DNS, endpoints, security rules and actual path validation.

## Can I just point it at my production bucket?

Not for execution in this release. Use immutable synthetic fixtures with the independent reference manifest and documented limits. Optional read-only discovery is separate from migration admission.

## What do I enter in the browser?

The local setup assistant accepts deployment specifications and public identity references. Do not enter static AWS secrets or OCI private keys there. The deployed migration UI accepts its own operator token, displays the installed scope and lets you preview/start/control jobs within that scope.

## Can I change the source bucket or credentials after installation?

Not by editing a job. Profiles are immutable and checked independently on workers. A new scope needs an administrator-reviewed configuration and supported deployment/upgrade procedure; this preview has no in-place plan replacement.

## Are files downloaded onto the coordinator first?

Workers transfer the payload. The coordinator handles planning, state and evidence. Streaming and multipart buffers still use worker memory; the tool is not a disk staging service.

## What proves a copy is correct?

The pilot requires full SHA-256 comparisons against independent fixture references and source/destination reads, plus separate tag and supported-property checks. The manifest shows each result. It does not certify application permissions, lifecycle or retention equivalence.

## Can we skip verification to go faster?

Not in this release. Separate copy-only experiments were faster, but the portable runtime has no approved copy-only switch. Alternative verification modes require explicit engineering and acceptance work.

## What happens to tags, owner and ETag?

Tags are reversibly encoded in OCI metadata and included in the manifest. Applications need to change tag API calls. Owner/ACL and original ETag observations are evidence; OCI does not adopt them as native ownership, permissions or ETags. See [compatibility](COMPATIBILITY.md).

## Are lifecycle and Deep Archive objects migrated?

No lifecycle rules are recreated. This pilot admits Standard/SSE-S3 fixtures only. Glacier Instant Retrieval is planned, and Deep Archive remains excluded; the tool does not restore archives.

## Will lots of tiny files slow it down?

Yes, they can. Each object adds metadata, tag, permission-observation and verification requests. Measure objects per second as well as bytes per second. Large-object benchmark rates should not be applied to a 20 KB-heavy bucket.

## Will it automatically fill a 10 or 100 Gbps connection?

No. Recommendation logic exists, but shared rate enforcement and automatic worker provisioning/draining are future work. Choose a fixed pilot fleet and calibrate the real path. Worker NIC traffic alone is not interconnect utilization.

## Which shape should I start with?

One Optimized3 Flex worker at 2 OCPUs/16 GiB is the starting suggestion. E5 is supported as an alternative. Ampere benchmark results do not mean this installer supports ARM. See [shape guidance](SHAPES_AND_PERFORMANCE.md).

## What if a worker fails?

The ledger tracks leases and attempts and applies bounded retry/control rules. Do not promise seamless VM recovery: portable host lifecycle and reboot behavior still require fresh-tenancy acceptance. Preserve the ledger and inspect errors before restarting work.

## Will certificates renew without me?

Only when managed issuers and their permissions are configured. The timer requests new leaves and waits for safe activation. It does not roll over CAs, remove old versions or send alerts. Manual mode still requires workstation signing.

## Is my cloud bill capped?

No. Runtime limits bound a trial, not the account's bill. VMs, volumes and storage may keep accruing costs after a job stops. AWS outbound data and requests may cost money, including verification reads. Set cloud budgets/alerts and a cleanup owner.

## Does the tool delete the source or cut applications over?

No. The generated reader is read-only. Source deletion, application cutover, lifecycle changes and acceptance of production data are separate decisions.

## What should I send when asking for help?

Release checksum, image ID, schema/profile revision, failed stage, safe status/error summary and redacted service logs. Never send private keys, tokens, Vault bundles, raw cloud profiles or unreviewed manifests. See [operations](OPERATIONS.md).
