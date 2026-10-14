"""Worker-local dispatch query. No SSH or remote shell commands are accepted from HTTP."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from sqlalchemy import text
from migration_runner import security
from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from migration_runner.worker import queue


def poll(engine, node, expires, token, service_state='inactive', *, allowed_nodes=None, scope_check=None):
    if node not in (allowed_nodes or ('worker-1', 'worker-2')):
        raise ValueError('Unknown worker')
    with engine.connect() as c:
        security.worker(c, token, required=True)
        job = c.execute(text('SELECT job_id FROM cloud_ui_dispatch WHERE id=1')).scalar_one()
    action = 'idle'
    if job:
        if scope_check:
            scope_check(job)
        state = queue.status(engine, job)
        if state['bound'] and not state['queue_complete'] and not state['exceptions'] and not state['limit_reached']:
            if int(node[-1]) <= state['run_limits']['max_workers']:
                action = 'run' if state['control'] == 'active' else 'hold'
    with engine.begin() as c:
        c.execute(text('''INSERT INTO cloud_ui_workers (node_id,last_seen,certificate_expires_at,job_id,status,service_state)
            VALUES (:node,clock_timestamp(),:expires,:job,:status,:service_state)
            ON CONFLICT (node_id) DO UPDATE SET last_seen=excluded.last_seen,
            certificate_expires_at=excluded.certificate_expires_at,job_id=excluded.job_id,status=excluded.status,service_state=excluded.service_state'''),
            {'node': node, 'expires': expires, 'job': job, 'status': action, 'service_state': service_state})
    return {'action': action, 'job_id': str(job) if job else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', choices=['worker-1', 'worker-2'], required=True)
    parser.add_argument('--service-state', choices=['active','inactive','activating','deactivating','failed','unknown'], default='unknown')
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        if settings.environment != 'cloud-worker':
            raise ValueError('Worker certificate mode required')
        expires = datetime.fromisoformat(json.loads(Path('/run/migration/expiry.json').read_text())['expires_at'])
        if expires <= datetime.now(timezone.utc):
            raise ValueError('Credentials expired')
        engine = make_engine(settings)
        print(json.dumps(poll(engine, args.node, expires, settings.worker_token.get_secret_value(), args.service_state)))
        return 0
    except Exception:
        print(json.dumps({'dispatch_unavailable': True}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
