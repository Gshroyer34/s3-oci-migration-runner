"""Fleet aggregation uses a shared wall-clock interval, never sums worker rates."""
from datetime import datetime


def aggregate(reports):
    if not reports:
        raise ValueError('No worker reports')
    first = reports[0]
    count = first['worker_count']
    if len(reports) != count or {r['worker_index'] for r in reports} != set(range(count)):
        raise ValueError('Missing or duplicate worker shards')
    identity = ('campaign', 'run_id', 'worker_count')
    common = ('mode', 'shape', 'native_arch', 'object_concurrency', 'part_size_mib',
              'part_concurrency', 'source_bucket', 'destination_bucket', 'namespace', 'region')
    for report in reports:
        if (any(report[k] != first[k] for k in identity)
                or any(report['configuration'][k] != first['configuration'][k] for k in common)):
            raise ValueError('Reports describe different benchmark trials')
    keys = [obj['key'] for report in reports for obj in report['objects']]
    if len(set(keys)) != len(keys):
        raise ValueError('Object appears in multiple worker shards')
    start = min(datetime.fromisoformat(r['started_at']) for r in reports)
    finish = max(datetime.fromisoformat(r['finished_at']) for r in reports)
    duration = (finish - start).total_seconds()
    if duration <= 0:
        raise ValueError('Invalid fleet measurement interval')
    verified = sum(r['verified_bytes'] for r in reports)
    copied = sum(r['copied_bytes'] for r in reports)
    complete = all(r['all_objects_completed'] for r in reports)
    mode = first['configuration']['mode']
    return {'campaign': first['campaign'], 'run_id': first['run_id'], 'worker_count': count,
            'mode': mode, 'all_objects_completed': complete,
            'elapsed_seconds': duration, 'verified_bytes': verified,
            'verified_bytes_per_second': verified / duration,
            'copied_bytes': copied, 'copied_bytes_per_second': copied / duration,
            'copy_only_ranking_eligible': complete and mode == 'copy-only',
            'ranking_eligible': all(r['ranking_eligible'] for r in reports),
            'requires_synchronized_worker_clocks': True}
