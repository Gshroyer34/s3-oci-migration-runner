"""Pure recommendation evaluator: no clocks, database writes, threads, or actuators."""
import hashlib
import math

from .contracts import CapacityPolicy, CapacitySnapshot, ControllerState, Decision


def digest(model):
    return hashlib.sha256(model.model_dump_json().encode()).hexdigest()


def evaluate(policy: CapacityPolicy, snapshot: CapacitySnapshot,
             previous: ControllerState | None = None) -> Decision:
    fingerprint = digest(policy)
    now = snapshot.now_seconds
    reset = previous is None or previous.policy_digest != fingerprint
    state = ControllerState(policy_digest=fingerprint, ready_nodes=snapshot.ready_nodes,
                            ready_changed_seconds=now) if reset else previous
    if now < state.last_evaluated_seconds:
        raise ValueError('Evaluation time moved backwards')
    updates = {'last_evaluated_seconds': now}
    if snapshot.ready_nodes != state.ready_nodes:
        updates.update(ready_nodes=snapshot.ready_nodes, ready_changed_seconds=now,
                       low_windows=0, excess_since_seconds=None)
    state = state.model_copy(update=updates)
    reasons = []
    cap = policy.max_nodes
    for value, reason in ((snapshot.quota_node_limit, 'quota_limit'),
                          (snapshot.runtime_node_limit, 'runtime_limit')):
        if value is not None and value < cap:
            cap = value
            reasons.append(reason)
    cost_known = all(x is not None for x in (policy.worker_cost_per_hour,
                     policy.worker_budget_per_hour, policy.worker_budget_remaining))
    if cost_known:
        hourly_cap = math.floor(policy.worker_budget_per_hour / policy.worker_cost_per_hour)
        reserve_cap = math.floor(policy.worker_budget_remaining /
                                 (policy.worker_cost_per_hour * policy.cost_horizon_seconds / 3600))
        if min(hourly_cap, reserve_cap) < cap:
            cap = min(hourly_cap, reserve_cap)
            reasons.append('worker_cost_limit')
    physical = snapshot.physical_nodes
    committed = snapshot.ready_nodes + snapshot.pending_nodes
    useful = math.ceil(snapshot.runnable_batches / policy.batch_slots_per_node)
    budget = 0.0
    raw = desired = add = drain = 0
    pause = False

    def result(*extra):
        return Decision(mode=policy.mode, policy_digest=fingerprint, input_digest=digest(snapshot),
                        budget_bytes_per_second=budget,
                        verified_unique_upper_bound_bytes_per_second=budget / policy.source_read_amplification,
                        unconstrained_nodes=raw, desired_nodes=desired, effective_node_limit=cap,
                        recommended_add_nodes=add, recommended_drain_nodes=drain,
                        pause_payload=pause, reasons=tuple(dict.fromkeys(reasons + list(extra))), state=state)

    # Safety and terminal controls precede all normal optimization.
    if snapshot.control == 'cancelled':
        pause = True
        return result('cancelled')
    if not snapshot.route_validated:
        pause = True
        state = state.model_copy(update={'recovering': True, 'recovery_windows': 0, 'low_windows': 0})
        return result('route_unvalidated')

    obs = snapshot.observation
    fresh = bool(obs and obs.valid and obs.path_id == policy.path_id
                 and 0 <= now - obs.latest_sample_seconds <= 15
                 and now >= obs.window_end_seconds
                 and now - obs.window_end_seconds <= 15
                 and obs.window_end_seconds - obs.window_start_seconds >= 60
                 and obs.sample_interval_seconds <= 10
                 and obs.attribution_coverage >= 0.95
                 and obs.migration_bytes_per_second <= obs.total_bytes_per_second
                     + 0.05 * policy.capacity_bytes_per_second)
    if not fresh:
        budget = policy.safe_fallback_bytes_per_second
        pause = budget == 0
        desired = min(committed, cap, useful)
        state = state.model_copy(update={'recovering': True, 'recovery_windows': 0,
                                        'low_windows': 0, 'excess_since_seconds': None})
        return result('telemetry_unavailable', 'static_reservation' if budget else 'no_safe_reservation')

    budget = max(0.0, min(policy.migration_cap_bytes_per_second,
                         policy.capacity_bytes_per_second * (1 - policy.headroom_fraction)
                         - obs.external_p95_bytes_per_second))
    if budget == 0:
        pause = True
        state = state.model_copy(update={'low_windows': 0, 'excess_since_seconds': None})
        return result('zero_bandwidth_budget')
    # Only distinct, nonoverlapping, complete windows count toward hysteresis.
    new_window = obs.window_start_seconds >= state.last_window_end_seconds
    if new_window:
        contiguous = obs.window_start_seconds <= state.last_window_end_seconds + 15
        low = (state.low_windows if contiguous else 0) + 1 if obs.migration_bytes_per_second < 0.9 * budget else 0
        recovered = (state.recovery_windows if contiguous else 0) + 1
        state = state.model_copy(update={'last_window_end_seconds': obs.window_end_seconds,
                                        'low_windows': low, 'recovery_windows': recovered,
                                        'recovering': state.recovering and recovered < 3})
    if snapshot.control != 'active' or snapshot.run_limit_reached:
        desired = min(committed, cap)
        if snapshot.control in ('unbound', 'complete'):
            desired = 0
        state = state.model_copy(update={'low_windows': 0, 'excess_since_seconds': None})
        return result('run_limit_reached' if snapshot.run_limit_reached else snapshot.control)
    if snapshot.runnable_batches == 0 or snapshot.remaining_unique_bytes == 0:
        # This is a drain recommendation, never a termination or lease cancellation.
        desired = 0
        drain = 0 if snapshot.hold_capacity else snapshot.ready_nodes
        return result('queue_empty')

    calibrated = snapshot.worker_source_bytes_per_second is not None and snapshot.worker_profile_seconds >= 300
    raw = math.ceil(budget / snapshot.worker_source_bytes_per_second) if calibrated else policy.initial_nodes
    desired = min(cap, useful, max(policy.min_active_nodes, raw))
    if desired < raw:
        reasons.append('capacity_constrained')
    if cap < policy.min_active_nodes:
        reasons.append('minimum_exceeds_ceiling')
    if not calibrated:
        reasons.append('calibration_required')
    if obs.total_bytes_per_second > 0.9 * policy.capacity_bytes_per_second:
        # A 20% lower budget is advisory until a real node limiter is implemented.
        budget = min(budget, obs.migration_bytes_per_second * 0.8)
        desired = min(desired, committed)
        pause = budget == 0
        state = state.model_copy(update={'low_windows': 0, 'excess_since_seconds': None})
        return result('congestion_backoff')
    if snapshot.hold_capacity or state.recovering:
        return result('operator_hold' if snapshot.hold_capacity else 'telemetry_recovery')
    if snapshot.bottleneck in ('backend', 'ledger', 'verification', 'no_gain'):
        state = state.model_copy(update={'low_windows': 0, 'excess_since_seconds': None})
        return result('bottleneck_' + snapshot.bottleneck)

    if desired > committed:
        state = state.model_copy(update={'excess_since_seconds': None})
        if not cost_known or snapshot.quota_node_limit is None:
            return result('provisioning_inputs_unknown')
        if snapshot.pending_nodes:
            return result('pending_capacity')
        eta = snapshot.remaining_unique_bytes * policy.source_read_amplification / budget
        if eta < policy.startup_p95_seconds + 2 * policy.target_batch_seconds:
            return result('queue_tail')
        cold = snapshot.ready_nodes == 0
        if not cold and (state.low_windows < 3 or now - state.ready_changed_seconds < 180
                         or state.last_scale_up_seconds is not None and now - state.last_scale_up_seconds < 180):
            return result('scale_up_stabilizing')
        step = min(4, max(1, math.ceil(0.25 * snapshot.ready_nodes)))
        # Initial pool is also capped at four; this is a recommendation, not a launch.
        if cold:
            step = min(4, policy.initial_nodes)
        add = min(desired - committed, max(0, cap - physical), step, max(0, 4 - snapshot.pending_nodes))
        if add:
            state = state.model_copy(update={'last_scale_up_seconds': now, 'low_windows': 0})
        return result('recommend_scale_up' if add else 'physical_capacity_reserved')

    # No down-sizing on an uncalibrated throughput hypothesis.
    if calibrated and desired < snapshot.ready_nodes:
        if state.excess_since_seconds is None:
            state = state.model_copy(update={'excess_since_seconds': now})
        if now - state.excess_since_seconds >= 600 and now - state.ready_changed_seconds >= 900 and (
                state.last_drain_seconds is None or now - state.last_drain_seconds >= 300):
            drain = min(snapshot.ready_nodes - desired, max(1, math.floor(0.1 * snapshot.ready_nodes)))
            state = state.model_copy(update={'last_drain_seconds': now})
            return result('recommend_drain')
        return result('scale_down_stabilizing')
    state = state.model_copy(update={'excess_since_seconds': None})
    return result('hold')
