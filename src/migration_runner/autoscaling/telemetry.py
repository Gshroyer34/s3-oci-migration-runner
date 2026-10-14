"""Normalize aligned path counters; payload counters are never treated as wire bytes."""
import math
from typing import Literal

from pydantic import Field, model_validator

from .contracts import CapacityPolicy, Contract, Count, Identifier, PathObservation, Seconds


class PathCounterSample(Contract):
    path_id: Identifier
    direction: Literal['source-to-destination'] = 'source-to-destination'
    meter_id: Identifier
    boot_id: Identifier
    sequence: Count
    timestamp_seconds: Seconds
    total_wire_bytes: int = Field(ge=0, le=2**64 - 1)
    migration_wire_bytes: int = Field(ge=0, le=2**64 - 1)
    attribution_coverage: float = Field(default=1, ge=0, le=1)


class PhaseCounters(Contract):
    """Transport-adapter contract only; source totals already include copy/retry bytes."""
    node_id: Identifier
    boot_id: Identifier
    sequence: Count
    timestamp_seconds: Seconds
    source_read_bytes: int = Field(ge=0, le=2**64 - 1)
    source_retry_bytes: int = Field(ge=0, le=2**64 - 1)
    destination_read_bytes: int = Field(ge=0, le=2**64 - 1)
    destination_write_bytes: int = Field(ge=0, le=2**64 - 1)

    @model_validator(mode='after')
    def retry_subset(self):
        if self.source_retry_bytes > self.source_read_bytes:
            raise ValueError('Source retries are a subset of total source reads')
        return self


def aggregate_path(policy: CapacityPolicy, samples: list[PathCounterSample], *,
                   now_seconds: float) -> PathObservation:
    """Accept a bounded, ordered, >=60s window from ONE aligned directional meter.

    Migration counters must already be aligned wire counters, not Rclone payload
    statistics or summed NIC RX/TX. Collection adapters are a later integration.
    Invalid input produces an explicitly invalid window; it never implies idle.
    """
    if not math.isfinite(now_seconds) or now_seconds < 0:
        raise ValueError('Invalid observation clock')
    if len(samples) > 256:
        raise ValueError('At most 256 samples per aggregation')

    def invalid(reason):
        # The placeholder window is invalid and cannot enter budget calculations.
        return PathObservation(path_id=policy.path_id, window_start_seconds=max(0, now_seconds - 60),
                               window_end_seconds=max(now_seconds, 1), latest_sample_seconds=max(now_seconds, 1),
                               sample_interval_seconds=5, total_bytes_per_second=0,
                               migration_bytes_per_second=0, external_p95_bytes_per_second=0,
                               attribution_coverage=0, valid=False, invalid_reason=reason)

    if len(samples) < 2:
        return invalid('insufficient_samples')
    first, last = samples[0], samples[-1]
    if any(s.path_id != policy.path_id or s.meter_id != first.meter_id or s.boot_id != first.boot_id
           or s.direction != first.direction for s in samples):
        return invalid('meter_changed')
    if last.timestamp_seconds > now_seconds:
        return invalid('future_sample')
    if now_seconds - last.timestamp_seconds > 15:
        return invalid('stale')
    elapsed = last.timestamp_seconds - first.timestamp_seconds
    if elapsed < 60:
        return invalid('insufficient_samples')
    # A 60s rolling window may start one sample before its boundary.
    if elapsed > 70:
        return invalid('sample_gap')
    backgrounds, intervals = [], []
    for previous, current in zip(samples, samples[1:]):
        seconds = current.timestamp_seconds - previous.timestamp_seconds
        if seconds <= 0 or current.sequence <= previous.sequence:
            return invalid('out_of_order')
        if seconds > 10:
            return invalid('sample_gap')
        total = current.total_wire_bytes - previous.total_wire_bytes
        migration = current.migration_wire_bytes - previous.migration_wire_bytes
        if min(total, migration) < 0:
            return invalid('counter_reset')
        if migration - total > 0.05 * policy.capacity_bytes_per_second * seconds:
            return invalid('attribution_invalid')
        intervals.append(seconds)
        backgrounds.append((max(0.0, (total - migration) / seconds), seconds))
    # Time-weighted nearest-rank p95 avoids over-weighting short sample intervals.
    cumulative = 0.0
    external = 0.0
    for rate, duration in sorted(backgrounds):
        cumulative += duration
        if cumulative >= 0.95 * elapsed:
            external = rate
            break
    return PathObservation(path_id=policy.path_id, window_start_seconds=first.timestamp_seconds,
                           window_end_seconds=last.timestamp_seconds, latest_sample_seconds=last.timestamp_seconds,
                           sample_interval_seconds=max(intervals),
                           total_bytes_per_second=(last.total_wire_bytes - first.total_wire_bytes) / elapsed,
                           migration_bytes_per_second=(last.migration_wire_bytes - first.migration_wire_bytes) / elapsed,
                           external_p95_bytes_per_second=external,
                           attribution_coverage=min(s.attribution_coverage for s in samples))
