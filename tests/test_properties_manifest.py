import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from sqlalchemy import text

from migration_runner.worker import properties, queue
from migration_runner.worker.cloud import inspect_cloud_object, configure_cloud
from migration_runner.worker.cloud_properties import read_source, read_destination
from migration_runner.worker.rclone import WorkerError
from migration_runner.manifest import write_archive, export
from migration_runner.operations import RunLimits
from test_cloud import MemoryCloud, record, planned


def snapshot():
    return {'revision': properties.REVISION, 'head': {'ContentLength': 4, 'Metadata': {'team': 'Finance'},
        'ContentType': 'application/octet-stream', 'CacheControl': 'private, max-age=60',
        'ETag': 'original-etag', 'LastModified': '2026-09-11T00:00:00+00:00', 'ServerSideEncryption': 'AES256'},
        'acl': {'status': 'captured', 'Owner': {'ID': 'owner-id'}, 'Grants': []}}


def test_property_mapping_native_values_and_provenance_are_separate():
    value = snapshot()
    properties.validate_source(value, {'Owner': 'A'})
    target = {'Content-Type': 'application/octet-stream', 'content-length': '4',
              'cache-control': 'private, max-age=60', 'opc-meta-team': 'Finance', 'etag': 'oci-etag'}
    assert properties.compare_destination(value, target)['status'] == 'verified'
    target['opc-meta-team'] = 'wrong'
    assert properties.compare_destination(value, target)['mismatches'] == ['Metadata.team']
    assert properties.digest(value) == properties.digest(copy.deepcopy(value))
    value['acl']['Owner']['ID'] = 'another-owner'
    assert properties.digest(value) != properties.digest(snapshot())


@pytest.mark.parametrize('name', ['content-type', 'mtime', 'btime', 'tier', 'md5chksum', 'expires', 'migration-s3-tags-v1', 'opc-meta-team'])
def test_reserved_source_user_metadata_blocks_before_copy(name):
    value = snapshot(); value['head']['Metadata'] = {name: 'value'}
    with pytest.raises(WorkerError): properties.validate_source(value, {})


@pytest.mark.parametrize('field,value', [('MissingMeta', 1), ('ObjectLockMode', 'COMPLIANCE'),
                                        ('ObjectLockLegalHoldStatus', 'ON'), ('CacheControl', 'bad\r\nheader'),
                                        ('StorageClass', 'GLACIER_IR'), ('ServerSideEncryption', 'aws:kms')])
def test_unsupported_properties_fail_closed(field, value):
    source = snapshot(); source['head'][field] = value
    with pytest.raises(WorkerError): properties.validate_source(source, {})


def test_sdk_reads_use_expected_owner_and_capture_permission_gap():
    boto3 = pytest.importorskip('boto3')
    from botocore.stub import Stubber
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='TEST', aws_secret_access_key='TEST')
    args = {'Bucket': 'test-bucket', 'Key': 'prefix/object', 'ExpectedBucketOwner': '111111111111'}
    head = {'ContentLength': 4, 'Metadata': {}, 'LastModified': datetime(2026, 9, 11, tzinfo=timezone.utc),
            'ETag': 'etag', 'VersionId': 'version-one'}
    with Stubber(client) as stub:
        stub.add_response('head_object', head, args)
        stub.add_response('get_object_acl', {'Owner': {'ID': 'owner'}, 'Grants': []}, args | {'VersionId': 'version-one'})
        result = read_source(client, 'test-bucket', 'prefix/object', '111111111111')
        assert result['acl']['Owner']['ID'] == 'owner'
        assert result['head']['LastModified'].endswith('+00:00')
        stub.add_response('head_object', head, args)
        stub.add_client_error('get_object_acl', service_error_code='AccessDenied', expected_params=args | {'VersionId': 'version-one'})
        assert read_source(client, 'test-bucket', 'prefix/object', '111111111111')['acl']['status'] == 'not_captured'
        stub.add_response('head_object', head, args)
        stub.add_client_error('get_object_acl', service_error_code='SlowDown', expected_params=args | {'VersionId': 'version-one'})
        with pytest.raises(Exception): read_source(client, 'test-bucket', 'prefix/object', '111111111111')


def test_destination_helper_only_exports_object_headers():
    client = SimpleNamespace(head_object=lambda *a: SimpleNamespace(headers={
        'Content-Type': 'text/plain', 'opc-meta-owner': 'data', 'opc-request-id': 'do-not-export', 'Set-Cookie': 'no'}))
    assert read_destination(client, 'namespace', 'bucket', 'key') == {'content-type': 'text/plain', 'opc-meta-owner': 'data'}


def test_property_evidence_copy_recheck_retry_baseline_and_mismatch(monkeypatch):
    adapter = MemoryCloud()
    original = adapter.object_properties
    durable = []
    result = inspect_cloud_object(adapter, None, None, record(), property_mapping=properties.REVISION, before_copy=durable.append)
    assert result['properties'] == 'verified'
    assert durable[0]['source_properties_sha256'] == result['source_properties_sha256']
    assert result['property_review'] == ['bucket_lifecycle_not_captured', 'application_tag_api_and_policy_review', 'owner_acl_not_captured']
    def wrong_target(ref):
        value = original(ref)
        if ref.side == 'destination': value['cache-control'] = 'unexpected'
        return value
    monkeypatch.setattr(adapter, 'object_properties', wrong_target)
    result = inspect_cloud_object(adapter, None, None, record(), property_mapping=properties.REVISION, verify_only=True)
    assert result['properties'] == 'mismatch' and adapter.copies == 1
    result = inspect_cloud_object(adapter, None, None, record(), property_mapping=properties.REVISION, property_baseline='changed')
    assert result['structural'] == 'source_changed' and adapter.copies == 1


def test_property_changes_during_copy_fail_even_with_identical_payload(monkeypatch):
    adapter = MemoryCloud()
    original = adapter.object_properties
    def changing(ref):
        value = original(ref)
        if ref.side == 'source' and adapter.copies: value['head']['ETag'] = 'changed'
        return value
    monkeypatch.setattr(adapter, 'object_properties', changing)
    result = inspect_cloud_object(adapter, None, None, record(), property_mapping=properties.REVISION)
    assert result['properties'] == result['structural'] == 'source_changed'
    assert result['content'] != 'verified'


def test_blocked_properties_keep_observations_without_payload_reads(monkeypatch):
    adapter = MemoryCloud(); adapter.tags = {'Owner': 'Team'}
    source = snapshot(); source['head']['StorageClass'] = 'DEEP_ARCHIVE'
    monkeypatch.setattr(adapter, 'object_properties', lambda ref: source)
    result = inspect_cloud_object(adapter, None, None, record(), property_mapping=properties.REVISION)
    assert result['source_tags'] == adapter.tags and result['source_properties'] == source
    assert 'Standard storage' in result['property_error']
    assert adapter.copies == 0 and adapter.reads == []


def test_property_helper_uses_only_bound_scope(monkeypatch):
    from migration_runner.worker.cloud import CloudRclone, ObjectRef
    from test_cloud import binding
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile', lambda _: '/private/config')
    adapter = CloudRclone(binding(), 'ignored')
    calls = []
    monkeypatch.setattr(adapter, 'metadata', lambda args: calls.append(args) or {})
    adapter.object_properties(adapter.binding.reference('source', 'fixtures/a'))
    assert calls[0][-5:] == ['source', 'us-east-1', adapter.binding.source_bucket, 'fixtures/a', '357633903375']
    adapter.object_properties(adapter.binding.reference('destination', 'pilot/run1/a'))
    assert calls[1][-5:] == ['destination', 'us-ashburn-1', adapter.binding.destination_bucket, 'pilot/run1/a', 'idswphn9j6by']
    with pytest.raises(WorkerError): adapter.object_properties(ObjectRef('source', 'other-bucket', 'fixtures/a'))
    assert len(calls) == 2


def row(n=1, disposition='eligible', evidence=None):
    return dict(ordinal=n, source_key='prefix/Unicode é\n"' + str(n), destination_key='out/' + str(n), size=4,
                source_identity={'last_modified': 'original-time'}, disposition=disposition, reason=None,
                batch_state='planned', current_attempt_id=None, checked_at=None, evidence=evidence)


def test_manifest_partitions_hashes_all_dispositions_and_historical_gaps():
    output = io.BytesIO()
    records = iter([row(1, 'eligible', {'content': 'verified', 'source_tags': {'Owner': ''}}),
                    row(2, 'blocked'), row(3, 'excluded')])
    result = write_archive(output, {'job_id': 'test'}, records, part_objects=2)
    assert result['dispositions'] == {'eligible': 1, 'blocked': 1, 'excluded': 1}
    assert result['property_verification'] == {'not_captured': 3}
    with zipfile.ZipFile(output) as archive:
        assert len(result['parts']) == 2
        for part in result['parts']:
            data = archive.read(part['path'])
            assert hashlib.sha256(data).hexdigest() == part['sha256']
            assert len(data.splitlines()) == part['objects']
        first = json.loads(archive.read(result['parts'][0]['path']).splitlines()[0])
        assert first['source_key'] == row()['source_key']
        assert first['source_tags'] == {'Owner': ''} and first['source_properties'] is None
        assert hashlib.sha256(archive.read('property-mapping.json')).hexdigest() == result['mapping_sha256']


def test_empty_and_oversized_exports_are_explicit():
    assert write_archive(io.BytesIO(), {}, iter([]))['objects'] == 0
    with pytest.raises(WorkerError): write_archive(io.BytesIO(), {}, iter([row()]), max_bytes=1)
    with pytest.raises(WorkerError): write_archive(io.BytesIO(), {}, iter([row(), row(2)]), max_objects=1)


@pytest.mark.integration
def test_ledger_export_current_attempt_scope_and_missing_property_gate(db_engine):
    adapter = MemoryCloud(); job = planned(db_engine, adapter.binding)
    configure_cloud(db_engine, job, adapter.binding, RunLimits(max_scheduled_bytes=8, max_seconds=60, max_workers=1))
    first = queue.claim(db_engine, job, worker='manifest-test')
    good = {'content': 'verified', 'structural': 'match', 'method': 'full-read-sha256',
            'copy': 'process_succeeded', 'tags': 'verified'}
    queue.record(db_engine, first, 1, good)
    assert queue.finish(db_engine, first, {'state': 'verified'})['state'] == 'failed'
    second = queue.claim(db_engine, job, worker='manifest-test', manual=True, batch=1)
    assert second is not None
    output = io.BytesIO(); index = export(db_engine, job, output)
    with zipfile.ZipFile(output) as archive:
        obj = json.loads(archive.read(index['parts'][0]['path']))
        assert obj['attempt_id'] == str(second.attempt)
        assert obj['verification']['content'] == 'not_captured'  # No stale first-attempt success.
    with pytest.raises(WorkerError): export(db_engine, job, io.BytesIO(), source_bucket='other-bucket')
    with pytest.raises(WorkerError): export(db_engine, uuid4(), io.BytesIO())
    queue.record(db_engine, second, 1, good | {'properties': 'verified', 'property_mapping': properties.REVISION})
    assert queue.finish(db_engine, second, {'state': 'verified'})['state'] == 'verified'


def test_authenticated_manifest_download_and_cleanup():
    from fastapi.testclient import TestClient
    from migration_runner.cloud_api import create_app
    from test_cloud_ui import FakeService, TOKEN, auth
    service = FakeService(); output = io.BytesIO()
    write_archive(output, {}, iter([row()])); output.seek(0)
    service.manifest = lambda job: output
    with TestClient(create_app(service, TOKEN)) as client:
        path = f'/api/jobs/{uuid4()}/manifest'
        assert client.get(path).status_code == 401
        assert not output.closed
        response = client.get(path, headers=auth())
        assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
        assert response.headers['cache-control'] == 'no-store'
        assert output.closed
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read('index.json'))['objects'] == 1
