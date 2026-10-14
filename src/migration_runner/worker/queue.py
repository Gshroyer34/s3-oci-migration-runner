"""Transactional PostgreSQL leases. All mutation paths lock job before batch."""
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import func, select, update, text, or_
from sqlalchemy.dialects.postgresql import insert

from migration_runner.ledger.models import Attempt, Batch, Migration, ObjectEvidence, QueueEvent, WorkerHeartbeat
from .rclone import WorkerError


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    lease_seconds: int = 30
    grace_seconds: int = 10
    max_attempts: int = 3
    retry_seconds: int = 4

    def __post_init__(self):
        if not (2 <= self.lease_seconds <= 300 and 0 <= self.grace_seconds <= 300 and
                1 <= self.max_attempts <= 10 and 0 <= self.retry_seconds <= 60):
            raise WorkerError('Invalid lease/retry policy')

    def delay(self, count):
        return min(300, self.retry_seconds * 2 ** min(max(count - 1, 0), 8))


@dataclass(frozen=True)
class Lease:
    job: object
    batch: int
    attempt: object
    token: object
    worker: str
    policy: Policy
    automatic: bool = True
    credential: str = field(default="", repr=False)


def event(c, job, kind, **details):
    c.execute(insert(QueueEvent).values(id=uuid4(), migration_id=job, kind=kind, details=details))


def job_lock(c, job, exclusive=False):
    # Short, bounded transactions; heartbeat failure must fail closed.
    c.execute(text("SET LOCAL lock_timeout = '1000ms'"))
    c.execute(text("SET LOCAL statement_timeout = '2000ms'"))
    row = c.execute(select(Migration.__table__).where(Migration.id == job).with_for_update(read=not exclusive)).mappings().one_or_none()
    if not row:
        raise WorkerError('Unknown migration')
    return row


def configure(engine, job, binding, policy=None):
    from .execution import roots
    source, destination = roots(binding['source_root'], binding['destination_root'])
    binding = {'source_root': str(source), 'destination_root': str(destination)}
    with engine.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(739015032)'))
        row = job_lock(c, job, exclusive=True)
        if row['state'] != 'planned' or row['control'] == 'cancelled':
            raise WorkerError('Only a planned, non-cancelled migration may be bound')
        if row['execution']:
            if row['execution']['binding'] != binding or (policy and row['execution']['policy'] != asdict(policy)):
                raise WorkerError('Execution binding and retry policy are immutable for this migration')
            return row['execution']
        # Include Step 3 bindings while upgrading existing plans.
        prior = c.execute(select(Attempt.migration_id, Attempt.settings, Migration.destination_prefix).join(Migration, Attempt.migration_id == Migration.id))
        bindings = [(j, settings['binding'], prefix) for j, settings, prefix in prior if 'binding' in settings]
        bindings.extend((j, cfg['binding'], prefix) for j, cfg, prefix in c.execute(select(Migration.id, Migration.execution, Migration.destination_prefix).where(Migration.execution.is_not(None))))
        target = str(destination / row['destination_prefix'])
        for other, known, prefix in bindings:
            if other == job:
                if known != binding:
                    raise WorkerError('Roots differ from the previous execution binding')
            elif known.get('kind') != 's3-oci-pilot':
                path = str(Path(known['destination_root']) / prefix)
                if path.startswith(target) or target.startswith(path):
                    raise WorkerError('Physical destination overlaps another migration')
        config = {'binding': binding, 'policy': asdict(policy or Policy())}
        c.execute(update(Migration).where(Migration.id == job).values(execution=config))
        event(c, job, 'bound')
        return config


def claim(engine, job, worker=None, batch=None, manual=False, verify_only=False, credential=None):
    from migration_runner.config import Settings
    from migration_runner.security import worker as authenticate
    credential = Settings().worker_token.get_secret_value() if credential is None else credential
    worker = worker or str(uuid4())
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', worker):
        raise WorkerError('Invalid worker identifier')
    with engine.connect() as c:
        authenticate(c, credential)
    reap(engine, job)
    with engine.begin() as c:
        migration = job_lock(c, job, exclusive=True)
        authenticate(c, credential)
        if migration['control'] != 'active' or migration['state'] != 'planned' or not migration['execution']:
            return None
        policy = Policy(**migration['execution']['policy'])
        limits = migration['run_limits']
        if limits:
            now = c.execute(select(func.clock_timestamp())).scalar_one()
            running = c.execute(select(func.count()).select_from(Batch).where(Batch.migration_id == job, Batch.state == 'running')).scalar_one()
            if migration['run_deadline'] is None or now >= migration['run_deadline'] or running >= limits['max_workers']:
                return None

        eligible = ['planned', 'retry_wait'] + (['verified', 'failed', 'source_changed'] if manual else [])
        query = select(Batch.__table__).where(Batch.migration_id == job, Batch.state.in_(eligible),
            or_(Batch.available_at.is_(None), Batch.available_at <= func.clock_timestamp()))
        if limits:
            query = query.where(Batch.planned_bytes <= limits['max_scheduled_bytes'] - migration['scheduled_bytes'])
        if batch is not None:
            query = query.where(Batch.ordinal == batch)
        row = c.execute(query.order_by(Batch.ordinal).limit(1).with_for_update(skip_locked=True)).mappings().one_or_none()
        if not row:
            return None
        count = (0 if manual and row['state'] in ('verified', 'failed', 'source_changed') else row['claim_count']) + 1
        if count > policy.max_attempts:
            c.execute(update(Batch).where(Batch.migration_id == job, Batch.ordinal == row['ordinal']).values(state='failed'))
            return None
        c.execute(update(Migration).where(Migration.id == job).values(scheduled_bytes=Migration.scheduled_bytes + row['planned_bytes']))
        attempt, token = uuid4(), uuid4()
        settings = {**migration['execution'], 'verify_only': verify_only, 'automatic_retry': not manual}
        c.execute(insert(Attempt).values(id=attempt, migration_id=job, batch_ordinal=row['ordinal'], state='running',
            lease_token=token, worker_id=worker, settings=settings, summary={}))
        c.execute(update(Batch).where(Batch.migration_id == job, Batch.ordinal == row['ordinal']).values(
            state='running', lease_token=token, lease_owner=worker, current_attempt_id=attempt,
            lease_expires_at=func.clock_timestamp() + timedelta(seconds=policy.lease_seconds), claim_count=count, available_at=None))
        beat(c, worker, attempt)
        event(c, job, 'claimed', batch=row['ordinal'], attempt=str(attempt), worker=worker)
        return Lease(job, row['ordinal'], attempt, token, worker, policy, not manual, credential)


def beat(c, worker, attempt):
    stmt = insert(WorkerHeartbeat).values(id=worker, last_seen=func.clock_timestamp(), attempt_id=attempt)
    c.execute(stmt.on_conflict_do_update(index_elements=['id'], set_={'last_seen': func.clock_timestamp(), 'attempt_id': attempt}))


def owned(c, lease):
    from migration_runner.security import worker as authenticate
    authenticate(c, lease.credential)
    job = job_lock(c, lease.job)
    row = c.execute(select(Batch.__table__).where(Batch.migration_id == lease.job, Batch.ordinal == lease.batch).with_for_update()).mappings().one_or_none()
    now = c.execute(select(func.clock_timestamp())).scalar_one()
    if (job['control'] == 'cancelled' or job['state'] != 'planned' or not row or row['state'] != 'running' or
            row['lease_token'] != lease.token or row['lease_owner'] != lease.worker or
            row['current_attempt_id'] != lease.attempt or row['lease_expires_at'] <= now):
        raise LeaseLost('Lease is no longer owned')
    return row


def heartbeat(engine, lease):
    with engine.begin() as c:
        owned(c, lease)
        c.execute(update(Batch).where(Batch.migration_id == lease.job, Batch.ordinal == lease.batch).values(
            lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease.policy.lease_seconds)))
        beat(c, lease.worker, lease.attempt)


def record(engine, lease, ordinal, evidence):
    with engine.begin() as c:
        owned(c, lease)
        # Membership is part of the fence, not merely an application convention.
        member = c.execute(text('SELECT 1 FROM inventory_objects WHERE migration_id=:job AND ordinal=:ordinal AND batch_ordinal=:batch'),
                           {'job': lease.job, 'ordinal': ordinal, 'batch': lease.batch}).scalar_one_or_none()
        if not member:
            raise LeaseLost('Object is outside the owned batch')
        stmt = insert(ObjectEvidence).values(attempt_id=lease.attempt, migration_id=lease.job, object_ordinal=ordinal, evidence=evidence)
        c.execute(stmt.on_conflict_do_update(index_elements=['attempt_id', 'object_ordinal'], set_={'evidence': evidence, 'checked_at': func.clock_timestamp()}))


def set_settings(engine, lease, settings):
    with engine.begin() as c:
        owned(c, lease)
        c.execute(update(Attempt).where(Attempt.id == lease.attempt).values(settings=settings))


def record_telemetry(engine, lease, value):
    from migration_runner.autoscaling.worker_metrics import WorkerTelemetry
    data = WorkerTelemetry.model_validate(value).model_dump(mode='json')
    with engine.begin() as c:
        owned(c, lease)
        prior = c.execute(select(Attempt.summary).where(Attempt.id == lease.attempt)).scalar_one()
        previous = prior.get('telemetry')
        if previous:
            if data['sequence'] <= previous['sequence']:
                return False
            if any(data['counters'][k] < previous['counters'][k] for k in data['counters']):
                raise WorkerError('Telemetry counters moved backwards')
        data['observed_at'] = c.execute(select(func.clock_timestamp())).scalar_one().isoformat()
        c.execute(update(Attempt).where(Attempt.id == lease.attempt).values(summary={**prior, 'telemetry':data}))
        return True


def finish(engine, lease, summary, retryable=False, source_changed=False):
    with engine.begin() as c:
        row = owned(c, lease)
        prior = c.execute(select(Attempt.summary).where(Attempt.id == lease.attempt)).scalar_one()
        if 'telemetry' in summary:
            from migration_runner.autoscaling.worker_metrics import WorkerTelemetry
            final = WorkerTelemetry.model_validate(summary['telemetry']).model_dump(mode='json')
            previous = prior.get('telemetry')
            if previous and previous['sequence'] > final['sequence']:
                final = previous
            final['observed_at'] = c.execute(select(func.clock_timestamp())).scalar_one().isoformat()
            summary = {**summary, 'telemetry':final}
        elif 'telemetry' in prior:
            summary = {**summary, 'telemetry':prior['telemetry']}
        # Independently derive successful coverage from committed object evidence.
        counts = c.execute(text("""SELECT count(*) AS n, coalesce(sum(o.size),0) AS bytes
            FROM object_evidence e JOIN inventory_objects o ON o.migration_id=e.migration_id AND o.ordinal=e.object_ordinal
            JOIN migrations m ON m.id=e.migration_id
            WHERE e.attempt_id=:attempt AND o.batch_ordinal=:batch
              AND e.evidence->>'content'='verified' AND e.evidence->>'structural'='match'
              AND e.evidence->>'method'='full-read-sha256' AND e.evidence->>'copy' <> 'process_failed'
              AND (coalesce(m.execution->'binding'->>'kind','') NOT IN ('s3-oci-pilot','s3-oci-deployment-v1')
                   OR e.evidence->>'tags'='verified')
              AND (m.execution->>'property_mapping' IS NULL OR
                   (m.execution->>'property_mapping'='s3-oci-properties/v1'
                    AND e.evidence->>'property_mapping'=m.execution->>'property_mapping'
                    AND e.evidence->>'properties'='verified'))"""), {'attempt': lease.attempt, 'batch': lease.batch}).mappings().one()
        success = (summary.get('state') == 'verified' and counts['n'] == row['object_count'] and int(counts['bytes']) == row['planned_bytes'])
        state = 'verified' if success else ('source_changed' if source_changed else 'failed')
        summary = {**summary, 'state': state, 'verified_objects': counts['n'], 'verified_bytes': int(counts['bytes']),
                   'failed_or_unprocessed': row['object_count'] - counts['n']}
        retry = not success and not source_changed and retryable and lease.automatic and row['claim_count'] < lease.policy.max_attempts
        c.execute(update(Attempt).where(Attempt.id == lease.attempt).values(state=state, summary=summary, finished_at=func.clock_timestamp()))
        c.execute(update(Batch).where(Batch.migration_id == lease.job, Batch.ordinal == lease.batch).values(
            state='retry_wait' if retry else state, lease_token=None, lease_owner=None, lease_expires_at=None,
            available_at=func.clock_timestamp() + timedelta(seconds=lease.policy.delay(row['claim_count'])) if retry else None))
        event(c, lease.job, 'finished', batch=lease.batch, attempt=str(lease.attempt), state=state, retry=retry)
        return summary


def reap(engine, job):
    with engine.begin() as c:
        migration = job_lock(c, job)
        if not migration['execution']:
            return 0
        policy = Policy(**migration['execution']['policy'])
        rows = c.execute(select(Batch.__table__).where(Batch.migration_id == job, Batch.state == 'running',
            Batch.lease_expires_at <= func.clock_timestamp() - timedelta(seconds=policy.grace_seconds))
            .order_by(Batch.ordinal).limit(100).with_for_update(skip_locked=True)).mappings().all()
        for row in rows:
            c.execute(update(Attempt).where(Attempt.id == row['current_attempt_id'], Attempt.state == 'running').values(
                state='abandoned', finished_at=func.clock_timestamp(), summary=Attempt.summary.op('||')({'reason': 'lease_expired', 'content_verified': False})))
            state = 'retry_wait' if row['claim_count'] < policy.max_attempts else 'failed'
            if migration['control'] == 'cancelled':
                state = 'cancelled'
            c.execute(update(Batch).where(Batch.migration_id == job, Batch.ordinal == row['ordinal']).values(
                state=state, lease_token=None, lease_owner=None, lease_expires_at=None,
                available_at=func.clock_timestamp() + timedelta(seconds=policy.delay(row['claim_count']))))
            event(c, job, 'reclaimed', batch=row['ordinal'], old_attempt=str(row['current_attempt_id']), state=state)
        return len(rows)


def control(engine, job, action):
    states = {'pause': 'paused', 'drain': 'draining', 'resume': 'active', 'cancel': 'cancelled'}
    if action not in states:
        raise WorkerError('Unknown queue action')
    with engine.begin() as c:
        migration = job_lock(c, job, exclusive=True)
        if migration['control'] == 'cancelled' and action != 'cancel':
            raise WorkerError('Cancellation is terminal; create a reviewed new migration')
        c.execute(update(Migration).where(Migration.id == job).values(control=states[action]))
        if action == 'cancel':
            c.execute(update(Attempt).where(Attempt.migration_id == job, Attempt.state == 'running').values(
                state='cancelled', finished_at=func.clock_timestamp(), summary=Attempt.summary.op('||')({'reason': 'operator_cancelled', 'content_verified': False})))
            c.execute(update(Batch).where(Batch.migration_id == job, Batch.state != 'verified').values(
                state='cancelled', lease_token=None, lease_owner=None, lease_expires_at=None, available_at=None))
        event(c, job, action)
    return status(engine, job)


def status(engine, job):
    with engine.connect() as c:
        # One statement gives a consistent view during concurrent completions.
        row = c.execute(text("""SELECT m.state, m.control, m.execution IS NOT NULL AS bound, m.run_limits, m.scheduled_bytes, m.run_deadline,
             (m.run_deadline <= clock_timestamp()) AS time_limit_reached,
             NOT EXISTS (SELECT 1 FROM batches pending WHERE pending.migration_id=m.id
               AND pending.state IN ('planned','retry_wait')
               AND pending.planned_bytes <= (m.run_limits->>'max_scheduled_bytes')::bigint-m.scheduled_bytes) AND EXISTS (SELECT 1 FROM batches pending WHERE pending.migration_id=m.id AND pending.state IN ('planned','retry_wait')) AS no_batch_fits,
             count(b.ordinal) AS batches, count(*) FILTER (WHERE b.state='running') AS running,
             count(*) FILTER (WHERE b.state='verified') AS verified_batches,
             count(*) FILTER (WHERE b.state IN ('failed','source_changed','cancelled')) AS exceptions,
             coalesce(sum(b.planned_bytes) FILTER (WHERE b.state='verified'),0) AS verified_bytes,
             coalesce(sum(b.object_count) FILTER (WHERE b.state='verified'),0) AS verified_objects
             FROM migrations m LEFT JOIN batches b ON b.migration_id=m.id WHERE m.id=:job
             GROUP BY m.id"""), {'job': job}).mappings().one_or_none()
        if not row:
            raise WorkerError('Unknown migration')
        result = {k: int(v) if k in ('batches','running','verified_batches','exceptions','verified_bytes','verified_objects') else v for k, v in row.items()}
        result['limit_reached'] = bool(row['run_limits'] and (row['time_limit_reached'] or row['no_batch_fits']))
        result['queue_complete'] = bool(row['bound'] and row['state'] == 'planned' and row['control'] != 'cancelled' and row['batches'] > 0 and row['batches'] == row['verified_batches'])
        result['drained'] = row['control'] == 'draining' and row['running'] == 0
        result.update(job_id=str(job), scope='local batch verification; not cutover approval', payload_transfer_bytes=None)
        return result
