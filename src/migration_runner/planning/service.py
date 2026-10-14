"""Transactional import and deterministic bounded-window byte packing."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from psycopg.types.json import Jsonb
from sqlalchemy import select, text

from migration_runner.ledger.models import Migration
from .contracts import InventoryObject, Mapping, PlanningError, disposition

ALGORITHM = 'key-order-minload-v1'
CHUNK_SIZE = 2000


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


@dataclass
class Bin:
    ordinal: int
    size: int = 0
    count: int = 0


class Packer:
    """At most width open bins. Emit completed batches through callback."""
    def __init__(self, mapping, width, completed):
        self.mapping, self.width, self.completed = mapping, width, completed
        self.bins = []
        self.next_ordinal = 1

    def new(self):
        result = Bin(self.next_ordinal)
        self.next_ordinal += 1
        return result

    def assign(self, size):
        if size > self.mapping.target_batch_bytes:
            result = self.new()
            self.completed(result.ordinal, size, 1, True)
            return result.ordinal
        if len(self.bins) < self.width:
            self.bins.append(self.new())
        selected = min(self.bins, key=lambda b: (b.size, b.count, b.ordinal))
        if selected.count and (selected.size + size > self.mapping.target_batch_bytes or selected.count >= self.mapping.max_batch_objects):
            self.completed(selected.ordinal, selected.size, selected.count, False)
            self.bins.remove(selected)
            selected = self.new()
            self.bins.append(selected)
        selected.size += size
        selected.count += 1
        return selected.ordinal

    def finish(self):
        for item in sorted(self.bins, key=lambda b: b.ordinal):
            if item.count:
                self.completed(item.ordinal, item.size, item.count, False)
        self.bins.clear()


def plan(engine, mapping: Mapping, objects, provenance: dict):
    """All-or-nothing transaction. No incomplete job is published on parser/DB error."""
    if not provenance.get('kind') or not provenance.get('observed_at'):
        raise PlanningError('Inventory kind and observation timestamp are required')
    observed = provenance['observed_at']
    if isinstance(observed, str):
        observed = datetime.fromisoformat(observed.replace('Z', '+00:00'))
    if not isinstance(observed, datetime) or observed.utcoffset() is None:
        raise PlanningError('Inventory observation timestamp requires a timezone')
    if provenance.get('source_bucket', mapping.source_bucket) != mapping.source_bucket:
        raise PlanningError('Manifest source bucket does not match mapping')
    # Persist only known provenance fields, never arbitrary config/credential dictionaries.
    evidence = {'kind': provenance['kind'], 'observed_at': observed.isoformat(),
                'import_started_at': datetime.now(timezone.utc).isoformat(), 'atomic_snapshot': False,
                'artifact_digest': provenance.get('artifact_digest'), 'algorithm': ALGORITHM}
    if 'deployment_revision' in provenance:
        revision = provenance['deployment_revision']
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
            raise PlanningError('Invalid deployment revision')
        evidence['deployment_revision'] = revision
    job_id = uuid4()
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('statement_timeout', '120000', true)"))
        scope = canonical([mapping.destination_namespace, mapping.destination_bucket, mapping.destination_region])
        connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))'), {'scope': scope})
        existing = connection.execute(select(Migration.destination_prefix).where(
            Migration.destination_namespace == mapping.destination_namespace,
            Migration.destination_bucket == mapping.destination_bucket,
            Migration.destination_region == mapping.destination_region,
            Migration.state != 'archived',
        ))
        for (prefix,) in existing:
            if prefix.startswith(mapping.destination_prefix) or mapping.destination_prefix.startswith(prefix):
                raise PlanningError('Destination prefix overlaps an existing active plan')
        connection.execute(Migration.__table__.insert().values(
            id=job_id, source_bucket=mapping.source_bucket,
            destination_namespace=mapping.destination_namespace, destination_bucket=mapping.destination_bucket,
            destination_region=mapping.destination_region, destination_prefix=mapping.destination_prefix,
            state='planning', config=mapping.model_dump(), provenance=evidence,
            manifest_digest='', plan_digest='',
        ))
        raw = connection.connection.driver_connection
        count = eligible_count = eligible_bytes = 0
        pending_objects = []
        for value in objects:
            item = value if isinstance(value, InventoryObject) else InventoryObject.model_validate(value)
            target, category, reason = disposition(item, mapping)
            count += 1
            identity = item.model_dump(mode='json', exclude={'key', 'size'})
            pending_objects.append((job_id, count, item.key, target, item.size, Jsonb(identity), category, reason))
            if category == 'eligible':
                eligible_count += 1
                eligible_bytes += item.size
            if len(pending_objects) >= CHUNK_SIZE:
                flush_objects(raw, pending_objects)
        flush_objects(raw, pending_objects)
        # Read in C-collated key order so input/shard order cannot change assignments.
        connection.execute(text('CREATE TEMP TABLE plan_assignments (ordinal bigint PRIMARY KEY, batch_ordinal bigint NOT NULL) ON COMMIT DROP'))
        width = min(mapping.open_batches, max(1,
            (eligible_bytes + mapping.target_batch_bytes - 1) // mapping.target_batch_bytes,
            (eligible_count + mapping.max_batch_objects - 1) // mapping.max_batch_objects))
        pending_batches = []
        packer = Packer(mapping, width, lambda ordinal, size, number, oversized: pending_batches.append((job_id, ordinal, size, number, oversized)))
        manifest_hash = hashlib.sha256()
        plan_hash = hashlib.sha256(canonical({'mapping': mapping.model_dump(), 'algorithm': ALGORITHM}).encode())
        with raw.cursor(name='inventory_for_planning') as cursor:
            cursor.execute('SELECT ordinal, source_key, destination_key, size, source_identity, disposition, reason FROM inventory_objects WHERE migration_id = %s ORDER BY source_key COLLATE "C"', (job_id,))
            while rows := cursor.fetchmany(CHUNK_SIZE):
                assignments = []
                for ordinal, key, target, size, identity, category, reason in rows:
                    manifest_hash.update((canonical([key, size, identity]) + '\n').encode())
                    batch_ordinal = packer.assign(size) if category == 'eligible' else None
                    plan_hash.update((canonical([key, target, size, identity, category, reason, batch_ordinal]) + '\n').encode())
                    if batch_ordinal is not None:
                        assignments.append((ordinal, batch_ordinal))
                with raw.cursor().copy('COPY plan_assignments (ordinal, batch_ordinal) FROM STDIN') as copy:
                    for row in assignments:
                        copy.write_row(row)
                flush_batches(raw, pending_batches)
        packer.finish()
        flush_batches(raw, pending_batches)
        connection.execute(text('UPDATE inventory_objects o SET batch_ordinal = a.batch_ordinal FROM plan_assignments a WHERE o.migration_id = :job AND o.ordinal = a.ordinal'), {'job': job_id})
        bad = connection.execute(text("SELECT count(*) FROM inventory_objects WHERE migration_id = :job AND disposition = 'eligible' AND batch_ordinal IS NULL"), {'job': job_id}).scalar_one()
        if bad:
            raise RuntimeError('Incomplete batch assignment')
        connection.execute(Migration.__table__.update().where(Migration.id == job_id).values(
            state='planned' if eligible_count == count else 'needs_review',
            manifest_digest=manifest_hash.hexdigest(), plan_digest=plan_hash.hexdigest(),
            provenance={**evidence, 'import_completed_at': datetime.now(timezone.utc).isoformat()},
        ))
    return job_id


def flush_objects(raw, rows):
    if rows:
        with raw.cursor().copy('COPY inventory_objects (migration_id, ordinal, source_key, destination_key, size, source_identity, disposition, reason) FROM STDIN') as copy:
            for row in rows:
                copy.write_row(row)
        rows.clear()


def flush_batches(raw, rows):
    if rows:
        with raw.cursor().copy('COPY batches (migration_id, ordinal, planned_bytes, object_count, oversized) FROM STDIN') as copy:
            for row in rows:
                copy.write_row(row)
        rows.clear()


def report(engine, job_id):
    with engine.connect() as connection:
        connection.execute(text("SELECT set_config('statement_timeout', '120000', true)"))
        job = connection.execute(select(Migration.__table__).where(Migration.id == job_id)).mappings().one()
        groups = connection.execute(text('SELECT disposition, reason, count(*) AS objects, sum(size) AS bytes FROM inventory_objects WHERE migration_id = :job GROUP BY disposition, reason ORDER BY disposition, reason'), {'job': job_id}).mappings()
        totals = [{'disposition': row['disposition'], 'reason': row['reason'], 'objects': row['objects'], 'bytes': int(row['bytes'])} for row in groups]
        batches = connection.execute(text('SELECT count(*) AS count, coalesce(sum(planned_bytes),0) AS bytes, coalesce(min(planned_bytes),0) AS min_bytes, coalesce(max(planned_bytes),0) AS max_bytes FROM batches WHERE migration_id = :job'), {'job': job_id}).mappings().one()
        examples = connection.execute(text('SELECT source_key, destination_key, disposition, reason FROM inventory_objects WHERE migration_id = :job ORDER BY source_key LIMIT 5'), {'job': job_id}).mappings().all()
        return {'job_id': str(job_id), 'state': job['state'], 'mapping': job['config'], 'provenance': job['provenance'],
                'manifest_digest': job['manifest_digest'], 'plan_digest': job['plan_digest'],
                'total_objects': sum(row['objects'] for row in totals), 'total_bytes': sum(row['bytes'] for row in totals),
                'categories': totals, 'batches': {k: int(v) for k, v in batches.items()},
                'mapping_examples': [dict(row) for row in examples], 'transfers_enabled': False,
                'content_verified': False, 'notice': 'Dated inventory observation; not an atomic snapshot or a transfer-ready approval.'}


def export_objects(engine, job_id, path):
    # Exclusive file creation avoids silently overwriting an existing evidence artifact.
    report(engine, job_id)  # Reject unknown IDs before creating output.
    with engine.connect() as connection, open(path, 'x', encoding='utf-8', newline='\n') as output:
        result = connection.execution_options(stream_results=True, max_row_buffer=CHUNK_SIZE).execute(text(
            'SELECT source_key, destination_key, size, source_identity, disposition, reason, batch_ordinal FROM inventory_objects WHERE migration_id = :job ORDER BY source_key'
        ), {'job': job_id}).mappings()
        for row in result:
            output.write(canonical(dict(row)) + '\n')



