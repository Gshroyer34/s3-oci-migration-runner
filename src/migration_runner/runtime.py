"""OCI worker lifecycle. No cloud access at import; secret values never logged."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class RuntimeErrorSafe(ValueError):
    pass


class Node(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    node_id: str = Field(pattern=r'^worker-[1-4]$')
    job_id: UUID
    secret_id: str = Field(pattern=r'^ocid1\.vaultsecret\.oc1\.iad\.[A-Za-z0-9.]+$')
    log_id: str = Field(pattern=r'^ocid1\.log\.oc1\.iad\.[A-Za-z0-9.]+$')
    trust_anchor_arn: str = Field(pattern=r'^arn:aws:rolesanywhere:us-east-1:357633903375:trust-anchor/[a-f0-9-]{36}$')
    profile_arn: str = Field(pattern=r'^arn:aws:rolesanywhere:us-east-1:357633903375:profile/[a-f0-9-]{36}$')
    role_arn: str = 'arn:aws:iam::357633903375:role/migration-lab-reader'

    @model_validator(mode='after')
    def scope(self):
        if self.role_arn != 'arn:aws:iam::357633903375:role/migration-lab-reader':
            raise ValueError('Only the lab reader role is permitted')
        return self


class Bundle(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    version: int = 1
    node_id: str
    tls_cert: SecretStr
    tls_key: SecretStr
    roles_cert: SecretStr
    roles_key: SecretStr
    worker_token: SecretStr


def read_bounded(path, maximum=131072):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum:
        raise RuntimeErrorSafe('Invalid runtime input file')
    with path.open('rb') as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise RuntimeErrorSafe('Runtime input exceeds limit')
    return value


def validate_pair(cert_pem, key_pem, ca_pem, common_name, now=None):
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PublicFormat
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    now = now or datetime.now(timezone.utc)
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        ca = x509.load_pem_x509_certificate(ca_pem)
        key = load_pem_private_key(key_pem.encode(), password=None)
        cert.verify_directly_issued_by(ca)
        if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError()
        if not ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise ValueError()
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError()
        if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
            raise ValueError()
        if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise ValueError()
        names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(names) != 1 or names[0].value != common_name:
            raise ValueError()
        if cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo):
            raise ValueError()
        if not ca.not_valid_before_utc <= now < ca.not_valid_after_utc or cert.not_valid_after_utc > ca.not_valid_after_utc:
            raise ValueError()
        if not cert.not_valid_before_utc <= now or cert.not_valid_after_utc <= now + timedelta(hours=1):
            raise ValueError()
        if cert.not_valid_after_utc - cert.not_valid_before_utc > timedelta(hours=25):
            raise ValueError()
        return cert.not_valid_after_utc
    except Exception:
        raise RuntimeErrorSafe('Certificate identity, key, chain or lifetime rejected') from None


def validate_bundle(bundle, node, trust_dir):
    bundle = Bundle.model_validate(bundle)
    if bundle.version != 1 or bundle.node_id != node.node_id:
        raise RuntimeErrorSafe('Secret belongs to a different node or format')
    token = bundle.worker_token.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,256}', token):
        raise RuntimeErrorSafe('Worker enrollment token is invalid')
    tls_ca = read_bounded(Path(trust_dir)/'tls-ca.crt')
    roles_ca = read_bounded(Path(trust_dir)/'roles-ca.crt')
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    if x509.load_pem_x509_certificate(tls_ca).fingerprint(hashes.SHA256()) == x509.load_pem_x509_certificate(roles_ca).fingerprint(hashes.SHA256()):
        raise RuntimeErrorSafe('AWS and transport certificate authorities must be separate')
    tls_expiry = validate_pair(bundle.tls_cert.get_secret_value(), bundle.tls_key.get_secret_value(), tls_ca, 'migration-worker')
    roles_expiry = validate_pair(bundle.roles_cert.get_secret_value(), bundle.roles_key.get_secret_value(), roles_ca, 'migration-worker')
    return bundle, tls_ca, min(tls_expiry, roles_expiry)


def sdk_clients(kind):
    import oci
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    cls = oci.secrets.SecretsClient if kind == 'secrets' else oci.loggingingestion.LoggingClient
    return cls({'region':'us-ashburn-1'}, signer=signer, timeout=(10,30), retry_strategy=oci.retry.NoneRetryStrategy())


def fetch_bundle(node, client=None):
    client = client or sdk_clients('secrets')
    result = client.get_secret_bundle(node.secret_id, stage='CURRENT').data
    content = result.secret_bundle_content
    if content.content_type != 'BASE64' or len(content.content) > 180000:
        raise RuntimeErrorSafe('Invalid secret payload format')
    payload = base64.b64decode(content.content, validate=True)
    if len(payload) > 131072:
        raise RuntimeErrorSafe('Secret exceeds runtime limit')
    return json.loads(payload)


def private_directory(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeErrorSafe('Symlinked runtime directories are forbidden')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def install_bundle(bundle, node, trust_dir, runtime_dir, owner=None):
    """Called before the worker starts; never replace an active mounted credential set."""
    bundle, ca, expiry = validate_bundle(bundle, node, trust_dir)
    root = private_directory(runtime_dir)
    staged = root/('staged-'+uuid4().hex)
    staged.mkdir(mode=0o700)
    previous = root/'previous'
    current = root/'current'
    values = {
        'tls/ca.crt': ca.decode(), 'tls/worker.crt': bundle.tls_cert.get_secret_value(),
        'tls/worker.key': bundle.tls_key.get_secret_value(),
        'roles-anywhere.crt': bundle.roles_cert.get_secret_value(),
        'roles-anywhere.key': bundle.roles_key.get_secret_value(),
        'expiry.json': json.dumps({'expires_at':expiry.isoformat(),'node_id':node.node_id}),
        'aws-config': '[profile migration-reader]\nregion = us-east-1\ncredential_process = '
            '/usr/local/bin/aws_signing_helper credential-process --certificate /run/migration/roles-anywhere.crt '
            '--private-key /run/migration/roles-anywhere.key --trust-anchor-arn '+node.trust_anchor_arn+
            ' --profile-arn '+node.profile_arn+' --role-arn '+node.role_arn+' --session-duration 900\n',
        'worker.env': 'MIGRATION_ENVIRONMENT=cloud-worker\nMIGRATION_DB_HOST=10.241.0.10\nMIGRATION_DB_PORT=5432\n'
            'MIGRATION_DB_NAME=migration\nMIGRATION_DB_USER=migration_worker\nMIGRATION_DB_PASSWORD=\n'
            'MIGRATION_DB_SSLROOTCERT=/run/migration/tls/ca.crt\nMIGRATION_DB_SSLCERT=/run/migration/tls/worker.crt\n'
            'MIGRATION_DB_SSLKEY=/run/migration/tls/worker.key\nMIGRATION_AWS_CONFIG_FILE=/run/migration/aws-config\n'
            'MIGRATION_OPERATOR_ENABLED=false\nMIGRATION_OPERATOR_TOKEN=\nMIGRATION_WORKER_TOKEN='+bundle.worker_token.get_secret_value()+'\n',
    }
    try:
        for name,value in values.items():
            target=staged/name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('x',encoding='utf-8',newline='\n') as stream: stream.write(value)
            target.chmod(0o600)
        if owner is not None:
            for target in [staged,*staged.rglob('*')]: os.chown(target,owner,owner)
        for path in (current, previous):
            if path.is_symlink(): raise RuntimeErrorSafe('Unsafe credential generation path')
        if previous.exists(): shutil.rmtree(previous)
        if current.exists(): current.rename(previous)
        try:
            staged.rename(current)
        except Exception:
            if previous.exists(): previous.rename(current)
            raise
        if previous.exists(): shutil.rmtree(previous)
    finally:
        if staged.exists(): shutil.rmtree(staged)
    return {'event':'credentials_prepared','node_id':node.node_id,'expires_at':expiry.isoformat()}


def renewal_needed(runtime_dir, now=None):
    now=now or datetime.now(timezone.utc)
    try:
        data=json.loads(read_bounded(Path(runtime_dir)/'current/expiry.json'))
        expiry=datetime.fromisoformat(data['expires_at'])
        return expiry <= now + timedelta(hours=2)
    except Exception:
        return True


NUMERIC_FIELDS={'objects','verified_objects','verified_bytes','copy_process_successes','failed_or_unprocessed','payload_transfer_bytes','exit_code'}
STATES={'verified','failed','no_work','lease_lost'}


def safe_event(value, node):
    result={'event':'worker_status','node_id':node.node_id,'job_id':str(node.job_id)}
    if isinstance(value,dict):
        if value.get('event') == 'worker_exit': result['event']='worker_exit'
        if value.get('state') in STATES: result['state']=value['state']
        for key in NUMERIC_FIELDS:
            number=value.get(key)
            if type(number) is int and 0 <= number <= 10**18: result[key]=number
    return result


def sync_directory(path):
    if sys.platform == 'linux':
        descriptor=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def enqueue(outbox, event, limit=10000):
    root=private_directory(outbox)
    if sum(1 for _ in root.iterdir()) >= limit:
        raise RuntimeErrorSafe('Log outbox is full; stop work until logs are drained')
    identifier=uuid4().hex
    record={'id':identifier,'time':datetime.now(timezone.utc).isoformat(),'event':event}
    pending=root/(identifier+'.tmp')
    with pending.open('x',encoding='utf-8') as stream:
        json.dump(record,stream); stream.flush(); os.fsync(stream.fileno())
    pending.chmod(0o600)
    pending.rename(root/(identifier+'.json'))
    sync_directory(root)


def flush_logs(node, outbox, client=None):
    import oci
    paths=sorted(Path(outbox).glob('*.json'))[:100]
    if not paths: return 0
    entries=[]
    for path in paths:
        if not re.fullmatch(r'[a-f0-9]{32}\.json',path.name): raise RuntimeErrorSafe('Invalid log spool path')
        record=json.loads(read_bounded(path,16384))
        # Re-apply the allowlist at the network boundary; never ship arbitrary file text.
        event=safe_event(record['event'],node)
        entries.append(oci.loggingingestion.models.LogEntry(id=path.stem,
            time=datetime.fromisoformat(record['time']),data=json.dumps(event)))
    batch=oci.loggingingestion.models.LogEntryBatch(entries=entries,source=node.node_id,type='migration.worker')
    details=oci.loggingingestion.models.PutLogsDetails(specversion='1.0',log_entry_batches=[batch])
    client=client or sdk_clients('logs')
    client.put_logs(node.log_id,details)
    for path in paths: path.unlink()
    sync_directory(outbox)
    return len(paths)


def run_worker(node, outbox):
    # stderr discarded; stdout is reserialized through a strict field allowlist.
    child=subprocess.Popen([sys.executable,'-m','migration_runner.cloud_cli','run-loop',
        '--job',str(node.job_id),'--worker-id',node.node_id],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    def stop(*_):
        if child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try:
        while line:=child.stdout.readline(16385):
            if len(line)>16384: raise RuntimeErrorSafe('Worker output exceeded limit')
            try: value=json.loads(line)
            except ValueError: value={}
            event=safe_event(value,node)
            enqueue(outbox,event)
            print(json.dumps(event),flush=True)
        code=child.wait()
        enqueue(outbox,{'event':'worker_exit','exit_code':code})
        return code
    finally:
        stop()
        try: child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','renew-needed','run','flush','validate-node'])
    parser.add_argument('--node',default='/etc/migration/node.json')
    parser.add_argument('--trust',default='/etc/migration/trust')
    parser.add_argument('--runtime-dir',default='/run/migration-host')
    parser.add_argument('--outbox',default='/var/lib/migration/outbox')
    args=parser.parse_args()
    try:
        node=Node.model_validate_json(read_bounded(args.node))
        if args.action=='validate-node':
            print(json.dumps({'valid':True,'cloud_contacted':False})); return 0
        if args.action=='renew-needed': return 0 if renewal_needed(args.runtime_dir) else 1
        if args.action=='prepare':
            if sys.platform!='linux' or os.getuid()!=0: raise RuntimeErrorSafe('Linux bootstrap identity required')
            print(json.dumps(install_bundle(fetch_bundle(node),node,args.trust,args.runtime_dir,owner=10001)))
        elif args.action=='flush':
            print(json.dumps({'events_delivered':flush_logs(node,args.outbox)}))
        else: return run_worker(node,args.outbox)
        return 0
    except Exception:
        print(json.dumps({'error':'Runtime operation failed; review identity, certificate validity, secret enrollment and private paths'}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
