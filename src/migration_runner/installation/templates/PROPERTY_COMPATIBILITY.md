# Object properties and application compatibility

This package contains an infrastructure/profile preview, not an automatically
installed migration application. The property revision described here is locally
tested source code; qualify it in your own deployment before accepting data.

## What the mapping means

- Content-Type, Content-Encoding, Content-Disposition, Content-Language and
  Cache-Control are copied and checked against OCI native HEAD responses.
- Compatible user metadata is copied and checked. Reserved-name collisions,
  unsupported encodings or a metadata budget overflow block the object.
- S3 tags become a padded URL-safe Base64 UTF-8 JSON map in OCI metadata named
  `migration-s3-tags-v1`. Original tags are also included in the evidence manifest.
- Owner/ACL, ETag, source version, timestamps, class, encryption and selected
  other HEAD observations are provenance. OCI assigns its own native identities
  and properties; the manifest does not recreate S3 authorization or policies.
- A denied owner/ACL read is explicitly not captured. Other failures stop the
  attempt. Missing HEAD fields mean not observed, not necessarily absent.
- HTTP Expires, redirects, replication and expiration indications are retained
  when observed, but native behavior is not qualified. Observed active retention
  or legal hold blocks copying pending a reviewed destination policy.
- Bucket lifecycle configuration is not captured or applied by this release.
  Per-object Expiration is not a substitute for the complete bucket rules.

`property-mapping.json` contains the machine-readable field contract. Current
execution remains a bounded synthetic Standard/SSE-S3 pilot; it is not qualified
for unrestricted customer objects, all versions, or a 750 TB migration.

## Required application review

Applications using S3 GetObjectTagging must read OCI metadata and decode the
envelope. Tag mutation, tag-based permissions/search and lifecycle selection need
separate OCI application logic. Review who can read metadata, because tag values
may become visible under permissions different from S3 tag-read permissions.

Review dependencies on S3 ETags, versions, timestamps, redirects and cache headers.
Rebuild IAM, ACL, retention, lifecycle and replication policies explicitly. Do
not enable destination deletion rules based only on the stored source properties.

## Permissions and evidence

The generated AWS template grants scoped object/tag/ACL reads, including
GetObjectVersionAcl for an observed version. A restrictive Roles Anywhere session
policy must permit the same operations. It grants no source writes or restores.

Install matching coordinator and worker releases while idle before binding new
jobs. Old jobs retain their original verification contract. New mapping jobs
require supported-field verification in addition to content and tags. Owner/ACL
availability and policy compatibility remain separate acceptance decisions.

After installation, Download manifest exports partitioned JSONL plus an index,
mapping and checksums. The coordinator export-manifest CLI provides the same
read-only operation. It includes every planned object and only the current batch
attempt; earlier runs may show not captured. No cloud calls or transfers occur.
Exports reject more than 1 million objects or 100 MiB of object records; a
production storage/query solution for larger inventories remains development work.

Protect exports like source data: they contain keys, tags, metadata and possibly
owner identities. Checksums detect corruption but the archive is not signed.
