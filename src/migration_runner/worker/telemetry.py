"""Bounded application-byte telemetry. Never a wire-byte or billing meter."""
from collections import deque
from contextlib import contextmanager
import threading
import time

COUNTERS = ('source_hash_bytes', 'source_recheck_bytes', 'destination_verify_bytes',
            'copy_bytes', 'source_retry_bytes', 'copy_retry_bytes')
PHASES = ('idle', 'source_hash', 'source_recheck', 'destination_verify', 'copy')


class WorkerMeter:
    def __init__(self, *, retry=False, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.counts = dict.fromkeys(COUNTERS, 0)
        self.phase_name = 'idle'
        self.batch_retry = retry
        self.copy_retry = False
        self.copy_highwater = 0
        self.copy_seen = False
        self.copy_processes = self.missing_copy_stats = self.publish_errors = 0
        self.sequence = 0
        self.history = deque(maxlen=25)
        self.sample()

    @contextmanager
    def phase(self, name):
        if name not in PHASES:
            raise ValueError('Unknown telemetry phase')
        with self.lock:
            previous, self.phase_name = self.phase_name, name
        try:
            yield
        finally:
            with self.lock:
                self.phase_name = previous

    def read(self, length):
        with self.lock:
            field = {'source_hash':'source_hash_bytes', 'source_recheck':'source_recheck_bytes',
                     'destination_verify':'destination_verify_bytes'}.get(self.phase_name)
            if field:
                self.counts[field] += length
                if self.batch_retry and self.phase_name.startswith('source_'):
                    self.counts['source_retry_bytes'] += length

    def start_copy(self, retry):
        with self.lock:
            self.copy_processes += 1
            self.copy_seen = False
            self.copy_highwater = 0
            self.copy_retry = retry or self.batch_retry

    def copy_stats(self, value):
        """Process-scoped high-water mark avoids duplicate final/periodic stats."""
        if type(value) is not int or not 0 <= value <= 10**18:
            return
        with self.lock:
            if value < self.copy_highwater:
                return
            delta = value - self.copy_highwater
            self.copy_highwater = value
            self.copy_seen = True
            self.counts['copy_bytes'] += delta
            if self.copy_retry:
                self.counts['copy_retry_bytes'] += delta
                self.counts['source_retry_bytes'] += delta

    def end_copy(self, *, complete=True):
        with self.lock:
            if not self.copy_seen or not complete:
                self.missing_copy_stats += 1

    def sample(self):
        with self.lock:
            elapsed = max(0.0, self.clock() - self.started)
            point = {'elapsed_seconds': elapsed, **self.counts}
            # Coalesce samples taken at the same instant; no unbounded history.
            if self.history and elapsed == self.history[-1]['elapsed_seconds']:
                self.history[-1] = point
            else:
                self.history.append(point)
            self.sequence += 1
            return {'schema_version':1, 'sequence':self.sequence, 'kind':'application_bytes',
                    'phase':self.phase_name, 'elapsed_seconds':elapsed, 'counters':dict(self.counts),
                    'source_read_bytes':self.counts['source_hash_bytes']+self.counts['source_recheck_bytes']+self.counts['copy_bytes'],
                    'copy_processes':self.copy_processes, 'missing_copy_stats':self.missing_copy_stats,
                    'publish_errors':self.publish_errors, 'samples':list(self.history)}


class TelemetryPublisher:
    """An independent slow sampler; telemetry faults do not weaken lease checks."""
    def __init__(self, engine, lease, meter):
        self.engine, self.lease, self.meter = engine, lease, meter
        self.stop = threading.Event()

    def publish(self):
        from .queue import record_telemetry
        try:
            record_telemetry(self.engine, self.lease, self.meter.sample())
        except Exception:
            # No raw errors or object names enter telemetry. LeaseGuard remains authoritative.
            with self.meter.lock:
                self.meter.publish_errors += 1

    def _run(self):
        while not self.stop.wait(5):
            self.publish()

    def __enter__(self):
        self.publish()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=4)
