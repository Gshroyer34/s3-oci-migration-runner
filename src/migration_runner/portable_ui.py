"""Operator actions bound to the root-installed deployment, without lab defaults."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from migration_runner import reports, security
from migration_runner.cloud_ui_service import CloudUIService, UIConflict
from migration_runner.operations import RunLimits
from migration_runner.planning.contracts import Mapping
from migration_runner.planning.service import plan as create_plan
from migration_runner.worker.cloud import CloudRclone, attach_references, configure_cloud, resolve_binding
from migration_runner.worker import queue


class PortableUIService(CloudUIService):
    def __init__(self, engine, settings, host_plan, reference_path, **kwargs):
        if not host_plan.ui:
            raise ValueError('Operator UI is not enabled in the reviewed host plan')
        self.host_plan = host_plan
        self.profile = host_plan.deployment
        super().__init__(engine, settings, reference_path, host_plan.ui.reference_sha256, **kwargs)

    def scope(self, job):
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT config,provenance,execution FROM migrations WHERE id=:job'), {'job': job}).mappings().one_or_none()
        if row is None or row['provenance'].get('deployment_revision') != self.profile.revision:
            raise UIConflict('Migration does not belong to the installed deployment.')
        fields = {name: row['config'][name] for name in ('source_bucket','source_region','source_prefix',
            'destination_namespace','destination_bucket','destination_region','destination_prefix')}
        binding = self.profile.binding(fields['destination_prefix'])
        if any(getattr(binding, name) != value for name, value in fields.items()):
            raise UIConflict('Migration scope differs from the installed deployment.')
        self.profile.authorize(binding)
        if row['execution']:
            resolve_binding(row['execution']['binding'])
            if row['execution'].get('deployment_profile') != self.profile.model_dump():
                raise UIConflict('Execution profile differs from the approved deployment.')
        return binding

    def configuration(self):
        return {'deployment_name': self.profile.deployment_name, 'deployment_revision': self.profile.revision,
                'source': self.profile.source.model_dump(), 'destination': self.profile.destination.model_dump(),
                'limits': self.profile.limits.model_dump(), 'verification': self.profile.verification,
                'route': self.profile.payload_route, 'configuration_editable': False,
                'renewal_mode': 'oci-managed-issuer' if self.host_plan.managed_certificates else 'manual',
                'max_runs': self.host_plan.ui.max_runs}

    def _destination_empty(self, binding):
        import oci
        client = oci.object_storage.ObjectStorageClient({'region': binding.destination_region},
            signer=oci.auth.signers.InstancePrincipalsSecurityTokenSigner(), timeout=(10, 30), retry_strategy=oci.retry.NoneRetryStrategy())
        return not client.list_objects(binding.destination_namespace, binding.destination_bucket,
                                       prefix=binding.destination_prefix, limit=1).data.objects

    def ready(self, workers=0):
        from cryptography import x509
        expiry = x509.load_pem_x509_certificate(Path(self.settings.db_sslcert).read_bytes()).not_valid_after_utc
        now = datetime.now(timezone.utc)
        with self.engine.connect() as connection:
            security.worker(connection, self.settings.worker_token.get_secret_value(), required=True)
            rows = {row['node_id']: dict(row) for row in connection.execute(text('SELECT * FROM cloud_ui_workers')).mappings()}
            admitted = connection.execute(text("SELECT count(*) FROM cloud_ui_runs u JOIN migrations m ON m.id=u.job_id WHERE u.admitted_at IS NOT NULL AND m.provenance->>'deployment_revision'=:revision"), {'revision': self.profile.revision}).scalar_one()
        agents = []
        for node in self.host_plan.nodes:
            if node.node_id == 'coordinator': continue
            row = rows.get(node.node_id, {'node_id': node.node_id, 'status': 'offline'})
            row['ready'] = bool(row.get('last_seen') and row['last_seen'] > now - timedelta(seconds=60)
                and row['certificate_expires_at'] > now + timedelta(hours=2) and row.get('service_state') not in ('failed','unknown'))
            agents.append(row)
        agents.sort(key=lambda row: row['node_id'])
        selected = agents[:workers]
        good = expiry > now + timedelta(hours=2) and (not workers or len(selected) == workers and all(row['ready'] for row in selected))
        renewal = {'state':'not_checked'}
        try:
            value=json.loads(Path('/run/health/renewal-status.json').read_bytes())
            renewal={key:value[key] for key in ('state','activation','checked_at','message') if key in value}
        except (ValueError,OSError): pass
        return {'ready': good, 'certificate_expires_at': expiry, 'workers': agents,'renewal':renewal,
                'remaining_runs': max(0, self.host_plan.ui.max_runs-admitted), 'max_runs': self.host_plan.ui.max_runs,
                'max_workers': min(len(agents), self.profile.limits.max_workers), 'deployment': self.configuration()}

    def jobs(self):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text("SELECT m.id,m.state,m.control,m.created_at,m.destination_prefix,u.job_id IS NOT NULL AS ui_managed FROM migrations m LEFT JOIN cloud_ui_runs u ON u.job_id=m.id WHERE m.provenance->>'deployment_revision'=:revision ORDER BY m.created_at DESC LIMIT 100"), {'revision': self.profile.revision}).mappings()]

    def detail(self, job):
        binding = self.scope(job)
        result = reports.report(self.engine, job)
        result['destination_prefix'] = binding.destination_prefix
        with self.engine.connect() as connection:
            result['ui_managed'] = bool(connection.execute(text('SELECT 1 FROM cloud_ui_runs WHERE job_id=:job'), {'job': job}).scalar_one_or_none())
            result['objects'] = [dict(row) for row in connection.execute(text('''SELECT o.source_key,o.destination_key,o.size,b.state AS batch_state,
                e.evidence->>'content' AS content,e.evidence->>'tags' AS tags,e.evidence->>'properties' AS properties
                FROM inventory_objects o LEFT JOIN batches b ON b.migration_id=o.migration_id AND b.ordinal=o.batch_ordinal
                LEFT JOIN object_evidence e ON e.attempt_id=b.current_attempt_id AND e.object_ordinal=o.ordinal
                WHERE o.migration_id=:job ORDER BY o.ordinal LIMIT 100'''), {'job':job}).mappings()]
        return result

    def create(self):
        with self.locked():
            readiness=self.ready()
            if not readiness['ready'] or not readiness['remaining_runs']:
                raise UIConflict('Coordinator credentials are not ready.')
            if self.reference_path.stat().st_size > 262144:
                raise UIConflict('Reference manifest exceeds its size limit.')
            raw = self.reference_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.reference_sha256:
                raise UIConflict('Reference manifest differs from the reviewed installation.')
            binding = self.profile.binding(self.profile.destination.prefix + 'ui-' + uuid4().hex + '/')
            self.profile.authorize(binding)
            if not self.destination_empty(binding):
                raise UIConflict('Destination prefix is not empty.')
            records = attach_references(self.inventory(binding), json.loads(raw))
            if len(records) > self.profile.limits.max_objects or sum(record.size for record in records) > self.profile.limits.max_dataset_bytes or any(record.size > self.profile.limits.max_object_bytes for record in records):
                raise UIConflict('Inventory exceeds the installed pilot limits.')
            mapping = Mapping(**{key: getattr(binding,key) for key in ('source_bucket','source_region','source_prefix','destination_namespace','destination_bucket','destination_region','destination_prefix')}, target_batch_bytes=500_000_000, max_batch_objects=5)
            job = create_plan(self.engine, mapping, records, {'kind':'portable-ui-reference', 'observed_at':datetime.now(timezone.utc),
                'artifact_digest':self.reference_sha256, 'atomic_snapshot':False,'deployment_revision':self.profile.revision})
            with self.engine.begin() as connection:
                connection.execute(text('INSERT INTO cloud_ui_runs (job_id) VALUES (:job)'), {'job':job})
            security.audit(self.engine,'portable-operator','create-preview',job)
            return self.detail(job)

    def start(self, job, workers):
        if type(workers) is not int or not 1 <= workers <= min(self.profile.limits.max_workers,len(self.host_plan.nodes)-1):
            raise UIConflict('Worker count exceeds this installation.')
        with self.locked():
            binding = self.scope(job)
            managed = self.managed(job)
            readiness = self.ready(workers)
            if not readiness['ready'] or (managed['admitted_at'] is None and not readiness['remaining_runs']):
                raise UIConflict('Workers, credentials or the reviewed run allowance are not ready.')
            with self.engine.connect() as connection:
                current = connection.execute(text('SELECT job_id FROM cloud_ui_dispatch WHERE id=1')).scalar_one()
            if current and current != job:
                self.scope(current)
                previous = queue.status(self.engine,current)
                if previous['running'] or not (previous['queue_complete'] or previous['drained'] or previous['control']=='cancelled'):
                    raise UIConflict('Finish or drain the current migration first.')
            state = queue.status(self.engine,job)
            if state['queue_complete'] or state['control']=='cancelled' or state['time_limit_reached']:
                raise UIConflict('Migration is complete, cancelled or expired.')
            if managed['admitted_at'] is None and not state['bound'] and not self.destination_empty(binding):
                raise UIConflict('Destination changed since preview.')
            limits = self.profile.limits
            configure_cloud(self.engine,job,binding,RunLimits(max_workers=workers,max_seconds=limits.max_seconds,max_scheduled_bytes=limits.max_scheduled_bytes))
            with self.engine.begin() as connection:
                connection.execute(text('UPDATE cloud_ui_runs SET admitted_at=coalesce(admitted_at,clock_timestamp()) WHERE job_id=:job'), {'job':job})
                connection.execute(text('UPDATE cloud_ui_dispatch SET job_id=:job WHERE id=1'), {'job':job})
            security.audit(self.engine,'portable-operator','start',job)
            return self.detail(job)

    def control(self, job, action):
        self.scope(job)
        return super().control(job, action)

    def manifest(self, job):
        self.scope(job)
        from migration_runner.manifest import temporary_export
        return temporary_export(self.engine,job,source_bucket=self.profile.source.bucket)


def create_app():
    from migration_runner.cloud_api import create_app as surface
    from migration_runner.config import Settings
    from migration_runner.installation.deployment import installed_profile
    from migration_runner.installation.host_plan import HostPlan
    from migration_runner.ledger.database import make_engine
    settings = Settings()
    host_plan = HostPlan.model_validate_json(Path('/etc/migration/host-plan.json').read_bytes())
    if settings.environment != 'cloud-cli' or installed_profile() != host_plan.deployment:
        raise ValueError('Portable coordinator profile required')
    service = PortableUIService(make_engine(settings),settings,host_plan,'/etc/migration/reference.json')
    token = Path('/run/operator/token').read_text().strip()
    if token == settings.worker_token.get_secret_value():
        raise ValueError('Separate operator credential required')
    return surface(service,token,portable=True)
