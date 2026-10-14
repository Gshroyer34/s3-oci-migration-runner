# Portable pilot execution profile

This package binds a small pilot to its deployment's source, destination, AWS
reader identity and limits. It does not install services, issue certificates,
prove cloud access or activate a migration. A separate portable host installer now
consumes this profile and provides deployment-owned certificate enrollment and
explicit CLI pilot execution; see `docs/PORTABLE_INSTALLER.md` in the source
handoff. Preview 2 adds a separate portable operator UI and managed leaf renewal;
see `docs/PORTABLE_UI_AND_RENEWAL.md`. Fresh-tenancy acceptance is pending.

## Supported envelope

- Public-internet, immutable synthetic test data only, STANDARD storage and SSE-S3.
- At most 100 objects, 100 MB per object, 10 GB unique data, four workers, one hour
  and 30 GB of scheduled copies including retries. The actual profile can be narrower.
- Full source/destination SHA-256 verification and an independent fixture manifest.
- Dedicated run prefixes beneath the deployment destination prefix.
- No production cutover, KMS-encrypted inventory, private path, 10 GB objects,
  automatic scaling or general production inventory support is claimed.

## S3 tag compatibility and upgrade requirement

The updated worker requires `s3:GetObjectTagging` on its approved source prefix
and the updated cloud dependencies. It copies tags into OCI object metadata
`migration-s3-tags-v1` as padded URL-safe Base64 of a UTF-8 JSON map, preserving
case, Unicode and empty values. It verifies the field after upload and rejects
source tag changes. Matching payload bytes alone no longer complete a cloud job.

Applications using S3 `GetObjectTagging` must read and decode OCI metadata instead.
Tag-write APIs, tag-driven permissions, expiration and search need separate
integration. Review metadata visibility because S3 tag access has a separate
permission. Keep source tags frozen during the pilot. Copies block on reserved
name collisions or a conservative 4,096-byte combined metadata budget including
512 bytes reserved for backend fields. The source release's
`docs/S3_TAG_METADATA_HANDOFF.md` includes the decoder and full limitations.

## Property mapping and manifest release

New jobs on the updated source runtime require `s3-oci-properties/v1`: native
content headers and compatible user metadata must pass OCI HEAD read-back checks.
Original S3 HEAD observations and owner/ACL evidence are retained for export.
Generated reader permissions include prefix-scoped `s3:GetObjectAcl` and
`s3:GetObjectVersionAcl`; update restrictive Roles Anywhere session policies too.
Denied ACL access is reported as not captured, not as an empty ACL. S3 ownership,
ACLs, ETags, versions, lifecycle and retention are not recreated as OCI policies.

Deploy matching coordinator/worker code while idle before creating new jobs.
Historical jobs keep their original verification contract. The operator's
Download manifest action and coordinator `export-manifest` command produce a
bounded ZIP of JSONL records, mapping and index/checksums. This source change has
local harness qualification; read-only property checks passed on existing lab
copies. A new queued property-contract pilot and populated-field cloud acceptance
remain required.
See the source release's `docs/PROPERTY_MAPPING_AND_MANIFEST.md` for exact fields,
application API changes, export limits and remaining work.

## Files and review

`deployment-profile.json` is the administrator-approved nonsecret scope.
`binding.json` is the example job binding, pinned to the profile revision.
`aws-config.template` contains a credential-process command referencing local
certificate files and the exact AWS reader role/profile/trust anchor. It contains
no credential values. Its identity inputs must come from this deployment's AWS
stack outputs. `deployment.env.example` contains the pinned runtime profile path.

Review the profile's account, buckets, regions, prefixes, identity ARNs and limits.
Its installation revision refers to the configuration that generated it; this is
not proof that a cloud stack has been deployed or that network/access tests passed.
File hashes detect changes, not publisher authenticity. Use a trusted release channel.

## Publish to a prepared Linux runtime

The runtime Python package must already be installed. An administrator creates an
existing root-owned directory such as `/etc/migration/deployments`, with mode 0755
and root-owned parents that are not group/world writable. Run as root:

```text
python -m migration_runner.installation install-profile --profile deployment-profile.json --expected-revision REVISION_FROM_MANIFEST --output /etc/migration/deployments
```

This writes `REVISION.json`. Rerunning with the same bytes is safe. A differing
existing file, unsafe ownership, symlink or revision mismatch is rejected. The
command does not start or restart a service, update the ledger or select an active
profile. Interrupted publication fails closed; inspect any incomplete file before
an administrator removes that exact file and retries.

Install the same revision on the coordinator and each worker. Mount the profile
read-only in containers at the path in `deployment.env.example`; set those two
environment variables through the service administrator's configuration. The
profile and its parent directories must appear root-owned inside the container.
Workers do not have permission to publish or modify approved profiles.

Provision deployment-owned TLS and AWS certificates separately. Use the public
certificate common name recorded in the manifest for AWS Roles Anywhere. Place
the AWS certificate/key at the fixed paths in the template. Install the generated
AWS profile as a private Linux file owned by root or the runtime user, with mode
0600. Keep the existing certificate-authenticated database and worker-token
requirements. Enrollment/renewal automation is not provided by this profile-only
package. The separate portable installer provides node CSR generation, offline
signing, Vault enrollment and explicit manual renewal.

## Bounded coordinator and worker workflow

With the appropriate scoped cloud environment and reviewed fixture manifest:

```text
python -m migration_runner.cloud_cli validate --binding binding.json
python -m migration_runner.cloud_cli inventory-plan --binding binding.json --reference-manifest fixture-reference.json
python -m migration_runner.cloud_cli start --binding binding.json --job REVIEWED_JOB_ID --max-workers 1 --max-seconds 3600 --max-scheduled-bytes REVIEWED_BYTE_ALLOWANCE
python -m migration_runner.cloud_cli work-once --job REVIEWED_JOB_ID --worker-id worker-1
```

These are explicit activation commands, not steps performed by package generation
or profile publication. Do not execute cloud commands until scoped credentials,
source freeze, exclusive destination ownership, connection checks and the pilot
budget have been reviewed. The current fixture collector requires an independently
generated SHA-256 manifest matching every object and size.

The coordinator stores the profile snapshot and binding with the job. Every worker
checks the job against its separately installed profile and pinned revision before
claiming work. A mismatched revision, scope, limits or credential-process identity
stops execution. An installed portable profile cannot execute legacy lab bindings
or local filesystem jobs. A changed profile requires a new reviewed job; there is
no automatic profile switch for existing work.

The lab host bootstrap remains separate. The portable installer and optional
portable UI consume this profile for fresh hosts; read their manual guides and
acceptance limitations before using them in another tenancy.
