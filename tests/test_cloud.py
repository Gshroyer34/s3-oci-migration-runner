from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from migration_runner.worker.cloud import CloudBinding, CloudRclone, ObjectRef, inspect_cloud_object, configure_cloud, private_profile
from migration_runner.worker.rclone import Rclone, WorkerError
from migration_runner.config import Settings


def binding(**kwargs):
    return CloudBinding(destination_prefix='pilot/run1/', immutable_source=True, exclusive_destination=True, **kwargs)


def record():
    return {'ordinal': 1, 'source_key': 'fixtures/object-001.bin', 'destination_key': 'pilot/run1/object-001.bin',
        'size': 4, 'source_identity': {'last_modified': '2026-09-11T00:00:00+00:00', 'reference_sha256': hashlib.sha256(b'abcd').hexdigest()}}


class MemoryCloud(CloudRclone):
    """Cloud contract double only. No remote authenticity or performance claims."""
    def __init__(self, b=None):
        Rclone.__init__(self)
        self.binding = b or binding()
        self.payload = {'source': b'abcd'}
        self.stamp = '2026-09-11T00:00:00+00:00'
        self.copies = 0
        self.reads = []
        self.tags = {}
        self.target_metadata = {}

    def source_tags(self, ref):
        self.validate_read(ref)
        return dict(self.tags)

    def object_properties(self, ref):
        from migration_runner.worker.properties import REVISION
        self.validate_read(ref)
        if ref.side == 'source':
            return {'revision': REVISION, 'head': {'Metadata': {}, 'ContentLength': len(self.payload['source']),
                'ETag': 'source-etag', 'LastModified': self.stamp, 'ServerSideEncryption': 'AES256'}, 'acl': {'status': 'not_captured'}}
        return {'content-length': str(len(self.payload['destination']))}

    def object_metadata(self, ref):
        self.validate_read(ref)
        return {} if ref.side == 'source' else dict(self.target_metadata)

    def copy_with_tags(self, src, dst, tags):
        from migration_runner.worker.tags import TAG_METADATA_KEY, encode_tags
        result = self.copy(src, dst)
        self.target_metadata = {TAG_METADATA_KEY: encode_tags(tags)}
        return result

    def probe(self, root=None):
        return {'backend': 'memory-contract-double'}

    def stat(self, ref):
        self.validate_read(ref)
        return {'size': len(self.payload[ref.side]), 'last_modified': self.stamp} if ref.side in self.payload else None

    def digest(self, ref):
        self.check()
        self.reads.append(ref.side)
        payload = self.payload[ref.side]
        return len(payload), hashlib.sha256(payload).hexdigest()

    def copy(self, src, dst):
        self.check()
        self.validate_copy(src, dst)
        self.copies += 1
        self.payload['destination'] = self.payload['source']
        return 0, [{'event': 'copy_process', 'try': 1, 'exit_code': 0}]


def test_full_cloud_contract_copy_repeat_and_corruption():
    a = MemoryCloud()
    result = inspect_cloud_object(a, None, None, record())
    assert result['content'] == 'verified'
    assert a.copies == 1 and a.reads.count('source') == 2
    assert inspect_cloud_object(a, None, None, record())['copy'] == 'skipped_content_match'
    a.payload['destination'] = b'xxxx'
    result = inspect_cloud_object(a, None, None, record(), verify_only=True)
    assert result['structural'] == 'match' and result['content'] == 'mismatch'
    assert a.payload['destination'] == b'xxxx' and a.copies == 1


def test_source_mutation_and_durable_baseline(monkeypatch):
    a = MemoryCloud()
    original = a.copy
    baseline = []
    def mutate(src, dst):
        result = original(src, dst)
        a.payload['source'] = b'zzzz'
        return result
    monkeypatch.setattr(a, 'copy', mutate)
    result = inspect_cloud_object(a, None, None, record(), before_copy=baseline.append)
    assert result['structural'] == 'source_changed' and result['content'] != 'verified'
    assert baseline[0]['source_sha256'] == hashlib.sha256(b'abcd').hexdigest()
    a.payload['source'] = b'yyyy'
    assert inspect_cloud_object(a, None, None, record(), baseline=baseline[0]['source_sha256'])['structural'] == 'source_changed'
    assert a.copies == 1


def test_failed_copy_inspects_ambiguous_destination(monkeypatch):
    a = MemoryCloud()
    original = a.copy
    def failed(src, dst):
        original(src, dst)
        return 5, [{'exit_code': 5}]
    monkeypatch.setattr(a, 'copy', failed)
    result = inspect_cloud_object(a, None, None, record())
    assert result['copy'] == 'process_failed' and result['content'] == 'verified'


def test_metadata_error_never_becomes_permission_to_overwrite(monkeypatch):
    a = MemoryCloud()
    original = a.stat
    def denied(ref):
        if ref.side == 'destination':
            raise WorkerError('fixture denied')
        return original(ref)
    monkeypatch.setattr(a, 'stat', denied)
    result = inspect_cloud_object(a, None, None, record())
    assert result['error'] == 'cloud_io_or_process_failure' and a.copies == 0


@pytest.mark.parametrize('field,value', [('source_bucket','customer-data'), ('source_prefix',''),
    ('destination_bucket','production'), ('source_region','us-west-2'), ('max_dataset_bytes',10_000_000_001),
    ('max_objects',101), ('immutable_source',False), ('exclusive_destination',False), ('kind','local')])
def test_binding_rejects_scope_expansion(field, value):
    data = binding().model_dump(); data[field] = value
    with pytest.raises((ValidationError, WorkerError)):
        CloudBinding.model_validate(data)


@pytest.mark.parametrize('key', ['fixtures/../secret', 'fixtures//x', 'fixtures/a b', 'fixtures/a:remote',
    'fixtures/a\\b', 'fixtures/a\n', 'outside/a', 'fixtures/é', 'fixtures/'])
def test_exact_keys_fail_closed(key):
    with pytest.raises(WorkerError):
        binding().reference('source', key)


def test_no_reverse_copy_or_mismatched_mapping():
    a = MemoryCloud()
    src = a.binding.reference('source', 'fixtures/a')
    dst = a.binding.reference('destination', 'pilot/run1/a')
    with pytest.raises(WorkerError): a.validate_copy(dst, src)
    with pytest.raises(WorkerError): a.validate_copy(src, a.binding.reference('destination', 'pilot/run1/b'))
    with pytest.raises(WorkerError): a.validate_read(ObjectRef('source','unreviewed','fixtures/a'))
    for command in ('sync','delete','purge','move','backend','rcat'):
        with pytest.raises(WorkerError): a.args(command, src)


def test_environment_excludes_ambient_keys_and_endpoints(monkeypatch):
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile', lambda _: '/run/migration/aws-config')
    for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','RCLONE_CONFIG_SOURCE_ENDPOINT','OCI_CONFIG_FILE','HTTPS_PROXY'):
        monkeypatch.setenv(key, 'SENSITIVE_TEST_MARKER')
    a = CloudRclone(binding(), 'ignored')
    assert 'SENSITIVE_TEST_MARKER' not in json.dumps(a.env)
    assert a.env['AWS_SHARED_CREDENTIALS_FILE'] == os.devnull
    assert a.env['RCLONE_CONFIG_DESTINATION_PROVIDER'] == 'instance_principal_auth'
    assert a.env['AWS_PROFILE'] == 'migration-reader'


def test_stat_absence_is_distinct_from_failure(monkeypatch):
    a = MemoryCloud()
    ref = a.binding.reference('destination','pilot/run1/a')
    monkeypatch.setattr(a, 'metadata', lambda args: {'IsDir': True})
    assert CloudRclone.stat(a, ref) is None
    monkeypatch.setattr(a, 'metadata', lambda args: {'IsDir': False, 'Size': '4'})
    with pytest.raises(WorkerError): CloudRclone.stat(a, ref)


def test_inventory_bounds_and_duplicate_rejection(monkeypatch):
    a = MemoryCloud(binding(max_objects=1))
    item = {'Path':'a', 'Size':4, 'ModTime':'2026-09-11T00:00:00Z','IsDir':False}
    monkeypatch.setattr(a, 'metadata', lambda *args, **kw: [item])
    assert a.inventory()[0].key == 'fixtures/a'
    monkeypatch.setattr(a, 'metadata', lambda *args, **kw: [item,item])
    with pytest.raises(WorkerError): a.inventory()
    a.binding = binding(max_dataset_bytes=3)
    monkeypatch.setattr(a, 'metadata', lambda *args, **kw: [item])
    with pytest.raises(WorkerError): a.inventory()


def test_cloud_db_requires_tls_and_http_stays_disabled(tmp_path):
    files = []
    for name in ('ca.crt','client.crt','client.key'):
        p=tmp_path/name; p.write_text('test fixture only'); files.append(str(p))
    data = dict(_env_file=None, environment='cloud-worker', db_user='migration_worker',
        worker_token='w'*40, db_password='', operator_enabled=False, operator_token='',
        db_sslrootcert=files[0], db_sslcert=files[1], db_sslkey=files[2])
    settings = Settings(**data)
    assert settings.database_url().query['sslmode'] == 'verify-full'
    assert settings.database_url().password is None
    from migration_runner.api import create_app
    with pytest.raises(ValueError): create_app(settings=settings)
    for changes in ({'db_sslkey':''}, {'db_user':'postgres'}, {'worker_token':''}, {'operator_enabled':True}, {'db_password':'bad'}):
        with pytest.raises(ValidationError): Settings(**(data|changes))


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux ownership and signing profile checks')
def test_reader_profile_permissions_and_static_key_rejection(tmp_path):
    p=tmp_path/'config'
    p.write_text('[profile migration-reader]\nregion=us-east-1\ncredential_process=/usr/local/bin/aws_signing_helper credential-process --fixture\n')
    p.chmod(0o600)
    assert private_profile(p) == str(p)
    p.chmod(0o644)
    with pytest.raises(WorkerError): private_profile(p)
    p.chmod(0o600); p.write_text('[profile migration-reader]\nregion=us-east-1\naws_access_key_id=bad\n')
    with pytest.raises(WorkerError): private_profile(p)


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('rclone'), reason='Pinned OL9 Rclone required')
def test_real_rclone_accepts_pinned_cloud_flags_without_network():
    a=MemoryCloud()
    # --help validates flags and exits before constructing or contacting either backend.
    args=a.args('copyto', a.binding.reference('source','fixtures/a'), a.binding.reference('destination','pilot/run1/a')) + ['--help']
    r=subprocess.run(args,capture_output=True,timeout=10,env=a.env)
    assert r.returncode == 0


def planned(engine, b):
    from migration_runner.planning.contracts import Mapping, InventoryObject
    from migration_runner.planning.service import plan
    fields = {k:getattr(b,k) for k in ('source_bucket','source_region','source_prefix','destination_namespace','destination_bucket','destination_region','destination_prefix')}
    return plan(engine,Mapping(**fields,target_batch_bytes=4),
        [InventoryObject(key='fixtures/object-001.bin',size=4,last_modified='2026-09-11T00:00:00Z',storage_class='STANDARD',reference_sha256=hashlib.sha256(b'abcd').hexdigest())],
        {'kind':'test','observed_at':datetime.now(timezone.utc)})


@pytest.mark.integration
def test_cloud_queue_full_evidence_limits_and_authentication(db_engine, monkeypatch):
    from migration_runner import security
    from migration_runner.worker.execution import work_once
    from migration_runner.worker import queue
    from migration_runner.ledger.models import ObjectEvidence
    from migration_runner.operations import RunLimits
    a=MemoryCloud(); job=planned(db_engine,a.binding)
    configure_cloud(db_engine,job,a.binding,RunLimits(max_scheduled_bytes=4,max_seconds=60,max_workers=2))
    with pytest.raises(WorkerError): work_once(db_engine,job,adapter=a)
    security.initialize(db_engine,'w'*40); monkeypatch.setenv('MIGRATION_WORKER_TOKEN','w'*40)
    result=work_once(db_engine,job,adapter=a,worker='cloud-test-1')
    assert result['state']=='verified' and result['verified_bytes']==4
    assert queue.status(db_engine,job)['queue_complete']
    with db_engine.connect() as c:
        evidence=c.execute(select(ObjectEvidence.evidence)).scalars().all()
    assert any(e.get('content')=='verified' for e in evidence)
    assert work_once(db_engine,job,adapter=MemoryCloud())['state']=='no_work'
    with pytest.raises(WorkerError):
        configure_cloud(db_engine,job,a.binding,RunLimits(max_scheduled_bytes=5,max_seconds=60,max_workers=2))


@pytest.mark.integration
def test_cloud_binding_rejects_mapping_and_size_mismatch(db_engine):
    from migration_runner.operations import RunLimits
    b=binding(); job=planned(db_engine,b)
    limits=RunLimits(max_scheduled_bytes=4,max_seconds=60,max_workers=1)
    data=b.model_dump(); data['destination_prefix']='pilot/wrong/'
    with pytest.raises(WorkerError): configure_cloud(db_engine,job,CloudBinding.model_validate(data),limits)
    with pytest.raises(WorkerError): configure_cloud(db_engine,job,binding(max_dataset_bytes=3),limits)


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('rclone'), reason='Pinned OL9 Rclone required')
def test_real_rclone_s3_stat_and_streaming_digest_stay_on_local_endpoint(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from email.utils import format_datetime
    calls=[]
    payload=b'local synthetic s3 payload'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, head=False):
            calls.append((self.command,self.path))
            assert self.path.split('?', 1)[0].endswith('/fixtures/object-001.bin')
            self.send_response(200)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Content-Type','application/octet-stream')
            self.send_header('ETag','"'+hashlib.md5(payload).hexdigest()+'"')
            self.send_header('Last-Modified','Fri, 11 Sep 2026 00:00:00 GMT')
            self.end_headers()
            if not head: self.wfile.write(payload)
        def do_HEAD(self): self.respond(True)
        def do_GET(self): self.respond()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=Thread(target=server.serve_forever,daemon=True); t.start()
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile',lambda _: '/nonexistent')
    a=CloudRclone(binding(),'ignored')
    # Test-only replacement: explicit loopback S3 endpoint and dummy signing keys.
    a.env.update({'RCLONE_CONFIG_SOURCE_ENDPOINT':f'http://127.0.0.1:{server.server_port}',
        'RCLONE_CONFIG_SOURCE_FORCE_PATH_STYLE':'true','RCLONE_CONFIG_SOURCE_ENV_AUTH':'false',
        'RCLONE_CONFIG_SOURCE_ACCESS_KEY_ID':'TESTONLY','RCLONE_CONFIG_SOURCE_SECRET_ACCESS_KEY':'TESTONLY'})
    a.env.pop('RCLONE_CONFIG_SOURCE_PROFILE')
    ref=a.binding.reference('source','fixtures/object-001.bin')
    try:
        assert a.probe()['rclone_version']=='1.75.1'
        assert a.stat(ref)=={'size':len(payload),'last_modified':'2026-09-11T00:00:00+00:00'}
        assert a.digest(ref)==(len(payload),hashlib.sha256(payload).hexdigest())
        assert calls and {c[0] for c in calls}=={'HEAD','GET'}
    finally:
        a.cancel(); server.shutdown(); server.server_close(); t.join()


def test_independent_reference_detects_preexisting_bad_source():
    a=MemoryCloud(); a.payload['source']=b'xxxx'
    result=inspect_cloud_object(a,None,None,record())
    assert result['structural']=='source_changed' and a.copies==0


def test_independent_manifest_requires_exact_key_set_and_sizes():
    from migration_runner.worker.cloud import attach_references
    from migration_runner.planning.contracts import InventoryObject
    records=[InventoryObject(key='fixtures/a',size=4)]
    manifest={'format':'migration-pilot-reference-v1','object_count':1,'total_bytes':4,
        'objects':[{'source_key':'fixtures/a','size':4,'sha256':hashlib.sha256(b'abcd').hexdigest()}]}
    assert attach_references(records,manifest)[0].reference_sha256==hashlib.sha256(b'abcd').hexdigest()
    with pytest.raises(WorkerError): attach_references([InventoryObject(key='fixtures/other',size=4)],manifest)
    with pytest.raises(WorkerError): attach_references([InventoryObject(key='fixtures/a',size=5)],manifest)
    with pytest.raises(WorkerError): attach_references(records,manifest|{'objects':manifest['objects']*2})


@pytest.mark.integration
def test_actual_deployment_worker_grants_support_queue_and_deny_admin(db_engine, monkeypatch):
    from sqlalchemy import text, create_engine
    from migration_runner import security
    from migration_runner.worker.execution import work_once
    from migration_runner.operations import RunLimits
    from psycopg import sql
    role='test_worker_'+uuid4().hex
    a=MemoryCloud(); job=planned(db_engine,a.binding)
    configure_cloud(db_engine,job,a.binding,RunLimits(max_scheduled_bytes=4,max_seconds=60,max_workers=1))
    security.initialize(db_engine,'w'*40); monkeypatch.setenv('MIGRATION_WORKER_TOKEN','w'*40)
    with db_engine.connect() as c:
        schema=c.execute(text('SELECT current_schema()')).scalar_one()
    script=(Path(__file__).parents[1]/'deploy/runtime/database-roles.sql').read_text()
    script='\n'.join(line for line in script.splitlines() if not line.lstrip().startswith('--'))
    # Execute the shipped worker grants against a uniquely named role/schema.
    statements=[]
    for statement in script.split(';'):
        statement=statement.strip()
        if 'migration_worker' not in statement: continue
        if statement.startswith('GRANT CONNECT') or statement.startswith('GRANT USAGE'):
            statement=statement.replace('migration_coordinator, ', '')
        statements.append(statement.replace('migration_worker',role).replace('SCHEMA public','SCHEMA '+schema))
    scoped=None
    try:
        with db_engine.begin() as c:
            for statement in statements: c.execute(text(statement))
        scoped=create_engine(Settings().database_url(),connect_args={'options':f'-c search_path={schema} -c role={role}'})
        result=work_once(scoped,job,adapter=a,worker='scoped-worker')
        assert result['state']=='verified'
        with scoped.connect() as c:
            assert not c.execute(text("SELECT has_table_privilege(current_user,'runtime_security','UPDATE')")).scalar_one()
            assert not c.execute(text("SELECT has_table_privilege(current_user,'inventory_objects','INSERT')")).scalar_one()
    finally:
        if scoped is not None: scoped.dispose()
        with db_engine.begin() as c:
            c.execute(text(f'DROP OWNED BY {role}'))
            c.execute(text(f'DROP ROLE {role}'))
