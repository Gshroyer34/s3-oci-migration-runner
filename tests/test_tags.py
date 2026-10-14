import base64
import json
import os
import sys

import pytest

from migration_runner.worker.tags import (
    TAG_METADATA_KEY, METADATA_BUDGET, decode_tags, encode_tags, normalize_tags,
    tags_digest, validate_metadata,
)
from migration_runner.worker.cloud import CloudRclone, inspect_cloud_object
from migration_runner.worker.rclone import WorkerError
from migration_runner.worker.s3_tags import read_tags
from test_cloud import MemoryCloud, record, binding


def test_reversible_case_unicode_empty_and_deterministic_encoding():
    tags = {'Owner': 'Finance', 'owner': '', '分類': '記録 😀', 'punctuation': 'a,b=c "\\\n'}
    assert decode_tags(encode_tags(tags)) == tags
    assert encode_tags(dict(reversed(list(tags.items())))) == encode_tags(tags)
    assert tags_digest(tags) == tags_digest(dict(reversed(list(tags.items()))))
    assert decode_tags(encode_tags({})) == {}
    assert encode_tags({})  # Empty set still has explicit, verifiable evidence.
    assert encode_tags(tags).isascii()


@pytest.mark.parametrize('value', [None, {}, [{'Key':'a','Value':'b'}]*2,
    [{'Key':'','Value':'x'}], [{'Key':'x','Value':4}], [{'Key':'x'}],
    [{'Key':'a'*129,'Value':''}], [{'Key':'x','Value':'😀'*129}],
    [{'Key':'\ud800','Value':'x'}], [{'Key':str(n),'Value':''} for n in range(11)]])
def test_invalid_tag_sets_fail_closed(value):
    with pytest.raises(WorkerError): normalize_tags(value)


@pytest.mark.parametrize('value', ['not base64!', '', 'A'*(METADATA_BUDGET+1),
    base64.urlsafe_b64encode(b'{"a":"x","a":"y"}').decode(),
    base64.urlsafe_b64encode(b'{"a":4}').decode(),
    base64.urlsafe_b64encode(b'[1]').decode()])
def test_decode_rejects_invalid_or_noncanonical_data(value):
    with pytest.raises(WorkerError): decode_tags(value)


@pytest.mark.parametrize('metadata', [{TAG_METADATA_KEY:'original'},
    {TAG_METADATA_KEY.upper():'original'}, {'opc-meta-other':'value'},
    {'Owner':'a','owner':'b'}, {'empty':''}, {'bad name':'x'}, {'x':'\r\nsecret'},
    {'x':'café'}, {'x':'a'*4096}])
def test_collisions_unrepresentable_metadata_and_overflow_block(metadata):
    with pytest.raises(WorkerError): validate_metadata(metadata, {'label':'safe'})


def test_large_tag_set_is_not_truncated():
    tags = {str(i):'x'*256 for i in range(10)}
    assert decode_tags(validate_metadata({},tags)) == tags
    with pytest.raises(WorkerError):
        validate_metadata({'existing':'x'*800},tags)


def test_sdk_request_scoped_owner_and_denied_is_not_empty():
    import boto3
    from botocore.stub import Stubber
    client=boto3.client('s3',region_name='us-east-1',aws_access_key_id='TEST',aws_secret_access_key='TEST')
    args={'Bucket':'test-bucket','Key':'prefix/object','ExpectedBucketOwner':'111111111111'}
    with Stubber(client) as stub:
        stub.add_response('get_object_tagging',{'TagSet':[{'Key':'Owner','Value':''}]},args)
        assert read_tags(client,'test-bucket','prefix/object','111111111111') == {'Owner':''}
        stub.add_response('get_object_tagging',{'TagSet':[]},args)
        assert read_tags(client,'test-bucket','prefix/object','111111111111') == {}
        stub.add_client_error('get_object_tagging',service_error_code='AccessDenied',expected_params=args)
        with pytest.raises(Exception): read_tags(client,'test-bucket','prefix/object','111111111111')


def test_source_tag_command_has_fixed_scope_and_no_ambient_credentials(monkeypatch):
    monkeypatch.setattr('migration_runner.worker.cloud.private_profile',lambda _: '/run/migration/aws-config')
    monkeypatch.setenv('AWS_ENDPOINT_URL','https://untrusted.invalid')
    a=CloudRclone(binding(),'ignored')
    seen=[]
    monkeypatch.setattr(a,'metadata',lambda args: seen.append(args) or {'Owner':'Finance'})
    src=a.binding.reference('source','fixtures/a')
    assert a.source_tags(src)=={'Owner':'Finance'}
    assert seen[0]==[sys.executable,'-m','migration_runner.worker.s3_tags','us-east-1',src.bucket,src.key,'357633903375']
    assert 'AWS_ENDPOINT_URL' not in a.env
    with pytest.raises(WorkerError): a.source_tags(a.binding.reference('destination','pilot/run1/a'))


def test_copy_injects_private_environment_and_cleans_on_error(monkeypatch):
    a=MemoryCloud(); tags={'Owner':'SENSITIVE_TAG_FIXTURE'}
    src=a.binding.reference('source','fixtures/a'); dst=a.binding.reference('destination','pilot/run1/a')
    def copy(*args):
        assert a.env['RCLONE_METADATA']=='true'
        assert a.env['RCLONE_METADATA_SET']==TAG_METADATA_KEY+'='+encode_tags(tags)
        assert 'SENSITIVE_TAG_FIXTURE' not in str(a.args('copyto',src,dst))
        raise WorkerError('synthetic')
    monkeypatch.setattr(a,'copy',copy)
    with pytest.raises(WorkerError): CloudRclone.copy_with_tags(a,src,dst,tags)
    assert 'RCLONE_METADATA_SET' not in a.env and 'RCLONE_METADATA' not in a.env


def test_tag_read_failure_prevents_copy_and_redacts_values(monkeypatch):
    a=MemoryCloud()
    def deny(ref): raise WorkerError('SENSITIVE_TAG_FIXTURE')
    monkeypatch.setattr(a,'source_tags',deny)
    result=inspect_cloud_object(a,None,None,record())
    assert result['error']=='cloud_io_or_process_failure' and a.copies==0
    assert 'SENSITIVE_TAG_FIXTURE' not in json.dumps(result)


def test_missing_and_wrong_destination_tags_repair_even_if_bytes_match():
    a=MemoryCloud(); a.tags={'Owner':'Finance'}; a.payload['destination']=a.payload['source']
    first=inspect_cloud_object(a,None,None,record(),verify_only=True)
    assert first['content']=='verified' and first['tags']=='mismatch' and a.copies==0
    result=inspect_cloud_object(a,None,None,record())
    assert result['tags']=='verified' and a.copies==1
    assert decode_tags(a.target_metadata[TAG_METADATA_KEY])==a.tags
    assert inspect_cloud_object(a,None,None,record())['copy']=='skipped_content_match'
    a.target_metadata[TAG_METADATA_KEY]=encode_tags({'Owner':'Wrong'})
    assert inspect_cloud_object(a,None,None,record())['tags']=='verified' and a.copies==2


def test_tag_only_source_mutation_is_not_verified(monkeypatch):
    a=MemoryCloud(); a.tags={'Owner':'Before'}
    copy=a.copy
    def mutate(src,dst):
        result=copy(src,dst); a.tags={'Owner':'After'}; return result
    monkeypatch.setattr(a,'copy',mutate)
    evidence=inspect_cloud_object(a,None,None,record())
    assert evidence['structural']=='source_changed' and evidence['tags']=='source_changed'
    assert evidence['source_tags']=={'Owner':'Before'}
    previous_copies=a.copies
    evidence=inspect_cloud_object(a,None,None,record(),tag_baseline=tags_digest({'Owner':'Before'}))
    assert evidence['structural']=='source_changed' and a.copies==previous_copies


def test_dropped_destination_metadata_never_verifies_tags(monkeypatch):
    a=MemoryCloud()
    monkeypatch.setattr(a,'copy_with_tags',lambda src,dst,tags:a.copy(src,dst))
    result=inspect_cloud_object(a,None,None,record())
    assert result['content']=='verified' and result['tags']=='mismatch'


@pytest.mark.integration
def test_database_completion_requires_tags_and_retry_keeps_baseline(db_engine,monkeypatch):
    from migration_runner import security
    from migration_runner.worker.execution import work_once
    from migration_runner.worker.cloud import configure_cloud
    from migration_runner.operations import RunLimits
    from test_cloud import planned
    a=MemoryCloud(); a.tags={'Owner':'Finance'}; job=planned(db_engine,a.binding)
    configure_cloud(db_engine,job,a.binding,RunLimits(max_scheduled_bytes=30,max_seconds=60,max_workers=1))
    security.initialize(db_engine,'w'*40); monkeypatch.setenv('MIGRATION_WORKER_TOKEN','w'*40)
    copy=a.copy_with_tags
    monkeypatch.setattr(a,'copy_with_tags',lambda src,dst,tags:a.copy(src,dst))
    result=work_once(db_engine,job,adapter=a)
    assert result['state']=='failed' and result['verified_objects']==0
    monkeypatch.setattr(a,'copy_with_tags',copy)
    a.tags={'Owner':'Changed'}
    result=work_once(db_engine,job,adapter=a,manual=True)
    assert result['state']=='source_changed' and result['verified_objects']==0


@pytest.mark.integration
@pytest.mark.parametrize('tag_state',[None,'mismatch','verified'])
def test_ledger_independently_enforces_tag_coverage(db_engine,monkeypatch,tag_state):
    from migration_runner import security
    from migration_runner.worker import queue
    from migration_runner.worker.cloud import configure_cloud
    from migration_runner.operations import RunLimits
    from test_cloud import planned
    a=MemoryCloud(); job=planned(db_engine,a.binding)
    configure_cloud(db_engine,job,a.binding,RunLimits(max_scheduled_bytes=4,max_seconds=60,max_workers=1))
    security.initialize(db_engine,'w'*40); monkeypatch.setenv('MIGRATION_WORKER_TOKEN','w'*40)
    lease=queue.claim(db_engine,job,worker='tag-ledger-test')
    evidence={'structural':'match','content':'verified','method':'full-read-sha256','copy':'process_succeeded'}
    from migration_runner.worker.properties import REVISION
    evidence.update(properties='verified', property_mapping=REVISION)
    if tag_state is not None: evidence['tags']=tag_state
    queue.record(db_engine,lease,1,evidence)
    result=queue.finish(db_engine,lease,{'state':'verified'})
    assert (result['state']=='verified') == (tag_state=='verified')
    assert result['verified_objects'] == int(tag_state=='verified')
