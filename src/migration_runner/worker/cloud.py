"""Bounded Ashburn pilot adapter. No credential discovery or destructive commands.

Only immutable synthetic fixtures with conservative ASCII keys are supported.
Cloud correctness/identity are not established by offline contract tests.
"""
from dataclasses import dataclass
from datetime import datetime
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .rclone import Rclone, WorkerError

SOURCE_BUCKET = 'migration-lab-357633903375-us-east-1-source'
DESTINATION_BUCKET = 'migration-lab-target'
NAMESPACE = 'idswphn9j6by'


def key_ok(key):
    if (not isinstance(key, str) or len(key.encode('utf-8')) > 1024
        or not re.fullmatch(r'[A-Za-z0-9_./-]+', key)
        or any(part in ('', '.', '..') for part in key.split('/'))):
        raise WorkerError('Pilot requires conservative exact ASCII object keys')
    return key


class CloudBinding(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: str = 's3-oci-pilot'
    source_bucket: str = SOURCE_BUCKET
    source_prefix: str = 'fixtures/'
    destination_bucket: str = DESTINATION_BUCKET
    destination_namespace: str = NAMESPACE
    destination_prefix: str
    source_region: str = 'us-east-1'
    destination_region: str = 'us-ashburn-1'
    immutable_source: bool = False
    exclusive_destination: bool = False
    max_dataset_bytes: int = Field(default=10_000_000_000, strict=True, ge=1, le=10_000_000_000)
    max_objects: int = Field(default=100, strict=True, ge=1, le=100)

    @model_validator(mode='after')
    def boundaries(self):
        if (self.kind != 's3-oci-pilot' or self.source_bucket != SOURCE_BUCKET
            or self.destination_bucket != DESTINATION_BUCKET or self.destination_namespace != NAMESPACE
            or self.source_region != 'us-east-1' or self.destination_region != 'us-ashburn-1'
            or self.source_prefix != 'fixtures/'):
            raise ValueError('Only the reviewed Ashburn pilot scope is supported')
        if not self.immutable_source or not self.exclusive_destination:
            raise ValueError('Source freeze and exclusive destination ownership are required')
        if not self.destination_prefix.startswith('pilot/') or not self.destination_prefix.endswith('/'):
            raise ValueError('Use a dedicated pilot/RUN/ destination prefix')
        key_ok(self.destination_prefix.rstrip('/'))
        return self

    def reference(self, side, key):
        key_ok(key)
        prefix = self.source_prefix if side == 'source' else self.destination_prefix
        if side not in ('source', 'destination') or not key.startswith(prefix):
            raise WorkerError('Object is outside the bound pilot prefix')
        bucket = self.source_bucket if side == 'source' else self.destination_bucket
        return ObjectRef(side, bucket, key)


class DeploymentBinding(CloudBinding):
    """Job snapshot only. Authority comes from the separately installed profile."""
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    kind: Literal['s3-oci-deployment-v1'] = 's3-oci-deployment-v1'
    deployment_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_bucket: str = Field(...)
    source_prefix: str = Field(...)
    source_region: str = Field(...)
    destination_bucket: str = Field(...)
    destination_namespace: str = Field(...)
    destination_region: str = Field(...)

    @model_validator(mode='after')
    def boundaries(self):
        from migration_runner.installation.profile import prefix
        if self.immutable_source is not True or self.exclusive_destination is not True:
            raise ValueError('Source freeze and exclusive destination ownership are required')
        prefix(self.source_prefix)
        prefix(self.destination_prefix)
        return self


def resolve_binding(value):
    """Return a validated binding and its host authority; never trust job-supplied scope alone."""
    from migration_runner.installation.deployment import installed_profile
    data = value.model_dump() if isinstance(value, BaseModel) else value
    if not isinstance(data, dict):
        raise WorkerError('Cloud binding must be an object')
    if data.get('kind') == 's3-oci-deployment-v1':
        binding = DeploymentBinding.model_validate(data)
        profile = installed_profile()
        profile.authorize(binding)
        return binding, profile
    if os.environ.get('MIGRATION_DEPLOYMENT_PROFILE') or os.environ.get('MIGRATION_DEPLOYMENT_REVISION'):
        raise WorkerError('A deployment-scoped process cannot execute an unscoped or legacy lab binding')
    return CloudBinding.model_validate(data), None


@dataclass(frozen=True)
class ObjectRef:
    side: str
    bucket: str
    key: str

    def __str__(self):
        return f'{self.side}:{self.bucket}/{self.key}'


def private_profile(path, deployment=None):
    """Accept only a provisioned reader credential_process profile, not static keys."""
    path = Path(path)
    if (sys.platform != 'linux' or not path.is_absolute() or not path.is_file()
        or any(p.is_symlink() for p in (path, *path.parents))
        or path.stat().st_mode & 0o077 or path.stat().st_uid not in (0, os.getuid())):
        raise WorkerError('Use a private Linux reader profile owned by the worker or root')
    if path.stat().st_size > 16384:
        raise WorkerError('Reader profile is too large')
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(path.read_text())
        if config.defaults() or config.sections() != ['profile migration-reader']:
            raise ValueError()
        profile = config['profile migration-reader']
        expected_region = deployment.source.region if deployment else 'us-east-1'
        if set(profile) != {'credential_process', 'region'} or profile['region'] != expected_region:
            raise ValueError()
        if not profile['credential_process'].startswith('/usr/local/bin/aws_signing_helper credential-process '):
            raise ValueError()
        if deployment and shlex.split(profile['credential_process']) != deployment.credential_command():
            raise ValueError()
    except (ValueError, configparser.Error):
        raise WorkerError('Reader profile must contain only the reviewed renewable signing helper') from None
    return str(path)


class CloudRclone(Rclone):
    def __init__(self, binding, aws_config_file, executable='rclone', limits=None):
        super().__init__(executable, limits)
        self.binding, deployment = resolve_binding(binding)
        self.source_account = deployment.source.account_id if deployment else '357633903375'
        profile = private_profile(aws_config_file, deployment) if deployment else private_profile(aws_config_file)
        self.env.update({
            'AWS_CONFIG_FILE': profile, 'AWS_SHARED_CREDENTIALS_FILE': os.devnull,
            'AWS_PROFILE': 'migration-reader', 'AWS_EC2_METADATA_DISABLED': 'true',
            'RCLONE_CONFIG_SOURCE_TYPE': 's3', 'RCLONE_CONFIG_SOURCE_PROVIDER': 'AWS',
            'RCLONE_CONFIG_SOURCE_ENV_AUTH': 'true', 'RCLONE_CONFIG_SOURCE_PROFILE': 'migration-reader',
            'RCLONE_CONFIG_SOURCE_REGION': self.binding.source_region, 'RCLONE_CONFIG_SOURCE_NO_CHECK_BUCKET': 'true',
            'RCLONE_CONFIG_SOURCE_ENCODING': 'None',
            'RCLONE_CONFIG_DESTINATION_TYPE': 'oracleobjectstorage',
            'RCLONE_CONFIG_DESTINATION_PROVIDER': 'instance_principal_auth',
            'RCLONE_CONFIG_DESTINATION_NAMESPACE': self.binding.destination_namespace,
            'RCLONE_CONFIG_DESTINATION_REGION': self.binding.destination_region,
            'RCLONE_CONFIG_DESTINATION_NO_CHECK_BUCKET': 'true',
            'RCLONE_CONFIG_DESTINATION_ENCODING': 'None',
        })

    def args(self, command, *paths):
        if command not in ('cat', 'copyto', 'lsjson'):
            raise WorkerError('Cloud adapter command is not allowed')
        return super().args(command, *paths) + ['--use-server-modtime',
            '--oos-chunk-size', '16Mi', '--oos-upload-concurrency', '2',
            '--oos-attempt-resume-upload=false', '--oos-leave-parts-on-error=false']

    def source_tags(self, ref):
        from .tags import normalize_tags
        self.validate_read(ref)
        if ref.side != 'source':
            raise WorkerError('Tag discovery requires a source object')
        value = self.metadata([sys.executable, '-m', 'migration_runner.worker.s3_tags',
            self.binding.source_region, ref.bucket, ref.key, self.source_account])
        if not isinstance(value, dict):
            raise WorkerError('Source tags are unavailable')
        return normalize_tags([{'Key': k, 'Value': v} for k, v in value.items()])

    def object_metadata(self, ref):
        self.validate_read(ref)
        value = self.metadata(self.args('lsjson', ref) + ['--stat', '--metadata'])
        if (not isinstance(value, dict) or value.get('IsDir') is not False
                or not isinstance(value.get('Metadata'), dict)):
            raise WorkerError('Object metadata is unavailable')
        return value['Metadata']

    def object_properties(self, ref):
        self.validate_read(ref)
        source = ref.side == 'source'
        value = self.metadata([sys.executable, '-m', 'migration_runner.worker.cloud_properties',
            ref.side, self.binding.source_region if source else self.binding.destination_region,
            ref.bucket, ref.key, self.source_account if source else self.binding.destination_namespace])
        if not isinstance(value, dict):
            raise WorkerError('Object properties are unavailable')
        return value

    def copy_with_tags(self, source, destination, tags):
        from .tags import TAG_METADATA_KEY, validate_metadata
        self.validate_copy(source, destination)
        encoded = validate_metadata(self.object_metadata(source), tags)
        # Environment keeps customer values off the command line. The envelope
        # contains no CSV delimiters. Adapter sharing is forbidden by work_once.
        self.env['RCLONE_METADATA'] = 'true'
        self.env['RCLONE_METADATA_SET'] = TAG_METADATA_KEY + '=' + encoded
        try:
            return self.copy(source, destination)
        finally:
            self.env.pop('RCLONE_METADATA', None)
            self.env.pop('RCLONE_METADATA_SET', None)

    def validate_read(self, ref):
        if not isinstance(ref, ObjectRef) or self.binding.reference(ref.side, ref.key) != ref:
            raise WorkerError('Cloud object reference differs from the binding')

    def validate_copy(self, source, destination):
        self.validate_read(source)
        self.validate_read(destination)
        if source.side != 'source' or destination.side != 'destination':
            raise WorkerError('Only AWS source to OCI destination copying is permitted')
        if destination.key != self.binding.destination_prefix + source.key[len(self.binding.source_prefix):]:
            raise WorkerError('Destination does not match the planned prefix mapping')

    def probe(self, root=None):
        try:
            result = subprocess.run([self.executable, 'version'], env=self.env, capture_output=True, text=True, timeout=10)
            if result.returncode or result.stdout.splitlines()[0] != 'rclone v' + self.version:
                raise ValueError()
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            raise WorkerError('Pinned cloud Rclone is unavailable') from None
        return {'backend': 's3-to-oracleobjectstorage', 'rclone_version': self.version,
                'authentication_verified': False, 'route_verified': False,
                'verification': 'full-read-sha256', 'multipart_resume': False}

    def digest(self, ref):
        # Explicit membership plus no-traverse avoids enumerating siblings for cat.
        import tempfile
        self.validate_read(ref)
        with tempfile.TemporaryDirectory(prefix='migration-read-') as directory:
            manifest = Path(directory) / 'object.txt'
            manifest.write_text(ref.key + '\n', encoding='ascii')
            manifest.chmod(0o600)
            args = self.args('cat', f'{ref.side}:{ref.bucket}') + [
                '--files-from-raw', str(manifest), '--no-traverse']
            result = self.stream_digest(args)
        # files-from can silently omit a missing object. Never verify missing as empty.
        if result[0] == 0 and self.stat(ref) is None:
            raise WorkerError('Empty full-content read has no corresponding object')
        return result

    def metadata(self, args, *, max_bytes=65536):
        """Bound metadata output, elapsed time, and subprocess lifetime like payload reads."""
        try:
            with self.spawn(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                timer = threading.Timer(min(self.limits.timeout_seconds, 120), self.kill, args=(process,))
                timer.start()
                try:
                    data = process.stdout.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise WorkerError('Cloud metadata exceeded the pilot limit')
                    code = process.wait()
                    self.check()
                    if code != 0:
                        raise WorkerError('Cloud metadata request failed')
                    return json.loads(data)
                finally:
                    timer.cancel()
                    self.kill(process)
                    self.release(process)
        except (OSError, ValueError):
            raise WorkerError('Cloud metadata request failed or returned invalid data') from None

    def stat(self, ref):
        self.validate_read(ref)
        data = self.metadata(self.args('lsjson', ref) + ['--stat'])
        # lsjson documents an empty directory for a missing key on bucket backends.
        # Request failures are never treated as absence.
        if not isinstance(data, dict):
            raise WorkerError('Invalid cloud object metadata')
        if data.get('IsDir') is True:
            return None
        if data.get('IsDir') is not False or type(data.get('Size')) is not int or data['Size'] < 0:
            raise WorkerError('Invalid cloud object metadata')
        try:
            stamp = datetime.fromisoformat(data['ModTime'].replace('Z', '+00:00'))
            if stamp.utcoffset() is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise WorkerError('Cloud object modification time is unavailable') from None
        return {'size': data['Size'], 'last_modified': stamp.isoformat()}

    def inventory(self):
        """Small fixture inventory only; bounded output, never a PB inventory collector."""
        from migration_runner.planning.contracts import InventoryObject
        root = f'source:{self.binding.source_bucket}/{self.binding.source_prefix}'
        data = self.metadata(self.args('lsjson', root) + ['--recursive', '--files-only'], max_bytes=262144)
        if not isinstance(data, list) or len(data) > self.binding.max_objects:
            raise WorkerError('Inventory exceeds the approved pilot object count')
        records = []
        total = 0
        seen = set()
        for item in data:
            if not isinstance(item, dict) or item.get('IsDir') is not False:
                raise WorkerError('Invalid pilot inventory')
            key = self.binding.source_prefix + item.get('Path', '')
            self.binding.reference('source', key)
            if key in seen or type(item.get('Size')) is not int or item['Size'] < 0:
                raise WorkerError('Invalid or duplicate pilot inventory object')
            seen.add(key)
            total += item['Size']
            if total > self.binding.max_dataset_bytes:
                raise WorkerError('Inventory exceeds the approved pilot byte count')
            # Only fixtures generated with Standard/SSE-S3 may use this collector.
            # Do not infer arbitrary customer encryption/retention from lsjson.
            records.append(InventoryObject(key=key, bucket=self.binding.source_bucket,
                size=item['Size'], last_modified=item['ModTime'], storage_class='STANDARD', encryption_status='SSE-S3'))
        if not records:
            raise WorkerError('Pilot inventory is empty')
        return records


def inspect_cloud_object(adapter, source, destination, row, baseline=None, verify_only=False, before_copy=None, tag_baseline=None,
                         property_mapping=None, property_baseline=None):
    from .tags import TAG_METADATA_KEY, tags_digest, validate_metadata
    from . import properties
    evidence = {'structural': 'unchecked', 'content': 'unverified', 'method': 'full-read-sha256',
        'copy': 'not_started', 'process_events': [], 'source_sha256': None, 'destination_sha256': None,
        'tags': 'unverified', 'tag_mapping': TAG_METADATA_KEY}
    if property_mapping:
        evidence.update(properties='unverified', property_mapping=property_mapping)
    try:
        if property_mapping and property_mapping != properties.REVISION:
            raise WorkerError('Unsupported property mapping revision')
        src = adapter.binding.reference('source', row['source_key'])
        dst = adapter.binding.reference('destination', row['destination_key'])
        adapter.validate_copy(src, dst)
        original = adapter.stat(src)
        expected = row['source_identity'].get('last_modified')
        if (not original or original['size'] != row['size'] or not expected
            or datetime.fromisoformat(original['last_modified']) != datetime.fromisoformat(expected.replace('Z', '+00:00'))
            or row['source_identity'].get('version_id')):
            evidence['structural'] = 'source_changed'
            return evidence
        tags = adapter.source_tags(src)
        evidence['source_tags'] = tags
        evidence['source_tags_sha256'] = tags_digest(tags)
        if property_mapping:
            snapshot = adapter.object_properties(src)
            evidence['source_properties'] = snapshot
            evidence['properties_observed_at'] = datetime.now().astimezone().isoformat()
            try:
                properties.validate_source(snapshot, tags)
            except WorkerError as error:
                evidence['property_error'] = str(error)
                raise
            head = snapshot['head']
            if (head['ContentLength'] != row['size'] or
                    datetime.fromisoformat(head['LastModified'].replace('Z', '+00:00')) != datetime.fromisoformat(original['last_modified'])):
                evidence['structural'] = evidence['properties'] = 'source_changed'
                return evidence
            evidence['source_properties_sha256'] = properties.digest(snapshot)
            evidence['property_review'] = properties.review_items(snapshot)
            if property_baseline and evidence['source_properties_sha256'] != property_baseline:
                evidence['structural'] = evidence['properties'] = 'source_changed'
                return evidence
        encoded_tags = validate_metadata(adapter.object_metadata(src), tags)
        if tag_baseline and evidence['source_tags_sha256'] != tag_baseline:
            evidence['structural'] = 'source_changed'
            evidence['tags'] = 'source_changed'
            return evidence
        source_digest = adapter.digest_phase(src, 'source_hash')
        evidence['source_sha256'] = source_digest[1]
        if (source_digest[0] != row['size'] or (baseline and source_digest[1] != baseline)
            or source_digest[1] != row['source_identity'].get('reference_sha256')):
            evidence['structural'] = 'source_changed'
            return evidence
        if before_copy:
            before_copy(evidence.copy())
        info = adapter.stat(dst)
        tag_match = bool(info and adapter.object_metadata(dst).get(TAG_METADATA_KEY) == encoded_tags)
        def property_match():
            if not property_mapping:
                return True
            if not info:
                return False
            observed = adapter.object_properties(dst)
            evidence['destination_properties'] = observed
            comparison = properties.compare_destination(snapshot, observed)
            evidence['property_comparison'] = comparison
            return comparison['status'] == 'verified'
        properties_match = property_match()
        target_digest = adapter.digest_phase(dst, 'destination_verify') if info and info['size'] == row['size'] else None
        if target_digest == source_digest and tag_match and properties_match:
            evidence['copy'] = 'skipped_content_match'
        elif not verify_only:
            code, events = adapter.copy_with_tags(src, dst, tags)
            evidence['process_events'] = events
            evidence['copy'] = 'process_succeeded' if code == 0 else 'process_failed'
            evidence['log_level_counts'] = getattr(adapter, 'last_log_counts', {})
            info = adapter.stat(dst)
            tag_match = bool(info and adapter.object_metadata(dst).get(TAG_METADATA_KEY) == encoded_tags)
            properties_match = property_match()
            target_digest = adapter.digest_phase(dst, 'destination_verify') if info and info['size'] == row['size'] else None
        evidence['structural'] = 'match' if target_digest and target_digest[0] == row['size'] else 'missing_or_size_mismatch'
        if target_digest:
            evidence['destination_sha256'] = target_digest[1]
        final_digest = adapter.digest_phase(src, 'source_recheck')
        if property_mapping and properties.digest(adapter.object_properties(src)) != evidence['source_properties_sha256']:
            evidence['structural'] = evidence['properties'] = 'source_changed'
        elif adapter.source_tags(src) != tags:
            evidence['structural'] = 'source_changed'
            evidence['tags'] = 'source_changed'
        elif adapter.stat(src) != original or final_digest != source_digest:
            evidence['structural'] = 'source_changed'
        elif evidence['structural'] == 'match':
            evidence['content'] = 'verified' if target_digest == source_digest else 'mismatch'
            evidence['tags'] = 'verified' if tag_match else 'mismatch'
            if property_mapping:
                evidence['properties'] = 'verified' if properties_match else 'mismatch'
        return evidence
    except (OSError, WorkerError, ValueError, KeyError):
        evidence['error'] = 'cloud_io_or_process_failure'
        return evidence


def configure_cloud(engine, job, binding, limits):
    """Coordinator-only binding/start, under the same admission lock as local work."""
    from dataclasses import asdict
    from datetime import timedelta
    from sqlalchemy import select, update, func, text
    from migration_runner.ledger.models import Migration, ObjectRecord
    from migration_runner.operations import RunLimits
    from . import queue
    binding, deployment = resolve_binding(binding)
    limits = RunLimits.model_validate(limits)
    if deployment:
        deployment.authorize(binding, limits)
    if limits.max_scheduled_bytes > 30_000_000_000:
        raise WorkerError('Pilot scheduled payload ceiling is 30 GB including retries')
    with engine.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(739015032)'))
        row = queue.job_lock(c, job, exclusive=True)
        if deployment and row['provenance'].get('deployment_revision') != deployment.revision:
            raise WorkerError('Inventory plan belongs to a different or missing deployment revision')
        if row['state'] != 'planned' or row['control'] == 'cancelled':
            raise WorkerError('Only a reviewed non-cancelled plan may be bound')
        fields = ('source_bucket', 'source_region', 'source_prefix', 'destination_namespace',
                  'destination_bucket', 'destination_region', 'destination_prefix')
        if any(row['config'].get(k) != getattr(binding, k) for k in fields):
            raise WorkerError('Cloud binding must match the complete inventory mapping')
        if row['execution']:
            if row['execution']['binding'] != binding.model_dump() or row['run_limits'] != limits.model_dump():
                raise WorkerError('Cloud binding and run limits are immutable; use a new reviewed run')
            if deployment and row['execution'].get('deployment_profile') != deployment.model_dump():
                raise WorkerError('Stored deployment snapshot differs from the installed profile')
            return row['execution']
        # A bounded pilot must not accidentally consume a customer-sized imported inventory.
        records = c.execute(select(ObjectRecord.__table__).where(ObjectRecord.migration_id == job).limit(binding.max_objects + 1)).mappings().all()
        if not records or len(records) > binding.max_objects or sum(r['size'] for r in records) > binding.max_dataset_bytes:
            raise WorkerError('Plan exceeds the pilot fixture limits')
        for r in records:
            if (r['disposition'] != 'eligible' or not r['source_identity'].get('last_modified')
                or r['source_identity'].get('version_id') or not r['source_identity'].get('reference_sha256')
                or not 1 <= r['size'] <= (deployment.limits.max_object_bytes if deployment else 100_000_000)):
                raise WorkerError('Pilot requires current eligible objects with upload timestamps')
            src = binding.reference('source', r['source_key'])
            dst = binding.reference('destination', r['destination_key'])
            if dst.key != binding.destination_prefix + src.key[len(binding.source_prefix):]:
                raise WorkerError('Plan key mapping differs from the pilot binding')
        others = c.execute(select(Migration.id, Migration.execution).where(Migration.execution.is_not(None))).all()
        for other, cfg in others:
            known = cfg['binding']
            same_target = all(known.get(k) == getattr(binding, k) for k in
                              ('destination_bucket', 'destination_namespace', 'destination_region'))
            if other != job and known.get('kind') in ('s3-oci-pilot', 's3-oci-deployment-v1') and same_target:
                prefix = known['destination_prefix']
                if prefix.startswith(binding.destination_prefix) or binding.destination_prefix.startswith(prefix):
                    raise WorkerError('Destination overlaps another cloud execution')
        from .properties import REVISION
        config = {'binding': binding.model_dump(), 'policy': asdict(queue.Policy()), 'property_mapping': REVISION}
        if deployment:
            config['deployment_profile'] = deployment.model_dump()
        now = c.execute(select(func.clock_timestamp())).scalar_one()
        c.execute(update(Migration).where(Migration.id == job).values(execution=config,
            run_limits=limits.model_dump(), run_started_at=now,
            run_deadline=now + timedelta(seconds=limits.max_seconds), control='active'))
        queue.event(c, job, 'cloud_bound', limits=limits.model_dump())
        return config


def attach_references(records, manifest):
    """Match a generator reference to every listed object before ledger import."""
    from migration_runner.planning.contracts import InventoryObject
    if not isinstance(manifest, dict) or manifest.get('format') != 'migration-pilot-reference-v1':
        raise WorkerError('Use the independent synthetic fixture reference manifest')
    entries = manifest.get('objects')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise WorkerError('Invalid pilot reference count')
    references = {}
    for entry in entries:
        if (not isinstance(entry, dict) or type(entry.get('size')) is not int
            or not 1 <= entry['size'] <= 100_000_000
            or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))):
            raise WorkerError('Invalid independent reference hash or size')
        key = key_ok(entry['source_key'])
        if key in references:
            raise WorkerError('Duplicate independent reference key')
        references[key] = entry
    if (manifest.get('object_count') != len(entries)
        or manifest.get('total_bytes') != sum(e['size'] for e in entries)
        or {r.key for r in records} != set(references) or len(records) != len(entries)):
        raise WorkerError('Cloud inventory differs from the independent fixture manifest')
    result = []
    for record in records:
        entry = references[record.key]
        if record.size != entry['size']:
            raise WorkerError('Cloud inventory size differs from the independent fixture manifest')
        result.append(InventoryObject.model_validate(record.model_dump() | {'reference_sha256': entry['sha256']}))
    return result
