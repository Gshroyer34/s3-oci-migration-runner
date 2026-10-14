"""python -m migration_runner.benchmark CONFIG [--execute --report NEW_FILE]."""
import argparse
from pathlib import Path

from .adapter import BenchmarkRclone
from .contracts import Run
from .runner import execute, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    run = Run.model_validate_json(args.config.read_text(encoding='utf-8'))
    if not args.execute:
        print({'mode': 'validation_only', 'objects_on_worker': len(run.shard()),
               'bytes_on_worker': sum(obj.size for obj in run.shard()),
               'buffer_estimate_mib': run.buffer_estimate_mib,
               'destination_prefix': run.destination_prefix})
        return 0
    if not args.report or args.report.exists() or not args.report.parent.is_dir():
        parser.error('--execute requires a new report file in an existing directory')
    BenchmarkRclone(run).probe()
    report = execute(run)
    write_report(args.report, report)
    print({'all_objects_completed': report['all_objects_completed'], 'report': str(args.report)})
    return 0 if report['all_objects_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
