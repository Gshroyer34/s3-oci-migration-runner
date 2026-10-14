# S3 tags in OCI object metadata

Tag contract reference for handoff preview 2. See [release status](KNOWN_LIMITATIONS.md) for qualification boundaries.

## What the transfer now does

The cloud worker reads each object's S3 tags with its provisioned Roles Anywhere reader identity, writes a reversible representation into OCI object metadata during the payload upload, and reads it back before reporting success. Both the existing lab binding and portable CLI binding use this path. The existing synthetic-pilot size, tier, encryption, identity and source-freeze restrictions still apply.

An object with matching bytes but missing or incorrect migrated tags is recopied with the correct metadata during a normal run. Verify-only runs report the difference without writing. Repair currently recopies the payload; it is not an inexpensive metadata-only update. Copy budgets and retry limits still apply.

The worker reads the source tags again after transfer. A detected change invalidates the result even when the bytes match. The first durable tag digest also fences later attempts against a different tag set. Source freeze must include tagging changes; these observations are not a transactional snapshot and cannot detect a change that is reverted between reads.

## Metadata contract for application owners

| Item | Contract |
| --- | --- |
| OCI metadata key | `migration-s3-tags-v1` |
| Native HTTP response header | `opc-meta-migration-s3-tags-v1` |
| Value | Padded URL-safe Base64 of UTF-8 JSON containing the complete tag key/value map |
| JSON format | Keys sorted; compact separators; original Unicode, key case and values preserved |
| Empty source tags | Explicit encoded `{}`; different from a missing field or failed read |
| Version | The `v1` key identifies the encoding contract; do not infer another encoding |

The single encoded field preserves case-distinct keys such as `Owner` and `owner`, Unicode, punctuation and empty values without depending on HTTP header casing or blank-value handling. It is an encoded document, not one plain metadata field per tag. Base64 is not encryption.

The package includes a strict Python decoder:

```python
from migration_runner.worker.tags import TAG_METADATA_KEY, decode_tags

# Read the exact header from an authenticated OCI HeadObject response.
tags = decode_tags(response.headers['opc-meta-' + TAG_METADATA_KEY])
department = tags.get('Department')
```

For another language, Base64url-decode the value, decode UTF-8, then parse the JSON object. Preserve key case and empty strings. A missing or malformed field is a migration/compatibility error, not an empty tag set. Test the selected SDK's header/metadata representation in the receiving application.

### Application changes required

- Replace S3 `GetObjectTagging` calls with OCI object-metadata reads and decoding. The migration does not implement a compatibility shim for this API.
- Applications that write or delete tags need a new update workflow. This implementation performs a one-time copy and does not synchronize later S3 tag edits or emulate `PutObjectTagging` / `DeleteObjectTagging`.
- Recreate tag-driven authorization separately. A value in metadata does not enforce an OCI IAM policy. OCI objects are not individually taggable. [OCI tag limitations](https://docs.oracle.com/en-us/iaas/Content/Tagging/Tasks/managingaccesswithtags.htm)
- Review visibility: S3 has a separate tag-reading permission; mapped metadata is returned to callers allowed to read the object's metadata. Ensure the destination audience is permitted to see those values. [AWS GetObjectTagging permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectTagging.html)
- Recreate tag-filtered lifecycle, retention, processing and reporting separately. Storing `Retention=7years` does not activate a retention rule. This change does not configure bucket policies or lifecycle actions.
- Search requires an index or catalog. Complete original tags and their digest are recorded in protected per-object ledger evidence; bounded manifest export is available, while a large-scale searchable manifest service remains future work. Keep tags out of ordinary application logs and public reports.

## Guardrails and failure behavior

The mapper accepts at most ten tags and checks S3 key/value length boundaries in UTF-16 units. Duplicate keys, malformed responses and missing `TagSet` fail. `AccessDenied`, SDK failures and timeouts never become an empty tag set. The helper uses an explicit regional AWS endpoint and expected bucket owner, the approved reader profile, isolated credentials and a killable worker-owned process.

The metadata key is reserved. A case-insensitive collision with existing source metadata blocks the copy. Source metadata already prefixed `opc-meta-`, colliding names, empty values, unsupported header characters or non-ASCII values also block this first implementation instead of relying on the backend's silent dropping behavior. Tags themselves support Unicode through the encoding above. Broader user-metadata transformations are separate work.

The implementation enforces a conservative **4,096-byte combined metadata budget**, including a **512-byte allowance for backend-generated fields**. This is a product guardrail, not a statement of the OCI service maximum. Encoded tags and existing metadata share that budget. Oversize combinations block; nothing is truncated or silently omitted. A manifest-only overflow policy is not enabled.

Transfer values are carried in a per-copy environment setting, not command-line arguments, and removed afterward. Worker logs retain fixed errors and numeric process counts. Original tag values are intentionally stored in access-controlled ledger evidence to support audit and future manifest export; protect that ledger accordingly.

Content verification and tag verification are separate evidence fields. Both the worker and the PostgreSQL completion check require `tags=verified` for cloud jobs. Previously completed jobs are not retroactively reverified when the software is upgraded.


## Deployment and acceptance

The generated AWS reader includes scoped tag-read rights. Verify any additional session policies and bucket/endpoint policies. Repeat single/multipart and nonempty-tag acceptance using the recipient workload identity. See [property and manifest mapping](PROPERTY_MAPPING_AND_MANIFEST.md) and [first pilot](FIRST_PILOT.md).
