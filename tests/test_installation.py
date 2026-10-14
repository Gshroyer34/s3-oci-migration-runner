import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from migration_runner.cloud_api import create_app
from migration_runner.installation.aws import reader_template
from migration_runner.installation.package import build_package, terraform_values
from migration_runner.installation.profile import InstallationProfile

TOKEN = 'installation-operator-' + 'x' * 48


def example(account='111111111111', deployment='alpha-migration'):
    def ocid(kind):
        return f'ocid1.{kind}.oc1..' + deployment.replace('-', '') * 3
    key = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(range(32))
    return {
        'schema_version': 'migration-installation/v1', 'deployment_name': deployment,
        'source': {'account_id': account, 'region': 'us-east-1', 'bucket': deployment + '-source', 'prefix': 'approved/'},
        'destination': {'tenancy_ocid': ocid('tenancy'), 'compartment_ocid': ocid('compartment'),
                        'region': 'us-ashburn-1', 'namespace': 'example_namespace', 'bucket': deployment + '-target', 'prefix': 'incoming/'},
        'network': {'vcn_ocid': ocid('vcn'), 'subnet_ocid': ocid('subnet'), 'admin_cidr': '192.0.2.10/32', 'payload_route': 'internet'},
        'compute': {'availability_domain': 'example:US-ASHBURN-AD-1', 'image_ocid': ocid('image'),
                    'ssh_public_key': 'ssh-ed25519 ' + base64.b64encode(key).decode()},
    }


def profile(**kwargs):
    return InstallationProfile.model_validate(example(**kwargs))


@pytest.mark.parametrize(('section', 'field', 'value'), [
    ('source', 'prefix', ''), ('source', 'prefix', 'approved/*/'), ('source', 'prefix', '../'),
    ('source', 'prefix', 'a//'), ('source', 'prefix', 'missing-slash'),
    ('source', 'bucket', '10.1.1.1'), ('source', 'bucket', 'a..b'),
    ('source', 'region', 'us-gov-west-1'),
    ('source', 'kms_key_arn', 'arn:aws:kms:us-east-1:222222222222:key/key-id'),
    ('source', 'kms_key_arn', 'arn:aws:kms:us-west-2:111111111111:key/key-id'),
    ('source', 'access_key', 'SECRET_SENTINEL'),
    ('network', 'admin_cidr', '0.0.0.0/0'), ('network', 'admin_cidr', '192.0.2.1/24'),
    ('network', 'payload_route', 'private'), ('network', 's3_endpoint_id', 'vpce-12345678'),
    ('network', 'subnet_ocid', 'https://attacker.invalid'),
    ('compute', 'initial_workers', True), ('compute', 'initial_workers', 5),
    ('compute', 'maximum_workers', 1), ('compute', 'ocpus', 0),
    ('compute', 'memory_gib', 256), ('compute', 'shape', 'VM.Standard.A1.Flex'),
    ('compute', 'ssh_public_key', '-----BEGIN PRIVATE KEY-----' + 'X' * 80),
    ('compute', 'ssh_public_key', 'ssh-ed25519 ' + base64.b64encode(b'not-a-valid-key' * 10).decode()),
])
def test_rejects_unbounded_or_inconsistent_inputs(section, field, value):
    data = example(); data[section][field] = value
    with pytest.raises(ValidationError):
        InstallationProfile.model_validate(data)


def test_two_deployments_have_independent_scope_and_stable_revision():
    first, second = profile(), profile(account='222222222222', deployment='bravo-migration')
    assert first.revision != second.revision
    assert first.revision == InstallationProfile.model_validate(first.model_dump()).revision
    a, b = json.dumps(reader_template(first)), json.dumps(reader_template(second))
    assert 'alpha-migration-source' in a and 'alpha-migration-source' not in b
    assert '111111111111' not in b and '222222222222' not in a
    for doc in (a, b):
        assert '357633903375' not in doc and 'idswphn9j6by' not in doc
        assert 'AWS::S3::Bucket' not in doc and 's3:PutObject' not in doc and 's3:DeleteObject' not in doc
        assert 'aws:SourceArn' in doc and 'aws:SourceAccount' in doc and 'x509Subject/CN' in doc


def test_private_reader_restricts_every_s3_permission_and_optional_kms():
    data = example(); data['network'].update(payload_route='private', s3_endpoint_id='vpce-0123456789abcdef0')
    data['source']['kms_key_arn'] = 'arn:aws:kms:us-east-1:111111111111:key/example-key'
    document = reader_template(InstallationProfile.model_validate(data))
    statements = document['Resources']['ReaderRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    for statement in statements:
        if statement['Action'][0].startswith('s3:'):
            assert statement['Condition']['StringEquals']['aws:SourceVpce'] == data['network']['s3_endpoint_id']
        else:
            assert statement['Action'] == ['kms:Decrypt']
            assert statement['Resource'] == data['source']['kms_key_arn']
            assert statement['Condition']['StringEquals']['kms:ViaService'] == 's3.us-east-1.amazonaws.com'


def test_tag_reads_are_scoped_and_never_grant_tag_writes():
    p=profile()
    statements=reader_template(p)['Resources']['ReaderRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    tag_reads=[s for s in statements if 's3:GetObjectTagging' in s['Action']]
    assert len(tag_reads)==1
    assert 's3:GetObjectAcl' in tag_reads[0]['Action'] and 's3:GetObjectVersionAcl' in tag_reads[0]['Action']
    assert tag_reads[0]['Resource']==f'arn:aws:s3:::{p.source.bucket}/{p.source.prefix}*'
    assert all('s3:PutObjectTagging' not in s['Action'] and 's3:DeleteObjectTagging' not in s['Action'] for s in statements)


def test_archive_is_deterministic_allowlisted_and_checksummed():
    p = profile(); content = build_package(p)
    assert content == build_package(p)
    with zipfile.ZipFile(io.BytesIO(content)) as outer:
        assert 'GetObjectTagging' in outer.read('PROPERTY_COMPATIBILITY.md').decode()
        assert json.loads(outer.read('property-mapping.json'))['revision'] == 's3-oci-properties/v1'
        manifest = json.loads(outer.read('manifest.json'))
        assert set(outer.namelist()) == set(manifest['files']) | {'manifest.json'}
        for name, digest in manifest['files'].items():
            assert hashlib.sha256(outer.read(name)).hexdigest() == digest
        with zipfile.ZipFile(io.BytesIO(outer.read('oci-foundation.zip'))) as inner:
            names = inner.namelist()
            assert set(names) == {'versions.tf', 'variables.tf', 'main.tf', 'cloud-init.yaml', 'schema.yaml',
                                  'installation.auto.tfvars.json', 'README.md', '.terraform.lock.hcl'}
            assert not any('..' in n or n.startswith('/') or '.secrets' in n for n in names)
            values = json.loads(inner.read('installation.auto.tfvars.json'))
            assert values == terraform_values(p) and values['acknowledge_foundation_only'] is False
            assert b'backend "oci"' not in inner.read('versions.tf')
            assert b'config_file_profile' not in inner.read('versions.tf')
            assert b'oci_objectstorage_bucket"' not in inner.read('main.tf')
            schema = json.loads(inner.read('schema.yaml'))
            assert set(schema['variables']) == set(values)


def test_key_comments_are_not_exported():
    data = example(); data['compute']['ssh_public_key'] += ' SECRET_SENTINEL'
    p = InstallationProfile.model_validate(data)
    assert 'SECRET_SENTINEL' not in json.dumps(p.model_dump())


@pytest.fixture
def client():
    # Every setup action must work without querying the ledger or cloud service.
    class NoRuntime:
        def __getattr__(self, name):
            raise AssertionError('Installation preparation touched runtime: ' + name)
    with TestClient(create_app(NoRuntime(), TOKEN)) as value:
        yield value


def test_setup_api_authentication_and_isolation(client):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    for path in ('/api/installation/validate', '/api/installation/package'):
        assert client.post(path, json=example()).status_code == 401
        assert client.post(path, json=example(), headers=headers | {'Origin': 'https://attacker.invalid'}).status_code == 403
    report = client.post('/api/installation/validate', json=example(), headers=headers).json()
    assert report['configuration_valid'] and report['cloud_validation'] == 'not_performed'
    assert not report['runtime_activation_supported'] and report['summary']['initial_instances'] == 3
    result = client.post('/api/installation/package', json=example(), headers=headers)
    assert result.status_code == 200 and result.headers['Content-Type'] == 'application/zip'
    assert zipfile.is_zipfile(io.BytesIO(result.content))
    assert client.get('/static/install.html').status_code == 200
    assert client.get('/static/install.js').status_code == 200
    assert 'install.html' in client.get('/static/setup.html').text


def test_api_redacts_secrets_and_enforces_actual_body_limit(client):
    data = example(); data['source']['access_key'] = 'SECRET_SENTINEL'
    headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}
    result = client.post('/api/installation/package', json=data, headers=headers)
    assert result.status_code == 422 and 'SECRET_SENTINEL' not in result.text
    assert result.json()['fields'] == ['source']
    for path, size in [('/api/installation/package', 16385), ('/api/jobs', 4097)]:
        assert client.post(path, content=b'x' * size, headers=headers).status_code == 413


def test_cli_never_overwrites_and_does_not_echo_credentials(tmp_path):
    source = tmp_path / 'profile.json'; output = tmp_path / 'package.zip'
    source.write_text(json.dumps(example()))
    command = [sys.executable, '-m', 'migration_runner.installation', 'package', '--profile', str(source), '--output', str(output)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    source.write_text(json.dumps(example() | {'password': 'SECRET_SENTINEL'}))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and 'SECRET_SENTINEL' not in result.stderr + result.stdout


def test_standalone_assistant_has_no_runtime_or_cloud_access():
    from migration_runner.installation.web import create_app as setup_app
    with TestClient(setup_app()) as local:
        assert 'Prepare an installation' in local.get('/').text
        assert local.get('/api/installation/context').json()['cloud_access'] is False
        assert local.get('/api/jobs').status_code == 404
        assert local.post('/api/installation/validate', json=example()).json()['configuration_valid']
        assert local.post('/api/installation/package', json=example()).status_code == 200
        assert local.post('/api/installation/package', json=example(), headers={'Origin': 'https://attacker.invalid'}).status_code == 403
        assert local.post('/api/installation/package', json=example(), headers={'Host': 'attacker.invalid'}).status_code == 403
        assert local.post('/api/installation/package', content='x').status_code == 415
        assert local.post('/api/installation/package', content=b'x'*16385, headers={'Content-Type': 'application/json'}).status_code == 413
