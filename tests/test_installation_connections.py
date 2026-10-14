from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from migration_runner.installation.connections import ConnectionSettings, LocalConnectionTester, ProbeError, safe_error
from migration_runner.installation.oci_probe import probe
from migration_runner.installation.web import create_app
from test_installation import example, TOKEN


def settings():
    data = example()
    return ConnectionSettings.model_validate({k: data[k] for k in ('source', 'destination')} |
                                            {'network': {'payload_route': 'internet'}})


def aws_runner(monkeypatch, *, account='111111111111', location=None, objects=None, fail=None):
    calls = []
    if objects is None:
        objects = [{'Key': 'approved/sample.bin', 'Size': 100, 'ETag': '"sample"'}]
    def run(command, **kwargs):
        calls.append((command, kwargs))
        operation = next(x for x in command if x in ('get-caller-identity', 'get-bucket-location', 'list-objects-v2', 'get-object'))
        if operation == fail:
            return SimpleNamespace(returncode=1, stdout='', stderr='AccessDenied SECRET_SENTINEL')
        if operation == 'get-caller-identity':
            data = {'Account': account}
        elif operation == 'get-bucket-location':
            data = {'LocationConstraint': location}
        elif operation == 'list-objects-v2':
            data = {'Contents': objects}
        else:
            Path(command[-1]).write_bytes(b'x')
            data = {'ContentLength': 1}
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    return calls


def test_source_is_bounded_scoped_read_only_and_removes_sample(monkeypatch):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'SECRET_SENTINEL')
    calls = aws_runner(monkeypatch)
    report = LocalConnectionTester('test-aws', 'test-oci', aws_cli='aws').aws_checks(settings())
    assert all(x['status'] == 'passed' for x in report)
    assert len(calls) == 4
    for command, kwargs in calls:
        assert kwargs['timeout'] <= 15 and kwargs['env']['AWS_MAX_ATTEMPTS'] == '1'
        assert 'AWS_ACCESS_KEY_ID' not in kwargs['env']
        assert '--no-paginate' in command and '--endpoint-url' in command
        if 's3api' in command:
            assert command[command.index('--expected-bucket-owner') + 1] == settings().source.account_id
    listing = calls[2][0]
    assert listing[listing.index('--prefix') + 1] == 'approved/'
    assert listing[listing.index('--max-keys') + 1] == '1'
    sample = calls[3][0]
    assert sample[sample.index('--range') + 1] == 'bytes=0-0'
    assert '--if-match' in sample and not Path(sample[-1]).exists()
    assert 'sample.bin' not in json.dumps(report) and 'SECRET_SENTINEL' not in json.dumps(report)


@pytest.mark.parametrize(('kwargs', 'count', 'last'), [
    ({'account': '222222222222'}, 1, 'failed'),
    ({'location': 'us-west-2'}, 2, 'failed'),
    ({'objects': []}, 3, 'not_tested'),
    ({'objects': [{'Key': 'approved/empty', 'Size': 0}]}, 3, 'not_tested'),
    ({'objects': [{'Key': 'outside/sample', 'Size': 1}]}, 3, 'failed'),
    ({'fail': 'get-object'}, 4, 'failed'),
])
def test_source_fail_closed_and_empty_prefix(monkeypatch, kwargs, count, last):
    calls = aws_runner(monkeypatch, **kwargs)
    result = LocalConnectionTester('a', 'o', aws_cli='aws').aws_checks(settings())
    assert len(calls) == count
    assert any(x['status'] == last for x in result)
    assert not all(x['status'] == 'passed' for x in result)
    assert 'SECRET_SENTINEL' not in json.dumps(result)


def test_source_timeout_and_missing_cli(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=subprocess.TimeoutExpired('SECRET_SENTINEL', 15)))
    tester = LocalConnectionTester('a', 'o', aws_cli='aws')
    result = tester.aws_checks(settings())
    assert result[0]['status'] == 'failed' and 'timed out' in result[0]['detail']
    tester.aws_cli = None
    assert 'not installed' in tester.aws_checks(settings())[0]['detail']


def oci_sdk(*, tenancy=None, compartment=None, namespace=None):
    destination = settings().destination
    client = Mock()
    client.get_namespace.return_value = SimpleNamespace(data=namespace or destination.namespace)
    client.get_bucket.return_value = SimpleNamespace(data=SimpleNamespace(compartment_id=compartment or destination.compartment_ocid))
    sdk = SimpleNamespace(config=SimpleNamespace(from_file=Mock(return_value={'tenancy': tenancy or destination.tenancy_ocid})),
                          retry=SimpleNamespace(NoneRetryStrategy=lambda: 'no-retry'),
                          object_storage=SimpleNamespace(ObjectStorageClient=Mock(return_value=client)))
    return sdk, client


def test_oci_checks_scope_and_never_write():
    sdk, client = oci_sdk()
    destination = settings().destination
    result = probe(destination, 'chosen-profile', sdk=sdk)
    assert all(x['status'] == 'passed' for x in result)
    assert [call[0] for call in client.mock_calls] == ['get_namespace', 'get_bucket', 'list_objects']
    client.list_objects.assert_called_once_with(destination.namespace, destination.bucket, prefix=destination.prefix, limit=1)
    opts = sdk.object_storage.ObjectStorageClient.call_args.kwargs
    assert opts['service_endpoint'] == 'https://objectstorage.us-ashburn-1.oraclecloud.com'
    assert opts['timeout'] == (5, 8) and opts['retry_strategy'] == 'no-retry'


@pytest.mark.parametrize('kwargs', [{'tenancy': 'other'}, {'namespace': 'other'}, {'compartment': 'other'}])
def test_oci_scope_mismatch_stops_dependent_calls(kwargs):
    sdk, client = oci_sdk(**kwargs)
    result = probe(settings().destination, 'p', sdk=sdk)
    assert any(x['status'] == 'failed' for x in result)
    client.list_objects.assert_not_called()
    if 'tenancy' in kwargs:
        sdk.object_storage.ObjectStorageClient.assert_not_called()


def test_oci_permission_failure_redacts():
    sdk, client = oci_sdk()
    client.get_bucket.side_effect = RuntimeError('NotAuthorized SECRET_SENTINEL')
    result = probe(settings().destination, 'p', sdk=sdk)
    assert 'Access denied' in result[1]['detail']
    assert 'SECRET_SENTINEL' not in json.dumps(result)


def test_oci_process_timeout_is_bounded_and_redacted(monkeypatch):
    runner = Mock(side_effect=subprocess.TimeoutExpired('SECRET_SENTINEL', 45))
    monkeypatch.setattr(subprocess, 'run', runner)
    result = LocalConnectionTester('a', 'o').oci_checks(settings())
    assert runner.call_args.kwargs['timeout'] == 45
    assert result[0]['status'] == 'failed' and 'timed out' in result[0]['detail']
    assert 'SECRET_SENTINEL' not in json.dumps(result)


def fake_tester():
    tester = LocalConnectionTester('a', 'o', defaults=settings())
    tester.aws_checks = Mock(return_value=[{'name': 'AWS', 'status': 'passed', 'detail': 'ok'}])
    tester.oci_checks = Mock(return_value=[{'name': 'OCI', 'status': 'passed', 'detail': 'ok'}])
    return tester


def test_read_success_never_means_migration_ready_and_rate_limit():
    tester = fake_tester()
    report = tester.run(settings())
    assert report['read_checks_passed'] and not report['migration_ready']
    assert report['connection_revision'] == settings().revision
    assert len([x for x in report['checks'] if x['status'] == 'not_tested']) == 3
    with pytest.raises(ProbeError, match='ten seconds'):
        tester.run(settings())
    tester.lock.acquire()
    try:
        with pytest.raises(ProbeError, match='already running'):
            tester.run(settings())
    finally:
        tester.lock.release()


def test_private_selection_does_not_claim_private_path_verified():
    data = settings().model_dump()
    data['network'] = {'payload_route': 'private', 's3_endpoint_id': 'vpce-0123456789abcdef0'}
    report = fake_tester().run(ConnectionSettings.model_validate(data))
    assert report['checks'][-1]['name'] == 'Private interconnect'
    assert report['checks'][-1]['status'] == 'not_tested'
    assert not report['migration_ready']


def test_api_opt_in_csrf_body_limits_and_invalid_inputs():
    body = settings().model_dump()
    with TestClient(create_app()) as client:
        assert client.post('/api/installation/test-connections', json=body).status_code == 503
    tester = fake_tester()
    with TestClient(create_app(tester)) as client:
        assert client.get('/api/installation/context').json()['connections']['enabled']
        for headers in ({'Origin': 'https://evil.invalid'}, {'Host': 'evil.invalid'}):
            assert client.post('/api/installation/test-connections', json=body, headers=headers).status_code == 403
        bad = deepcopy(body); bad['source']['secret'] = 'SECRET_SENTINEL'
        response = client.post('/api/installation/test-connections', json=bad)
        assert response.status_code == 422 and 'SECRET_SENTINEL' not in response.text
        assert client.post('/api/installation/test-connections', content=b'x' * 16385,
                           headers={'Content-Type': 'application/json'}).status_code == 413
        assert not tester.aws_checks.called
        response = client.post('/api/installation/test-connections', json=body)
        assert response.status_code == 200 and not response.json()['migration_ready']
        assert client.post('/api/installation/test-connections', json=body).status_code == 429


def test_embedded_api_stays_authenticated_and_disabled():
    from migration_runner.cloud_api import create_app as cloud_app
    with TestClient(cloud_app(object(), TOKEN)) as client:
        assert client.post('/api/installation/test-connections', json=settings().model_dump()).status_code == 401
        result = client.post('/api/installation/test-connections', json=settings().model_dump(),
                             headers={'Authorization': 'Bearer ' + TOKEN})
        assert result.status_code == 503


@pytest.mark.parametrize('value', ['expired SECRET_SENTINEL', 'unknown SECRET_SENTINEL', 'AccessDenied SECRET_SENTINEL'])
def test_error_messages_never_echo_upstream_text(value):
    assert 'SECRET_SENTINEL' not in safe_error(value)
