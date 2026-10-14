from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from migration_runner.cloud_api import create_app
from migration_runner.cloud_dispatch import poll
from migration_runner.cloud_ui_service import CloudUIService, UIConflict
from migration_runner.planning.contracts import InventoryObject
from migration_runner import security
from migration_runner.worker.rclone import WorkerError

TOKEN = 'operator-' + 'x' * 48
WORKER = 'worker-' + 'y' * 48


class FakeService:
    def __init__(self): self.calls = []
    def ready(self): return {'ready': True}
    def jobs(self): return []
    def create(self): self.calls.append('create'); return {'job_id': str(uuid4())}
    def detail(self, job): return {'job_id': str(job)}
    def capacity_status(self, job): return {'job_id':str(job), 'actuation_enabled':False}
    def start(self, job, workers): self.calls.append(workers); return {'accepted': True}
    def capacity_preview(self, job, request):
        return {'job_id': str(job), 'mode': request.policy.mode, 'actuation_enabled': False}
    def control(self, job, action):
        if action not in ('pause','drain','resume','cancel'): raise UIConflict('Unsupported control.')
        self.calls.append(action); return {'control': action}


@pytest.fixture
def client():
    service = FakeService()
    with TestClient(create_app(service, TOKEN)) as value:
        value.service = service
        yield value


def auth(): return {'Authorization': 'Bearer ' + TOKEN}


def test_cloud_auth_and_same_origin(client):
    assert client.get('/health/live').status_code == 200
    assert client.get('/api/jobs').status_code == 401
    assert client.get('/api/jobs', headers={'Authorization': 'Bearer ' + WORKER}).status_code == 401
    assert client.get('/api/jobs', headers=auth()).status_code == 200
    assert client.post('/api/jobs', json={}, headers=auth() | {'Origin': 'https://attacker.invalid'}).status_code == 403
    assert client.get('/api/jobs', headers=auth() | {'Host': 'attacker.invalid'}).status_code == 403
    assert client.service.calls == []


def test_cloud_controls_are_bounded_not_shell_commands(client):
    job = uuid4()
    for body in ({'workers': 3}, {'workers': True}, {'workers': 2, 'command': 'anything'}, {'workers': 2, 'source_bucket': 'other'}):
        assert client.post(f'/api/jobs/{job}/start', json=body, headers=auth()).status_code == 422
    assert client.post('/api/jobs/not-a-uuid/start', json={'workers': 2}, headers=auth()).status_code == 422
    assert client.post(f'/api/jobs/{job}/shell', json={}, headers=auth()).status_code == 409
    assert client.post(f'/api/jobs/{job}/start', json={'workers': 2}, headers=auth()).status_code == 200
    assert client.service.calls == [2]


def test_cloud_body_limit_and_static_security(client):
    response = client.post('/api/jobs', content=b'x' * 4097, headers=auth() | {'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert client.post('/api/jobs', content='x', headers=auth()).status_code == 415
    response = client.get('/')
    assert response.status_code == 200 and TOKEN not in response.text
    assert response.headers['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert client.get('/static/app.js').status_code == 200


def test_customer_setup_is_an_unbound_static_draft(client):
    home = client.get('/')
    assert '/static/setup.html' in home.text
    preview = client.get('/static/setup.html')
    assert preview.status_code == 200
    assert 'Configuration draft' in preview.text
    assert TOKEN not in preview.text
    assert 'migration-lab-357633903375' not in preview.text
    assert "script-src 'self'" in preview.headers['Content-Security-Policy']
    for asset in ('setup.css', 'setup-model.js', 'setup.js'):
        assert client.get('/static/' + asset).status_code == 200
    assert client.post('/api/deployment-profile', json={}, headers=auth()).status_code == 404
    assert client.service.calls == []


@pytest.fixture
def service(db_engine, tmp_path):
    security.initialize(db_engine, WORKER)
    now = datetime.now(timezone.utc)
    rows = [{'source_key': f'fixtures/object-{i:03}.bin', 'size': 100_000_000,
             'sha256': hashlib.sha256(str(i).encode()).hexdigest()} for i in range(100)]
    reference = tmp_path / 'reference.json'
    reference.write_text(json.dumps({'format': 'migration-pilot-reference-v1', 'object_count': 100,
                                    'total_bytes': 10_000_000_000, 'objects': rows}))
    records = [InventoryObject(key=r['source_key'], bucket='migration-lab-357633903375-us-east-1-source',
               size=r['size'], last_modified=now, storage_class='STANDARD', encryption_status='SSE-S3') for r in rows]
    result = CloudUIService(db_engine, SimpleNamespace(worker_token=SecretStr(WORKER)), reference,
        hashlib.sha256(reference.read_bytes()).hexdigest(), destination_empty=lambda binding: True, inventory=lambda binding: records)
    def ready(workers=0):
        with db_engine.connect() as c:
            count = c.execute(text('SELECT count(*) FROM cloud_ui_runs WHERE admitted_at IS NOT NULL')).scalar_one()
        return {'ready': True, 'remaining_runs': 3 - count}
    result.ready = ready
    return result


def test_preview_uses_new_prefix_and_no_dispatch(service):
    first, second = service.create(), service.create()
    assert first['destination_prefix'] != second['destination_prefix']
    assert first['destination_prefix'].startswith('pilot/ui-')
    assert first['inventory']['eligible'] == {'objects': 100, 'bytes': 10_000_000_000}
    assert not first['bound'] and first['batches'] == 20
    with service.engine.connect() as c:
        assert c.execute(text('SELECT job_id FROM cloud_ui_dispatch')).scalar_one() is None


def test_repeat_start_is_idempotent_and_one_active_run(service):
    from uuid import UUID
    first = UUID(service.create()['job_id']); second = UUID(service.create()['job_id'])
    service.start(first, 2); service.start(first, 2)
    assert service.ready()['remaining_runs'] == 2
    with pytest.raises(UIConflict, match='current run'): service.start(second, 1)
    with pytest.raises(WorkerError, match='immutable'): service.start(first, 1)
    assert service.ready()['remaining_runs'] == 2


def test_admission_cap_survives_new_service_and_cancel(service):
    from uuid import UUID
    for _ in range(3):
        job = UUID(service.create()['job_id']); service.start(job, 2); service.control(job, 'cancel')
    job = UUID(service.create()['job_id'])
    with pytest.raises(UIConflict, match='allowance'): service.start(job, 2)
    assert service.ready()['remaining_runs'] == 0
    with service.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM cloud_ui_runs WHERE admitted_at IS NOT NULL')).scalar_one() == 3


def test_worker_selection_pause_resume_and_cancel(service):
    from uuid import UUID
    job = UUID(service.create()['job_id']); service.start(job, 1)
    expiry = datetime.now(timezone.utc) + timedelta(hours=12)
    assert poll(service.engine, 'worker-1', expiry, WORKER)['action'] == 'run'
    assert poll(service.engine, 'worker-2', expiry, WORKER)['action'] == 'idle'
    service.control(job, 'pause')
    assert poll(service.engine, 'worker-1', expiry, WORKER)['action'] == 'hold'
    service.control(job, 'resume')
    assert poll(service.engine, 'worker-1', expiry, WORKER)['action'] == 'run'
    service.control(job, 'cancel')
    assert poll(service.engine, 'worker-1', expiry, WORKER)['action'] == 'idle'
    with pytest.raises(WorkerError): poll(service.engine, 'worker-1', expiry, 'wrong-token')


def test_modified_reference_or_destination_refuses_work(service):
    service.destination_empty = lambda binding: False
    with pytest.raises(UIConflict, match='empty'): service.create()
    service.reference_path.write_text('{}')
    with pytest.raises(UIConflict, match='reference'): service.create()


def test_unmanaged_jobs_cannot_be_started(service):
    from uuid import UUID
    job = UUID(service.create()['job_id'])
    with service.engine.begin() as c: c.execute(text('DELETE FROM cloud_ui_runs WHERE job_id=:job'), {'job': job})
    with pytest.raises(UIConflict, match='read-only'): service.start(job, 2)


def capacity_body():
    return {'policy': {'path_id':'lab-path', 'mode':'shadow',
                      'capacity_bytes_per_second':1_250_000_000,
                      'migration_cap_bytes_per_second':1_062_500_000}}


def test_capacity_preview_is_authenticated_bounded_and_read_only(client):
    path = f'/api/jobs/{uuid4()}/capacity/preview'
    body = capacity_body()
    assert client.post(path,json=body).status_code == 401
    assert client.post(path,json=body,headers={'Authorization':'Bearer '+WORKER}).status_code == 401
    result = client.post(path,json=body,headers=auth())
    assert result.status_code == 200 and result.json()['actuation_enabled'] is False
    assert client.post(path,json=body | {'ready_nodes':99},headers=auth()).status_code == 422
    body['policy']['mode'] = 'automatic'
    assert client.post(path,json=body,headers=auth()).status_code == 422
    assert client.service.calls == []


def test_live_capacity_requires_operator_and_serves_dashboard(client):
    path = f'/api/jobs/{uuid4()}/capacity'
    assert client.get(path).status_code == 401
    assert client.get(path,headers={'Authorization':'Bearer '+WORKER}).status_code == 401
    assert client.get(path,headers=auth()).json()['actuation_enabled'] is False
    assert client.get('/static/capacity.js').status_code == 200
    assert 'Throughput &amp; capacity' in client.get('/').text
    assert client.service.calls == []


def test_live_capacity_unconfigured_does_not_consume_admission(service):
    from uuid import UUID
    job = UUID(service.create()['job_id'])
    value = service.capacity_status(job)
    assert value['network']['status'] == 'not_configured'
    assert value['worker_metrics']['coverage'] == 'unavailable'
    assert value['worker_metrics']['active_source_bytes_per_second'] is None
    assert value['recommendation'] is None
    assert not value['actuation_enabled']
    assert service.ready()['remaining_runs'] == 3
    with pytest.raises(UIConflict):
        service.capacity_status(uuid4())


def test_capacity_preview_keeps_lab_ceiling_and_does_not_bind(service):
    from uuid import UUID
    from migration_runner.autoscaling.contracts import CapacityPreviewRequest
    job = UUID(service.create()['job_id'])
    before = service.detail(job)
    result = service.capacity_preview(job, CapacityPreviewRequest.model_validate(capacity_body()))
    assert result['lab_node_ceiling'] == result['decision']['effective_node_limit'] == 2
    assert not result['live_path_telemetry_available'] and not result['policy_persisted']
    assert result['snapshot']['control'] == 'unbound'
    assert result['snapshot']['runnable_batches'] == 20
    assert result['decision']['pause_payload'] and result['decision']['recommended_add_nodes'] == 0
    assert result['effective_policy']['object_slots_per_node'] == 1
    after = service.detail(job)
    assert before['bound'] == after['bound'] is False
    assert before['scheduled_bytes'] == after['scheduled_bytes'] == 0
    assert service.ready()['remaining_runs'] == 3
    with service.engine.connect() as c:
        assert c.execute(text('SELECT job_id FROM cloud_ui_dispatch')).scalar_one() is None


def test_capacity_preview_preserves_original_limit_and_excludes_delayed_retry(service):
    from uuid import UUID
    from migration_runner.autoscaling.contracts import CapacityPreviewRequest
    job = UUID(service.create()['job_id'])
    service.start(job,1)
    with service.engine.begin() as c:
        c.execute(text("UPDATE batches SET state='retry_wait',available_at=clock_timestamp()+interval '1 hour' WHERE migration_id=:job AND ordinal=1"), {'job':job})
    before = service.detail(job)
    request = CapacityPreviewRequest.model_validate(capacity_body())
    result = service.capacity_preview(job,request)
    assert result['snapshot']['runtime_node_limit'] == result['decision']['effective_node_limit'] == 1
    assert result['snapshot']['runnable_batches'] == 19
    assert result['snapshot']['remaining_unique_bytes'] == 9_500_000_000
    after = service.detail(job)
    assert before['run_limits'] == after['run_limits']
    assert before['scheduled_bytes'] == after['scheduled_bytes']
    assert service.ready()['remaining_runs'] == 2
    with pytest.raises(UIConflict):
        service.capacity_preview(uuid4(),request)
