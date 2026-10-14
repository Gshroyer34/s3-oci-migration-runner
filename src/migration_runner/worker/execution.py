"""Leased local worker with durable, fenced per-object evidence."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from migration_runner.ledger.models import Attempt, Batch, Migration, ObjectEvidence, ObjectRecord
from .rclone import Rclone, WorkerError, safe_path


def roots(source, destination):
    if sys.platform != 'linux':
        raise WorkerError('Run local transfers in the Linux/OL9 worker, not on Windows')
    paths = []
    for name in (source, destination):
        p = Path(name)
        if not p.is_absolute() or not p.is_dir() or any(x.is_symlink() for x in (p, *p.parents)):
            raise WorkerError('Use existing absolute local directories without symlinks')
        paths.append(p.resolve())
    if paths[0].is_relative_to(paths[1]) or paths[1].is_relative_to(paths[0]):
        raise WorkerError('Source and destination roots must be disjoint')
    return paths


def inspect_object(adapter, source, destination, row, baseline=None, verify_only=False, before_copy=None):
    evidence = {'structural': 'unchecked', 'content': 'unverified', 'method': 'full-read-sha256',
                'copy': 'not_started', 'process_events': [], 'source_sha256': None, 'destination_sha256': None}
    try:
        src = safe_path(source, row['source_key'])
        dst = safe_path(destination, row['destination_key'])
        if not src.is_file() or src.stat().st_size != row['size']:
            evidence['structural'] = 'source_changed'
            return evidence
        observed = row['source_identity'].get('last_modified')
        if observed and abs(src.stat().st_mtime - datetime.fromisoformat(observed).timestamp()) > 0.000001:
            evidence['structural'] = 'source_changed'
            return evidence
        original_stat = src.stat()
        source_digest = adapter.digest_phase(src, 'source_hash')
        evidence['source_sha256'] = source_digest[1]
        if source_digest[0] != row['size'] or (baseline and source_digest[1] != baseline):
            evidence['structural'] = 'source_changed'
            return evidence
        if before_copy:
            before_copy(evidence.copy())  # Durable baseline before any write.
        destination_digest = adapter.digest_phase(dst, 'destination_verify') if dst.is_file() else None
        if destination_digest == source_digest:
            evidence['copy'] = 'skipped_content_match'
        elif not verify_only:
            dst.parent.mkdir(parents=True, exist_ok=True)
            safe_path(destination, row['destination_key'])
            code, events = adapter.copy(src, dst)
            evidence['process_events'] = events
            evidence['copy'] = 'process_succeeded' if code == 0 else 'process_failed'
            evidence['log_level_counts'] = getattr(adapter, 'last_log_counts', {})
            # A failed process can leave a complete or partial destination. Inspect it.
            destination_digest = adapter.digest_phase(dst, 'destination_verify') if dst.is_file() else None
        evidence['structural'] = 'match' if destination_digest and destination_digest[0] == row['size'] else 'missing_or_size_mismatch'
        if destination_digest:
            evidence['destination_sha256'] = destination_digest[1]
        # Re-read source after the operation: changed bytes invalidate the evidence.
        final_digest = adapter.digest_phase(src, 'source_recheck')
        final_stat = src.stat()
        if final_digest != source_digest or (final_stat.st_ino, final_stat.st_mtime_ns, final_stat.st_size) != (original_stat.st_ino, original_stat.st_mtime_ns, original_stat.st_size):
            evidence['structural'] = 'source_changed'
        elif evidence['structural'] == 'match':
            evidence['content'] = 'verified' if destination_digest == source_digest else 'mismatch'
        return evidence
    except (OSError, WorkerError):
        evidence['error'] = 'local_io_or_process_failure'
        return evidence


def run_batch(engine, job, batch, source_root, destination_root, adapter=None, verify_only=False):
    """Explicit operator rerun. All writes still go through the same lease fence."""
    return work_once(engine, job, source_root, destination_root, adapter, verify_only, batch=batch, manual=True)


def work_once(engine, job, source_root=None, destination_root=None, adapter=None, verify_only=False, batch=None, manual=False, worker=None):
    from . import queue
    from .lease_guard import LeaseGuard
    if source_root is not None and destination_root is not None:
        queue.configure(engine, job, {'source_root': str(source_root), 'destination_root': str(destination_root)})
    with engine.connect() as c:
        config = c.execute(select(Migration.execution).where(Migration.id == job)).scalar_one_or_none()
    if not config:
        raise WorkerError('Bind the local job roots before claiming work')
    cloud = config['binding'].get('kind') in ('s3-oci-pilot', 's3-oci-deployment-v1')
    if cloud:
        from .cloud import CloudRclone, resolve_binding
        binding, deployment = resolve_binding(config['binding'])
        if deployment:
            from migration_runner.operations import RunLimits
            with engine.connect() as c:
                limits = c.execute(select(Migration.run_limits).where(Migration.id == job)).scalar_one()
            deployment.authorize(binding, RunLimits.model_validate(limits))
            if config.get('deployment_profile') != deployment.model_dump():
                raise WorkerError('Job deployment snapshot differs from the installed profile')
        if not isinstance(adapter, CloudRclone) or adapter.binding != binding:
            raise WorkerError('An explicitly configured matching cloud adapter is required')
        from migration_runner import security
        from migration_runner.config import Settings
        with engine.connect() as c:
            security.worker(c, Settings().worker_token.get_secret_value(), required=True)
        source, destination = 'source', 'destination'
    else:
        import os
        if os.environ.get('MIGRATION_DEPLOYMENT_PROFILE') or os.environ.get('MIGRATION_DEPLOYMENT_REVISION'):
            raise WorkerError('Deployment-scoped workers cannot execute local bindings')
        source, destination = roots(**{'source': config['binding']['source_root'], 'destination': config['binding']['destination_root']})
        adapter = adapter or Rclone()
    # Live adapters cannot be shared across workers.
    with adapter.process_lock:
        if adapter.processes or adapter.lease_check is not None:
            raise WorkerError('An adapter cannot be shared by concurrent workers')
    capabilities = {'source': adapter.probe(source), 'destination': adapter.probe(destination)}
    lease = queue.claim(engine, job, worker=worker, batch=batch, manual=manual, verify_only=verify_only)
    if lease is None:
        return {'state': 'no_work', 'job_id': str(job)}
    try:
        with LeaseGuard(engine, lease, adapter) as guard:
            from .telemetry import WorkerMeter, TelemetryPublisher
            with engine.connect() as c:
                claims = c.execute(select(Batch.claim_count).where(Batch.migration_id == job, Batch.ordinal == lease.batch)).scalar_one()
            adapter.telemetry = WorkerMeter(retry=claims > 1)
            with TelemetryPublisher(engine, lease, adapter.telemetry):
                return _run(engine, lease, source, destination, adapter, verify_only, capabilities, guard)
    except WorkerError:
        try:
            return queue.finish(engine, lease, {'attempt_id': str(lease.attempt), 'state': 'failed',
                'reason': 'invalid_local_execution', 'payload_transfer_bytes': None})
        except queue.LeaseLost:
            adapter.cancel()
            return {'state': 'lease_lost', 'attempt_id': str(lease.attempt)}
    except queue.LeaseLost:
        adapter.cancel()
        return {'state': 'lease_lost', 'attempt_id': str(lease.attempt)}


def _run(engine, lease, source, destination, adapter, verify_only, capabilities, guard):
    from . import queue
    job, batch, attempt = lease.job, lease.batch, lease.attempt
    from .cloud import CloudRclone, inspect_cloud_object
    cloud = isinstance(adapter, CloudRclone)
    binding = adapter.binding.model_dump() if cloud else {'source_root': str(source), 'destination_root': str(destination)}
    inspect = inspect_cloud_object if cloud else inspect_object
    with engine.connect() as c:
        planned = c.execute(select(Batch.__table__).where(Batch.migration_id == job, Batch.ordinal == batch)).mappings().one()
        execution = c.execute(select(Migration.execution).where(Migration.id == job)).scalar_one()
    property_mapping = execution.get('property_mapping') if cloud else None
    total = verified = verified_bytes = copied = failed = 0
    retryable = source_changed = False
    # Explicit, private JSONL manifest, built from the ledger. No filters or bucket scans.
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as manifest:
        digest = hashlib.sha256()
        size_sum = 0
        with engine.connect() as c:
            records = c.execution_options(stream_results=True, max_row_buffer=1000).execute(select(ObjectRecord.__table__).where(ObjectRecord.migration_id == job, ObjectRecord.batch_ordinal == batch).order_by(ObjectRecord.ordinal)).mappings()
            for row in records:
                if row['disposition'] != 'eligible':
                    raise WorkerError('Batch contains an ineligible object')
                # Validate every path before any payload operation starts.
                if cloud:
                    adapter.validate_copy(adapter.binding.reference('source', row['source_key']),
                                          adapter.binding.reference('destination', row['destination_key']))
                else:
                    safe_path(source, row['source_key'])
                    safe_path(destination, row['destination_key'])
                record = {k: row[k] for k in ('ordinal', 'source_key', 'destination_key', 'size', 'source_identity')}
                line = json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n'
                manifest.write(line)
                digest.update(line.encode())
                total += 1
                size_sum += row['size']
        if total != planned['object_count'] or size_sum != planned['planned_bytes']:
            raise WorkerError('Batch totals do not match its explicit membership')
        settings = {'binding': binding, 'policy': asdict(lease.policy), 'automatic_retry': lease.automatic, 'capabilities': capabilities, 'limits': asdict(adapter.limits),
                    'manifest_sha256': digest.hexdigest(), 'verify_only': verify_only,
                    'concurrent_objects': 1, 'verification': 'full-read-sha256'}
        queue.set_settings(engine, lease, settings)
        manifest.seek(0)
        try:
            for line in manifest:
                adapter.check()
                row = json.loads(line)
                with engine.connect() as c:
                    baseline = c.execute(select(ObjectEvidence.evidence['source_sha256'].astext).where(
                        ObjectEvidence.migration_id == job, ObjectEvidence.object_ordinal == row['ordinal'],
                        ObjectEvidence.evidence['source_sha256'].astext.is_not(None)
                    ).order_by(ObjectEvidence.checked_at).limit(1)).scalar_one_or_none()
                    tag_baseline = c.execute(select(ObjectEvidence.evidence['source_tags_sha256'].astext).where(
                        ObjectEvidence.migration_id == job, ObjectEvidence.object_ordinal == row['ordinal'],
                        ObjectEvidence.evidence['source_tags_sha256'].astext.is_not(None)
                    ).order_by(ObjectEvidence.checked_at).limit(1)).scalar_one_or_none() if cloud else None
                    property_baseline = c.execute(select(ObjectEvidence.evidence['source_properties_sha256'].astext).where(
                        ObjectEvidence.migration_id == job, ObjectEvidence.object_ordinal == row['ordinal'],
                        ObjectEvidence.evidence['source_properties_sha256'].astext.is_not(None)
                    ).order_by(ObjectEvidence.checked_at).limit(1)).scalar_one_or_none() if property_mapping else None
                evidence = inspect(adapter, source, destination, row, baseline, verify_only,
                    lambda value: queue.record(engine, lease, row['ordinal'], value),
                    **({'tag_baseline': tag_baseline, 'property_mapping': property_mapping,
                        'property_baseline': property_baseline} if cloud else {}))
                queue.record(engine, lease, row['ordinal'], evidence)
                source_changed = source_changed or evidence['structural'] == 'source_changed'
                retryable = retryable or bool(evidence.get('error')) or any(e.get('exit_code') in (5, 124) for e in evidence['process_events'])
                good = (evidence['content'] == 'verified' and evidence['copy'] != 'process_failed'
                        and (not cloud or evidence.get('tags') == 'verified')
                        and (not property_mapping or evidence.get('properties') == 'verified'))
                verified += int(good)
                verified_bytes += row['size'] if good else 0
                copied += int(evidence['copy'] == 'process_succeeded')
                failed += int(not good)
        except queue.LeaseLost:
            raise
        except Exception:
            # Preserve partial evidence; no raw exception/credentials in the ledger.
            failed = max(1, total - verified)
            retryable = True
        summary = {'attempt_id': str(attempt), 'state': 'verified' if not failed and verified == total else 'failed',
                   'objects': total, 'verified_objects': verified, 'verified_bytes': verified_bytes,
                   'copy_process_successes': copied, 'failed_or_unprocessed': total - verified,
                   'payload_transfer_bytes': None, 'scope': 'one batch; not migration completion'}
        summary['telemetry'] = adapter.telemetry.sample()
        guard.renew()
        guard.check()
        guard.stop.set()  # Prevent a late heartbeat from treating our own completion as lease loss.
        return queue.finish(engine, lease, summary, retryable=retryable, source_changed=source_changed)



def attempt_report(engine, attempt):
    with engine.connect() as c:
        result = c.execute(select(Attempt.__table__).where(Attempt.id == attempt)).mappings().one_or_none()
        if not result:
            raise WorkerError('Unknown attempt')
        return {'attempt_id': str(attempt), 'state': result['state'], 'summary': result['summary'], 'settings': result['settings']}
