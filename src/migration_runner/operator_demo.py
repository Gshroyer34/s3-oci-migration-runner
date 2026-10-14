"""Create a new 100 MB local fixture for the authenticated operator workflow."""
import json
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from migration_runner.planning.contracts import Mapping, InventoryObject
from migration_runner.planning.service import plan
from migration_runner.worker import queue

settings=Settings()
root=Path(settings.lab_root)/uuid4().hex
source,destination=root/'source',root/'destination'
source.mkdir(parents=True)
destination.mkdir()
rows=[]
for name,size in [('large.bin',100_000_000),('small.txt',33)]:
    with (source/name).open('wb') as f:
        chunk=b'x'*100_000
        for _ in range(size//len(chunk)): f.write(chunk)
        f.write(b'x'*(size%len(chunk)))
    rows.append(InventoryObject(key=name,size=size,storage_class='STANDARD'))
engine=make_engine(settings)
try:
    job=plan(engine,Mapping(source_bucket='local',source_region='local',destination_namespace='local',destination_bucket=root.name,destination_region='local',destination_prefix='out/',target_batch_bytes=100_000_000,open_batches=1),rows,{'kind':'local-fixture','observed_at':datetime.now(timezone.utc)})
    queue.control(engine,job,'pause')
finally:
    engine.dispose()
print(json.dumps({'job_id':str(job),'start_request':{'source_root':str(source),'destination_root':str(destination),'limits':{'max_scheduled_bytes':300_000_000,'max_seconds':3600,'max_workers':2}}}))
