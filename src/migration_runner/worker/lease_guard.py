"""Heartbeat plus an independent monotonic watchdog, including during blocked I/O."""
import threading
import time

from .queue import LeaseLost, heartbeat


class LeaseGuard:
    def __init__(self, engine, lease, adapter):
        self.engine, self.lease, self.adapter = engine, lease, adapter
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.deadline = time.monotonic()  # Never start payload work before confirmed renewal.

    def renew(self):
        started = time.monotonic()
        heartbeat(self.engine, self.lease)
        self.deadline = started + self.lease.policy.lease_seconds

    def abort(self):
        self.lost.set()
        self.adapter.cancel()

    def check(self):
        if self.lost.is_set() or time.monotonic() >= self.deadline:
            self.abort()
            raise LeaseLost('Lease ownership could not be confirmed')

    def _heartbeat(self):
        while not self.stop.wait(self.lease.policy.lease_seconds / 4):
            try:
                self.renew()
            except Exception:
                if not self.stop.is_set():
                    self.abort()
                return

    def _watch(self):
        while not self.stop.wait(0.1):
            if time.monotonic() >= self.deadline:
                self.abort()
                return

    def __enter__(self):
        self.renew()
        self.adapter.lease_check = self.check
        self.threads = [threading.Thread(target=self._heartbeat, daemon=True), threading.Thread(target=self._watch, daemon=True)]
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)
        self.adapter.lease_check = None
