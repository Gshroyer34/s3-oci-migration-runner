# S3 compatibility and validation

Object bytes can be copied, but storage semantics do not all transfer. Review the application, authorization and retention requirements before using a production release.

| S3 information | Current handling | Required follow-up |
| --- | --- | --- |
| Payload | Full-read SHA-256 plus independent fixture reference | Production inventories and alternate validation modes remain development work |
| Object tags | Encoded together in OCI metadata `migration-s3-tags-v1`; complete map in evidence/manifest | Change application tag reads to metadata reads and decode; no tag-write API shim or ongoing sync |
| Compatible user metadata | Copied and checked after upload | Case-insensitive key rules and size/encoding restrictions apply |
| Content-Type, Content-Encoding, Content-Disposition, Content-Language, Cache-Control | Native target headers, exact read-back comparison | Test populated fields with the receiving application |
| Owner and ACL grants | Captured as provenance when permitted | Rebuild destination access; source owner/ACL is not enforced on OCI |
| ETag, LastModified, VersionId, StorageClass | Original observations in manifest | OCI assigns its own native values; no version-history migration |
| Encryption/checksum observations | Recorded when returned; destination has its own encryption | AWS keys are not copied; SSE-KMS is not admitted by this pilot |
| HTTP Expires, redirects, replication/restore information | Manifest observations/review items when returned | Native equivalence is not verified |
| Bucket lifecycle/expiration rules | Not captured or applied | Discover and translate rules separately; per-object expiration is not the full rule set |
| Object Lock/legal hold | Available observations recorded; observed active protections block copying | Design and approve destination retention independently; absent observations prove nothing |

## Tags are data, not a replacement API

Read the native OCI header `opc-meta-migration-s3-tags-v1`. Its value is padded URL-safe Base64 of a UTF-8 JSON tag map with original case and values retained. Base64 is not encryption. An encoded empty object is different from missing or unreadable metadata.

```python
from migration_runner.worker.tags import TAG_METADATA_KEY, decode_tags
tags = decode_tags(response.headers['opc-meta-' + TAG_METADATA_KEY])
```

Applications must replace `GetObjectTagging` with an OCI metadata read and decoding. `PutObjectTagging`, tag filtering, tag-based IAM, lifecycle selection and searchable indexes are not emulated. Metadata readers may see values previously protected by separate S3 tag permissions. Review that visibility change.

The implementation uses a conservative 4,096-byte combined metadata budget, including 512 bytes for backend fields; this is an application guardrail. It rejects collisions, unsupported encodings and overflow rather than truncating. A tag repair currently recopies the object payload.

## Verification choices

This release requires full SHA-256 reads and verifies tags and supported properties separately. Source identity and properties are rechecked; source data/tag/metadata freeze is still necessary because these requests are not an atomic snapshot.

Other approaches for a future release include compatible provider checksums, size/inventory reconciliation and sampled full reads. They offer different assurance. An ETag must not be assumed to be a portable full-object MD5, especially for multipart objects. Size matching alone cannot prove byte equality. No alternate method is currently enabled, and benchmark copy-only results do not change the pilot contract.

The manifest includes explicit `not_captured` and review states. For example, denied owner/ACL access does not become an empty ACL and does not automatically invalidate supported-header verification. Application acceptance must decide whether those provenance gaps are acceptable.

Detailed contracts: [tag mapping](S3_TAG_METADATA_HANDOFF.md) and [property/manifest mapping](PROPERTY_MAPPING_AND_MANIFEST.md).
