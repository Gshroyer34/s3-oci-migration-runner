"""Bounded reports; never infer content verification from size or copy exit code."""
from sqlalchemy import text
from migration_runner.worker import queue


def report(engine, job):
    result = queue.status(engine, job)
    with engine.connect() as c:
        c.execute(text("SET statement_timeout='120s'"))
        rows = c.execute(text('SELECT disposition,count(*) objects,coalesce(sum(size),0) bytes FROM inventory_objects WHERE migration_id=:j GROUP BY disposition'), {'j': job}).mappings()
        result['inventory'] = {r['disposition']: {'objects': r['objects'], 'bytes': int(r['bytes'])} for r in rows}
        metrics = c.execute(text("""WITH latest AS (
          SELECT DISTINCT ON (object_ordinal) object_ordinal,evidence FROM object_evidence
          WHERE migration_id=:j ORDER BY object_ordinal,checked_at DESC,attempt_id DESC), copied AS (
          SELECT DISTINCT object_ordinal FROM object_evidence WHERE migration_id=:j AND evidence->>'copy'='process_succeeded')
          SELECT coalesce(sum(o.size) FILTER (WHERE cp.object_ordinal IS NOT NULL),0) copied_bytes,
          coalesce(sum(o.size) FILTER (WHERE l.evidence->>'content'='verified' AND l.evidence->>'structural'='match'
            AND l.evidence->>'method'='full-read-sha256' AND l.evidence->>'copy'<>'process_failed'),0) content_verified_bytes,
          count(*) FILTER (WHERE l.evidence->>'content'='verified' AND l.evidence->>'structural'='match'
            AND l.evidence->>'method'='full-read-sha256' AND l.evidence->>'copy'<>'process_failed') content_verified_objects
          FROM inventory_objects o LEFT JOIN latest l ON l.object_ordinal=o.ordinal
          LEFT JOIN copied cp ON cp.object_ordinal=o.ordinal WHERE o.migration_id=:j AND o.disposition='eligible'"""), {'j': job}).mappings().one()
        result.update({k: int(v) for k,v in metrics.items()})
        result['retries'] = c.execute(text('SELECT count(*)-count(DISTINCT batch_ordinal) FROM worker_attempts WHERE migration_id=:j'), {'j': job}).scalar_one()
        result['exceptions_detail'] = [dict(r) for r in c.execute(text("SELECT ordinal,state,claim_count FROM batches WHERE migration_id=:j AND state IN ('failed','source_changed','retry_wait','cancelled') ORDER BY ordinal LIMIT 100"), {'j': job}).mappings()]
        result['failed_objects'] = [dict(r) for r in c.execute(text("""WITH latest AS (SELECT DISTINCT ON (object_ordinal) object_ordinal,evidence FROM object_evidence WHERE migration_id=:j ORDER BY object_ordinal,checked_at DESC,attempt_id DESC) SELECT o.ordinal,o.source_key,o.batch_ordinal,l.evidence->>'content' AS content,l.evidence->>'structural' AS structural,l.evidence->>'copy' AS copy,l.evidence->>'error' AS error FROM latest l JOIN inventory_objects o ON o.migration_id=:j AND o.ordinal=l.object_ordinal WHERE l.evidence->>'content' IN ('mismatch','failed','source_changed') OR l.evidence->>'copy'='process_failed' OR l.evidence->>'structural' NOT IN ('match','unchecked') OR l.evidence ? 'error' ORDER BY o.ordinal LIMIT 100"""), {'j': job}).mappings()]
        result['mapping_preview'] = [dict(r) for r in c.execute(text('SELECT ordinal,source_key,destination_key,size,disposition,reason FROM inventory_objects WHERE migration_id=:j ORDER BY ordinal LIMIT 20'), {'j': job}).mappings()]
    eligible = result['inventory'].get('eligible', {'objects':0,'bytes':0})
    result.update(verification_method='full-read-sha256', coverage_objects=result['content_verified_objects'],
        full_content_verified=bool(eligible['objects'] and result['content_verified_objects']==eligible['objects'] and result['queue_complete']),
        rolling_payload_gbps=None, eta_seconds=None, eta_confidence='unknown: live payload telemetry unavailable')
    return result
