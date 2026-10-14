"""Local operator queue controls. Trusted local database administration; use the authenticated operator client for normal operation."""
import argparse
import json
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from . import queue
from .rclone import WorkerError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bind', 'pause', 'drain', 'resume', 'cancel', 'status', 'reap'])
    parser.add_argument('--job', required=True, type=UUID)
    parser.add_argument('--source-root')
    parser.add_argument('--destination-root')
    args = parser.parse_args()
    settings = Settings()
    if settings.db_host not in ('db', '127.0.0.1', 'localhost'):
        parser.error('Only the local lab database is enabled')
    if args.action == 'bind' and not (args.source_root and args.destination_root):
        parser.error('Binding requires both local roots')
    engine = make_engine(settings)
    try:
        if args.action == 'bind':
            queue.configure(engine, args.job, {'source_root': args.source_root, 'destination_root': args.destination_root})
            result = queue.status(engine, args.job)
        elif args.action == 'status':
            result = queue.status(engine, args.job)
        elif args.action == 'reap':
            result = {'reclaimed': queue.reap(engine, args.job)}
        else:
            result = queue.control(engine, args.job, args.action)
        print(json.dumps(result, default=str))
        return 0
    except (WorkerError, SQLAlchemyError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, WorkerError) else 'Local ledger operation failed'}))
        return 1
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
