"""Operator business operations. Limits and retry admission share the claim lock."""
from datetime import timedelta
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from migration_runner.ledger.models import Batch, Migration
from migration_runner.worker import queue
from migration_runner.worker.rclone import WorkerError


class RunLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_scheduled_bytes: int = Field(ge=1, le=10**18, strict=True)
    max_seconds: int = Field(ge=1, le=86400, strict=True)
    max_workers: int = Field(ge=1, le=4, strict=True)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_root: str
    destination_root: str
    limits: RunLimits


def start(engine, job, request, lab_root):
    from migration_runner.worker.execution import roots
    source, destination = roots(request.source_root, request.destination_root)
    allowed = Path(lab_root).resolve()
    if not source.is_relative_to(allowed) or not destination.is_relative_to(allowed):
        raise WorkerError('Select source and destination directories inside the configured lab root')
    queue.configure(engine, job, {'source_root': str(source), 'destination_root': str(destination)})
    with engine.begin() as c:
        row = queue.job_lock(c, job, exclusive=True)
        if row['control'] == 'cancelled':
            raise WorkerError('Cancelled jobs cannot restart')
        limits = request.limits.model_dump()
        running = c.execute(select(func.count()).select_from(Batch).where(Batch.migration_id == job, Batch.state == 'running')).scalar_one()
        if running and row['run_limits'] != limits:
            raise WorkerError('Drain active work before changing limits')
        if limits['max_scheduled_bytes'] < row['scheduled_bytes']:
            raise WorkerError('The byte limit cannot be lower than already scheduled work')
        now = c.execute(select(func.clock_timestamp())).scalar_one()
        begun = row['run_started_at'] or now
        c.execute(update(Migration).where(Migration.id == job).values(run_limits=limits, run_started_at=begun,
            run_deadline=begun + timedelta(seconds=limits['max_seconds']), control='active'))
        queue.event(c, job, 'operator_start', actor='local-operator', limits=limits)
    return queue.status(engine, job)


def retry(engine, job, batch):
    with engine.begin() as c:
        migration = queue.job_lock(c, job, exclusive=True)
        if migration['control'] == 'cancelled':
            raise WorkerError('Cancelled jobs cannot retry')
        row = c.execute(select(Batch.__table__).where(Batch.migration_id == job, Batch.ordinal == batch).with_for_update()).mappings().one_or_none()
        if not row or row['state'] != 'failed':
            raise WorkerError('Only failed batches can retry; source changes require a new reviewed inventory')
        c.execute(update(Batch).where(Batch.migration_id == job, Batch.ordinal == batch).values(state='planned', claim_count=0, available_at=None))
        queue.event(c, job, 'operator_retry', actor='local-operator', batch=batch)
    return queue.status(engine, job)
