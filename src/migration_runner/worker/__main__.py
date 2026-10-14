import argparse
import json
import subprocess

from migration_runner.config import Settings
from migration_runner.ledger.readiness import ledger_ready


def rclone_ready(settings: Settings) -> bool:
    try:
        result = subprocess.run(
            [settings.rclone_path, "version"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        lines = result.stdout.splitlines()
        return result.returncode == 0 and bool(lines) and lines[0] == f"rclone v{settings.rclone_version}"
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    from uuid import UUID, uuid4
    import signal
    import threading
    from migration_runner.ledger.database import make_engine
    from migration_runner.worker.execution import run_batch, attempt_report, work_once
    from migration_runner.worker.rclone import Rclone, WorkerError
    from migration_runner.worker.queue import status, LeaseLost
    from sqlalchemy.exc import SQLAlchemyError
    import psycopg
    parser = argparse.ArgumentParser(description='Leased local worker; cloud backends disabled.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--attempt-report', type=UUID)
    group.add_argument('--batch', type=int, help='Explicit operator rerun of one batch')
    group.add_argument('--work-once', action='store_true', help='Claim one due batch and return')
    group.add_argument('--run-loop', action='store_true', help='Poll the bound local job until terminal or interrupted')
    parser.add_argument('--job', type=UUID)
    parser.add_argument('--source-root')
    parser.add_argument('--destination-root')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--worker-id')
    args = parser.parse_args()
    if args.batch is not None and args.batch < 1:
        parser.error('--batch must be positive')
    args.worker_id = args.worker_id or str(uuid4())
    settings = Settings()
    if args.check:
        database, rclone = ledger_ready(settings), rclone_ready(settings)
        print(json.dumps({'mode': 'local-preflight', 'ledger_ready': database, 'rclone_ready': rclone, 'transfers_enabled': False}))
        return 0 if database and rclone else 1
    if settings.db_host not in ('127.0.0.1','localhost','db'):
        parser.error('Only the local lab database is enabled')
    if not args.attempt_report and not args.job:
        parser.error('--job is required for execution')
    if bool(args.source_root) != bool(args.destination_root):
        parser.error('Provide both local roots or neither')
    if args.batch and not args.source_root:
        parser.error('Explicit batch reruns require both roots')
    if args.verify_only and not args.batch:
        parser.error('--verify-only requires an explicit --batch')
    engine = make_engine(settings)
    stopped = threading.Event()
    current = [None]
    def stop(*_):
        stopped.set()
        if current[0]:
            current[0].cancel()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if args.attempt_report:
            result = attempt_report(engine, args.attempt_report)
            print(json.dumps(result))
            return 0 if result['state'] == 'verified' else 1
        while not stopped.is_set():
            adapter = Rclone(settings.rclone_path)
            current[0] = adapter
            result = (run_batch(engine, args.job, args.batch, args.source_root, args.destination_root, adapter, args.verify_only)
                if args.batch else work_once(engine, args.job, args.source_root, args.destination_root, adapter, worker=args.worker_id))
            print(json.dumps(result), flush=True)
            if not args.run_loop:
                return 0 if result['state'] in ('verified', 'no_work') else 1
            report = status(engine, args.job)
            if report['queue_complete'] or report['control'] == 'cancelled' or report['drained'] or report['exceptions'] or (report['limit_reached'] and not report['running']):
                print(json.dumps(report, default=str), flush=True)
                return 0 if report['queue_complete'] or report['drained'] else 1
            if result['state'] == 'lease_lost':
                return 1
            stopped.wait(2)
        return 1
    except (WorkerError, LeaseLost, SQLAlchemyError, psycopg.Error) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, (WorkerError, LeaseLost)) else 'Local ledger operation failed'}))
        return 1
    finally:
        stop()
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
