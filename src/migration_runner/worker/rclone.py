"""Pinned, credential-isolated local Rclone adapter. No shell or remote configuration."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import signal
import sys


class WorkerError(ValueError):
    """Safe operator-facing error (never include raw process output)."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int = 3600
    attempts: int = 2
    backoff_seconds: float = 1

    def __post_init__(self):
        if not 1 <= self.timeout_seconds <= 86400 or not 1 <= self.attempts <= 3 or not 0 <= self.backoff_seconds <= 30:
            raise WorkerError('Invalid worker limits')


def safe_path(root: Path, key: str) -> Path:
    # Local execution is stricter than S3: no directories, normalization, or links.
    if not key or key != key.strip() or any(c in key for c in '\r\n\\\0') or any(p in ('', '.', '..') for p in key.split('/')):
        raise WorkerError('Object requires an unsupported exact-key adapter')
    result = root.joinpath(*key.split('/'))
    for component in (result, *result.parents):
        if component.is_symlink():
            raise WorkerError('Symlinks are unsupported')
    if not result.resolve().is_relative_to(root):
        raise WorkerError('Object escapes its local root')
    if result.exists() and not result.is_file():
        raise WorkerError('Object path is not a regular file')
    return result


class Rclone:
    version = '1.75.1'

    def __init__(self, executable='rclone', limits=None):
        from .telemetry import WorkerMeter
        self.telemetry = WorkerMeter()
        self.cancelled = threading.Event()
        self.process_lock = threading.RLock()
        self.processes = set()
        self.lease_check = None
        self.executable = executable
        self.limits = limits or Limits()
        # No inherited RCLONE_*, AWS_*, OCI_*, config file, or proxy credentials.
        self.env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'TEMP', 'SYSTEMROOT', 'LANG')}

    def check(self):
        from .queue import LeaseLost
        if self.cancelled.is_set():
            raise LeaseLost('Worker process was stopped')
        if self.lease_check:
            self.lease_check()

    def cancel(self):
        self.cancelled.set()
        with self.process_lock:
            for process in self.processes:
                self.kill(process)

    @staticmethod
    def kill(process):
        try:
            if process.poll() is None:
                if sys.platform == 'linux':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
        except ProcessLookupError:
            pass

    def spawn(self, args, **kwargs):
        self.check()
        command = [sys.executable, '-m', 'migration_runner.worker.child', str(os.getpid()), *args] if sys.platform == 'linux' else args
        process = subprocess.Popen(command, env=self.env, start_new_session=sys.platform == 'linux', **kwargs)
        with self.process_lock:
            self.processes.add(process)
            if self.cancelled.is_set():
                self.kill(process)
        return process

    def release(self, process):
        with self.process_lock:
            self.processes.discard(process)

    def args(self, command, *paths):
        return [self.executable, command, *map(str, paths), '--config', os.devnull,
                '--use-json-log', '--log-level', 'ERROR', '--retries', '1',
                '--low-level-retries', '1', '--contimeout', '10s', '--timeout', '60s']

    def local_path(self, value, *, require_file=False):
        path = Path(value)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise WorkerError('Only absolute local paths without symlinks are enabled')
        if require_file and not path.is_file():
            raise WorkerError('Expected a local regular file')
        return path

    def probe(self, root):
        self.local_path(root)
        if not Path(root).is_dir():
            raise WorkerError('Probe requires an existing local directory')
        try:
            version = subprocess.run([self.executable, 'version'], env=self.env, capture_output=True, text=True, timeout=10)
            if version.returncode or version.stdout.splitlines()[0] != 'rclone v' + self.version:
                raise WorkerError('Pinned Rclone version is unavailable')
            result = subprocess.run(self.args('backend', 'features', root), env=self.env, capture_output=True, text=True, timeout=10)
            data = json.loads(result.stdout)
            if result.returncode or data['Name'] != 'local' or not data['Features']['IsLocal'] or data['Features']['CaseInsensitive']:
                raise WorkerError('Only a case-sensitive local backend is enabled')
            return {'backend': 'local', 'rclone_version': self.version, 'hashes': data['Hashes'], 'case_sensitive': True}
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError) as exc:
            raise WorkerError('Local backend capability probe failed') from exc

    def copy(self, source, destination):
        """Force copy only after independent content comparison has found a difference."""
        events = []
        for attempt in range(1, self.limits.attempts + 1):
            self.check()
            self.telemetry.start_copy(retry=attempt > 1)
            code = None
            try:
                with self.telemetry.phase('copy'):
                    code = self._copy_once(source, destination)
            finally:
                self.telemetry.end_copy(complete=code == 0)
            self.check()
            events.append({'event': 'copy_process', 'try': attempt, 'exit_code': code})
            # Rclone 5 is temporary failure; timeout uses our synthetic 124.
            if code == 0 or code not in (5, 124):
                break
            if attempt < self.limits.attempts:
                self.cancelled.wait(self.limits.backoff_seconds * 2 ** (attempt - 1))
                self.check()
        return code, events

    def validate_copy(self, source, destination):
        self.local_path(source)
        if Path(source).exists() and not Path(source).is_file():
            raise WorkerError('Recursive source copying is disabled')
        self.local_path(destination)

    def _copy_once(self, source, destination):
        self.validate_copy(source, destination)
        args = self.args('copyto', source, destination) + ['--ignore-times', '--no-traverse',
                '--transfers', '1', '--checkers', '1', '--multi-thread-streams', '0', '--buffer-size', '8Mi',
                '--stats', '5s', '--stats-log-level', 'ERROR']
        # Drain logs as bounded lines; retain only numeric levels, never messages/paths.
        levels = {}
        try:
            with self.spawn(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
                timer = threading.Timer(self.limits.timeout_seconds, self.kill, args=(process,))
                timer.start()
                try:
                    while line := process.stderr.readline(65536):
                        try:
                            record = json.loads(line)
                            if isinstance(record.get('stats'), dict):
                                self.telemetry.copy_stats(record['stats'].get('bytes'))
                                continue  # A statistics record at ERROR verbosity is not a copy error.
                            level = record.get('level')
                            if level in ('error', 'warning', 'info', 'debug'):
                                levels[level] = levels.get(level, 0) + 1
                        except (ValueError, AttributeError):
                            pass
                    code = process.wait()
                    self.last_log_counts = levels
                    return 124 if code < 0 else code
                finally:
                    timer.cancel()
                    self.kill(process)
                    self.release(process)
        except OSError:
            return 127

    def validate_read(self, path):
        self.local_path(path, require_file=True)

    def digest(self, path):
        """Read the entire object through Rclone cat; bound RAM, never stage payloads."""
        self.validate_read(path)
        return self.stream_digest(self.args('cat', path))

    def digest_phase(self, path, phase):
        with self.telemetry.phase(phase):
            return self.digest(path)

    def stream_digest(self, args):
        sha = hashlib.sha256()
        length = 0
        try:
            with self.spawn(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                timer = threading.Timer(self.limits.timeout_seconds, self.kill, args=(process,))
                timer.start()
                try:
                    while block := process.stdout.read(1024 * 1024):
                        length += len(block)
                        self.telemetry.read(len(block))
                        sha.update(block)
                    if process.wait() != 0:
                        raise WorkerError('Full-content read failed')
                finally:
                    timer.cancel()
                    self.kill(process)
                    self.release(process)
        except OSError as exc:
            raise WorkerError('Full-content read could not start') from exc
        self.check()
        return length, sha.hexdigest()
