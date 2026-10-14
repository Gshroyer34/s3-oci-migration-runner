"""Streaming local JSONL / downloaded AWS CSV inventory / injected S3 client adapters."""
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

from .contracts import InventoryObject, PlanningError

MAX_JSON_LINE = 64 * 1024


def decode_key(value):
    if re.search(r'%(?![0-9a-fA-F]{2})', value):
        raise PlanningError('Malformed percent-encoded inventory key')
    # S3 uses percent encoding, not form encoding: literal plus must remain plus.
    return unquote(value, encoding='utf-8', errors='strict')


def jsonl_objects(path):
    with Path(path).open('rb') as stream:
        line_number = 0
        while line := stream.readline(MAX_JSON_LINE + 1):
            line_number += 1
            if len(line) > MAX_JSON_LINE:
                raise PlanningError(f'JSONL line {line_number} exceeds 64 KiB')
            try:
                yield InventoryObject.model_validate_json(line)
            except ValueError:
                raise PlanningError(f'Invalid inventory record at JSONL line {line_number}') from None


def file_md5(path):
    result = hashlib.md5(usedforsecurity=False)
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def inventory_manifest(path, data_root):
    """Return dated provenance and an iterator; all files must already be downloaded.

    File paths mirror manifest keys beneath data_root. Validate all shard checksums
    before yielding data. MD5 proves downloaded inventory-file consistency only.
    """
    path, root = Path(path), Path(data_root).resolve()
    if path.stat().st_size > 16 * 1024 * 1024:
        raise PlanningError('Inventory manifest exceeds supported 16 MiB metadata limit')
    checksum_path = path.with_name('manifest.checksum')
    if checksum_path.read_text(encoding='ascii').strip().lower() != file_md5(path):
        raise PlanningError('Inventory manifest checksum mismatch')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('fileFormat') != 'CSV':
        raise PlanningError('Only downloaded CSV/GZIP S3 Inventory is supported; ORC/Parquet are not yet supported')
    fields = [field.strip() for field in manifest['fileSchema'].split(',')]
    if len(fields) != len(set(fields)) or not {'Bucket', 'Key', 'Size'}.issubset(fields):
        raise PlanningError('Inventory schema must have unique fields including Bucket, Key, Size')
    observed = datetime.fromtimestamp(int(manifest['creationTimestamp']) / 1000, timezone.utc)
    source_bucket = manifest['sourceBucket'].removeprefix('arn:aws:s3:::')
    files = []
    seen = set()
    for entry in manifest['files']:
        key = entry['key']
        parts = key.split('/')
        if '\\' in key or ':' in key or any(part in ('', '.', '..') for part in parts):
            raise PlanningError('Unsafe inventory shard path')
        local = root.joinpath(*parts).resolve()
        if not local.is_relative_to(root) or local in seen:
            raise PlanningError('Inventory shard is duplicated or escapes data root')
        seen.add(local)
        if local.stat().st_size != entry['size'] or file_md5(local) != entry['MD5checksum'].lower():
            raise PlanningError('Inventory shard size/checksum mismatch')
        files.append(local)

    def records():
        for local in files:
            opener = gzip.open if local.suffix == '.gz' else open
            with opener(local, 'rt', encoding='utf-8', newline='') as stream:
                for line_number, values in enumerate(csv.reader(stream), 1):
                    if len(values) != len(fields):
                        raise PlanningError(f'Inventory column count mismatch at row {line_number}')
                    row = dict(zip(fields, values))
                    if row.get('IsLatest', 'true').lower() != 'true' or row.get('IsDeleteMarker', 'false').lower() != 'false':
                        raise PlanningError('Historical versions/delete markers unsupported; supply current-object inventory')
                    if row['Bucket'] != source_bucket:
                        raise PlanningError('Inventory row bucket differs from manifest')
                    yield InventoryObject(
                        bucket=row['Bucket'], key=decode_key(row['Key']), size=int(row['Size']),
                        last_modified=row.get('LastModifiedDate') or None, etag=row.get('ETag') or None,
                        version_id=row.get('VersionId') or None, storage_class=row.get('StorageClass') or 'UNKNOWN',
                        encryption_status=row.get('EncryptionStatus') or None,
                        object_lock_mode=row.get('ObjectLockMode') or None,
                        retention_until=row.get('ObjectLockRetainUntilDate') or None,
                        legal_hold=row.get('ObjectLockLegalHoldStatus') or None,
                        checksum_algorithms=[row['ChecksumAlgorithm']] if row.get('ChecksumAlgorithm') else [],
                    )
    return {'kind': 's3-inventory-csv', 'observed_at': observed, 'source_bucket': source_bucket,
            'artifact_digest': file_md5(path), 'atomic_snapshot': False}, records()


def s3_objects(client, bucket, prefix=''):
    """Client-injected ListObjectsV2 adapter; no SDK import or credential discovery.

    Explicit EncodingType=url expects encoded response keys and decodes exactly once.
    Each page is bounded to 1000. ListObjectsV2 does not provide version IDs or
    full checksum values. Repeated/cyclic continuation tokens fail the import.
    """
    token = None
    seen_tokens = set()
    while True:
        arguments = dict(Bucket=bucket, Prefix=prefix, MaxKeys=1000, EncodingType='url')
        if token is not None:
            arguments['ContinuationToken'] = token
        page = client.list_objects_v2(**arguments)
        if page.get('EncodingType') != 'url':
            raise PlanningError('S3 adapter requires URL-encoded response keys')
        contents = page.get('Contents', [])
        if len(contents) > 1000 or page.get('CommonPrefixes'):
            raise PlanningError('Unexpected S3 page shape; delimiter-based listings are unsupported')
        for item in contents:
            yield InventoryObject(
                bucket=bucket, key=decode_key(item['Key']), size=item['Size'],
                last_modified=item.get('LastModified'), etag=item.get('ETag'),
                storage_class=item.get('StorageClass', 'UNKNOWN'),
                checksum_algorithms=item.get('ChecksumAlgorithm', []),
            )
        if page.get('IsTruncated') is False:
            return
        if page.get('IsTruncated') is not True:
            raise PlanningError('S3 page is missing IsTruncated')
        token = page.get('NextContinuationToken')
        if not token or token in seen_tokens:
            raise PlanningError('S3 continuation token missing or repeated')
        seen_tokens.add(token)
        if len(seen_tokens) > 100000:
            raise PlanningError('S3 page limit exceeded; split source scope')



