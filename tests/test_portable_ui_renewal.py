from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from migration_runner import security
from migration_runner.cloud_api import create_app
from migration_runner.cloud_ui_service import UIConflict
from migration_runner.installation import enrollment, managed_renewal, portable_host
from migration_runner.installation.host_files import InstallError, encoded, sha
from migration_runner.installation.host_plan import HostPlan, credential_policy
from migration_runner.planning.contracts import InventoryObject
from migration_runner.portable_ui import PortableUIService
from test_host_installer import setup, enrolled, trust_bytes, PASSWORD, TOKEN, Vault
from test_cloud_ui import FakeService


@pytest.fixture
def managed(setup):
    plan,authorities=setup
    data=plan.model_dump()
    data['managed_certificates']=[{'node_id':node.node_id,'purpose':purpose,
        'certificate_id':'ocid1.certificate.oc1..'+'synthetic'*3+node.node_id+purpose,
        'authority_id':'ocid1.certificateauthority.oc1..'+'synthetic'*3+('roles' if purpose=='roles' else 'tls')}
        for node in plan.nodes for purpose in plan.purposes(node.node_id)]
    return HostPlan.model_validate(data),authorities


class Issuer:
    def __init__(self,setup,directory,initial):
        self.plan,self.authorities=setup; self.directory=directory
        self.entries={entry.certificate_id:entry for entry in self.plan.managed_certificates if entry.node_id=='worker-1'}
        self.certs={key:initial['credentials'][entry.purpose]['certificate'] for key,entry in self.entries.items()}
        self.versions={key:1 for key in self.entries}; self.updates=0; self.fail=False; self.wrong_issuer=False; self.revoked=False

    def get_certificate(self,identifier):
        entry=self.entries[identifier]
        return NS(headers={'etag':str(self.versions[identifier])},data=NS(id=identifier,lifecycle_state='ACTIVE',
            compartment_id=self.plan.deployment.destination.compartment_ocid,
            issuer_certificate_authority_id='wrong' if self.wrong_issuer else entry.authority_id,
            config_type='MANAGED_EXTERNALLY_ISSUED_BY_INTERNAL_CA',certificate_profile_type='TLS_CLIENT',
            current_version=NS(version_number=self.versions[identifier])))

    def get_certificate_bundle(self,identifier,stage,certificate_bundle_type):
        assert stage=='CURRENT' and certificate_bundle_type=='CERTIFICATE_CONTENT_PUBLIC_ONLY'
        return NS(data=NS(certificate_id=identifier,version_number=self.versions[identifier],certificate_pem=self.certs[identifier],
                         revocation_status=NS(time_revoked=datetime.now(timezone.utc)) if self.revoked else None))

    def update_certificate(self,identifier,details,if_match):
        assert if_match==str(self.versions[identifier])
        self.updates+=1
        if self.fail: raise RuntimeError('provider secret sentinel')
        request=enrollment.request_credentials(self.plan,'worker-1',self.directory)
        entry=self.entries[identifier]
        assert request['requests'][entry.purpose].split('-----')[1]==details.certificate_config.csr_pem.split('-----')[1]
        request['requests'][entry.purpose]=details.certificate_config.csr_pem
        signed=enrollment.sign_requests(self.plan,request,self.authorities,PASSWORD)
        self.certs[identifier]=signed['certificates'][entry.purpose]
        self.versions[identifier]+=1


def test_managed_enrollment_and_retry_without_duplicate_versions(managed,tmp_path):
    plan,authorities=managed
    initial,_,_=enrolled(managed,tmp_path/'old')
    directory=tmp_path/'node'; directory.mkdir()
    issuer=Issuer(managed,directory,initial); vault=Vault(plan)
    with pytest.raises(managed_renewal.Pending):
        managed_renewal.renew(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),vault,vault,issuer,issuer,TOKEN)
    assert issuer.updates==2 and vault.writes==0
    result=managed_renewal.renew(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),vault,vault,issuer,issuer,TOKEN)
    assert result['changed'] and result['enrolled'] and vault.writes==1
    again=managed_renewal.renew(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),vault,vault,issuer,issuer)
    assert not again['changed'] and issuer.updates==2 and vault.writes==1


def test_uncertain_issuance_is_checkpointed_not_blindly_retried(managed,tmp_path):
    plan,authorities=managed
    initial,_,_=enrolled(managed,tmp_path/'old')
    directory=tmp_path/'node'; directory.mkdir()
    issuer=Issuer(managed,directory,initial); issuer.fail=True
    with pytest.raises(RuntimeError): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer)
    assert issuer.updates==1 and (directory/'tls-issuance.json').exists()
    # The other purpose can request once, but the uncertain request is not repeated.
    with pytest.raises(RuntimeError): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer)
    assert issuer.updates==2
    with pytest.raises(managed_renewal.Pending): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer)
    assert issuer.updates==2
    with pytest.raises(InstallError): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer,now=datetime.now(timezone.utc)+timedelta(hours=1))
    assert issuer.updates==2


def test_managed_issuer_and_authority_expiry_rejected(managed,tmp_path):
    plan,authorities=managed
    initial,_,_=enrolled(managed,tmp_path/'old')
    directory=tmp_path/'node'; directory.mkdir()
    issuer=Issuer(managed,directory,initial); issuer.wrong_issuer=True
    with pytest.raises(InstallError): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer)
    assert issuer.updates==0
    issuer.wrong_issuer=False
    issuer.revoked=True
    with pytest.raises(InstallError,match='revoked'): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer)
    assert issuer.updates==0
    issuer.revoked=False
    with pytest.raises(InstallError): managed_renewal.issue(plan,plan.node('worker-1'),directory,*trust_bytes(authorities),issuer,issuer,now=datetime.now(timezone.utc)+timedelta(days=29))
    assert issuer.updates==0


def test_feature_plan_identity_and_policy_scope(managed):
    plan,_=managed
    assert HostPlan.model_validate_json(plan.canonical())==plan
    policy=credential_policy(plan)
    worker=policy['nodes'][1]
    assert worker['remove_temporary_policy_after_readback'] is False
    assert any('target.leaf-certificate.id' in value for value in worker['renewal_policies'])
    assert all('PRIVATE_KEY' not in value for value in worker['renewal_policies'])
    data=plan.model_dump(); data['managed_certificates'].pop()
    with pytest.raises(ValidationError): HostPlan.model_validate(data)


def test_portable_http_boundary_and_four_worker_input():
    service=FakeService()
    service.configuration=lambda: {'deployment_name':'synthetic'}
    token='operator-'+ 'x'*48
    with TestClient(create_app(service,token,portable=True)) as client:
        assert client.get('/api/deployment').status_code==401
        auth={'Authorization':'Bearer '+token}
        assert client.get('/api/deployment',headers=auth).json()['deployment_name']=='synthetic'
        assert client.post(f'/api/jobs/{uuid4()}/start',json={'workers':4},headers=auth).status_code==200
        assert client.post(f'/api/jobs/{uuid4()}/start',json={'workers':5},headers=auth).status_code==422
        assert client.post('/api/jobs',json={},headers=auth|{'Origin':'https://other.invalid'}).status_code==403
        home=client.get('/').text
        assert 'portable.js' in home and 'migration-lab' not in home and 'Ashburn' not in home


@pytest.fixture
def portable_service(setup,db_engine,tmp_path,monkeypatch):
    original,authorities=setup
    reference={'format':'migration-pilot-reference-v1','object_count':1,'total_bytes':100,
               'objects':[{'source_key':'approved/object.bin','size':100,'sha256':'a'*64}]}
    path=tmp_path/'reference.json'; path.write_bytes(encoded(reference))
    data=original.model_dump(); data['ui']={'reference_sha256':sha(path.read_bytes()),'max_runs':1}
    plan=HostPlan.model_validate(data)
    bundle,_,_=enrolled((plan,authorities),tmp_path/'coordinator','coordinator')
    cert=tmp_path/'client.crt'; cert.write_text(bundle['credentials']['tls']['certificate'])
    settings=NS(db_sslcert=str(cert),worker_token=SecretStr(TOKEN),aws_config_file='unused',rclone_path='unused')
    security.initialize(db_engine,TOKEN)
    monkeypatch.setattr('migration_runner.installation.deployment.installed_profile',lambda:plan.deployment)
    inventory=[InventoryObject(key='approved/object.bin',size=100,bucket=plan.deployment.source.bucket,
                storage_class='STANDARD',encryption_status='SSE-S3',last_modified=datetime.now(timezone.utc))]
    with db_engine.begin() as connection:
        connection.execute(text("INSERT INTO cloud_ui_workers VALUES ('worker-1',clock_timestamp(),clock_timestamp()+interval '24 hours',NULL,'idle','inactive')"))
    return PortableUIService(db_engine,settings,plan,path,inventory=lambda _:inventory,destination_empty=lambda _:True)


def test_portable_create_start_control_and_scope(portable_service):
    service=portable_service
    assert service.ready(1)['ready']
    job=UUID(service.create()['job_id'])
    assert len(service.jobs())==1 and service.detail(job)['destination_prefix'].startswith('incoming/ui-')
    started=service.start(job,1)
    assert started['bound'] and service.ready()['remaining_runs']==0
    assert service.control(job,'pause')['control']=='paused'
    assert service.control(job,'resume')['control']=='active'
    with pytest.raises(UIConflict): service.create()
    with pytest.raises(UIConflict): service.start(job,2)
    with pytest.raises(UIConflict): service.detail(uuid4())
    with service.engine.begin() as connection:
        connection.execute(text("UPDATE migrations SET provenance=jsonb_set(provenance,'{deployment_revision}',to_jsonb('other'::text)) WHERE id=:job"),{'job':job})
    assert service.jobs()==[]
    for action in (service.detail,service.manifest,lambda value: service.control(value,'cancel')):
        with pytest.raises(UIConflict): action(job)


def test_reference_drift_and_offline_workers_fail_before_start(portable_service):
    service=portable_service
    job=service.create()['job_id']
    with service.engine.begin() as connection:
        connection.execute(text("UPDATE cloud_ui_workers SET last_seen=clock_timestamp()-interval '1 hour'"))
    with pytest.raises(UIConflict): service.start(job,1)
    service.reference_path.write_text('{}')
    with pytest.raises(UIConflict): service.create()


@pytest.mark.parametrize('busy,running,expected',[(True,'0','waiting_for_idle'),(False,'1','waiting_for_idle'),(False,'0','activated')])
def test_renewal_activation_waits_for_workers_and_preserves_ui(managed,monkeypatch,busy,running,expected):
    plan,_=managed
    calls=[]
    monkeypatch.setattr(portable_host,'worker_busy',lambda:busy)
    def command(args,**kwargs):
        calls.append(args)
        if args[:2]==['podman','ps']: return b'migration-portable-ui\nmigration-portable-postgres\n'
        return b''
    monkeypatch.setattr(portable_host.host,'command',command)
    monkeypatch.setattr(portable_host.host,'sql',lambda statement,*_:running if "state='running'" in statement else '0')
    assert portable_host.activate_credentials(plan,plan.node('coordinator'))==expected
    db_stop=['systemctl','stop',portable_host.host.DB_UNIT]
    assert (db_stop in calls)==(expected=='activated')
    if not busy: assert calls[-1]==['systemctl','start',portable_host.UI_UNIT]
