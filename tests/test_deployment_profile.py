from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from migration_runner.installation.deployment import (
    AWSReaderIdentity, DeploymentProfile, DeploymentError, installed_profile, publish_profile,
)
from migration_runner.installation.package import build_runtime_package
from migration_runner.installation.profile import InstallationProfile
from migration_runner.operations import RunLimits
from migration_runner.worker.cloud import DeploymentBinding, CloudRclone, configure_cloud, resolve_binding, private_profile
from migration_runner.worker.rclone import WorkerError
from test_installation import example
from test_cloud import MemoryCloud, binding as lab_binding


def installation(account='111111111111', name='alpha-migration'):
    data = example(account, name)
    data['source']['region'] = 'us-west-2'
    data['destination']['region'] = 'us-phoenix-1'
    return InstallationProfile.model_validate(data)


def identity(account='111111111111'):
    return AWSReaderIdentity(role_arn=f'arn:aws:iam::{account}:role/migration/source-reader',
        trust_anchor_arn=f'arn:aws:rolesanywhere:us-west-2:{account}:trust-anchor/11111111-2222-3333-4444-555555555555',
        profile_arn=f'arn:aws:rolesanywhere:us-west-2:{account}:profile/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')


def profile(account='111111111111', name='alpha-migration'):
    return DeploymentProfile.from_installation(installation(account, name), identity(account))


def authority(monkeypatch, value):
    monkeypatch.setattr('migration_runner.installation.deployment.installed_profile', lambda: value)


def test_two_deployments_have_independent_revisions_and_no_lab_defaults():
    a, b = profile(), profile('222222222222', 'bravo-migration')
    assert a.revision != b.revision
    assert a.revision == hashlib.sha256(a.canonical()).hexdigest()
    assert a.revision == DeploymentProfile.model_validate_json(a.canonical()).revision
    for p in (a, b):
        binding = p.binding('incoming/run-1/')
        p.authorize(binding)
        assert binding.kind == 's3-oci-deployment-v1'
        assert binding.source_region == 'us-west-2' and binding.destination_region == 'us-phoenix-1'
        assert '357633903375' not in p.canonical().decode() and 'idswphn9j6by' not in str(binding)
    with pytest.raises(DeploymentError):
        a.authorize(b.binding('incoming/run-1/'))
    with pytest.raises(ValidationError):
        DeploymentBinding(destination_prefix='incoming/run-1/', deployment_revision=a.revision,
                          immutable_source=True, exclusive_destination=True)


@pytest.mark.parametrize(('section', 'field', 'value'), [
    ('source', 'account_id', '222222222222'),
    ('source', 'kms_key_arn', 'arn:aws:kms:us-west-2:111111111111:key/key'),
    ('aws_reader', 'role_arn', 'arn:aws:iam::222222222222:role/other'),
    ('aws_reader', 'profile_arn', 'arn:aws:rolesanywhere:us-east-1:111111111111:profile/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'),
    ('limits', 'max_workers', 5), ('limits', 'max_objects', 101),
    ('limits', 'max_object_bytes', 100_000_001), ('limits', 'max_seconds', 3601),
    ('limits', 'max_dataset_bytes', 10_000_000_001), ('limits', 'max_scheduled_bytes', 1),
    ('limits', 'max_workers', True), ('aws_reader', 'secret_key', 'SECRET_SENTINEL'),
])
def test_profile_rejects_unsupported_identity_or_expansion(section, field, value):
    data = profile().model_dump(); data[section][field] = value
    with pytest.raises(ValidationError):
        DeploymentProfile.model_validate(data)


def test_private_installation_cannot_silently_generate_public_runtime():
    data = installation().model_dump()
    data['network'].update(payload_route='private', s3_endpoint_id='vpce-12345678')
    with pytest.raises(ValidationError):
        DeploymentProfile.from_installation(InstallationProfile.model_validate(data), identity())


@pytest.mark.parametrize(('field', 'value'), [
    ('source_bucket', 'other-source'), ('source_region', 'us-east-1'), ('source_prefix', 'approved/sub/'),
    ('destination_bucket', 'other-target'), ('destination_namespace', 'other'),
    ('destination_region', 'us-ashburn-1'), ('deployment_revision', 'a' * 64),
    ('destination_prefix', 'incoming/'), ('destination_prefix', 'incoming-other/run/'),
])
def test_job_cannot_expand_or_rewrite_approved_scope(field, value):
    p = profile(); data = p.binding('incoming/run-1/').model_dump(); data[field] = value
    with pytest.raises(DeploymentError):
        p.authorize(DeploymentBinding.model_validate(data))


def test_execution_limits_are_separately_enforced():
    data = profile().model_dump(); data['limits']['max_workers'] = 1
    p = DeploymentProfile.model_validate(data)
    p.authorize(p.binding('incoming/run-1/'), RunLimits(max_workers=1, max_seconds=3600, max_scheduled_bytes=10_000_000_000))
    for changes in ({'max_workers': 2}, {'max_seconds': 3601}, {'max_scheduled_bytes': 30_000_000_001}):
        limits = RunLimits(**({'max_workers': 1, 'max_seconds': 3600, 'max_scheduled_bytes': 10_000_000_000} | changes))
        with pytest.raises(DeploymentError): p.authorize(p.binding('incoming/run-1/'), limits)


def test_runtime_package_is_reproducible_allowlisted_and_binds_revision():
    i = installation(); raw = build_runtime_package(i, identity(), 'incoming/run-1/')
    assert raw == build_runtime_package(i, identity(), 'incoming/run-1/')
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        assert set(bundle.namelist()) == {'START_HERE.md', 'deployment-profile.json', 'binding.json', 'aws-config.template', 'deployment.env.example', 'manifest.json', 'PROPERTY_COMPATIBILITY.md', 'property-mapping.json'}
        for path, digest in manifest['files'].items(): assert hashlib.sha256(bundle.read(path)).hexdigest() == digest
        assert manifest['deployment_revision'] == hashlib.sha256(bundle.read('deployment-profile.json')).hexdigest()
        binding = json.loads(bundle.read('binding.json'))
        assert binding['deployment_revision'] == manifest['deployment_revision']
        assert not manifest['automatic_installation_supported']
        assert b'role/migration/source-reader' in bundle.read('aws-config.template')


def test_adapter_resolves_host_authority_and_uses_approved_regions(monkeypatch):
    p = profile(); authority(monkeypatch, p)
    observed = []
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile', lambda path, deployment: observed.append(deployment) or '/run/migration/aws-config')
    monkeypatch.setenv('RCLONE_CONFIG_SOURCE_ENDPOINT', 'SECRET_SENTINEL')
    a = CloudRclone(p.binding('incoming/run-1/'), 'unused')
    assert observed == [p]
    assert a.env['RCLONE_CONFIG_SOURCE_REGION'] == p.source.region
    assert a.env['RCLONE_CONFIG_DESTINATION_REGION'] == p.destination.region
    assert 'SECRET_SENTINEL' not in json.dumps(a.env)
    authority(monkeypatch, profile('222222222222', 'bravo-migration'))
    with pytest.raises(DeploymentError): CloudRclone(p.binding('incoming/run-1/'), 'unused')


def test_profile_scoped_process_cannot_use_legacy_binding(monkeypatch):
    monkeypatch.setenv('MIGRATION_DEPLOYMENT_REVISION', 'a' * 64)
    with pytest.raises(WorkerError): resolve_binding(lab_binding())


@pytest.mark.parametrize('value', [None, [], 'SECRET_SENTINEL'])
def test_malformed_binding_is_rejected_without_echoing_input(value):
    with pytest.raises(WorkerError) as caught:
        resolve_binding(value)
    assert 'SECRET_SENTINEL' not in str(caught.value)


def test_cli_package_does_not_overwrite_or_contact_cloud(tmp_path):
    source, reader, target = [tmp_path / name for name in ('installation.json', 'identity.json', 'runtime.zip')]
    source.write_text(installation().model_dump_json()); reader.write_text(identity().model_dump_json())
    command = [sys.executable, '-m', 'migration_runner.installation', 'runtime-package', '--profile', str(source),
               '--reader-identity', str(reader), '--destination-run-prefix', 'incoming/run-1/', '--output', str(target)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    original = target.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert target.read_bytes() == original
    reader.write_text(json.dumps(identity().model_dump() | {'secret': 'SECRET_SENTINEL'}))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and 'SECRET_SENTINEL' not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux root ownership contract')
def test_linux_publication_is_idempotent_and_loader_is_pinned(monkeypatch):
    if os.geteuid() != 0: pytest.skip('Disposable root container required')
    with tempfile.TemporaryDirectory(prefix='migration-profile-test-', dir='/run') as directory:
        p = profile(); target = publish_profile(p, directory)
        assert publish_profile(p, directory) == target
        command = [sys.executable, '-m', 'migration_runner.installation', 'install-profile',
                   '--profile', str(target), '--output', directory, '--expected-revision', p.revision]
        installed = subprocess.run(command, capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
        assert json.loads(installed.stdout)['services_activated'] is False
        bad = subprocess.run(command[:-1] + ['a' * 64], capture_output=True, text=True)
        assert bad.returncode == 2 and 'Revision mismatch' in bad.stderr
        monkeypatch.setenv('MIGRATION_DEPLOYMENT_PROFILE', str(target))
        monkeypatch.setenv('MIGRATION_DEPLOYMENT_REVISION', p.revision)
        assert installed_profile() == p
        monkeypatch.setenv('MIGRATION_DEPLOYMENT_REVISION', 'a' * 64)
        with pytest.raises(DeploymentError): installed_profile()
        monkeypatch.setenv('MIGRATION_DEPLOYMENT_REVISION', p.revision)
        target.chmod(0o666)
        with pytest.raises(DeploymentError): installed_profile()
        with pytest.raises(DeploymentError): publish_profile(p, directory)
        target.chmod(0o644); target.write_text('{}')
        with pytest.raises(DeploymentError): installed_profile()
        with pytest.raises(DeploymentError): publish_profile(p, directory)
        target.unlink(); target.symlink_to('/etc/passwd')
        with pytest.raises(DeploymentError): installed_profile()
        with pytest.raises(DeploymentError): publish_profile(p, directory)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux credential profile contract')
def test_reader_rejects_wrong_role_extra_flags_and_region(tmp_path):
    p = profile(); target = tmp_path / 'aws-config'; target.write_text(p.aws_config()); target.chmod(0o600)
    assert private_profile(target, p) == str(target)
    for content in (p.aws_config().replace('source-reader', 'unapproved'),
                    p.aws_config().replace('900', '3600'),
                    p.aws_config().replace('us-west-2', 'us-east-1'),
                    p.aws_config().rstrip() + ' --endpoint https://example.invalid\n'):
        target.write_text(content)
        with pytest.raises(WorkerError): private_profile(target, p)


def planned(engine, binding):
    from migration_runner.planning.contracts import Mapping, InventoryObject
    from migration_runner.planning.service import plan
    mapping = {k: getattr(binding, k) for k in ('source_bucket', 'source_region', 'source_prefix',
        'destination_namespace', 'destination_bucket', 'destination_region', 'destination_prefix')}
    return plan(engine, Mapping(**mapping, target_batch_bytes=4),
        [InventoryObject(key=binding.source_prefix + 'object.bin', size=4,
                         last_modified='2026-09-11T00:00:00Z', storage_class='STANDARD',
                         reference_sha256=hashlib.sha256(b'abcd').hexdigest())],
        {'kind': 'portable-profile-test', 'observed_at': datetime.now(timezone.utc),
         'deployment_revision': binding.deployment_revision})


@pytest.mark.integration
def test_job_pins_profile_and_worker_rejects_drift_before_claim(db_engine, monkeypatch):
    from migration_runner import security
    from migration_runner.ledger.models import Migration, Attempt
    from migration_runner.worker.execution import work_once
    p = profile(); authority(monkeypatch, p)
    b = p.binding('incoming/run-1/'); job = planned(db_engine, b)
    limits = RunLimits(max_workers=1, max_seconds=60, max_scheduled_bytes=4)
    result = configure_cloud(db_engine, job, b, limits)
    assert result['deployment_profile'] == p.model_dump()
    assert configure_cloud(db_engine, job, b, limits) == result
    security.initialize(db_engine, 'w' * 40); monkeypatch.setenv('MIGRATION_WORKER_TOKEN', 'w' * 40)
    a = MemoryCloud(b)
    other = profile('222222222222', 'bravo-migration'); authority(monkeypatch, other)
    with pytest.raises(DeploymentError): work_once(db_engine, job, adapter=a)
    with db_engine.connect() as c: assert c.execute(select(Attempt)).first() is None
    authority(monkeypatch, p)
    result = work_once(db_engine, job, adapter=a, worker='portable-worker-1')
    assert result['state'] == 'verified' and result['verified_bytes'] == 4
    with pytest.raises((WorkerError, DeploymentError)):
        configure_cloud(db_engine, job, b, RunLimits(max_workers=2, max_seconds=60, max_scheduled_bytes=4))


@pytest.mark.integration
def test_destination_overlap_is_scoped_to_bucket_and_namespace(db_engine, monkeypatch):
    from migration_runner.planning.contracts import PlanningError
    p = profile(); authority(monkeypatch, p)
    limits = RunLimits(max_workers=1, max_seconds=60, max_scheduled_bytes=4)
    first = p.binding('incoming/run-1/'); configure_cloud(db_engine, planned(db_engine, first), first, limits)
    overlap = p.binding('incoming/run-1/nested/')
    with pytest.raises((WorkerError, PlanningError), match='overlaps'):
        configure_cloud(db_engine, planned(db_engine, overlap), overlap, limits)
    other = profile('222222222222', 'bravo-migration'); authority(monkeypatch, other)
    different_bucket = other.binding('incoming/run-1/')
    configure_cloud(db_engine, planned(db_engine, different_bucket), different_bucket, limits)


@pytest.mark.integration
def test_control_scope_and_tampered_snapshot_fail_closed(db_engine, monkeypatch):
    from migration_runner.cloud_cli import require_job_scope
    from migration_runner.ledger.models import Migration, Attempt
    from migration_runner.worker.execution import work_once
    from sqlalchemy import update
    p = profile(); authority(monkeypatch, p)
    monkeypatch.setenv('MIGRATION_DEPLOYMENT_REVISION', p.revision)
    b = p.binding('incoming/controls/'); job = planned(db_engine, b)
    require_job_scope(db_engine, job)
    other = profile('222222222222', 'bravo-migration'); authority(monkeypatch, other)
    with pytest.raises(WorkerError): require_job_scope(db_engine, job)
    with pytest.raises((WorkerError, DeploymentError)):
        configure_cloud(db_engine, job, other.binding('incoming/controls/'), RunLimits(max_workers=1, max_seconds=60, max_scheduled_bytes=4))
    authority(monkeypatch, p)
    cfg = configure_cloud(db_engine, job, b, RunLimits(max_workers=1, max_seconds=60, max_scheduled_bytes=4))
    cfg['deployment_profile']['aws_reader']['role_arn'] = other.aws_reader.role_arn
    with db_engine.begin() as c: c.execute(update(Migration).where(Migration.id == job).values(execution=cfg))
    with pytest.raises(WorkerError): require_job_scope(db_engine, job)
    with pytest.raises(WorkerError): work_once(db_engine, job, adapter=MemoryCloud(b))
    with db_engine.connect() as c: assert c.execute(select(Attempt)).first() is None
