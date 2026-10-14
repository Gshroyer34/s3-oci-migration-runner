"""Generate bounded synthetic files and independent SHA-256 references locally.

Never uploads, authenticates, or overwrites an existing output directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path


def generate(output, objects=100, object_bytes=100_000_000):
    if type(objects) is not int or type(object_bytes) is not int or not 1 <= objects <= 100 or not 1 <= object_bytes <= 100_000_000:
        raise ValueError('Maximum fixture is 100 objects of 100,000,000 bytes each')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    records = []
    seed = os.urandom(32)
    # Independent SHA-256 references are written only after every file completes.
    # A partial directory is retained for inspection and is never considered ready.
    for ordinal in range(objects):
        filename = f'object-{ordinal:03d}.bin'
        sha = hashlib.sha256()
        remaining = object_bytes
        block_number = 0
        with (output / filename).open('xb') as stream:
            while remaining:
                length = min(1024 * 1024, remaining)
                block = hashlib.shake_256(seed + ordinal.to_bytes(4,'big') + block_number.to_bytes(8,'big')).digest(length)
                stream.write(block); sha.update(block)
                remaining -= length; block_number += 1
        records.append({'source_key': 'fixtures/' + filename, 'size': object_bytes, 'sha256': sha.hexdigest()})
    manifest = {'format':'migration-pilot-reference-v1','object_count':objects,
                'total_bytes':objects*object_bytes,'objects':records}
    with (output/'reference.json').open('x',encoding='utf-8') as stream:
        json.dump(manifest,stream,indent=2)
    return {'object_count':objects,'total_bytes':objects*object_bytes,'cloud_contacted':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--objects',default=100,type=int)
    parser.add_argument('--object-bytes',default=100_000_000,type=int)
    args=parser.parse_args()
    try:
        print(json.dumps(generate(args.output,args.objects,args.object_bytes)))
    except (OSError,ValueError):
        raise SystemExit('Fixture generation failed; use a new directory within the bounded pilot scope') from None
