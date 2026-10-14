"""Nonsecret, administrator-owned execution scope for a portable bounded pilot."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Literal

from pydantic import Field, model_validator

from .profile import InputModel, Source, Destination


class DeploymentError(ValueError):
    """Safe message; submitted values and credentials must not be echoed."""


class AWSReaderIdentity(InputModel):
    role_arn: str = Field(max_length=256)
    trust_anchor_arn: str = Field(max_length=256)
    profile_arn: str = Field(max_length=256)


class PilotLimits(InputModel):
    max_dataset_bytes: int = Field(default=10_000_000_000, ge=1, le=10_000_000_000)
    max_objects: int = Field(default=100, ge=1, le=100)
    max_object_bytes: int = Field(default=100_000_000, ge=1, le=100_000_000)
    max_scheduled_bytes: int = Field(default=30_000_000_000, ge=1, le=30_000_000_000)
    max_workers: int = Field(default=2, ge=1, le=4)
    max_seconds: int = Field(default=3600, ge=1, le=3600)

    @model_validator(mode='after')
    def bounds(self):
        if self.max_scheduled_bytes < self.max_dataset_bytes or self.max_object_bytes > self.max_dataset_bytes:
            raise ValueError('Object and scheduled-byte limits must contain the dataset envelope.')
        return self


class DeploymentProfile(InputModel):
    schema_version: Literal['migration-deployment/v1'] = 'migration-deployment/v1'
    installation_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    deployment_name: str = Field(pattern=r'^[a-z][a-z0-9-]{2,23}$')
    source: Source
    destination: Destination
    payload_route: Literal['internet'] = 'internet'
    aws_reader: AWSReaderIdentity
    limits: PilotLimits = Field(default_factory=PilotLimits)
    verification: Literal['full-read-sha256'] = 'full-read-sha256'

    @model_validator(mode='after')
    def scope(self):
        account, region = self.source.account_id, re.escape(self.source.region)
        if not re.fullmatch(rf'arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]{{1,128}}', self.aws_reader.role_arn):
            raise ValueError('Reader role must belong to the source account.')
        for field, resource in [('trust_anchor_arn', 'trust-anchor'), ('profile_arn', 'profile')]:
            if not re.fullmatch(rf'arn:aws:rolesanywhere:{region}:{account}:{resource}/[a-f0-9]{{8}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{12}}', getattr(self.aws_reader, field)):
                raise ValueError('Roles Anywhere resources must match the source account and region.')
        if self.source.kms_key_arn:
            raise ValueError('The portable pilot inventory supports SSE-S3 only; KMS migration admission is not implemented.')
        return self

    @property
    def revision(self):
        return hashlib.sha256(self.canonical()).hexdigest()

    def canonical(self):
        return (json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':')) + '\n').encode()

    @classmethod
    def from_installation(cls, installation, aws_reader):
        return cls(installation_revision=installation.revision, deployment_name=installation.deployment_name,
                   source=installation.source, destination=installation.destination,
                   payload_route=installation.network.payload_route, aws_reader=aws_reader,
                   limits=PilotLimits(max_workers=installation.compute.maximum_workers))

    def binding(self, destination_prefix):
        from migration_runner.worker.cloud import DeploymentBinding
        return DeploymentBinding(deployment_revision=self.revision,
            source_bucket=self.source.bucket, source_region=self.source.region, source_prefix=self.source.prefix,
            destination_bucket=self.destination.bucket, destination_region=self.destination.region,
            destination_namespace=self.destination.namespace, destination_prefix=destination_prefix,
            immutable_source=True, exclusive_destination=True,
            max_dataset_bytes=self.limits.max_dataset_bytes, max_objects=self.limits.max_objects)

    def authorize(self, binding, limits=None):
        expected = self.binding(binding.destination_prefix)
        fixed = ('kind', 'deployment_revision', 'source_bucket', 'source_region', 'source_prefix',
                 'destination_bucket', 'destination_region', 'destination_namespace')
        if any(getattr(binding, field, None) != getattr(expected, field) for field in fixed):
            raise DeploymentError('Job scope or revision differs from the installed deployment profile.')
        if (binding.destination_prefix == self.destination.prefix
                or not binding.destination_prefix.startswith(self.destination.prefix)):
            raise DeploymentError('Use a dedicated run prefix below the approved destination prefix.')
        if binding.max_dataset_bytes > self.limits.max_dataset_bytes or binding.max_objects > self.limits.max_objects:
            raise DeploymentError('Job inventory limits exceed the deployment limits.')
        if limits is not None and any(getattr(limits, name) > getattr(self.limits, name)
                                     for name in ('max_workers', 'max_seconds', 'max_scheduled_bytes')):
            raise DeploymentError('Job execution limits exceed the deployment limits.')

    def credential_command(self):
        return ['/usr/local/bin/aws_signing_helper', 'credential-process',
                '--certificate', '/run/migration/roles-anywhere.crt',
                '--private-key', '/run/migration/roles-anywhere.key',
                '--trust-anchor-arn', self.aws_reader.trust_anchor_arn,
                '--profile-arn', self.aws_reader.profile_arn, '--role-arn', self.aws_reader.role_arn,
                '--session-duration', '900']

    def aws_config(self):
        return ('[profile migration-reader]\nregion = ' + self.source.region +
                '\ncredential_process = ' + ' '.join(self.credential_command()) + '\n')


def read_profile(path):
    """Offline input validation; does not authorize execution."""
    with Path(path).open('rb') as stream:
        value = stream.read(32769)
    if len(value) > 32768:
        raise DeploymentError('Deployment profile exceeds the size limit.')
    return DeploymentProfile.model_validate_json(value)


def installed_profile():
    """A job cannot supply its own authority. Only root-installed, pinned Linux files qualify."""
    path = Path(os.environ.get('MIGRATION_DEPLOYMENT_PROFILE', ''))
    expected = os.environ.get('MIGRATION_DEPLOYMENT_REVISION', '')
    if sys.platform != 'linux' or not path.is_absolute() or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise DeploymentError('Portable execution requires a pinned deployment profile installed on Linux.')
    try:
        # Reject writable parents too: a protected file inside a replaceable directory is not trusted.
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise DeploymentError('Deployment profile and parent directories must be root-owned and not group/world writable.')
        if not path.is_file():
            raise DeploymentError('Deployment profile must be a regular file.')
        profile = read_profile(path)
        if profile.revision != expected:
            raise DeploymentError('Installed deployment profile revision does not match its pinned revision.')
        return profile
    except (OSError, ValueError):
        raise DeploymentError('Installed deployment profile is unavailable, unsafe or differs from its pinned revision.') from None


def publish_profile(profile, directory):
    """Idempotent root publication; no active pointer, credentials, services or database changes."""
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise DeploymentError('Profile installation requires a Linux administrator.')
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise DeploymentError('Choose an existing absolute root-owned deployment directory.')
    for item in (directory, *directory.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise DeploymentError('Deployment directories must be root-owned, nonsymlink and not group/world writable.')
    target = directory / (profile.revision + '.json')
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or target.read_bytes() != profile.canonical():
            raise DeploymentError('An existing revision file differs or has unsafe ownership/permissions.') from None
        return target
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(profile.canonical())
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        target.unlink()
        raise
    return target
