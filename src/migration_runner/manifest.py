"""Read-only, snapshot-consistent, partitioned evidence export. No cloud calls."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import tempfile
from uuid import UUID
import zipfile

from sqlalchemy import text
from .worker.properties import CONTRACT
from .worker.rclone import WorkerError

MAX_BYTES = 100 * 1024 * 1024  # Uncompressed; reject oversized exports without truncation.
MAX_OBJECTS = 1_000_000
PART_OBJECTS = 10_000


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, default=str, separators=(',', ':')) + '\n').encode()


def object_record(row):
    evidence = row.get('evidence') or {}
    return {
        'ordinal': row['ordinal'], 'source_key': row['source_key'],
        'destination_key': row['destination_key'], 'size': row['size'],
        'source_identity_at_planning': row['source_identity'],
        'disposition': row['disposition'], 'reason': row['reason'],
        'batch_state': row['batch_state'], 'attempt_id': row['current_attempt_id'],
        'evidence_checked_at': row['checked_at'],
        'verification': {k: evidence.get(k, 'not_captured') for k in
                         ('structural', 'content', 'tags', 'properties', 'copy', 'method')},
        'source_tags': evidence.get('source_tags'),
        'source_properties': evidence.get('source_properties'),
        'destination_properties': evidence.get('destination_properties'),
        'property_mapping': evidence.get('property_mapping'),
        'property_comparison': evidence.get('property_comparison'),
        'property_error': evidence.get('property_error'),
        'property_review': evidence.get('property_review', ['property_capture_not_available_for_this_attempt']),
        'properties_observed_at': evidence.get('properties_observed_at'),
        'digests': {k: evidence.get(k) for k in ('source_sha256', 'destination_sha256',
                                               'source_tags_sha256', 'source_properties_sha256')},
        'error': evidence.get('error'),
    }


def write_archive(output, metadata, records, *, part_objects=PART_OBJECTS, max_bytes=MAX_BYTES, max_objects=MAX_OBJECTS):
    """Consumes records once; no object list retained. Caller discards any failed output."""
    if not 1 <= part_objects <= PART_OBJECTS:
        raise ValueError('Invalid partition size')
    parts = []
    count = total_bytes = 0
    dispositions, property_status, content_status = Counter(), Counter(), Counter()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        part = None
        try:
            for row in records:
                record = object_record(row)
                line = encode(record)
                total_bytes += len(line)
                count += 1
                if count > max_objects or total_bytes > max_bytes:
                    raise WorkerError('Manifest exceeds the export limit; no complete archive was produced')
                if (count - 1) % part_objects == 0:
                    if part is not None:
                        part.close()
                        parts[-1]['sha256'] = digest.hexdigest()
                    name = f'objects/part-{len(parts) + 1:06d}.jsonl'
                    part = archive.open(name, 'w', force_zip64=True)
                    digest = hashlib.sha256()
                    parts.append({'path': name, 'objects': 0, 'bytes': 0})
                part.write(line)
                digest.update(line)
                parts[-1]['objects'] += 1
                parts[-1]['bytes'] += len(line)
                dispositions[record['disposition']] += 1
                property_status[record['verification']['properties']] += 1
                content_status[record['verification']['content']] += 1
        finally:
            if part is not None:
                part.close()
        if parts:
            parts[-1]['sha256'] = digest.hexdigest()
        mapping = encode(CONTRACT)
        archive.writestr('property-mapping.json', mapping)
        index = {
            'schema': 'migration-evidence-manifest/v1', **metadata,
            'objects': count, 'dispositions': dict(dispositions),
            'property_verification': dict(property_status), 'content_verification': dict(content_status),
            'parts': parts, 'mapping_sha256': hashlib.sha256(mapping).hexdigest(),
            'bucket_lifecycle': {'status': 'not_captured', 'applied_to_destination': False},
            'evidence_scope': 'Current batch attempt only. No evidence means not captured; historical results are not backfilled from live clouds.',
            'cutover_approved': False,
            'sensitivity': 'Contains original object keys, metadata, tags and possibly owner identities. Protect like source data.',
            'integrity': 'Part digests detect corruption; they are not a cryptographic signature or independent proof of migration.',
        }
        archive.writestr('index.json', encode(index))
    return index


def export(engine, job, output, *, source_bucket=None):
    job = UUID(str(job))
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        with c.begin():
            c.execute(text('SET TRANSACTION READ ONLY'))
            c.execute(text("SET LOCAL statement_timeout='120s'"))
            migration = c.execute(text('''SELECT source_bucket,destination_bucket,destination_namespace,
                destination_region,config,execution,plan_digest,manifest_digest,control,state
                FROM migrations WHERE id=:j'''), {'j': job}).mappings().one_or_none()
            if migration is None or (source_bucket is not None and migration['source_bucket'] != source_bucket):
                raise WorkerError('Unknown migration or migration outside the allowed scope')
            observed = c.execute(text('SELECT transaction_timestamp()')).scalar_one()
            metadata = {'job_id': str(job), 'snapshot_at': observed.isoformat(),
                        'generated_at': datetime.now(timezone.utc).isoformat(),
                        'source_bucket': migration['source_bucket'],
                        'destination_bucket': migration['destination_bucket'],
                        'destination_namespace': migration['destination_namespace'],
                        'destination_region': migration['destination_region'],
                        'mapping': migration['config'], 'state': migration['state'], 'control': migration['control'],
                        'plan_digest': migration['plan_digest'], 'inventory_digest': migration['manifest_digest'],
                        'required_property_mapping': (migration['execution'] or {}).get('property_mapping')}
            rows = c.execution_options(stream_results=True, max_row_buffer=500).execute(text('''
                SELECT o.ordinal,o.source_key,o.destination_key,o.size,o.source_identity,o.disposition,o.reason,
                       b.state batch_state,b.current_attempt_id,e.checked_at,e.evidence
                FROM inventory_objects o
                LEFT JOIN batches b ON b.migration_id=o.migration_id AND b.ordinal=o.batch_ordinal
                LEFT JOIN object_evidence e ON e.migration_id=o.migration_id
                     AND e.object_ordinal=o.ordinal AND e.attempt_id=b.current_attempt_id
                WHERE o.migration_id=:j ORDER BY o.ordinal'''), {'j': job}).mappings()
            return write_archive(output, metadata, rows)


def temporary_export(engine, job, *, source_bucket=None):
    """Anonymous private file; streaming response closes it on completion/disconnect."""
    output = tempfile.TemporaryFile(mode='w+b')
    try:
        export(engine, job, output, source_bucket=source_bucket)
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
