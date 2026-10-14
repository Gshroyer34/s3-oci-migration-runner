# Release status and known limitations

October 14, 2026. This handoff packages source and tools for an administrator-led trial. Packaging checks do not qualify a fresh cloud installation.

## Available in source

- Installation/profile validation, foundation templates and a local setup assistant.
- Immutable deployment/host plans, pinned image checks, host identity checks and per-node Vault enrollment.
- Private portable UI, fixed-worker dispatch, job controls and manifest export.
- Full-read SHA-256, S3 tag mapping and supported-property checks.
- Optional managed leaf renewal with local checkpoints, safe activation deferral and visible coordinator health.
- Autoscaling recommendation evaluator, telemetry contracts and replay harness; no automatic actuation.

## Evidence and remaining acceptance

| Boundary | Evidence | Remaining work |
| --- | --- | --- |
| Core copy/tag adapter | Prior live AWS-to-OCI fixture acceptance and queued tag migration | Repeat with recipient identities and exact release |
| Property mapping/export | Local protocol/ledger tests and prior live read-back of existing objects | Fresh queued populated-property acceptance |
| Portable UI/installer | Local Python/JavaScript, Linux ownership and PostgreSQL tests | Fresh hosts, real browser, dispatch and reboot |
| Managed renewal | Cryptographic checks and mocked provider/service tests | Actual OCI issuance, complete IAM, AWS exchange and activation cycle |
| GitHub handoff | Package/checksum/link checks and selected local tests recorded in `QUALIFICATION.json` | Recipient rehearsal; publication/provenance review |

Historical local results are preserved in `docs/benchmarks/portable-installer-20261014.json` and `portable-ui-renewal-20261014.json`. Do not add their overlapping test counts together. The earlier embedded-browser visual check failed to initialize; no rendered-browser acceptance is claimed.

## Unsupported or not yet qualified

- Production inventory execution, 750 TB migration, arbitrary source keys/version histories and continuously changing sources.
- Glacier Instant Retrieval admission, archival restore, Deep Archive and SSE-KMS payload migration. Read-only discovery does not mean those objects can be migrated.
- Objects larger than 100 MB, datasets over 100 objects/10 GB, runs longer than one hour, or fleets above four workers.
- Private interconnect execution in the portable runtime. Foundation configuration alone does not establish private routing.
- Autoscaling, enforced shared bandwidth grants, dynamic enrollment/provisioning and automatic shape selection.
- Ampere/ARM installation with the supplied x86 runtime/helper.
- SSO, multiple operator roles, public HTTPS ingress, high availability, unattended upgrades or an automated backup/restore system.
- CA rollover, certificate/secret version cleanup and external alerting. Managed renewal does not mean indefinite unattended operation.
- Large searchable manifest service. Exported ZIPs have bounds; current small pilot limits remain authoritative.

## Operational details

The maximum worker value is a limit, not an autoscaler. UI admissions are lifetime allowances for a specific plan, not daily resets. Source, target, trust and scope are administrator-approved; changing them requires a reviewed configuration/deployment path, not editing a running job in the browser.

The installer refuses to overwrite an existing lab installation or replace a different portable plan. There is no supported automatic migration of an existing installation to a new plan. Preserve data and plan a separate upgrade path.

Dependency versions and image digests are pinned; no vulnerability attestation or binary reproducibility claim is made. The host bootstrap requires external package repositories. The source checksum is integrity evidence, not a publisher signature.
