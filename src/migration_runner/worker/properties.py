"""Versioned property contract. Provenance is not an OCI authorization policy."""
import hashlib
import json
from datetime import datetime

from .rclone import WorkerError
from .tags import validate_metadata

REVISION = 's3-oci-properties/v1'
HEAD_FIELDS = (
    'ContentLength', 'ContentType', 'ContentEncoding', 'ContentDisposition',
    'ContentLanguage', 'CacheControl', 'Expires', 'Metadata', 'ETag', 'LastModified',
    'VersionId', 'StorageClass', 'ServerSideEncryption', 'BucketKeyEnabled',
    'SSEKMSKeyId', 'ChecksumCRC32', 'ChecksumCRC32C', 'ChecksumCRC64NVME',
    'ChecksumSHA1', 'ChecksumSHA256', 'ChecksumType', 'ObjectLockMode',
    'ObjectLockRetainUntilDate', 'ObjectLockLegalHoldStatus', 'ReplicationStatus',
    'Restore', 'Expiration', 'WebsiteRedirectLocation', 'MissingMeta',
)
NATIVE_HEADERS = {
    'ContentType': 'content-type', 'ContentEncoding': 'content-encoding',
    'ContentDisposition': 'content-disposition', 'ContentLanguage': 'content-language',
    'CacheControl': 'cache-control',
}
RESERVED = set(NATIVE_HEADERS.values()) | {'mtime', 'btime', 'tier', 'md5chksum', 'expires'}
CONTRACT = {
    'revision': REVISION,
    'native_headers': NATIVE_HEADERS,
    'user_metadata': 'Case-insensitive names; exact values; reserved names and unsupported encodings block copying.',
    'tags': 'Canonical JSON in migration-s3-tags-v1 metadata plus original tags in the manifest.',
    'provenance_only': ['ETag', 'LastModified', 'VersionId', 'StorageClass', 'Owner', 'Grants',
                        'ServerSideEncryption', 'checksums', 'Expires', 'Expiration',
                        'WebsiteRedirectLocation', 'replication', 'restore', 'retention'],
    'semantics': 'OCI assigns its own ETag, timestamps, ownership and encryption. S3 IAM/ACL/tag policies do not transfer.',
    'bucket_lifecycle': 'Not captured by workers or applied. A separate bucket policy review is required.',
    'limitations': 'HEAD fields are observations, not every S3 property. Checksums and retention may require separate permissions/APIs. Missing means not observed, not absent.',
}


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    return value


def digest(snapshot):
    # Observation time is deliberately outside the stable property snapshot.
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def validate_source(snapshot, tags):
    if not isinstance(snapshot, dict) or snapshot.get('revision') != REVISION:
        raise WorkerError('Invalid property snapshot')
    head = snapshot.get('head', {})
    metadata = head.get('Metadata')
    if (type(head.get('ContentLength')) is not int or head['ContentLength'] < 0
            or not head.get('ETag') or not head.get('LastModified') or not isinstance(metadata, dict)):
        raise WorkerError('Incomplete source property snapshot')
    if head.get('MissingMeta', 0):
        raise WorkerError('S3 omitted metadata; complete preservation is unavailable')
    if head.get('StorageClass', 'STANDARD') != 'STANDARD' or head.get('ServerSideEncryption') != 'AES256':
        raise WorkerError('Current runtime requires Standard storage and SSE-S3 encryption')
    if any(k.lower() in RESERVED for k in metadata):
        raise WorkerError('User metadata collides with backend system fields')
    validate_metadata(metadata, tags)
    for name in NATIVE_HEADERS:
        if name in head:
            value = head[name]
            if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise WorkerError('Native header requires an unsupported encoding')
    # Retention must never silently become an unprotected destination object.
    if head.get('ObjectLockMode') or head.get('ObjectLockLegalHoldStatus') == 'ON':
        raise WorkerError('Source retention requires a reviewed destination policy')
    return snapshot


def compare_destination(source, destination):
    """Check actual OCI HEAD headers, including fields hidden by Rclone lsjson."""
    head = source['head']
    target = {k.lower(): v for k, v in destination.items()}
    mismatches = []
    for source_name, target_name in NATIVE_HEADERS.items():
        if source_name in head and target.get(target_name) != head[source_name]:
            mismatches.append(source_name)
        elif source_name not in head and target_name != 'content-type' and target.get(target_name):
            mismatches.append(source_name)
    for key, value in head['Metadata'].items():
        if target.get('opc-meta-' + key.lower()) != value:
            mismatches.append('Metadata.' + key)
    if str(target.get('content-length')) != str(head['ContentLength']):
        mismatches.append('ContentLength')
    return {'status': 'verified' if not mismatches else 'mismatch', 'mismatches': mismatches,
            'scope': 'Supported native headers and source user metadata only; provenance and policies are not native equivalence.'}


def review_items(snapshot):
    head = snapshot['head']
    items = ['bucket_lifecycle_not_captured', 'application_tag_api_and_policy_review']
    if snapshot.get('acl', {}).get('status') != 'captured':
        items.append('owner_acl_not_captured')
    for field in ('Expires', 'Expiration', 'WebsiteRedirectLocation', 'ReplicationStatus', 'Restore'):
        if field in head:
            items.append(field + '_manifest_only')
    return items
