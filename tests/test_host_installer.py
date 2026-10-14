"""Portable installer failure/retry tests; no real cloud calls or identities."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import zipfile

import pytest
from pydantic import ValidationError

from migration_runner.installation import enrollment as certs, host_runtime as host
from migration_runner.installation.host_cli import main
from migration_runner.installation.host_files import InstallError, encoded, sha
from migration_runner.installation.host_plan import HostPlan, credential_policy, SCHEMA
from migration_runner.installation.host_release import build_release, verify_release
from test_deployment_profile import profile

PASSWORD = b'test-only-enrollment-passphrase'
TOKEN = 'synthetic-deployment-token-' + 'x' * 48


@pytest.fixture
def setup(tmp_path):
    authorities = tmp_path / 'authorities'
    pins = certs.create_authorities(authorities, 'alpha-migration', PASSWORD)
    def ocid(kind, suffix):
        return 'ocid1.' + kind + '.oc1..' + 'synthetic' * 3 + suffix
    plan = HostPlan(deployment=profile(), runtime_image='sha256:' + 'a' * 64,
        tls_ca_sha256=pins['tls'], roles_ca_sha256=pins['roles'], worker_token_sha256=sha(TOKEN.encode()),
        nodes=[{'node_id': name, 'private_ip': address, 'instance_id': ocid('instance', name),
                'secret_id': ocid('vaultsecret', name)} for name, address in
               [('coordinator', '10.88.1.10'), ('worker-1', '10.88.1.20')]])
    return plan, authorities


def enrolled(setup, directory, node='worker-1'):
    plan, authorities = setup
    directory.mkdir()
    request = certs.request_credentials(plan, node, directory)
    signed = certs.sign_requests(plan, request, authorities, PASSWORD)
    bundle = certs.assemble(plan, node, signed, directory, TOKEN)
    return bundle, request, signed


def trust_bytes(authorities):
    return [(authorities / (kind + '-ca.crt')).read_bytes() for kind in ('tls', 'roles')]


def test_plan_scope_unique_roles_and_no_lab_values(setup):
    plan, authorities = setup
    assert plan.revision == HostPlan.model_validate_json(plan.canonical()).revision
    policy = credential_policy(plan)
    assert '357633903375' not in json.dumps(policy) and '10.241.' not in plan.canonical().decode()
    for node, value in zip(plan.nodes, policy['nodes']):
        assert node.instance_id in value['matching_rule'] and node.secret_id in value['steady_state_policy']
        assert 'SECRET_READ' in value['temporary_enrollment_policy'] and 'SECRET_UPDATE' in value['temporary_enrollment_policy']
        assert 'SECRET_DELETE' not in value['temporary_enrollment_policy']
    bad = plan.model_dump(); bad['nodes'][1]['secret_id'] = bad['nodes'][0]['secret_id']
    with pytest.raises(ValidationError):
        HostPlan.model_validate(bad)
    bad = plan.model_dump(); bad['nodes'][1]['private_ip'] = '8.8.8.8'
    with pytest.raises(ValidationError):
        HostPlan.model_validate(bad)


@pytest.mark.parametrize('node', ['coordinator', 'worker-1'])
def test_node_local_keys_public_request_signing_and_expiry(setup, tmp_path, node):
    plan, authorities = setup
    directory = tmp_path / node
    bundle, request, signed = enrolled(setup, directory, node)
    assert 'PRIVATE KEY' not in json.dumps(request) + json.dumps(signed)
    assert 'ENCRYPTED PRIVATE KEY' in (authorities / 'tls-ca.key').read_text()
    before = (directory / 'tls.key').read_bytes()
    certs.request_credentials(plan, node, directory)
    assert (directory / 'tls.key').read_bytes() == before
    expiry = certs.validate_bundle(plan, node, bundle, *trust_bytes(authorities))
    assert datetime.now(timezone.utc) + timedelta(hours=23) < expiry < datetime.now(timezone.utc) + timedelta(hours=25)
    with pytest.raises(InstallError):
        certs.validate_bundle(plan, node, bundle, *trust_bytes(authorities), now=expiry)


@pytest.mark.parametrize('mutation', ['token', 'node', 'plan', 'key', 'ca', 'server', 'extra'])
def test_bundle_rejects_identity_key_and_purpose_confusion(setup, tmp_path, mutation):
    plan, authorities = setup
    bundle, _, _ = enrolled(setup, tmp_path / 'keys', 'coordinator')
    roots = trust_bytes(authorities)
    if mutation == 'token': bundle['worker_token'] = 'other' * 20
    if mutation == 'node': bundle['node_id'] = 'worker-1'
    if mutation == 'plan': bundle['plan_revision'] = 'b' * 64
    if mutation == 'key': bundle['credentials']['tls']['private_key'] = bundle['credentials']['roles']['private_key']
    if mutation == 'ca': roots.reverse()
    if mutation == 'server': bundle['credentials']['database'] = bundle['credentials']['tls']
    if mutation == 'extra': bundle['extra'] = 'SENSITIVE_SENTINEL'
    with pytest.raises((InstallError, ValueError)):
        certs.validate_bundle(plan, 'coordinator', bundle, *roots)


def test_signer_rejects_wrong_request_plan_subject_and_password(setup, tmp_path):
    plan, authorities = setup
    _, request, _ = enrolled(setup, tmp_path / 'keys')
    bad = deepcopy(request); bad['plan_revision'] = 'b' * 64
    with pytest.raises(InstallError): certs.sign_requests(plan, bad, authorities, PASSWORD)
    bad = deepcopy(request); bad['node_id'] = 'coordinator'
    with pytest.raises(InstallError): certs.sign_requests(plan, bad, authorities, PASSWORD)
    with pytest.raises(ValueError): certs.sign_requests(plan, request, authorities, b'wrong-passphrase')


class Vault:
    def __init__(self, plan, node='worker-1'):
        self.secret = plan.node(node).secret_id
        self.current = {'format': 'migration-enrollment-pending/v1', 'plan_revision': plan.revision, 'node_id': node}
        self.version = 1; self.writes = 0; self.race = False; self.bad_readback = False

    def get_secret(self, secret):
        assert secret == self.secret
        return NS(data=NS(current_version_number=self.version), headers={'etag': str(self.version)})

    def get_secret_bundle(self, secret, stage):
        assert secret == self.secret and stage == 'CURRENT'
        content = encoded({'wrong': True}) if self.bad_readback and self.writes else encoded(self.current)
        return NS(data=NS(version_number=self.version, secret_bundle_content=NS(content_type='BASE64', content=base64.b64encode(content).decode())))

    def update_secret(self, secret, details, if_match):
        assert secret == self.secret and if_match == str(self.version)
        if self.race: raise RuntimeError('synthetic provider error SENSITIVE_SENTINEL')
        self.current = json.loads(base64.b64decode(details.secret_content.content))
        self.version += 1; self.writes += 1


def test_vault_first_enrollment_idempotency_explicit_renewal_and_unknown_refusal(setup, tmp_path):
    plan, authorities = setup
    bundle, _, _ = enrolled(setup, tmp_path / 'keys')
    vault = Vault(plan)
    result = certs.enroll(plan, 'worker-1', bundle, vault, vault, *trust_bytes(authorities))
    assert result['enrolled'] and vault.writes == 1
    assert certs.enroll(plan, 'worker-1', bundle, vault, vault, *trust_bytes(authorities)) == result
    assert vault.writes == 1
    replacement, _, _ = enrolled(setup, tmp_path / 'replacement')
    with pytest.raises(InstallError): certs.enroll(plan, 'worker-1', replacement, vault, vault, *trust_bytes(authorities))
    certs.enroll(plan, 'worker-1', replacement, vault, vault, *trust_bytes(authorities), replace_digest=result['credential_digest'])
    assert vault.writes == 2
    vault.current = {'unrelated': 'SENSITIVE_SENTINEL'}
    with pytest.raises(InstallError): certs.enroll(plan, 'worker-1', bundle, vault, vault, *trust_bytes(authorities), replace_digest=sha(encoded(vault.current)))
    assert vault.writes == 2


@pytest.mark.parametrize('mode', ['race', 'readback'])
def test_vault_race_or_bad_readback_never_reports_success(setup, tmp_path, mode):
    plan, authorities = setup
    bundle, _, _ = enrolled(setup, tmp_path / 'keys')
    vault = Vault(plan)
    vault.race = mode == 'race'; vault.bad_readback = mode == 'readback'
    with pytest.raises((InstallError, RuntimeError)):
        certs.enroll(plan, 'worker-1', bundle, vault, vault, *trust_bytes(authorities))


def test_release_checksums_member_allowlist_and_sql_generation(setup):
    plan, authorities = setup
    value = build_release(plan, *trust_bytes(authorities), Path(__file__).resolve().parents[1])
    loaded, files = verify_release(value, sha(value))
    assert loaded == plan and SCHEMA.encode() in files['schema.sql']
    with pytest.raises(InstallError): verify_release(value, '0' * 64)
    forged = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(value)) as source, zipfile.ZipFile(forged, 'w') as target:
        for name in source.namelist(): target.writestr(name, source.read(name))
        target.writestr('../escape', 'not allowed')
    with pytest.raises(InstallError): verify_release(forged.getvalue(), sha(forged.getvalue()))


def test_cli_sanitizes_validation_and_never_overwrites_token(tmp_path, capsys):
    target = tmp_path / 'worker-token'
    assert main(['create-token', '--output', str(target)]) == 0
    first = target.read_bytes()
    assert first.decode() not in capsys.readouterr().out
    assert main(['create-token', '--output', str(target)]) == 2
    assert target.read_bytes() == first
    bad = tmp_path / 'bad.json'; bad.write_text('{"SENSITIVE_SENTINEL":"secret"}')
    assert main(['validate-plan', '--plan', str(bad)]) == 2
    assert 'SENSITIVE_SENTINEL' not in capsys.readouterr().err


def test_make_plan_and_distributable_source_archive(setup, tmp_path, capsys):
    from scripts.build_portable_installer import build
    import tarfile
    plan, authorities = setup
    deployment = tmp_path / 'deployment.json'; deployment.write_bytes(plan.deployment.canonical())
    nodes = tmp_path / 'nodes.json'; nodes.write_bytes(encoded([node.model_dump() for node in plan.nodes]))
    token = tmp_path / 'token'; token.write_text(TOKEN)
    output = tmp_path / 'host-plan.json'
    assert main(['make-plan', '--deployment', str(deployment), '--nodes', str(nodes), '--runtime-image', plan.runtime_image,
        '--authorities', str(authorities), '--token-file', str(token), '--output', str(output)]) == 0
    assert HostPlan.model_validate_json(output.read_bytes()) == plan
    assert TOKEN not in capsys.readouterr().out and TOKEN not in output.read_text()
    root = Path(__file__).resolve().parents[1]
    content = build(root)
    assert content == build(root) and len(content) < 5_000_000
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        names = archive.getnames()
        assert all('/.secrets/' not in name and '/.terraform/' not in name and not name.endswith(('.key', '.pem', '.env', '.tfstate')) for name in names)
        assert 'migration-installer/deploy/install-portable.sh' in names
        manifest = json.load(archive.extractfile('migration-installer/SOURCE_MANIFEST.json'))
        assert all(sha(archive.extractfile('migration-installer/' + name).read()) == digest for name, digest in manifest['files'].items())


def test_services_use_exact_nodes_pinned_images_and_no_automatic_worker_activation(setup, monkeypatch):
    plan, _ = setup
    monkeypatch.setattr(host, 'script_prefix', lambda: '/opt/migration-installer/bin/python -m migration_runner.installation.host_cli')
    files = host.postgres_files(plan)
    assert b'10.88.1.20/32' in files['pg_hba.conf'] and b'/30' not in files['pg_hba.conf']
    assert plan.common_name('worker-1', 'tls').encode() in files['pg_ident.conf']
    db = host.unit_files(plan, plan.node('coordinator'))[host.DB_UNIT].decode()
    assert 'ExecStartPre=' in db and ' restore\n' in db and host.POSTGRES in db
    worker = host.unit_files(plan, plan.node('worker-1'))['migration-portable-worker@.service'].decode()
    assert 'worker --job %i' in worker and '[Install]' not in worker


@pytest.mark.parametrize('wrong_field', [None, 'id', 'compartmentId', 'canonicalRegionName', 'address'])
def test_actual_host_identity_checks_instance_region_compartment_and_address(setup, monkeypatch, wrong_field):
    plan, _ = setup
    node = plan.node('worker-1')
    data = {'id': node.instance_id, 'compartmentId': plan.deployment.destination.compartment_ocid,
            'canonicalRegionName': plan.deployment.destination.region}
    if wrong_field and wrong_field != 'address': data[wrong_field] = 'different'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'http://169.254.169.254/opc/v2/instance/' and timeout == 5
            return io.BytesIO(encoded(data))
    monkeypatch.setattr(host.urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(host, 'command', lambda _: encoded([{'addr_info': [{'local': '10.99.99.99' if wrong_field == 'address' else node.private_ip}]}]))
    if wrong_field:
        with pytest.raises(InstallError): host.instance_identity(plan, node)
    else:
        host.instance_identity(plan, node)


def test_bootstrap_is_one_transaction_without_plaintext_token(setup):
    plan, _ = setup
    value = host.bootstrap_sql(plan, TOKEN, 'BEGIN;\nCREATE TABLE example (id int);\nCOMMIT;\n', '-- grants')
    assert 'BEGIN;' not in value and 'COMMIT;' not in value
    assert TOKEN not in value and plan.worker_token_sha256 in value
    assert 'ON CONFLICT' not in value and 'portable_installation_identity' in value
    with pytest.raises(InstallError): host.bootstrap_sql(plan, 'wrong', '', '')


@pytest.fixture
def host_paths(monkeypatch):
    import shutil
    from uuid import uuid4
    if os.name != 'posix' or os.geteuid() != 0:
        pytest.skip('Disposable Linux root container required for real ownership/permission checks')
    root = Path('/root') / ('portable-installer-test-' + uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        for name in ('config', 'state', 'runtime', 'units'):
            (root / name).mkdir(mode=0o700)
        for variable, name in [('CONFIG', 'config'), ('STATE', 'state'), ('RUNTIME', 'runtime'), ('UNITS', 'units')]:
            monkeypatch.setattr(host, variable, root / name)
        monkeypatch.setattr(host, 'instance_identity', lambda *_: None)
        yield root
    finally:
        shutil.rmtree(root)


def test_linux_install_retry_drift_unowned_database_and_host_identity(setup, tmp_path, host_paths, monkeypatch):
    plan, authorities = setup
    release = build_release(plan, *trust_bytes(authorities), Path(__file__).resolve().parents[1])
    archive = tmp_path / 'release.zip'; archive.write_bytes(release)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ['podman', 'image', 'inspect']: return plan.runtime_image.encode()
        if args[0] == 'findmnt': return b'tmpfs\n'
        if args[-2:] == ['-u', 'postgres']: return b'999\n'
        return b''
    monkeypatch.setattr(host, 'command', run)
    result = host.install(archive, sha(release), 'coordinator')
    assert result['installed'] and not result['services_started']
    assert host.load()[0] == plan
    assert not any('start' in call or 'enable' in call for call in calls)
    checkpoint = host.STATE / 'checkpoint.json'
    checkpoint.write_bytes(encoded({'stage': 'ledger-ready'}))
    host.install(archive, sha(release), 'coordinator')
    assert json.loads(checkpoint.read_bytes())['stage'] == 'ledger-ready'
    with pytest.raises(InstallError): host.install(archive, sha(release), 'worker-1')
    (host.CONFIG / 'schema.sql').write_text('unexpected drift')
    with pytest.raises(InstallError): host.install(archive, sha(release), 'coordinator')
    assert not any('start' in call for call in calls)


def test_linux_restore_ownership_tmpfs_files_and_no_partial_ready_marker(setup, tmp_path, host_paths, monkeypatch):
    plan, authorities = setup
    bundle, _, _ = enrolled(setup, tmp_path / 'keys', 'coordinator')
    for kind in ('tls', 'roles'):
        (host.CONFIG / (kind + '-ca.crt')).write_bytes((authorities / (kind + '-ca.crt')).read_bytes())
    monkeypatch.setattr(host, 'command', lambda args, **_: b'tmpfs\n' if args[0] == 'findmnt' else b'')
    vault = Vault(plan, 'coordinator'); vault.current = bundle
    host.restore(plan, plan.node('coordinator'), vault)
    assert host.ready(plan, plan.node('coordinator'))['plan_revision'] == plan.revision
    assert (host.RUNTIME / 'client/tls.key').stat().st_uid == 10001
    assert (host.RUNTIME / 'database/server.key').stat().st_uid == 999
    assert (host.RUNTIME / 'client/runtime.env').stat().st_mode & 0o777 == 0o600
    host.restore(plan, plan.node('coordinator'), vault)
    (host.RUNTIME / 'client/tls.key').unlink()
    (host.RUNTIME / 'client/tls.key').symlink_to(host.CONFIG / 'roles-ca.crt')
    original = (host.CONFIG / 'roles-ca.crt').read_bytes()
    with pytest.raises(InstallError): host.restore(plan, plan.node('coordinator'), vault)
    assert not (host.RUNTIME / 'ready.json').exists()
    assert (host.CONFIG / 'roles-ca.crt').read_bytes() == original


def test_linux_refuses_existing_unowned_ledger(setup, host_paths, monkeypatch):
    plan, _ = setup
    (host.STATE / 'postgres').mkdir()
    commands = []
    monkeypatch.setattr(host, 'command', lambda *args, **kwargs: commands.append(args))
    with pytest.raises(InstallError): host.initialize(plan, plan.node('coordinator'))
    assert commands == []


def test_linux_partial_init_is_preserved_and_never_reset(setup, host_paths, monkeypatch):
    plan, _ = setup
    receipt = {'release_sha256': 'b' * 64}
    (host.CONFIG / 'receipt.json').write_bytes(encoded(receipt))
    (host.STATE / 'database-owner.json').write_bytes(encoded({'plan_revision': plan.revision, **receipt}))
    directory = host.STATE / 'postgres'; directory.mkdir(); os.chown(directory, 999, 999)
    (directory / 'partial-file').write_bytes(b'preserve-me')
    commands = []
    monkeypatch.setattr(host, 'command', lambda *args, **kwargs: commands.append(args))
    with pytest.raises(InstallError): host.initialize(plan, plan.node('coordinator'))
    assert (directory / 'partial-file').read_bytes() == b'preserve-me' and commands == []


@pytest.mark.parametrize('matching_identity', [True, False])
def test_linux_existing_ledger_is_verified_without_bootstrap(setup, host_paths, monkeypatch, matching_identity):
    plan, _ = setup
    receipt = {'release_sha256': 'b' * 64}
    (host.CONFIG / 'receipt.json').write_bytes(encoded(receipt))
    (host.STATE / 'database-owner.json').write_bytes(encoded({'plan_revision': plan.revision, **receipt}))
    data = host.STATE / 'postgres'; data.mkdir(); os.chown(data, 999, 999)
    (data / 'PG_VERSION').write_text('17\n')
    calls, queries = [], []
    monkeypatch.setattr(host, 'command', lambda args, **kwargs: calls.append(args) or b'')
    def query(statement, database='postgres'):
        queries.append(statement)
        if 'pg_database' in statement: return '1'
        if 'pg_tables' in statement: return '12'
        if 'portable_installation_identity' in statement: return plan.revision if matching_identity else 'different'
        if 'alembic_version' in statement: return SCHEMA
        if 'runtime_security' in statement: return plan.worker_token_sha256
        pytest.fail('Unexpected SQL mutation')
    monkeypatch.setattr(host, 'sql', query)
    if matching_identity:
        assert host.initialize(plan, plan.node('coordinator'))['ledger_ready']
        assert json.loads((host.STATE / 'checkpoint.json').read_bytes())['stage'] == 'ledger-ready'
    else:
        with pytest.raises(InstallError): host.initialize(plan, plan.node('coordinator'))
    assert all(statement.startswith('SELECT ') for statement in queries)
    assert not any('createdb' in call or 'initdb' in ' '.join(call) for call in calls)
