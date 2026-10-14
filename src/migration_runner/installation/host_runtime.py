"""Portable root-owned host installer and on-demand, bounded pilot services.

No cloud transfer or worker starts during installation or credential enrollment.
The lab runtime uses different paths and is deliberately not upgraded here.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import urllib.request
from uuid import UUID

from .deployment import publish_profile
from .enrollment import decode_secret, validate_bundle
from .host_files import InstallError, atomic, encoded, exact, lock, mkdir, protected, read, root_only, sha
from .host_plan import HostPlan, POSTGRES, SCHEMA
from .host_release import verify_release

CONFIG = Path('/etc/migration-portable')
STATE = Path('/var/lib/migration-portable')
RUNTIME = Path('/run/migration-portable')
UNITS = Path('/etc/systemd/system')
DB_UNIT = 'migration-portable-db.service'
DB_CONTAINER = 'migration-portable-postgres'


def command(args, data=None, timeout=120):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Provider errors, SQL parameters and helper output may contain credentials.
        raise InstallError('A host command failed; inspect service health and the installation checkpoint.')
    return result.stdout


def instance_identity(plan, node):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request('http://169.254.169.254/opc/v2/instance/', headers={'Authorization': 'Bearer Oracle'})
    with opener.open(request, timeout=5) as response:
        data = response.read(32769)
    if len(data) > 32768:
        raise InstallError('Instance metadata exceeds its limit.')
    identity = json.loads(data)
    if (identity.get('id') != node.instance_id or identity.get('compartmentId') != plan.deployment.destination.compartment_ocid
            or identity.get('canonicalRegionName') != plan.deployment.destination.region):
        raise InstallError('This host does not match the planned OCI instance, compartment and region.')
    addresses = json.loads(command(['ip', '-j', '-4', 'address', 'show']))
    if node.private_ip not in {a['local'] for interface in addresses for a in interface.get('addr_info', [])}:
        raise InstallError('This host does not have its planned private address.')


def clients(plan):
    import oci
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = {'region': plan.deployment.destination.region}
    options = dict(signer=signer, timeout=(10, 30), retry_strategy=oci.retry.NoneRetryStrategy())
    return oci.secrets.SecretsClient(config, **options), oci.vault.VaultsClient(config, **options)


def load():
    root_only()
    protected(CONFIG, directory=True); protected(STATE, directory=True)
    for name in ('plan.json', 'node-id', 'tls-ca.crt', 'roles-ca.crt', 'receipt.json'):
        protected(CONFIG / name)
    plan = HostPlan.model_validate_json(read(CONFIG / 'plan.json'))
    node = plan.node(read(CONFIG / 'node-id').decode().strip())
    receipt = json.loads(read(CONFIG / 'receipt.json'))
    if receipt['plan_revision'] != plan.revision or receipt['node_id'] != node.node_id:
        raise InstallError('Installed plan differs from its receipt.')
    return plan, node


def script_prefix():
    executable = Path(sys.executable).absolute()
    # A venv executable is normally a symlink. Validate the resolved binary plus
    # the entire venv location; executable paths must be safe in systemd syntax.
    protected(executable.resolve())
    protected(executable.parent, directory=True)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(executable)):
        raise InstallError('Install the Python environment under a simple protected absolute path.')
    return str(executable) + ' -m migration_runner.installation.host_cli'


def unit_files(plan, node):
    launcher = script_prefix()
    if node.node_id != 'coordinator':
        return {'migration-portable-worker@.service': (
            '[Unit]\nDescription=Migration worker for reviewed pilot %i\nWants=network-online.target\nAfter=network-online.target\n'
            '[Service]\nType=simple\nExecStart=' + launcher + ' worker --job %i\n'
            'Restart=no\nTimeoutStopSec=45\nKillMode=control-group\nUMask=0077\n') .encode()}
    mounts = (f'-v {STATE}/postgres:/var/lib/postgresql/data:Z '
              f'-v {CONFIG}/postgres:/etc/postgresql:ro,Z -v {RUNTIME}/database:/run/tls:ro,Z ')
    return {DB_UNIT: ('[Unit]\nDescription=Migration certificate-authenticated ledger\nWants=network-online.target\nAfter=network-online.target\n'
        '[Service]\nType=simple\nExecStartPre=' + launcher + ' restore\n'
        'ExecStart=/usr/bin/podman run --rm --name ' + DB_CONTAINER + ' --pull=never --network=host --user 999:999 '
        '--read-only --cap-drop=ALL --security-opt=no-new-privileges --memory=4g --cpus=2 --pids-limit=256 '
        '--tmpfs=/tmp:rw,noexec,nosuid,size=64m --tmpfs=/var/run/postgresql:rw,noexec,nosuid,mode=1777,size=16m '
        + mounts + '--entrypoint /usr/lib/postgresql/17/bin/postgres ' + POSTGRES
        + ' -D /var/lib/postgresql/data -c config_file=/etc/postgresql/postgresql.conf '
        '-c hba_file=/etc/postgresql/pg_hba.conf -c ident_file=/etc/postgresql/pg_ident.conf\n'
        'ExecStop=/usr/bin/podman stop --time 30 ' + DB_CONTAINER + '\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=45\nUMask=0077\n'
        '[Install]\nWantedBy=multi-user.target\n').encode()}


def postgres_files(plan):
    coordinator = plan.node('coordinator')
    hba = ['local all postgres peer', 'hostnossl all all 0.0.0.0/0 reject']
    identities = []
    for node in plan.nodes:
        role = 'migration_coordinator' if node.node_id == 'coordinator' else 'migration_worker'
        hba.append(f'hostssl migration {role} {node.private_ip}/32 cert map=migration')
        identities.append(f'migration {plan.common_name(node.node_id, "tls")} {role}')
    hba.append('hostssl all all 0.0.0.0/0 reject')
    return {'pg_hba.conf': ('\n'.join(hba) + '\n').encode(),
            'pg_ident.conf': ('\n'.join(identities) + '\n').encode(),
            'postgresql.conf': (f"listen_addresses = '{coordinator.private_ip}'\nport = 5432\nssl = on\n"
                "ssl_min_protocol_version = 'TLSv1.2'\nssl_cert_file = '/run/tls/server.crt'\n"
                "ssl_key_file = '/run/tls/server.key'\nssl_ca_file = '/run/tls/tls-ca.crt'\n"
                "log_statement = 'none'\nlog_min_error_statement = 'panic'\n").encode()}


def install(release, expected_sha, node_id):
    root_only()
    if platform.machine() != 'x86_64' or Path('/etc/migration/image-id').exists():
        raise InstallError('Use a fresh x86_64 OCI host; in-place lab conversion is not supported.')
    plan, files = verify_release(read(release, 2_000_000), expected_sha)
    node = plan.node(node_id)
    instance_identity(plan, node)
    actual = command(['podman', 'image', 'inspect', '--format', '{{.Id}}', plan.runtime_image]).decode().strip()
    if 'sha256:' + actual.removeprefix('sha256:') != plan.runtime_image:
        raise InstallError('Load the reviewed runtime image before installation.')
    if command(['findmnt', '-n', '-o', 'FSTYPE', '/run']).strip() != b'tmpfs':
        raise InstallError('Runtime credentials require /run on tmpfs.')
    mkdir(CONFIG); mkdir(STATE)
    receipt = {'format': 'migration-install-receipt/v1', 'plan_revision': plan.revision,
               'release_sha256': expected_sha, 'node_id': node_id}
    with lock(STATE):
        # An existing unmanaged data directory is never adopted, including empty
        # directories: it may belong to a different deployment.
        if not (CONFIG / 'receipt.json').exists() and (STATE / 'postgres').exists():
            raise InstallError('Existing ledger storage is not owned by this installation.')
        exact(CONFIG / 'receipt.json', encoded(receipt))
        for name, content in files.items():
            exact(CONFIG / name, content)
        # Nonsecret plan is mounted directly into the unprivileged UI/agent.
        (CONFIG/'plan.json').chmod(0o644)
        exact(CONFIG / 'node-id', node_id.encode())
        deployment_dir = mkdir(CONFIG / 'deployments')
        deployment_dir.chmod(0o755)
        publish_profile(plan.deployment, deployment_dir)
        if node_id == 'coordinator':
            directory = mkdir(CONFIG / 'postgres')
            for name, content in postgres_files(plan).items():
                exact(directory / name, content, 0o644)
            # Directory is intentionally readable only by root until container
            # mount traversal, where the mount point itself must permit postgres.
            directory.chmod(0o755)
            command(['podman', 'pull', POSTGRES], timeout=600)
            if command(['podman', 'run', '--rm', '--network=none', '--entrypoint', 'id', POSTGRES, '-u', 'postgres']).strip() != b'999':
                raise InstallError('PostgreSQL image user differs from the qualified release.')
        for name, content in unit_files(plan, node).items():
            exact(UNITS / name, content, 0o644)
        from .portable_host import units
        for name, content in units(plan,node).items():
            exact(UNITS/name,content,0o644)
        command(['systemctl', 'daemon-reload'])
        if not (STATE / 'checkpoint.json').exists():
            atomic(STATE / 'checkpoint.json', encoded({**receipt, 'stage': 'installed-awaiting-enrollment', 'services_started': False}))
    return {'installed': True, 'node_id': node_id, 'plan_revision': plan.revision, 'services_started': False}


def stopped():
    # No generation switch while a database/client has credentials mounted.
    names = command(['podman', 'ps', '--format', '{{.Names}}']).decode().splitlines()
    if any(name.startswith('migration-portable-') for name in names):
        raise InstallError('Stop portable database and worker/client containers before credential restoration.')


def restore(plan, node, reader=None):
    stopped()
    if command(['findmnt', '-n', '-o', 'FSTYPE', '/run']).strip() != b'tmpfs':
        raise InstallError('Runtime credential directory must reside on tmpfs.')
    reader = reader or clients(plan)[0]
    bundle = decode_secret(reader.get_secret_bundle(node.secret_id, stage='CURRENT'))
    tls_ca, roles_ca = read(CONFIG / 'tls-ca.crt'), read(CONFIG / 'roles-ca.crt')
    expiry = validate_bundle(plan, node.node_id, bundle, tls_ca, roles_ca)
    root = mkdir(RUNTIME)
    # Fixed owned subdirectories; replacement is file-atomic and occurs only while
    # services are stopped. The ready marker is written last.
    ready = root / 'ready.json'; ready.unlink(missing_ok=True)
    runtime = root / 'client'
    if runtime.exists():
        # Owned by the runtime UID after preparation. Parent remains root-only.
        if runtime.is_symlink() or runtime.stat().st_uid not in (0, 10001):
            raise InstallError('Unexpected credential directory owner.')
    else:
        runtime.mkdir(mode=0o700)
    values = {'tls-ca.crt': tls_ca,
        'tls.crt': bundle['credentials']['tls']['certificate'].encode(),
        'tls.key': bundle['credentials']['tls']['private_key'].encode(),
        'roles-anywhere.crt': bundle['credentials']['roles']['certificate'].encode(),
        'roles-anywhere.key': bundle['credentials']['roles']['private_key'].encode(),
        'aws-config': plan.deployment.aws_config().encode()}
    values['expiry.json'] = encoded({'expires_at':expiry.isoformat()})
    profile = '/etc/migration/deployments/' + plan.deployment.revision + '.json'
    environment = {'MIGRATION_ENVIRONMENT': 'cloud-cli' if node.node_id == 'coordinator' else 'cloud-worker',
        'MIGRATION_DB_HOST': plan.node('coordinator').private_ip, 'MIGRATION_DB_PORT': '5432',
        'MIGRATION_DB_NAME': 'migration', 'MIGRATION_DB_USER': 'migration_coordinator' if node.node_id == 'coordinator' else 'migration_worker',
        'MIGRATION_DB_PASSWORD': '', 'MIGRATION_DB_SSLROOTCERT': '/run/migration/tls-ca.crt',
        'MIGRATION_DB_SSLCERT': '/run/migration/tls.crt', 'MIGRATION_DB_SSLKEY': '/run/migration/tls.key',
        'MIGRATION_AWS_CONFIG_FILE': '/run/migration/aws-config', 'MIGRATION_WORKER_TOKEN': bundle['worker_token'],
        'MIGRATION_OPERATOR_ENABLED': 'false', 'MIGRATION_OPERATOR_TOKEN': '',
        'MIGRATION_DEPLOYMENT_PROFILE': profile, 'MIGRATION_DEPLOYMENT_REVISION': plan.deployment.revision}
    values['runtime.env'] = ''.join(k + '=' + v + '\n' for k, v in environment.items()).encode()
    for name, content in values.items():
        atomic(runtime / name, content); os.chown(runtime / name, 10001, 10001)
    os.chown(runtime, 10001, 10001)
    if node.node_id == 'coordinator':
        database = root / 'database'
        if database.is_symlink() or (database.exists() and database.stat().st_uid not in (0, 999)):
            raise InstallError('Unexpected database credential directory.')
        database.mkdir(mode=0o700, exist_ok=True)
        for name, content in {'server.crt': bundle['credentials']['database']['certificate'].encode(),
                              'server.key': bundle['credentials']['database']['private_key'].encode(), 'tls-ca.crt': tls_ca}.items():
            atomic(database / name, content); os.chown(database / name, 999, 999)
        os.chown(database, 999, 999)
    atomic(ready, encoded({'plan_revision': plan.revision, 'node_id': node.node_id,
                         'expires_at': expiry.isoformat(), 'credential_digest': sha(encoded(bundle))}))
    return bundle


def ready(plan, node):
    value = json.loads(read(RUNTIME / 'ready.json'))
    if value['plan_revision'] != plan.revision or value['node_id'] != node.node_id or datetime.fromisoformat(value['expires_at']) <= datetime.now(timezone.utc):
        raise InstallError('Restore current, valid node credentials before using the runtime.')
    return value


def sql(statement, database='postgres'):
    return command(['podman', 'exec', '-i', '--user', '999:999', DB_CONTAINER, 'psql', '-X', '-h', '/var/run/postgresql',
        '-U', 'postgres', '-d', database, '-v', 'ON_ERROR_STOP=1', '-At', '--single-transaction'], statement.encode()).decode().strip()


def bootstrap_sql(plan, token, schema, roles):
    if sha(token.encode()) != plan.worker_token_sha256:
        raise InstallError('Bootstrap token differs from the approved deployment.')
    # Alembic's offline transaction is replaced by the outer psql transaction so
    # schema, least-privilege roles, token and installation identity commit together.
    schema = re.sub(r'(?m)^(BEGIN|COMMIT);\s*$', '', schema)
    return (schema + '\n' + roles + '\n'
        "INSERT INTO runtime_security (id, worker_token_hash) VALUES (1, '" + plan.worker_token_sha256 + "');\n"
        'CREATE TABLE portable_installation_identity (id integer PRIMARY KEY CHECK (id=1), plan_revision text NOT NULL);\n'
        "INSERT INTO portable_installation_identity VALUES (1, '" + plan.revision + "');\n"
        'REVOKE ALL ON portable_installation_identity FROM PUBLIC, migration_coordinator, migration_worker;\n')


def initialize(plan, node):
    if node.node_id != 'coordinator':
        raise InstallError('Only the coordinator can initialize a ledger.')
    marker = STATE / 'database-owner.json'
    ownership = encoded({'plan_revision': plan.revision, 'release_sha256': json.loads(read(CONFIG / 'receipt.json'))['release_sha256']})
    data = STATE / 'postgres'
    if data.exists() and not marker.exists():
        raise InstallError('Refusing to initialize or adopt an unowned database directory.')
    exact(marker, ownership)
    if not data.exists():
        data.mkdir(mode=0o700); os.chown(data, 999, 999)
    if data.is_symlink() or data.stat().st_uid != 999:
        raise InstallError('Unexpected database storage path or owner.')
    if not (data / 'PG_VERSION').exists():
        if any(data.iterdir()):
            raise InstallError('Partial database initialization found. Preserve it for inspection; automatic reset is refused.')
        command(['podman', 'run', '--rm', '--network=none', '--user', '999:999', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '-v', str(data) + ':/var/lib/postgresql/data:Z', '--entrypoint', '/usr/lib/postgresql/17/bin/initdb', POSTGRES,
            '-D', '/var/lib/postgresql/data', '--auth-local=peer', '--auth-host=reject', '--encoding=UTF8', '--locale=C.UTF-8'])
    if read(data / 'PG_VERSION').strip() != b'17':
        raise InstallError('Existing database version is unsupported.')
    command(['systemctl', 'start', DB_UNIT])
    import time
    for attempt in range(30):
        try:
            command(['podman', 'exec', '--user', '999:999', DB_CONTAINER, 'pg_isready', '-h', '/var/run/postgresql', '-U', 'postgres'])
            break
        except InstallError:
            if attempt == 29:
                raise
            time.sleep(1)
    if sql("SELECT count(*) FROM pg_database WHERE datname='migration'") == '0':
        command(['podman', 'exec', '--user', '999:999', DB_CONTAINER, 'createdb', '-h', '/var/run/postgresql', '-U', 'postgres', 'migration'])
    tables = sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'", 'migration')
    if tables == '0':
        # Credentials were restored by the DB unit. Token is read privately,
        # checked against the plan, and never appears in subprocess arguments.
        env = read(RUNTIME / 'client/runtime.env').decode().splitlines()
        token = next(line.split('=', 1)[1] for line in env if line.startswith('MIGRATION_WORKER_TOKEN='))
        sql(bootstrap_sql(plan, token, read(CONFIG / 'schema.sql', 500000).decode(), read(CONFIG / 'database-roles.sql').decode()), 'migration')
    if sql("SELECT plan_revision FROM portable_installation_identity WHERE id=1", 'migration') != plan.revision or sql('SELECT version_num FROM alembic_version', 'migration') != SCHEMA or sql('SELECT worker_token_hash FROM runtime_security WHERE id=1', 'migration') != plan.worker_token_sha256:
        raise InstallError('Existing ledger identity, schema or token differs; no reinitialization was attempted.')
    command(['systemctl', 'enable', DB_UNIT])
    atomic(STATE / 'checkpoint.json', encoded({'stage': 'ledger-ready', 'plan_revision': plan.revision, 'schema': SCHEMA}))
    return {'ledger_ready': True, 'schema': SCHEMA, 'migration_started': False}


def execute(plan, node, arguments, job=None):
    if job is not None:
        if node.node_id == 'coordinator':
            raise InstallError('Worker execution requires a worker host.')
        job = str(UUID(job))
        with lock(STATE):
            restore(plan, node)
        arguments = ['run-loop', '--job', job, '--worker-id', node.node_id]
    elif node.node_id != 'coordinator':
        raise InstallError('Coordinator commands require the coordinator host.')
    ready(plan, node)
    # Only a fixed container command. CLI's own parser/scope/admission checks remain
    # authoritative. All paths supplied to it refer to the mounted exchange folder.
    exchange = STATE / 'exchange'
    if not exchange.exists():
        exchange.mkdir(mode=0o700); os.chown(exchange, 10001, 10001)
    if exchange.is_symlink() or exchange.stat().st_uid != 10001:
        raise InstallError('Unexpected coordinator exchange directory.')
    name = 'migration-portable-' + ('worker-' + job if job else 'cli')
    cmd = ['podman', 'run', '--rm', '--name', name, '--pull=never', '--network=host', '--user', '10001:10001',
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=4g', '--cpus=2', '--pids-limit=128',
        '--tmpfs=/tmp:rw,noexec,nosuid,size=128m', '--env-file', str(RUNTIME / 'client/runtime.env'),
        '-v', str(RUNTIME / 'client') + ':/run/migration:ro,z',
        '-v', str(CONFIG / 'deployments') + ':/etc/migration/deployments:ro,z',
        '-v', str(exchange) + ':/exchange:Z', plan.runtime_image, 'python', '-m', 'migration_runner.cloud_cli', *arguments]
    # cloud_cli only prints sanitized operation results. Never run a shell or let
    # caller arguments become Podman options. Forward termination to Podman.
    import signal
    child = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    def stop(*_):
        if child.poll() is None:
            child.terminate()
    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return child.wait()
    finally:
        stop()
        for sig, handler in old.items():
            signal.signal(sig, handler)
