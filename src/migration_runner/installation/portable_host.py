"""Host services for the portable UI, dispatcher and managed renewal timer."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
from uuid import UUID

from . import host_runtime as host, managed_renewal
from .host_files import InstallError, atomic, encoded, exact, mkdir, protected, read, sha

UI_UNIT = 'migration-portable-ui.service'


def units(plan,node):
    launch = host.script_prefix()
    result = {}
    if plan.ui:
        if node.node_id=='coordinator':
            result[UI_UNIT] = (f'[Unit]\nDescription=Private migration operator UI\nRequires={host.DB_UNIT}\nAfter={host.DB_UNIT}\n'
                f'[Service]\nExecStart={launch} serve-ui\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=45\nUMask=0077\nLimitCORE=0\n'
                '[Install]\nWantedBy=multi-user.target\n').encode()
        else:
            result.update(timer('dispatch',launch,15))
    if plan.managed_certificates:
        result.update(timer('renew',launch,300))
    return result


def timer(action,launch,seconds):
    name='migration-portable-'+action
    return {name+'.service': (f'[Unit]\nDescription=Migration {action}\nWants=network-online.target\nAfter=network-online.target\n'
        f'[Service]\nType=oneshot\nExecStart={launch} {action}\nTimeoutStartSec=240\nUMask=0077\nLimitCORE=0\n').encode(),
        name+'.timer': (f'[Unit]\nDescription=Migration {action} timer\n[Timer]\nOnBootSec=60\nOnUnitInactiveSec={seconds}\n'
        'AccuracySec=5\nPersistent=true\n[Install]\nWantedBy=timers.target\n').encode()}


def container(plan, suffix):
    return ['podman','run','--rm','--name','migration-portable-'+suffix,'--pull=never','--network=host','--user','10001:10001',
        '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--memory=1g','--cpus=1','--pids-limit=128',
        '--tmpfs=/tmp:rw,noexec,nosuid,size=64m','--env-file',str(host.RUNTIME/'client/runtime.env'),
        '-v',str(host.RUNTIME/'client')+':/run/migration:ro,z',
        '-v',str(host.CONFIG/'deployments')+':/etc/migration/deployments:ro,z',
        '-v',str(host.CONFIG/'plan.json')+':/etc/migration/host-plan.json:ro,z']


def worker_busy():
    from .host_files import lock
    try:
        with lock(host.STATE,'worker-operation.lock'): return False
    except InstallError: return True


def activate(plan,node,reference=None):
    if not plan.ui: raise InstallError('UI is not enabled in this host plan.')
    if node.node_id=='coordinator':
        if host.sql('SELECT plan_revision FROM portable_installation_identity WHERE id=1','migration')!=plan.revision:
            raise InstallError('Initialize the matching ledger before UI activation.')
        target=host.CONFIG/'reference.json'
        raw=read(reference if reference else target,262144)
        if sha(raw)!=plan.ui.reference_sha256:
            raise InstallError('Reference inventory differs from the approved UI plan.')
        exact(target,raw,0o644)
        token=host.STATE/'operator-token'
        if not token.exists():
            atomic(token,secrets.token_urlsafe(48).encode())
        protected(token)
        if token.stat().st_mode & 0o077: raise InstallError('Operator token must be private.')
        host.command(['systemctl','enable','--now',UI_UNIT])
    else:
        host.restore(plan,node)
        host.command(['systemctl','enable','--now','migration-portable-dispatch.timer'])
    if plan.managed_certificates:
        host.command(['systemctl','enable','--now','migration-portable-renew.timer'])
    return {'activated':True,'node_id':node.node_id,'ui_url':'http://127.0.0.1:8787' if node.node_id=='coordinator' else None,
            'operator_token_file':str(host.STATE/'operator-token') if node.node_id=='coordinator' else None}


def serve(plan,node):
    if not plan.ui or node.node_id!='coordinator': raise InstallError('Coordinator UI is not enabled.')
    host.ready(plan,node)
    # Persistent operator token remains root-only. A separate tmpfs copy is mapped
    # into the UI, never into a worker or a migration command container.
    directory=host.RUNTIME/'operator'
    directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink(): raise InstallError('Unsafe operator credential directory.')
    atomic(directory/'token',read(host.STATE/'operator-token',256))
    os.chown(directory,10001,10001); os.chown(directory/'token',10001,10001)
    health=mkdir(host.STATE/'health'); health.chmod(0o755)
    args=container(plan,'ui')+['-v',str(directory)+':/run/operator:ro,z',
        '-v',str(health)+':/run/health:ro,z',
        '-v',str(host.CONFIG/'reference.json')+':/etc/migration/reference.json:ro,z',plan.runtime_image,
        'python','-m','uvicorn','migration_runner.portable_ui:create_app','--factory','--host','127.0.0.1','--port','8787',
        '--no-access-log','--no-proxy-headers']
    return subprocess.call(args)


def dispatch(plan,node):
    if not plan.ui or node.node_id=='coordinator': raise InstallError('Worker dispatch is not enabled.')
    names=host.command(['podman','ps','--format','{{.Names}}']).decode().splitlines()
    active=worker_busy() or any(name.startswith('migration-portable-worker-') for name in names)
    if not active:
        host.restore(plan,node)
    host.ready(plan,node)
    raw=host.command(container(plan,'poll')+[plan.runtime_image,'python','-m','migration_runner.portable_agent',
        '--node',node.node_id,'--state','active' if active else 'inactive'])
    result=json.loads(raw)
    if result.get('action')=='run' and not active:
        job=str(UUID(result['job_id']))
        host.command(['systemctl','start','--no-block','migration-portable-worker@'+job+'.service'])
    return result


def renew(plan,node,token_file=None):
    if not plan.managed_certificates: raise InstallError('Managed renewal is not configured.')
    directory=mkdir(host.STATE/'enrollment')
    token=None
    if token_file:
        protected(token_file)
        if token_file.stat().st_mode & 0o077: raise InstallError('Token file must be private.')
        token=read(token_file,256).decode().strip()
    reader,writer=host.clients(plan)
    management,certificates=managed_renewal.sdk(plan)
    health=mkdir(host.STATE/'health'); health.chmod(0o755)
    status=health/'renewal-status.json'
    try:
        result=managed_renewal.renew(plan,node,directory,read(host.CONFIG/'tls-ca.crt'),read(host.CONFIG/'roles-ca.crt'),
                                   reader,writer,management,certificates,token)
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        try:
            active_digest=host.ready(plan,node)['credential_digest']
        except (ValueError,OSError,KeyError): active_digest=None
        if active_digest==result['credential_digest']:
            result['activation']='current'
        else:
            result['activation']=activate_credentials(plan,node)
        atomic(status,encoded(result),0o644)
        return result
    except managed_renewal.Pending:
        result={'state':'issuance_pending','checked_at':datetime.now(timezone.utc).isoformat()}
        atomic(status,encoded(result),0o644); return result
    except Exception:
        atomic(status,encoded({'state':'attention_required','checked_at':datetime.now(timezone.utc).isoformat(),
                              'message':'Renewal failed; inspect managed certificate permissions, issuer validity and pending requests.'}),0o644)
        raise


def activate_credentials(plan,node):
    # Respect the same host lock used by dispatch/restore/worker startup. Caller
    # serializes this function against those short operations.
    names=host.command(['podman','ps','--format','{{.Names}}']).decode().splitlines()
    if worker_busy() or any(name.startswith('migration-portable-worker-') or name=='migration-portable-cli' for name in names):
        return 'waiting_for_idle'
    if node.node_id!='coordinator':
        host.restore(plan,node)
        return 'activated'
    ui_active='migration-portable-ui' in names
    db_active=host.DB_CONTAINER in names
    if not db_active:
        host.restore(plan,node)
        return 'activated'
    if ui_active: host.command(['systemctl','stop',UI_UNIT])
    try:
        # UI is stopped before checking admission state, preventing a new UI run
        # from racing the ledger restart. CLI is serialized by the host lock.
        active=host.sql("SELECT count(*) FROM migrations WHERE execution IS NOT NULL AND control='active' AND run_deadline>clock_timestamp() AND scheduled_bytes<coalesce((run_limits->>'max_scheduled_bytes')::bigint,0) AND EXISTS (SELECT 1 FROM batches WHERE migration_id=migrations.id AND state NOT IN ('verified','failed','source_changed','cancelled'))",'migration')
        running=host.sql("SELECT count(*) FROM batches WHERE state='running'",'migration')
        if active!='0' or running!='0': return 'waiting_for_idle'
        host.command(['systemctl','stop',host.DB_UNIT])
        host.command(['systemctl','start',host.DB_UNIT])
        return 'activated'
    finally:
        if ui_active: host.command(['systemctl','start',UI_UNIT])
