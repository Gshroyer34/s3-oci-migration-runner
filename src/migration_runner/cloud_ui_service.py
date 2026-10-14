"""Bounded customer-style operator service for the already frozen 10 GB lab fixture."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from migration_runner import reports, security
from migration_runner.operations import RunLimits
from migration_runner.planning.contracts import Mapping
from migration_runner.planning.service import plan
from migration_runner.worker.cloud import CloudBinding, CloudRclone, attach_references, configure_cloud
from migration_runner.worker import queue

UI_LOCK = 739015034
MAX_RUNS = 3
DATA_BYTES = 10_000_000_000


class UIConflict(ValueError):
    pass


class CloudUIService:
    def __init__(self, engine, settings, reference_path, reference_sha256, destination_empty=None, inventory=None, capacity_monitor=None):
        self.engine, self.settings = engine, settings
        self.reference_path, self.reference_sha256 = Path(reference_path), reference_sha256
        self.destination_empty = destination_empty or self._destination_empty
        self.inventory = inventory or self._inventory
        from migration_runner.autoscaling.monitor import CapacityMonitor
        self.capacity_monitor = capacity_monitor or CapacityMonitor()
        self.manifest_lock = threading.Lock()

    def manifest(self, job):
        from migration_runner.manifest import temporary_export
        from migration_runner.worker.cloud import SOURCE_BUCKET
        if not self.manifest_lock.acquire(blocking=False):
            raise UIConflict('Another manifest is being prepared. Try again shortly.')
        try:
            return temporary_export(self.engine, job, source_bucket=SOURCE_BUCKET)
        finally:
            self.manifest_lock.release()

    @contextmanager
    def locked(self):
        # Session lock covers configure_cloud's separate transaction and admission/dispatch publication.
        with self.engine.connect() as c:
            if not c.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': UI_LOCK}).scalar_one():
                raise UIConflict('Another control operation is in progress. Try again.')
            c.commit()
            try:
                yield
            finally:
                c.rollback()
                c.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': UI_LOCK})
                c.commit()

    def _inventory(self, binding):
        return CloudRclone(binding, self.settings.aws_config_file, self.settings.rclone_path).inventory()

    @staticmethod
    def _destination_empty(binding):
        import oci
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.object_storage.ObjectStorageClient({'region': 'us-ashburn-1'}, signer=signer, timeout=(10, 30))
        return not client.list_objects(binding.destination_namespace, binding.destination_bucket,
                                       prefix=binding.destination_prefix, limit=1).data.objects

    def ready(self, workers=0):
        from cryptography import x509
        expiry = x509.load_pem_x509_certificate(Path(self.settings.db_sslcert).read_bytes()).not_valid_after_utc
        now = datetime.now(timezone.utc)
        with self.engine.connect() as c:
            security.worker(c, self.settings.worker_token.get_secret_value(), required=True)
            agents = [dict(r) for r in c.execute(text('SELECT * FROM cloud_ui_workers ORDER BY node_id')).mappings()]
            admitted = c.execute(text('SELECT count(*) FROM cloud_ui_runs WHERE admitted_at IS NOT NULL')).scalar_one()
        for agent in agents:
            agent['ready'] = agent['last_seen'] > now - timedelta(seconds=45) and agent['certificate_expires_at'] > now + timedelta(minutes=90) and agent['service_state'] not in ('failed','unknown')
        selected = [a for a in agents if int(a['node_id'][-1]) <= workers]
        good = expiry > now + timedelta(minutes=90) and (not workers or len(selected) == workers and all(a['ready'] for a in selected))
        return {'ready': good, 'certificate_expires_at': expiry, 'workers': agents,
                'remaining_runs': max(0, MAX_RUNS - admitted), 'max_runs': MAX_RUNS,
                'fixture_objects': 100, 'fixture_bytes': DATA_BYTES}

    def jobs(self):
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(text('''SELECT m.id,m.state,m.control,m.created_at,m.destination_prefix,
                u.job_id IS NOT NULL AS ui_managed FROM migrations m LEFT JOIN cloud_ui_runs u ON u.job_id=m.id
                WHERE m.source_bucket='migration-lab-357633903375-us-east-1-source'
                ORDER BY m.created_at DESC LIMIT 100''')).mappings()]

    def capacity_preview(self, job, request, *, previous=None):
        """Read-only what-if analysis; never changes immutable limits or dispatch."""
        from migration_runner.autoscaling import CapacitySnapshot, evaluate
        from migration_runner.worker.cloud import SOURCE_BUCKET
        with self.engine.connect() as c:
            row = c.execute(text('''SELECT m.source_bucket,m.control,m.state,m.execution,m.run_limits,
                m.run_deadline,m.scheduled_bytes,clock_timestamp() AS observed_at,
                count(b.ordinal) AS total_batches,
                count(*) FILTER (WHERE b.state='verified') AS verified_batches,
                count(*) FILTER (WHERE b.state IN ('failed','source_changed','cancelled')) AS exceptions,
                count(*) FILTER (WHERE b.state='running' OR
                    (b.state IN ('planned','retry_wait') AND (b.available_at IS NULL OR b.available_at<=clock_timestamp())
                     AND b.planned_bytes<=coalesce((m.run_limits->>'max_scheduled_bytes')::bigint,10000000000)-m.scheduled_bytes)) AS runnable_batches,
                coalesce(sum(b.planned_bytes) FILTER (WHERE b.state='running' OR
                    (b.state IN ('planned','retry_wait') AND (b.available_at IS NULL OR b.available_at<=clock_timestamp())
                     AND b.planned_bytes<=coalesce((m.run_limits->>'max_scheduled_bytes')::bigint,10000000000)-m.scheduled_bytes)),0) AS remaining_bytes
                FROM migrations m LEFT JOIN batches b ON b.migration_id=m.id
                WHERE m.id=:job GROUP BY m.id'''), {'job': job}).mappings().one_or_none()
        if row is None or row['source_bucket'] != SOURCE_BUCKET:
            raise UIConflict('Unknown job or job outside the lab source.')
        readiness = self.ready()
        nodes = readiness.get('workers', [])
        ready = min(2, sum(bool(n.get('ready')) for n in nodes))
        limits = row['run_limits'] or {}
        now = row['observed_at']
        control = row['control']
        if control != 'cancelled':
            if not row['execution'] or row['state'] != 'planned':
                control = 'unbound'
            elif row['total_batches'] and row['verified_batches'] == row['total_batches']:
                control = 'complete'
            elif row['exceptions'] or not readiness['ready']:
                control = 'paused'
        # Lab execution is one object and one batch per node. Preview cannot expand it.
        effective_policy = request.policy.model_copy(update={'batch_slots_per_node': 1, 'object_slots_per_node': 1})
        snapshot = CapacitySnapshot(now_seconds=now.timestamp(), observation=request.observation,
            route_validated=request.route_validated, ready_nodes=ready, unavailable_nodes=2-ready,
            runtime_node_limit=min(2, limits.get('max_workers', 2)),
            runnable_batches=int(row['runnable_batches']), remaining_unique_bytes=int(row['remaining_bytes']),
            worker_source_bytes_per_second=request.worker_source_bytes_per_second,
            worker_profile_seconds=request.worker_profile_seconds, control=control,
            run_limit_reached=bool(row['run_deadline'] and now >= row['run_deadline']) or
                              bool(limits and row['scheduled_bytes'] >= limits['max_scheduled_bytes']))
        decision = evaluate(effective_policy, snapshot, previous)
        return {'job_id': str(job), 'scope': 'Read-only capacity preview; telemetry/profile supplied by operator',
                'live_path_telemetry_available': False, 'policy_persisted': False,
                'lab_node_ceiling': 2, 'effective_policy': effective_policy.model_dump(mode='json'),
                'snapshot': snapshot.model_dump(mode='json'), 'decision': decision.model_dump(mode='json')}

    def capacity_status(self, job):
        from migration_runner.autoscaling.worker_metrics import read_worker_metrics
        from migration_runner.worker.cloud import SOURCE_BUCKET
        with self.engine.connect() as c:
            scope = c.execute(text('SELECT source_bucket FROM migrations WHERE id=:job'), {'job':job}).scalar_one_or_none()
            if scope != SOURCE_BUCKET:
                raise UIConflict('Unknown job or job outside the lab source.')
            timing = c.execute(text('SELECT min(created_at) first_claim,max(finished_at) last_finish FROM worker_attempts WHERE migration_id=:job'), {'job':job}).mappings().one()
        report = reports.report(self.engine, job)
        now = datetime.now(timezone.utc)
        end = timing['last_finish'] if report['queue_complete'] else now
        elapsed = (end-timing['first_claim']).total_seconds() if end and timing['first_claim'] else 0
        workers = read_worker_metrics(self.engine,job,now=now)
        workers['verified_unique_bytes'] = report['content_verified_bytes']
        workers['verified_average_bytes_per_second'] = report['content_verified_bytes']/elapsed if elapsed > 0 else None
        workers['observed_source_read_amplification'] = (workers['source_read_bytes']/report['content_verified_bytes']
            if report['queue_complete'] and report['content_verified_bytes'] and workers['coverage']=='measured' and not workers['truncated'] else None)
        result = self.capacity_monitor.status(job,self.capacity_preview)
        return {'job_id':str(job), 'worker_metrics':workers, **result}

    def detail(self, job):
        result = reports.report(self.engine, job)
        with self.engine.connect() as c:
            row = c.execute(text('SELECT source_bucket,destination_prefix,run_started_at,execution FROM migrations WHERE id=:job'), {'job': job}).mappings().one()
            if row['source_bucket'] != 'migration-lab-357633903375-us-east-1-source':
                raise UIConflict('Job is outside the lab source.')
            result['destination_prefix'] = row['destination_prefix']
            result['required_property_mapping'] = (row['execution'] or {}).get('property_mapping')
            result['ui_managed'] = bool(c.execute(text('SELECT 1 FROM cloud_ui_runs WHERE job_id=:job'), {'job': job}).scalar_one_or_none())
            result['objects'] = [dict(r) for r in c.execute(text('''SELECT o.source_key,o.destination_key,o.size,
                b.state AS batch_state,e.evidence->>'copy' AS copy,e.evidence->>'content' AS content,
                e.evidence->>'structural' AS structural,e.evidence->>'error' AS error,
                e.evidence->>'tags' AS tags,e.evidence->>'properties' AS properties
                FROM inventory_objects o LEFT JOIN batches b ON b.migration_id=o.migration_id AND b.ordinal=o.batch_ordinal
                LEFT JOIN object_evidence e ON e.attempt_id=b.current_attempt_id AND e.object_ordinal=o.ordinal
                WHERE o.migration_id=:job ORDER BY o.source_key LIMIT 100'''), {'job': job}).mappings()]
        result['scope'] = 'Cloud pilot; full content verification, not customer cutover approval'
        return result

    def create(self):
        with self.locked():
            if not self.ready()['ready']:
                raise UIConflict('Coordinator credentials need renewal before creating a test.')
            raw = self.reference_path.read_bytes()
            if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != self.reference_sha256:
                raise UIConflict('Independent fixture reference is unavailable or changed.')
            reference = json.loads(raw)
            if reference.get('object_count') != 100 or reference.get('total_bytes') != DATA_BYTES:
                raise UIConflict('Only the approved 100-object fixture is enabled.')
            binding = CloudBinding(destination_prefix='pilot/ui-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex + '/',
                                   immutable_source=True, exclusive_destination=True)
            if not self.destination_empty(binding):
                raise UIConflict('Destination prefix is not empty.')
            records = attach_references(self.inventory(binding), reference)
            mapping = Mapping(**{k: getattr(binding, k) for k in ('source_bucket', 'source_region', 'source_prefix',
                'destination_namespace', 'destination_bucket', 'destination_region', 'destination_prefix')},
                target_batch_bytes=500_000_000, max_batch_objects=5)
            job = plan(self.engine, mapping, records, {'kind': 'cloud-ui-frozen-fixture', 'observed_at': datetime.now(timezone.utc),
                                                     'artifact_digest': self.reference_sha256, 'atomic_snapshot': False})
            with self.engine.begin() as c:
                c.execute(text('INSERT INTO cloud_ui_runs (job_id) VALUES (:job)'), {'job': job})
            security.audit(self.engine, 'cloud-operator', 'create-preview', job)
            return self.detail(job)

    def managed(self, job):
        with self.engine.connect() as c:
            row = c.execute(text('SELECT admitted_at FROM cloud_ui_runs WHERE job_id=:job'), {'job': job}).mappings().one_or_none()
        if row is None:
            raise UIConflict('Historical CLI runs are read-only. Create a new UI test.')
        return row

    def start(self, job, workers):
        if type(workers) is not int or workers not in (1, 2):
            raise UIConflict('Select one or two workers.')
        with self.locked():
            row = self.managed(job)
            readiness = self.ready(workers)
            if not readiness['ready']:
                raise UIConflict('Selected workers are offline or certificates need renewal.')
            if row['admitted_at'] is None and readiness['remaining_runs'] == 0:
                raise UIConflict('The three-run lab allowance is exhausted. Review cost before extending it.')
            with self.engine.connect() as c:
                current = c.execute(text('SELECT job_id FROM cloud_ui_dispatch WHERE id=1')).scalar_one()
                prefix = c.execute(text('SELECT destination_prefix FROM migrations WHERE id=:job'), {'job': job}).scalar_one()
            if current and current != job:
                previous = queue.status(self.engine, current)
                if previous['running'] or not (previous['queue_complete'] or previous['drained'] or previous['control'] == 'cancelled'):
                    raise UIConflict('Finish, drain or cancel the current run before starting another.')
            state = queue.status(self.engine, job)
            if state['queue_complete'] or state['control'] == 'cancelled' or state['time_limit_reached']:
                raise UIConflict('This run has finished or expired. Create a new test.')
            binding = CloudBinding(destination_prefix=prefix, immutable_source=True, exclusive_destination=True)
            # Recheck exclusivity immediately before first admission; never erase existing objects.
            if row['admitted_at'] is None and not state['bound'] and not self.destination_empty(binding):
                raise UIConflict('Destination changed since preview. Create a new test.')
            configure_cloud(self.engine, job, binding, RunLimits(max_workers=workers, max_seconds=3600, max_scheduled_bytes=DATA_BYTES))
            with self.engine.begin() as c:
                c.execute(text('UPDATE cloud_ui_runs SET admitted_at=coalesce(admitted_at,clock_timestamp()) WHERE job_id=:job'), {'job': job})
                c.execute(text('UPDATE cloud_ui_dispatch SET job_id=:job WHERE id=1'), {'job': job})
            security.audit(self.engine, 'cloud-operator', 'start', job)
            return self.detail(job)

    def control(self, job, action):
        if action not in ('pause', 'drain', 'resume', 'cancel'):
            raise UIConflict('Unsupported control.')
        with self.locked():
            self.managed(job)
            with self.engine.connect() as c:
                current = c.execute(text('SELECT job_id FROM cloud_ui_dispatch WHERE id=1')).scalar_one()
            if current != job:
                raise UIConflict('Only the currently dispatched run can be controlled.')
            state = queue.status(self.engine, job)
            if action == 'resume' and (state['time_limit_reached'] or state['queue_complete'] or
                    not self.ready(state['run_limits']['max_workers'])['ready']):
                raise UIConflict('Run is finished, expired or workers need attention.')
            queue.control(self.engine, job, action)
            security.audit(self.engine, 'cloud-operator', action, job)
            return self.detail(job)
