# Property preservation and migration manifest

Contract reference for handoff preview 2. Read [release status](KNOWN_LIMITATIONS.md) for cloud acceptance limits.

## What this adds

New cloud jobs bound by this release store mapping revision `s3-oci-properties/v1`. Workers capture selected S3 HEAD properties and owner/ACL observations before copying. After copying, they read actual OCI HEAD headers independently of Rclone's metadata listing and compare the supported fields. They recheck the source properties after the payload read. A source change or a required field mismatch prevents successful completion.

An original property digest is recorded before the copy, alongside the existing payload and tag baselines. Retries compare against that first observation. Full-read SHA-256 verification remains required. A property pass means the fields below passed; it does not certify application compatibility, retention, permissions or readiness for cutover.

## Field contract

| Source field | Destination treatment | Evidence and limitations |
| --- | --- | --- |
| Content-Type, Content-Encoding, Content-Disposition, Content-Language, Cache-Control | Copied into OCI native response headers by the pinned backend | Exact read-back comparison; tested with actual Rclone single and multipart uploads against local API fixtures |
| User metadata | Compatible fields copied into OCI custom metadata | Original map in manifest; case-insensitive names, exact values; reserved backend names, unsupported encodings, omitted metadata or excessive size block copying |
| S3 object tags | Existing reversible `migration-s3-tags-v1` metadata envelope | Complete original map in manifest; case, Unicode and empty tag values preserved; independently verified |
| Owner and ACL grants | Manifest provenance only | `GetObjectAcl` uses the observed version when available. Explicit access denial is recorded as `not_captured`, never as an empty ACL. Transient or unexpected API failure fails the object attempt |
| ETag, LastModified, VersionId, StorageClass | Original observations in manifest | OCI assigns its own native values. An S3 ETag is not assumed to be a cross-cloud checksum |
| Encryption and returned checksum fields | Manifest observations | Checksum mode is not requested in HEAD. Absence means not observed. There is no new checksum-based validation mode or transfer of AWS encryption keys |
| HTTP Expires, expiration indication, redirect, replication and restore information | Manifest when returned by HEAD | Native equivalence is not verified; these produce review items. HTTP Expires is a cache header, not a bucket deletion rule |
| Object Lock and legal hold | Capture available HEAD fields; block observed active protections | A separate destination retention design is required. Missing fields do not prove protection is absent |
| Bucket lifecycle configuration | Not captured or applied in this release | Requires separate bucket-level discovery, rule translation and approval. The per-object `Expiration` response is not the complete lifecycle configuration |

The portable pilot envelope is restrictive. This new revision checks the live HEAD observation for Standard storage and SSE-S3 encryption before reading payloads; Glacier Instant Retrieval remains a later phase and Deep Archive remains excluded. A missing S3 StorageClass on HEAD is interpreted as Standard. User metadata named `mtime`, `btime`, `tier`, `md5chksum`, `expires`, a native content-header name, the tag-envelope name or an `opc-meta-` name is rejected rather than silently repurposed by the backend. Existing budget: 4,096 bytes including 512 bytes reserved for backend fields, with a further combined backend-metadata check before upload. No overflow/truncation fallback is enabled.

## Download and inspect

After this source release is installed, sign in to the operator app, select a migration, and choose **Download manifest**. The endpoint uses the same operator authorization and same-origin restrictions as the existing report. It performs no cloud calls and starts no transfers.

The ZIP contains:

- `index.json`: job identity, source/target scope, database snapshot time, plan/inventory digests, status counts and part checksums.
- `property-mapping.json`: the versioned mapping and semantic limitations.
- `objects/part-000001.jsonl`, and further parts as needed: one JSON record per planned object, including eligible, blocked, excluded and unprocessed entries.

Each object record includes planned source identity, keys, size, current attempt, original tags, captured source properties, observed destination headers, property comparison, review items and separate content/tag/property verification. Records with no observation use null or `not_captured`; they do not imply the source has no metadata. Historical runs are not backfilled by reading today's source. Earlier attempts remain in the ledger; this export deliberately uses only the batch's current attempt so an old success cannot mask a failed retry.

The export uses a read-only repeatable-read database transaction and a streamed cursor. It partitions at 10,000 objects and rejects an export above 1 million objects or 100 MiB of uncompressed object records. It returns no truncated success artifact. These are export guardrails, not proven production-scale capacity. The existing migration limit remains much smaller. A persistent object-store export/query service is still needed for very large migrations; this ZIP is not a hosted search index or Parquet dataset.

To inspect records with Python:

```python
import json, zipfile
with zipfile.ZipFile('migration-manifest.zip') as archive:
    index = json.loads(archive.read('index.json'))
    for part in index['parts']:
        with archive.open(part['path']) as records:
            for line in records:
                obj = json.loads(line)
                if obj['verification']['properties'] != 'verified':
                    print(obj['source_key'], obj['verification']['properties'])
```

Coordinator CLI alternative, using the already installed scoped environment and credentials:

```text
python -m migration_runner.cloud_cli export-manifest --job JOB_UUID --output /private/path/migration-manifest.zip
```

It applies the existing deployment-profile job scope and refuses to overwrite an existing output. Downloads contain source keys, metadata, tags and potentially canonical owner IDs; protect them like the source data. Internal checksums detect corruption, not tampering by an adversary who can replace both content and checksums. The ZIP is not digitally signed.

## Application changes to discuss during handoff

1. Replace S3 `GetObjectTagging` with an OCI metadata read and decode the envelope. OCI metadata is not an equivalent object-tag API. Tag updates need new application logic; this tool does not synchronize subsequent changes.
2. Review access to metadata: users who can read object metadata may see values formerly protected by separate S3 tag permissions.
3. Rebuild IAM, ACL, tag-based authorization, lifecycle selection, replication and retention in OCI explicitly. Storing an owner or rule in a manifest does not enforce it.
4. Review applications that depend on S3 ETags, version IDs, timestamps, redirects or cache expiration. OCI's native values and behavior may differ.
5. Identify which properties are required for acceptance. Owner/ACL permission gaps and other provenance-only items are visible review requirements; they do not block supported-field verification by themselves.


## Permissions and acceptance

Generated reader templates include prefix-scoped object, tag and ACL/version-ACL reads. Keep any Roles Anywhere session policy consistent; no source writes are required. Use matching coordinator/worker code. Historical jobs are not retroactively property-verified.

Property capture adds source HEAD/ACL and destination HEAD requests per object. Validate populated fields, metadata limits, multipart behavior and application interpretation on the recipient deployment. Local protocol and ledger tests do not establish fresh-tenancy cloud acceptance. See [first pilot](FIRST_PILOT.md).

API references: [S3 HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html), [S3 GetObjectAcl](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectAcl.html), [Rclone OCI backend](https://rclone.org/oracleobjectstorage/).
