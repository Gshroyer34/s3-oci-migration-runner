import json
import subprocess
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from migration_runner.installation.discovery import (
    AWSDiscoveryClient, DiscoveryError, DiscoveryService, DiscoverySettings, size_band)
from migration_runner.installation.web import create_app


def settings(**updates):
    return DiscoverySettings(**({'account_id':'111111111111', 'region':'us-east-1',
                                 'bucket':'test-source', 'scope':'bucket', 'metadata_samples':20} | updates))


def obj(key, size=20_000, tier='STANDARD'):
    return {'Key': quote(key, safe=''), 'Size': size, 'StorageClass': tier,
            'ETag': '"etag"', 'LastModified': '2026-10-13T00:00:00Z'}


def page(rows=(), cursor=None):
    return {'Contents': list(rows), 'IsTruncated': cursor is not None, 'EncodingType': 'url',
            'NextContinuationToken': cursor}


class FakeAWS:
    profile='test-profile'
    def __init__(self, pages, heads=None):
        self.pages=pages; self.heads=heads or {}; self.calls=[]
    def call(self, cfg, operation, **args):
        self.calls.append((operation,args))
        if operation=='get-bucket-location':
            return {'LocationConstraint': None}
        if operation=='list-objects-v2':
            result=self.pages[args.get('continuation_token')]
        elif operation=='head-object':
            result=self.heads.get(args['key'], {'ContentLength':20_000,'ETag':'"etag"','ServerSideEncryption':'AES256'})
        else:
            raise AssertionError('Payload/restore/mutation operation attempted')
        if isinstance(result,Exception): raise result
        return result


def service(tmp_path, pages, heads=None):
    return DiscoveryService(tmp_path, FakeAWS(pages, heads))


def test_mixed_tiers_never_touch_archive_payload_or_metadata(tmp_path):
    s=service(tmp_path,{None:page([obj('standard'),obj('instant',tier='GLACIER_IR'),
        obj('deep',tier='DEEP_ARCHIVE'),obj('flexible',tier='GLACIER'),obj('unknown',tier='UNKNOWN')])})
    r=s.advance(s.create(settings())['id'])
    assert r['listing_complete'] and not r['migration_ready'] and not r['atomic_snapshot']
    assert r['totals']=={'objects':5,'bytes':100_000}
    groups={x['name']:x['objects'] for x in r['phases']}
    assert groups=={'standard_first':1,'later_instant_retrieval':1,'excluded_deep_archive':1,'review_other_tier':2}
    assert [args['key'] for op,args in s.client.calls if op=='head-object']==['standard']
    assert r['head_requests']==1


def test_checkpoint_survives_restart_and_deduplicates(tmp_path):
    s=service(tmp_path,{None:page([obj('one')],'next')})
    scan=s.create(settings(metadata_samples=0))['id']
    assert s.advance(scan)['state']=='partial'
    reopened=service(tmp_path,{'next':page([obj('one'),obj('two')])})
    r=reopened.advance(scan)
    assert r['listing_complete'] and r['totals']['objects']==2 and r['pages_completed']==2
    assert reopened.client.calls[0][1]['continuation_token']=='next'
    assert 'cursor' not in r and 'NextContinuationToken' not in json.dumps(r)
    reopened.advance(scan)
    assert len(reopened.client.calls)==1


def test_limit_is_partial_and_no_extra_requests(tmp_path):
    s=service(tmp_path,{None:page([obj('one')],'next')})
    scan=s.create(settings(object_limit=1,metadata_samples=0))['id']
    r=s.advance(scan)
    assert r['state']=='limit_reached' and not r['listing_complete']
    assert s.client.calls[1][1]['max_keys']==1
    s.advance(scan)
    assert len(s.client.calls)==2


def test_failure_preserves_checkpoint_and_retry(tmp_path):
    s=service(tmp_path,{None:page([obj('one')],'next'),'next':DiscoveryError('Access denied.')})
    scan=s.create(settings(metadata_samples=0))['id'];s.advance(scan)
    r=s.advance(scan)
    assert r['state']=='failed' and r['totals']['objects']==1 and r['pages_completed']==1
    s.client.pages['next']=page([obj('two')])
    assert s.advance(scan)['listing_complete']


@pytest.mark.parametrize('bad',[page([obj('outside/key')]),page([dict(obj('scope/x'),Size=-1)]),
    {'Contents':[], 'EncodingType':'url','IsTruncated':True},
    {'Contents':[], 'EncodingType':'url','IsTruncated':'false'},
    {'Contents':[], 'IsTruncated':False}])
def test_bad_listing_fails_without_partial_page_commit(tmp_path,bad):
    s=service(tmp_path,{None:bad})
    r=s.advance(s.create(settings(scope='prefix',prefix='scope/'))['id'])
    assert r['state']=='failed' and r['totals']['objects']==0 and r['pages_completed']==0


def test_cursor_cycle_cannot_scan_forever(tmp_path):
    s=service(tmp_path,{None:page([obj('1')],'a'),'a':page([obj('2')],'b'),'b':page([obj('3')],'a')})
    scan=s.create(settings(metadata_samples=0))['id'];s.advance(scan);s.advance(scan)
    r=s.advance(scan)
    assert r['state']=='failed' and r['totals']['objects']==2


def test_unusual_keys_and_sizes_are_observed_but_blocked(tmp_path):
    s=service(tmp_path,{None:page([obj('normal'),obj('literal%2Fname'),obj('snow/\u96ea'),
        obj('spaces and\nnewline'),obj('folder//bad'),obj('large',10_000_000_000)])})
    r=s.advance(s.create(settings(metadata_samples=0))['id'])
    assert r['totals']['objects']==6
    assert r['standard_phase_blockers']=={'unsupported_keys':4,'exceeds_current_object_limit':1,'metadata_attention':0}
    with s.db() as c:
        assert c.execute("SELECT count(*) FROM objects WHERE key='literal%2Fname'").fetchone()[0]==1


@pytest.mark.parametrize('head,status',[
    ({'ServerSideEncryption':'aws:kms'},'encryption_review'),
    ({'ObjectLockMode':'COMPLIANCE'},'retention_review'),
    ({'ContentLength':21},'source_changed'),
    ({'StorageClass':'DEEP_ARCHIVE'},'source_changed'),
    ({'VersionId':'v1'},'observed_sse_s3'),
])
def test_metadata_is_observed_not_assumed(tmp_path,head,status):
    base={'ContentLength':20_000,'ETag':'"etag"','ServerSideEncryption':'AES256'}
    s=service(tmp_path,{None:page([obj('one')])},{'one':base|head})
    r=s.advance(s.create(settings())['id'])
    assert r['metadata_observations'][0]['name']==status
    assert not r['migration_ready']
    assert s.client.calls[2][1]['if_match']=='"etag"'


def test_metadata_denial_does_not_invent_encryption(tmp_path):
    s=service(tmp_path,{None:page([obj('one')])},{'one':DiscoveryError('Access denied.')})
    r=s.advance(s.create(settings())['id'])
    assert r['metadata_observations'][0]['name']=='unavailable'
    assert r['standard_phase_blockers']['metadata_attention']==1


def test_metadata_request_budget_per_page(tmp_path):
    s=service(tmp_path,{None:page([obj(str(i)) for i in range(30)])})
    r=s.advance(s.create(settings(metadata_samples=20))['id'])
    assert r['head_requests']==5 and len(s.client.calls)==7


def test_control_character_key_is_reported_without_head(tmp_path):
    s=service(tmp_path,{None:page([obj('unusual\x00key')])})
    r=s.advance(s.create(settings())['id'])
    assert r['listing_complete'] and r['standard_phase_blockers']['unsupported_keys']==1
    assert r['attention_examples'][0]['key']=='unusual\x00key' and r['head_requests']==0


def test_wrong_region_blocks_before_listing(tmp_path):
    s=service(tmp_path,{None:page()})
    r=s.advance(s.create(settings(region='us-west-2'))['id'])
    assert r['state']=='failed' and len(s.client.calls)==1 and not r['listing_complete']


def test_api_security_and_read_only_contract(tmp_path):
    s=service(tmp_path,{None:page([obj('one')])})
    c=TestClient(create_app(discovery=s))
    assert c.post('/api/discovery',json=settings().model_dump(),headers={'Origin':'https://evil.invalid'}).status_code==403
    assert c.post('/api/discovery',json=settings().model_dump(),headers={'Host':'evil.invalid'}).status_code==403
    assert c.post('/api/discovery',content='x'*20000,headers={'Content-Type':'application/json'}).status_code==413
    bad=settings().model_dump()|{'aws_secret_access_key':'SECRET_SENTINEL'}
    reply=c.post('/api/discovery',json=bad)
    assert reply.status_code==422 and 'SECRET_SENTINEL' not in reply.text
    run=c.post('/api/discovery',json=settings().model_dump()).json()
    result=c.post('/api/discovery/'+run['id']+'/advance',json={}).json()
    assert result['listing_complete'] and not result['migration_ready']
    assert c.get('/api/discovery').json()[0]['id']==run['id']
    assert c.get('/api/discovery/'+run['id']).headers['cache-control']=='no-store'
    assert TestClient(create_app()).post('/api/discovery',json=settings().model_dump()).status_code==503


def test_cli_exact_owner_endpoint_no_payload_and_safe_errors(monkeypatch):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID','SECRET_SENTINEL')
    calls=[]
    def fake(args,**kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(returncode=0,stdout=json.dumps(page()).encode(),stderr=b'')
    monkeypatch.setattr(subprocess,'run',fake)
    client=AWSDiscoveryClient('chosen-profile','aws')
    client.call(settings(),'list-objects-v2',prefix='',max_keys=1000,encoding_type='url')
    args,kw=calls[0]
    assert args[args.index('--expected-bucket-owner')+1]=='111111111111'
    assert args[args.index('--endpoint-url')+1]=='https://s3.us-east-1.amazonaws.com'
    assert args[args.index('--profile')+1]=='chosen-profile'
    assert '--no-paginate' in args and kw['timeout']==25
    assert 'AWS_ACCESS_KEY_ID' not in kw['env'] and kw['env']['PYTHONIOENCODING']=='utf-8'
    with pytest.raises(DiscoveryError): client.call(settings(),'get-object')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=b'',stderr=b'AccessDenied SECRET_SENTINEL'))
    with pytest.raises(DiscoveryError) as error:client.call(settings(),'head-object',key='one')
    assert 'SECRET_SENTINEL' not in str(error.value)


@pytest.mark.parametrize('data',[{'scope':'bucket','prefix':'x/'},{'scope':'prefix','prefix':''},
    {'object_limit':1000001},{'metadata_samples':101},{'object_limit':True},
    {'bucket':'test--zone--x-s3'},{'prefix':'\x00','scope':'prefix'}])
def test_invalid_scope_and_limits(data):
    with pytest.raises(ValidationError):settings(**data)


@pytest.mark.parametrize('size,expected',[(0,'0 B'),(20_000,'1 B–128 KiB'),(128*1024,'128 KiB–1 MiB'),
    (1024**2,'1 MiB–100 MB'),(100_000_000,'1 MiB–100 MB'),(100_000_001,'>100 MB–1 GiB'),
    (1024**3,'1 GiB–10 GB'),(10_000_000_000,'1 GiB–10 GB'),(10_000_000_001,'>10 GB')])
def test_size_boundaries(size,expected):
    assert size_band(size)==expected
