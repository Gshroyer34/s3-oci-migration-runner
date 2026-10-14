"""Finite, deadline-bound object parallelism and full verification evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import platform
import threading
import time

from migration_runner.worker.cloud import inspect_cloud_object
from .adapter import BenchmarkRclone
from .contracts import Run
from .resources import HostSampler


def execute(run, *, factory=BenchmarkRclone, clock=time.monotonic, check_arch=True):
    run = Run.model_validate(run)
    remaining = (run.expiry() - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ValueError('Campaign has expired')
    if check_arch and platform.machine().lower() != run.native_arch:
        raise ValueError('Run requires the specified native worker architecture')
    stop = threading.Event()
    lock = threading.Lock()
    adapters = set()
    started = clock()
    started_at = datetime.now(timezone.utc).isoformat()

    def cancel():
        stop.set()
        with lock:
            for adapter in adapters:
                adapter.cancel()

    def one(obj):
        result = {'key': obj.key, 'size': obj.size, 'status': 'not_started'}
        if stop.is_set():
            return result
        adapter = factory(run)
        with lock:
            adapters.add(adapter)
            if stop.is_set():
                adapter.cancel()
        try:
            adapter.check()
            destination = run.destination_prefix + obj.key[len(run.source_prefix):]
            src, dst = run.reference('source', obj.key), run.reference('destination', destination)
            # A repeat must have a fresh run ID; do not time existing-content skips.
            if adapter.stat(dst) is not None:
                return result | {'status': 'destination_exists'}
            if run.mode == 'verified':
                evidence = inspect_cloud_object(adapter, None, None, {
                    'source_key': obj.key, 'destination_key': destination, 'size': obj.size,
                    'source_identity': {'last_modified': obj.last_modified, 'reference_sha256': obj.sha256},
                })
                success = (evidence['content'] == 'verified' and evidence['structural'] == 'match'
                           and evidence['copy'] == 'process_succeeded')
                result.update(status='verified' if success else 'failed', evidence=evidence)
            else:
                original = adapter.stat(src)
                if not original or original['size'] != obj.size:
                    return result | {'status': 'source_changed'}
                code, events = adapter.copy(src, dst)
                result.update(status='copied_unverified' if code == 0 else 'failed', process_events=events)
            result['telemetry'] = adapter.telemetry.sample()
            return result
        except Exception:
            # Never include raw SDK/process failures which may contain request credentials.
            return result | {'status': 'cancelled' if stop.is_set() else 'failed'}
        finally:
            adapter.cancel()
            with lock:
                adapters.discard(adapter)

    sampler = HostSampler()
    sampler.start()
    timer = threading.Timer(min(run.max_seconds, remaining), cancel)
    timer.daemon = True
    timer.start()
    pool = ThreadPoolExecutor(max_workers=run.object_concurrency)
    try:
        results = list(pool.map(one, run.shard()))
    finally:
        timer.cancel()
        cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        host_resources = sampler.finish()
    elapsed = max(clock() - started, 0.000001)
    verified = sum(r['size'] for r in results if r['status'] == 'verified')
    copied = sum(r['size'] for r in results if r['status'] in ('verified', 'copied_unverified'))
    success = all(r['status'] == ('verified' if run.mode == 'verified' else 'copied_unverified') for r in results)
    return {'format': 'oci-worker-benchmark-result-v1', 'campaign': run.campaign, 'run_id': run.run_id,
            'worker_index': run.worker_index, 'worker_count': run.worker_count,
            'configuration_sha256': hashlib.sha256(run.model_dump_json().encode()).hexdigest(),
            'configuration': run.model_dump(exclude={'objects'}), 'started_at': started_at,
            'finished_at': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': elapsed,
            'native_arch': platform.machine(), 'host_resources': host_resources, 'all_objects_completed': success,
            'ranking_eligible': success and run.mode == 'verified',
            'verified_bytes': verified, 'copied_bytes': copied,
            'verified_bytes_per_second': verified / elapsed,
            'copied_bytes_per_second': copied / elapsed, 'objects': results,
            'measurement': 'application bytes; not interconnect utilization or billing'}


def write_report(path, report):
    # Exclusive creation prevents silently replacing evidence from a prior trial.
    with open(path, 'x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
