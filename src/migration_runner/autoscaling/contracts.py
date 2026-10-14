"""Versioned, unit-explicit contracts shared by previews, replay, and policy tests."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(ge=0, le=10**9)]
Rate = Annotated[float, Field(ge=0, le=10**15)]
Seconds = Annotated[float, Field(ge=0, le=10**12)]
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_.:-]+$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True,
                              allow_inf_nan=False, hide_input_in_errors=True)


class CapacityPolicy(Contract):
    schema_version: Literal[1] = 1
    policy_version: int = Field(default=1, ge=1)
    mode: Literal['recommend', 'shadow'] = 'recommend'
    path_id: Identifier
    capacity_bytes_per_second: float = Field(gt=0, le=10**15)
    migration_cap_bytes_per_second: Rate
    headroom_fraction: float = Field(default=0.15, ge=0.10, le=0.40)
    min_active_nodes: Count = 1
    initial_nodes: Count = 2
    max_nodes: int = Field(default=8, ge=1, le=1000)
    object_slots_per_node: int = Field(default=4, ge=1, le=16)
    batch_slots_per_node: int = Field(default=1, ge=1, le=2)
    source_read_amplification: float = Field(default=3.0, ge=1, le=100)
    safe_fallback_bytes_per_second: Rate = 0
    worker_cost_per_hour: float | None = Field(default=None, gt=0, le=10**9)
    worker_budget_per_hour: float | None = Field(default=None, ge=0, le=10**12)
    # A finite *remaining* allowance, after accrued/pending/drain cost reservations.
    worker_budget_remaining: float | None = Field(default=None, ge=0, le=10**15)
    cost_horizon_seconds: float = Field(default=900, ge=900, le=86400)
    startup_p95_seconds: float = Field(default=120, ge=0, le=3600)
    target_batch_seconds: float = Field(default=180, ge=120, le=300)

    @model_validator(mode='after')
    def bounds(self):
        if not self.min_active_nodes <= self.initial_nodes <= self.max_nodes:
            raise ValueError('Require minimum <= initial <= maximum')
        if self.safe_fallback_bytes_per_second > min(
                self.migration_cap_bytes_per_second,
                self.capacity_bytes_per_second * (1 - self.headroom_fraction)):
            raise ValueError('Fallback reservation exceeds the path budget')
        return self


class PathObservation(Contract):
    path_id: Identifier
    window_start_seconds: Seconds
    window_end_seconds: Seconds
    latest_sample_seconds: Seconds
    sample_interval_seconds: float = Field(gt=0, le=3600)
    total_bytes_per_second: Rate
    migration_bytes_per_second: Rate
    external_p95_bytes_per_second: Rate
    attribution_coverage: float = Field(default=1.0, ge=0, le=1)
    valid: bool = True
    invalid_reason: Literal['none', 'insufficient_samples', 'counter_reset',
                            'out_of_order', 'meter_changed', 'sample_gap',
                            'attribution_invalid', 'stale', 'future_sample'] = 'none'

    @model_validator(mode='after')
    def window(self):
        if self.window_end_seconds <= self.window_start_seconds:
            raise ValueError('Observation window must have positive duration')
        if self.latest_sample_seconds < self.window_end_seconds:
            raise ValueError('Latest sample cannot precede the window end')
        if self.valid != (self.invalid_reason == 'none'):
            raise ValueError('Validity and invalid reason disagree')
        return self


class CapacitySnapshot(Contract):
    now_seconds: Seconds
    observation: PathObservation | None = None
    route_validated: bool = False
    ready_nodes: Count = 0
    pending_nodes: Count = 0
    draining_nodes: Count = 0
    terminating_nodes: Count = 0
    unavailable_nodes: Count = 0
    # Total permitted pool size after subtracting unrelated resource allocations.
    quota_node_limit: Count | None = None
    runtime_node_limit: Count | None = None
    # Includes running batches + eligible unclaimed batches; excludes delayed retries.
    runnable_batches: Count = 0
    remaining_unique_bytes: int = Field(default=0, ge=0, le=10**18)
    worker_source_bytes_per_second: float | None = Field(default=None, ge=1, le=10**15)
    worker_profile_seconds: Seconds = 0
    control: Literal['active', 'paused', 'draining', 'cancelled', 'unbound', 'complete'] = 'active'
    run_limit_reached: bool = False
    hold_capacity: bool = False
    bottleneck: Literal['none', 'cpu', 'backend', 'ledger', 'verification', 'no_gain'] = 'none'

    @property
    def physical_nodes(self):
        return (self.ready_nodes + self.pending_nodes + self.draining_nodes
                + self.terminating_nodes + self.unavailable_nodes)


class ControllerState(Contract):
    policy_digest: str = ''
    last_evaluated_seconds: Seconds = 0
    last_window_end_seconds: Seconds = 0
    low_windows: Count = 0
    recovery_windows: Count = 0
    recovering: bool = False
    ready_nodes: Count = 0
    ready_changed_seconds: Seconds = 0
    last_scale_up_seconds: Seconds | None = None
    last_drain_seconds: Seconds | None = None
    excess_since_seconds: Seconds | None = None


class Decision(Contract):
    evaluator_version: Literal['1.0'] = '1.0'
    mode: Literal['recommend', 'shadow']
    actuation_enabled: Literal[False] = False
    policy_digest: str
    input_digest: str
    budget_bytes_per_second: Rate
    verified_unique_upper_bound_bytes_per_second: Rate
    unconstrained_nodes: int = Field(ge=0)
    desired_nodes: Count
    effective_node_limit: Count
    recommended_add_nodes: Count = 0
    recommended_drain_nodes: Count = 0
    pause_payload: bool = False
    reasons: tuple[str, ...]
    state: ControllerState


class CapacityPreviewRequest(Contract):
    """Operator-supplied what-if inputs; job controls/counts come from the ledger."""
    policy: CapacityPolicy
    observation: PathObservation | None = None
    route_validated: bool = False
    worker_source_bytes_per_second: float | None = Field(default=None, ge=1, le=10**15)
    worker_profile_seconds: Seconds = 0
