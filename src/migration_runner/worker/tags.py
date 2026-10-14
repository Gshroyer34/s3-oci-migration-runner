"""Reversible S3 tag representation for OCI metadata; no tag values in errors."""
import base64
import hashlib
import json
import re

from .rclone import WorkerError

TAG_METADATA_KEY = 'migration-s3-tags-v1'
# Product guardrail, not a claim about the provider's maximum. Leave space for
# Rclone's mtime/hash fields. Oversize records block rather than truncate.
METADATA_BUDGET = 4096
BACKEND_RESERVE = 512


def normalize_tags(tag_set):
    if not isinstance(tag_set, list) or len(tag_set) > 10:
        raise WorkerError('Invalid S3 tag set')
    tags = {}
    for item in tag_set:
        if not isinstance(item, dict) or set(item) != {'Key', 'Value'}:
            raise WorkerError('Invalid S3 tag entry')
        key, value = item['Key'], item['Value']
        if not isinstance(key, str) or not isinstance(value, str):
            raise WorkerError('Invalid S3 tag text')
        try:
            valid = (1 <= len(key.encode('utf-16-le')) // 2 <= 128
                     and len(value.encode('utf-16-le')) // 2 <= 256)
        except UnicodeError:
            valid = False
        if not valid or key in tags:
            raise WorkerError('Invalid or duplicate S3 tag key')
        tags[key] = value
    return dict(sorted(tags.items()))


def canonical_tags(tags):
    normalized = normalize_tags([{'Key': k, 'Value': v} for k, v in tags.items()])
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def encode_tags(tags):
    return base64.urlsafe_b64encode(canonical_tags(tags)).decode('ascii')


def decode_tags(value):
    """Application helper: pass the value of migration-s3-tags-v1 from HEAD."""
    try:
        if not isinstance(value, str) or len(value) > METADATA_BUDGET:
            raise ValueError()
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
        pairs = json.loads(raw.decode('utf-8'), object_pairs_hook=lambda entries: entries)
        if not isinstance(pairs, list):
            raise ValueError()
        tags = normalize_tags([{'Key': k, 'Value': v} for k, v in pairs])
        if encode_tags(tags) != value:
            raise ValueError()
        return tags
    except (ValueError, TypeError, UnicodeError, WorkerError):
        raise WorkerError('Invalid migrated tag metadata') from None


def tags_digest(tags):
    return hashlib.sha256(canonical_tags(tags)).hexdigest()


def validate_metadata(source_metadata, tags):
    if not isinstance(source_metadata, dict):
        raise WorkerError('Source metadata is unavailable')
    total = BACKEND_RESERVE
    names = set()
    for key, value in source_metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise WorkerError('Invalid source metadata')
        lowered = key.lower()
        if (lowered in names or lowered == TAG_METADATA_KEY
                or lowered.startswith('opc-meta-')):
            raise WorkerError('Source metadata collides with the migration namespace')
        names.add(lowered)
        # The pinned backend otherwise silently drops these values. Fail before
        # copying; encoding existing user metadata is a separate mapping change.
        if (not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", lowered)
                or not value or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise WorkerError('Source metadata requires an unsupported encoding')
        total += len('opc-meta-' + lowered) + len(value) + 4
    encoded = encode_tags(tags)
    total += len('opc-meta-' + TAG_METADATA_KEY) + len(encoded) + 4
    if total > METADATA_BUDGET:
        raise WorkerError('Combined metadata exceeds the migration metadata budget')
    return encoded
