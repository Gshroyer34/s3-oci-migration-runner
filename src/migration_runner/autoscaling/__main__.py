"""Replay bounded JSONL snapshots through the same evaluator used by the API."""
import argparse
import json
from pathlib import Path
import sys

from .contracts import CapacityPolicy, CapacitySnapshot
from .policy import evaluate

MAX_LINE = 65536
MAX_FRAMES = 100_000


def replay(policy, stream):
    state = None
    for index in range(MAX_FRAMES + 1):
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE or index == MAX_FRAMES:
            raise ValueError('Replay input exceeds bounded size')
        frame = CapacitySnapshot.model_validate_json(line)
        decision = evaluate(policy, frame, state)
        state = decision.state
        yield decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() in (args.policy.resolve(), args.input.resolve()):
        parser.error('Output must be separate from inputs')
    # Exclusive output creation prevents accidental overwrites of prior evidence.
    try:
        if args.policy.stat().st_size > MAX_LINE:
            raise ValueError('Policy exceeds bounded size')
        policy = CapacityPolicy.model_validate_json(args.policy.read_bytes())
        count = 0
        with args.input.open('rb') as source, args.output.open('x', encoding='utf-8') as target:
            for decision in replay(policy, source):
                target.write(decision.model_dump_json() + '\n')
                count += 1
            if count == 0:
                raise ValueError('Replay requires at least one frame')
        print(json.dumps({'frames': count, 'mode': policy.mode, 'actuation_enabled': False}))
        return 0
    except (OSError, ValueError):
        # Do not echo arbitrary JSON inputs, paths, or credentials in errors.
        print('Replay failed: check input schema, timestamps, limits, and output path. '
              'Any partial output is incomplete.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
