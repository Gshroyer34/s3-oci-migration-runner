"""Fail-closed workload and resource bounds for a finite benchmark shard."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from migration_runner.worker.cloud import ObjectRef, key_ok


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)


class Fixture(Contract):
    key: str
    size: int = Field(ge=1, le=10_000_000_000)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    last_modified: str

    @model_validator(mode='after')
    def valid(self):
        key_ok(self.key)
        stamp = datetime.fromisoformat(self.last_modified.replace('Z', '+00:00'))
        if stamp.utcoffset() is None:
            raise ValueError('Fixture timestamps must include a timezone')
        return self


class Run(Contract):
    format: Literal['oci-worker-benchmark-v1'] = 'oci-worker-benchmark-v1'
    campaign: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{2,39}$')
    run_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{2,59}$')
    namespace: str = Field(pattern=r'^[a-zA-Z0-9]{3,64}$')
    region: Literal['us-ashburn-1'] = 'us-ashburn-1'
    source_bucket: str
    destination_bucket: str
    mode: Literal['verified', 'copy-only'] = 'verified'
    objects: list[Fixture] = Field(min_length=1, max_length=10000)
    worker_count: int = Field(default=1, ge=1, le=8)
    worker_index: int = Field(default=0, ge=0, le=7)
    object_concurrency: int = Field(default=1, ge=1, le=16)
    part_size_mib: Literal[16, 32, 64, 128] = 32
    part_concurrency: int = Field(default=2, ge=1, le=8)
    memory_limit_mib: int = Field(default=8192, ge=2048, le=131072)
    max_seconds: int = Field(default=900, ge=1, le=3600)
    max_dataset_bytes: int = Field(default=100_000_000_000, ge=1, le=1_000_000_000_000)
    expires_at: str
    native_arch: Literal['x86_64', 'aarch64']
    shape: str = Field(pattern=r'^VM\.(Standard\.(E5|E6|A1|A2)|Optimized3)\.Flex$')

    @property
    def source_prefix(self):
        return f'fixtures/{self.campaign}/'

    @property
    def destination_prefix(self):
        return f'runs/{self.campaign}/{self.run_id}/worker-{self.worker_index}/'

    @property
    def buffer_estimate_mib(self):
        # Conservative allowance beyond Rclone multipart buffers, per object process.
        return 512 + self.object_concurrency * (self.part_size_mib * self.part_concurrency + 128)

    @model_validator(mode='after')
    def bounds(self):
        if self.mode == 'verified' and self.object_concurrency > 8:
            raise ValueError('Verified benchmarks support at most eight concurrent objects')
        if (self.source_bucket != f'migration-benchmark-{self.campaign}-source'
                or self.destination_bucket != f'migration-benchmark-{self.campaign}-target'):
            raise ValueError('Dedicated campaign source and target buckets are required')
        if self.worker_index >= self.worker_count or len(self.objects) < self.worker_count:
            raise ValueError('Every worker must have a valid nonempty shard')
        if len({obj.key for obj in self.objects}) != len(self.objects):
            raise ValueError('Duplicate fixture keys')
        if any(not obj.key.startswith(self.source_prefix) for obj in self.objects):
            raise ValueError('Fixture is outside the campaign source prefix')
        if sum(obj.size for obj in self.objects) > self.max_dataset_bytes:
            raise ValueError('Dataset exceeds campaign byte ceiling')
        if self.buffer_estimate_mib > self.memory_limit_mib * 0.60:
            raise ValueError('Multipart buffers exceed 60 percent of the process memory limit')
        arm = '.A1.' in self.shape or '.A2.' in self.shape
        if (self.native_arch == 'aarch64') != arm:
            raise ValueError('Shape and native architecture do not match')
        self.expiry()
        return self

    def expiry(self):
        stamp = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
        if stamp.utcoffset() is None:
            raise ValueError('Campaign expiration must include a timezone')
        return stamp.astimezone(timezone.utc)

    def reference(self, side, key):
        key_ok(key)
        if side not in ('source', 'destination'):
            raise ValueError('Unknown object side')
        prefix = self.source_prefix if side == 'source' else self.destination_prefix
        if not key.startswith(prefix):
            raise ValueError('Object is outside the campaign prefix')
        return ObjectRef(side, self.source_bucket if side == 'source' else self.destination_bucket, key)

    def shard(self):
        return self.objects[self.worker_index::self.worker_count]
