"""Explicit local-only demonstration; creates synthetic files in a fresh /tmp directory."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from migration_runner.planning.contracts import InventoryObject, Mapping
from migration_runner.planning.service import plan
from .execution import run_batch
from .rclone import Rclone, WorkerError


def main():
    settings = Settings()
    if sys.platform != 'linux' or settings.db_host not in ('db', 'localhost', '127.0.0.1'):
        raise WorkerError('Run the demo only in the local OL9 lab')
    folder = Path(tempfile.mkdtemp(prefix='migration-demo-'))
    source, destination = folder / 'source', folder / 'destination'
    (source / 'data').mkdir(parents=True)
    destination.mkdir()
    with (source / 'data/100mb.bin').open('wb') as stream:
        for _ in range(100):
            stream.write(b'0123456789' * 100000)
    (source / 'data/zero').write_bytes(b'')
    (source / 'data/café [x]*?+.bin').write_bytes(b'Unicode and wildcards are literal')
    (destination / 'retained-extra').write_bytes(b'keep')
    mapping = Mapping(source_bucket='local-demo', source_region='local', source_prefix='data/',
        destination_namespace='local-demo', destination_bucket=folder.name, destination_region='local',
        destination_prefix='mapped/', open_batches=1)
    inventory = [InventoryObject(key=p.relative_to(source).as_posix(), size=p.stat().st_size,
        last_modified=datetime.fromtimestamp(p.stat().st_mtime, timezone.utc), storage_class='STANDARD')
        for p in sorted((source / 'data').iterdir())]
    engine = make_engine(settings)
    try:
        job = plan(engine, mapping, inventory, {'kind': 'local-demo', 'observed_at': datetime.now(timezone.utc)})
        adapter = Rclone(settings.rclone_path)
        first = run_batch(engine, job, 1, source, destination, adapter)
        repeat = run_batch(engine, job, 1, source, destination, adapter)
        target = destination / 'mapped/100mb.bin'
        with target.open('r+b') as stream:
            stream.write(b'X')  # Deliberate same-size corruption of this new disposable fixture only.
        corruption = run_batch(engine, job, 1, source, destination, adapter, verify_only=True)
        repaired = run_batch(engine, job, 1, source, destination, adapter)
        extra_retained = (destination / 'retained-extra').read_bytes() == b'keep'
        result = {'job_id': str(job), 'local_fixture_root': str(folder), 'first': first, 'repeat': repeat,
                  'corruption_check': corruption, 'repair': repaired, 'extra_retained': extra_retained,
                  'cloud_requests': 0, 'backend': 'local', 'content_method': 'full-read-sha256'}
        print(json.dumps(result, indent=2))
        if not (first['state'] == repeat['state'] == repaired['state'] == 'verified' and
                repeat['copy_process_successes'] == 0 and corruption['state'] == 'failed' and extra_retained):
            raise WorkerError('Local demonstration did not meet its acceptance checks')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
