"""Explicit pilot CLI. No cloud requests occur on import or input validation."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import threading
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from migration_runner.ledger.models import Migration
from migration_runner import security
from migration_runner.operations import RunLimits
from migration_runner.planning.contracts import Mapping, PlanningError
from migration_runner.planning.service import plan, report
from migration_runner.worker.cloud import CloudBinding, CloudRclone, configure_cloud, attach_references, resolve_binding
from migration_runner.worker.execution import work_once
from migration_runner.worker import queue
from migration_runner.worker.rclone import WorkerError


def require_job_scope(engine, job):
    """Apply portable profile ownership to status and control actions as well as copies."""
    if not (os.environ.get('MIGRATION_DEPLOYMENT_PROFILE') or os.environ.get('MIGRATION_DEPLOYMENT_REVISION')):
        return
    from migration_runner.installation.deployment import installed_profile
    from migration_runner.worker.cloud import DeploymentBinding
    profile = installed_profile()
    with engine.connect() as c:
        row = c.execute(select(Migration.config, Migration.provenance, Migration.execution).where(Migration.id == job)).mappings().one_or_none()
    if row is None or row['provenance'].get('deployment_revision') != profile.revision:
        raise WorkerError('Job does not belong to the installed deployment revision')
    fields = {k: row['config'][k] for k in ('source_bucket', 'source_region', 'source_prefix',
              'destination_bucket', 'destination_namespace', 'destination_region', 'destination_prefix')}
    profile.authorize(DeploymentBinding(**fields, deployment_revision=profile.revision,
                      immutable_source=True, exclusive_destination=True,
                      max_dataset_bytes=profile.limits.max_dataset_bytes, max_objects=profile.limits.max_objects))
    if row['execution']:
        resolve_binding(row['execution']['binding'])
        if row['execution'].get('deployment_profile') != profile.model_dump():
            raise WorkerError('Job deployment snapshot differs from the installed profile')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'inventory-plan', 'start', 'work-once', 'run-loop', 'status', 'pause', 'drain', 'resume', 'cancel', 'export-manifest'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--binding', type=Path)
    parser.add_argument('--job', type=UUID)
    parser.add_argument('--reference-manifest', type=Path)
    parser.add_argument('--worker-id', default='pilot-worker')
    parser.add_argument('--max-scheduled-bytes', type=int, default=10_000_000_000)
    parser.add_argument('--max-seconds', type=int, default=3600)
    parser.add_argument('--max-workers', type=int, default=1)
    args = parser.parse_args()
    engine = None
    active = [None]
    stopped = threading.Event()
    def stop(*_):
        stopped.set()
        if active[0]:
            active[0].cancel()
    try:
        if args.binding and args.binding.stat().st_size > 16384:
            raise WorkerError('Binding file exceeds the size limit')
        binding = resolve_binding(json.loads(args.binding.read_text()))[0] if args.binding else None
        if args.action == 'validate':
            if binding is None:
                raise WorkerError('Binding file is required')
            print(json.dumps({'binding_valid': True, 'cloud_contacted': False, 'transfers_started': False}))
            return 0
        settings = Settings()
        required_mode = 'cloud-worker' if args.action in ('work-once', 'run-loop') else 'cloud-cli'
        if settings.environment != required_mode:
            raise WorkerError('Select the explicit cloud worker or coordinator CLI environment')
        engine = make_engine(settings)
        with engine.connect() as c:
            security.worker(c, settings.worker_token.get_secret_value(), required=True)
        if args.action == 'inventory-plan':
            if binding is None:
                raise WorkerError('Binding file is required')
            if args.reference_manifest is None or args.reference_manifest.stat().st_size > 262144:
                raise WorkerError('A bounded independent fixture reference manifest is required')
            reference = json.loads(args.reference_manifest.read_text())
            adapter = CloudRclone(binding, settings.aws_config_file, settings.rclone_path)
            adapter.probe()
            records = attach_references(adapter.inventory(), reference)
            fields = {k: getattr(binding, k) for k in ('source_bucket', 'source_region', 'source_prefix',
                'destination_namespace', 'destination_bucket', 'destination_region', 'destination_prefix')}
            mapping = Mapping(**fields, target_batch_bytes=500_000_000, max_batch_objects=5)
            provenance = {'kind': 'rclone-pilot-list', 'observed_at': datetime.now(timezone.utc), 'atomic_snapshot': False}
            if binding.kind == 's3-oci-deployment-v1':
                provenance['deployment_revision'] = binding.deployment_revision
            job = plan(engine, mapping, records, provenance)
            print(json.dumps(report(engine, job), default=str))
            return 0
        if args.job is None:
            raise WorkerError('A job ID is required')
        require_job_scope(engine, args.job)
        if args.action == 'export-manifest':
            from migration_runner.manifest import temporary_export
            import shutil
            if args.output is None:
                raise WorkerError('An output ZIP path is required')
            with temporary_export(engine, args.job) as archive:
                # Never overwrite an existing artifact, and do not leave a partial download.
                fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as target:
                        shutil.copyfileobj(archive, target, length=65536)
                except BaseException:
                    args.output.unlink()
                    raise
            print(json.dumps({'job_id': str(args.job), 'manifest_exported': True, 'cloud_contacted': False}))
            return 0
        if args.action == 'start':
            if binding is None:
                raise WorkerError('Binding file is required')
            configure_cloud(engine, args.job, binding, RunLimits(max_scheduled_bytes=args.max_scheduled_bytes,
                max_seconds=args.max_seconds, max_workers=args.max_workers))
            security.audit(engine, 'cloud-cli', 'start', args.job)
        elif args.action in ('work-once', 'run-loop'):
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            with engine.connect() as c:
                config = c.execute(select(Migration.execution).where(Migration.id == args.job)).scalar_one_or_none()
            if not config:
                raise WorkerError('Start and bind the reviewed pilot before workers poll')
            binding = resolve_binding(config['binding'])[0]
            while not stopped.is_set():
                adapter = CloudRclone(binding, settings.aws_config_file, settings.rclone_path)
                active[0] = adapter
                result = work_once(engine, args.job, adapter=adapter, worker=args.worker_id)
                print(json.dumps(result), flush=True)
                if args.action == 'work-once':
                    return 0 if result['state'] in ('verified', 'no_work') else 1
                status = queue.status(engine, args.job)
                if status['queue_complete'] or status['drained']:
                    return 0
                if status['control'] == 'cancelled' or status['exceptions'] or result['state'] == 'lease_lost' or (status['limit_reached'] and not status['running']):
                    return 1
                stopped.wait(2)
            return 1
        elif args.action != 'status':
            queue.control(engine, args.job, args.action)
            security.audit(engine, 'cloud-cli', args.action, args.job)
        print(json.dumps(queue.status(engine, args.job), default=str))
        return 0
    except (ValidationError, WorkerError, PlanningError, queue.LeaseLost, SQLAlchemyError, OSError, ValueError, KeyError):
        # Never include provider stderr, SQL parameters, credentials or record bodies.
        print(json.dumps({'error': 'Pilot operation rejected or failed; check scope, identities, TLS, ledger and run limits'}))
        return 1
    finally:
        stop()
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
