from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlanningError(ValueError):
    """Input error with a safe, operator-facing message."""


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source_bucket: str = Field(min_length=1, max_length=255)
    source_region: str = Field(min_length=1, max_length=100)
    source_prefix: str = ''
    destination_namespace: str = Field(min_length=1, max_length=255)
    destination_bucket: str = Field(min_length=1, max_length=255)
    destination_region: str = Field(min_length=1, max_length=100)
    destination_prefix: str = ''
    version_scope: Literal['current'] = 'current'
    target_batch_bytes: int = Field(default=100_000_000_000, strict=True, ge=1, le=10**15)
    max_batch_objects: int = Field(default=10000, strict=True, ge=1, le=100000)
    open_batches: int = Field(default=16, strict=True, ge=1, le=64)

    @field_validator('source_prefix', 'destination_prefix')
    @classmethod
    def prefix(cls, value):
        if '\x00' in value or len(value.encode('utf-8')) > 1024:
            raise ValueError('Prefix must be valid UTF-8, contain no NUL, and fit 1024 bytes')
        return value

    @field_validator('source_bucket', 'source_region', 'destination_namespace', 'destination_bucket', 'destination_region')
    @classmethod
    def identifier(cls, value):
        if any(c.isspace() for c in value) or any(c in value for c in '/\\\x00'):
            raise ValueError('Invalid bucket, region, or namespace identifier')
        return value


class InventoryObject(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    key: str
    size: int = Field(strict=True, ge=0, le=2**63-1)
    bucket: str | None = None
    last_modified: datetime | None = None
    reference_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    etag: str | None = None
    version_id: str | None = None
    storage_class: str = 'UNKNOWN'
    encryption_status: str | None = None
    object_lock_mode: str | None = None
    legal_hold: str | None = None
    retention_until: str | None = None
    checksum_algorithms: list[str] = Field(default_factory=list)
    is_latest: Literal[True] = True
    is_delete_marker: Literal[False] = False

    @field_validator('key')
    @classmethod
    def valid_key(cls, value):
        if not value or '\x00' in value or len(value.encode('utf-8')) > 1024:
            raise ValueError('Key must contain 1-1024 UTF-8 bytes and no NUL')
        return value

    @field_validator('last_modified')
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError('Observation times must include a timezone')
        return value


def disposition(item: InventoryObject, mapping: Mapping):
    if item.bucket is not None and item.bucket != mapping.source_bucket:
        raise ValueError('Inventory bucket does not match mapping')
    if not item.key.startswith(mapping.source_prefix):
        return None, 'excluded', 'outside_source_prefix'
    target = mapping.destination_prefix + item.key[len(mapping.source_prefix):]
    if not target or len(target.encode('utf-8')) > 1024:
        return target, 'blocked', 'destination_key_length'
    # Exact-key execution must be implemented/tested in Step 3 before these can transfer.
    if any(c in item.key + target for c in '\r\n\\') or any(
        part in ('', '.', '..') for path in (item.key, target) for part in path.split('/')
    ) or item.key != item.key.strip() or target != target.strip():
        return target, 'blocked', 'requires_exact_key_adapter'
    if item.object_lock_mode or item.retention_until or item.legal_hold not in (None, 'OFF'):
        return target, 'blocked', 'object_lock_requires_review'
    if item.storage_class != 'STANDARD':
        return target, 'blocked', 'storage_class_requires_review'
    if item.encryption_status not in (None, 'NOT-SSE', 'SSE-S3'):
        return target, 'blocked', 'encryption_requires_review'
    return target, 'eligible', None


