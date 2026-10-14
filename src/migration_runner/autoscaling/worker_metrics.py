"""Validate and summarize lease-owned application telemetry from the ledger."""
from datetime import datetime, timezone

from pydantic import Field, model_validator
from sqlalchemy import select

from .contracts import Contract
from migration_runner.worker.telemetry import COUNTERS, PHASES


class ByteCounters(Contract):
    source_hash_bytes: int = Field(ge=0, le=10**18)
    source_recheck_bytes: int = Field(ge=0, le=10**18)
    destination_verify_bytes: int = Field(ge=0, le=10**18)
    copy_bytes: int = Field(ge=0, le=10**18)
    source_retry_bytes: int = Field(ge=0, le=10**18)
    copy_retry_bytes: int = Field(ge=0, le=10**18)

    @model_validator(mode='after')
    def subsets(self):
        if self.copy_retry_bytes > self.copy_bytes or self.source_retry_bytes > self.source_bytes:
            raise ValueError('Retries must be subsets of measured traffic')
        return self

    @property
    def source_bytes(self):
        return self.source_hash_bytes + self.source_recheck_bytes + self.copy_bytes


class ByteSample(ByteCounters):
    elapsed_seconds: float = Field(ge=0, le=10**9)


class WorkerTelemetry(Contract):
    schema_version: int = Field(ge=1, le=1)
    sequence: int = Field(ge=1)
    kind: str = Field(pattern='^application_bytes$')
    phase: str
    elapsed_seconds: float = Field(ge=0, le=10**9)
    counters: ByteCounters
    source_read_bytes: int = Field(ge=0, le=3 * 10**18)
    copy_processes: int = Field(ge=0)
    missing_copy_stats: int = Field(ge=0)
    publish_errors: int = Field(ge=0)
    samples: list[ByteSample] = Field(min_length=1, max_length=25)

    @model_validator(mode='after')
    def consistent(self):
        if self.phase not in PHASES or self.source_read_bytes != self.counters.source_bytes:
            raise ValueError('Invalid phase or source accounting')
        if self.missing_copy_stats > self.copy_processes:
            raise ValueError('Invalid statistics coverage')
        for a,b in zip(self.samples, self.samples[1:]):
            if b.elapsed_seconds <= a.elapsed_seconds or any(getattr(b,k) < getattr(a,k) for k in COUNTERS):
                raise ValueError('Telemetry samples must be monotonic')
        last = self.samples[-1]
        if last.elapsed_seconds != self.elapsed_seconds or any(getattr(last,k) != getattr(self.counters,k) for k in COUNTERS):
            raise ValueError('Last sample must match totals')
        return self


def summarize_attempts(rows, *, now):
    totals = dict.fromkeys(COUNTERS, 0)
    workers, active_rates = [], []
    measured = missing = 0
    incomplete = False
    for row in rows:
        raw = row['summary'].get('telemetry')
        if not raw:
            missing += 1
            if row['state'] == 'running':
                active_rates.append(None)
            continue
        try:
            data = WorkerTelemetry.model_validate({k:v for k,v in raw.items() if k != 'observed_at'})
            stamp = datetime.fromisoformat(raw['observed_at'])
            if stamp.tzinfo is None:
                raise ValueError('Timestamp must be timezone aware')
            age = (now - stamp).total_seconds()
            if age < 0:
                raise ValueError('Future telemetry')
        except (ValueError, KeyError, TypeError):
            missing += 1
            if row['state'] == 'running':
                active_rates.append(None)
            continue
        measured += 1
        incomplete |= bool(data.missing_copy_stats or row["state"] in ("abandoned", "cancelled"))
        for key in COUNTERS:
            totals[key] += getattr(data.counters, key)
        rate = None
        if row['state'] == 'running':
            last = data.samples[-1]
            first = next((p for p in data.samples if p.elapsed_seconds >= last.elapsed_seconds-60), data.samples[0])
            elapsed = last.elapsed_seconds-first.elapsed_seconds
            if age <= 15 and elapsed >= 5 and data.missing_copy_stats == 0:
                rate = (last.source_bytes-first.source_bytes)/elapsed
            active_rates.append(rate)
            workers.append({'worker_id':row['worker_id'], 'phase':data.phase,
                            'sample_age_seconds':age, 'source_bytes_per_second':rate,
                            'status':'fresh' if age <= 15 else 'stale',
                            'missing_copy_stats':data.missing_copy_stats})
    return {'kind':'application_bytes', 'measured_attempts':measured, 'unmeasured_attempts':missing,
            'totals':totals, 'source_read_bytes':totals['source_hash_bytes']+totals['source_recheck_bytes']+totals['copy_bytes'],
            'active_source_bytes_per_second':sum(active_rates) if active_rates and all(v is not None for v in active_rates) else None,
            'workers':workers,
            'coverage':'unavailable' if not measured else 'partial' if missing or incomplete else 'measured',
            'wire_bytes_available':False}


def read_worker_metrics(engine, job, *, now=None):
    from migration_runner.ledger.models import Attempt
    now = now or datetime.now(timezone.utc)
    with engine.connect() as c:
        rows = c.execute(select(Attempt.worker_id, Attempt.state, Attempt.summary, Attempt.created_at, Attempt.finished_at)
                         .where(Attempt.migration_id == job).order_by(Attempt.created_at.desc()).limit(2049)).mappings().all()
    truncated = len(rows) > 2048
    result = summarize_attempts(rows[:2048], now=now)
    result['truncated'] = truncated
    if truncated:
        result['coverage'] = 'partial'
        result['active_source_bytes_per_second'] = None
    return result
