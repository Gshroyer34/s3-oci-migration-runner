"""Production evaluator contracts and deterministic replay; no cloud access."""
from io import BytesIO
import json
import math
import random

import pytest
from pydantic import ValidationError

from migration_runner.autoscaling import CapacityPolicy, CapacitySnapshot, ControllerState, evaluate
from migration_runner.autoscaling.contracts import PathObservation
from migration_runner.autoscaling.telemetry import PathCounterSample, PhaseCounters, aggregate_path
from migration_runner.autoscaling.__main__ import replay, main


def policy(gbps=10, **changes):
    values = dict(path_id='test-path', capacity_bytes_per_second=gbps * 1e9 / 8,
                  migration_cap_bytes_per_second=gbps * 1e9 / 8 * .85,
                  min_active_nodes=1 if gbps == 10 else 2, initial_nodes=2 if gbps == 10 else 4,
                  max_nodes=8 if gbps == 10 else 32,
                  worker_cost_per_hour=1.0, worker_budget_per_hour=100.0, worker_budget_remaining=10000.0)
    return CapacityPolicy(**(values | changes))


def observation(now=3600.0, **changes):
    return PathObservation(**(dict(path_id='test-path', window_start_seconds=now-60,
        window_end_seconds=now, latest_sample_seconds=now, sample_interval_seconds=5,
        total_bytes_per_second=800_000_000, migration_bytes_per_second=800_000_000,
        external_p95_bytes_per_second=0) | changes))


def snapshot(now=3600.0, **changes):
    return CapacitySnapshot(**(dict(now_seconds=now, observation=observation(now), route_validated=True,
        ready_nodes=2, quota_node_limit=100, runnable_batches=10000, remaining_unique_bytes=2 * 10**15,
        worker_source_bytes_per_second=400_000_000, worker_profile_seconds=300) | changes))


@pytest.mark.parametrize('gbps,budget,nodes', [(10, 1_062_500_000, 3), (100, 10_625_000_000, 27)])
def test_sizing_uses_wire_budget_and_separate_unique_throughput(gbps, budget, nodes):
    decision = evaluate(policy(gbps), snapshot())
    assert decision.budget_bytes_per_second == budget
    assert decision.unconstrained_nodes == decision.desired_nodes == nodes
    assert decision.verified_unique_upper_bound_bytes_per_second == budget / 3
    assert not decision.actuation_enabled and decision.recommended_add_nodes == 0


@pytest.mark.parametrize('external,budget', [(250_000_000,812_500_000), (625_000_000,437_500_000),
                                           (1_200_000_000,0)])
def test_shared_traffic_is_subtracted_after_headroom(external, budget):
    # Total instantaneous traffic is independent of the historical p95 background.
    decision = evaluate(policy(), snapshot(observation=observation(external_p95_bytes_per_second=external)))
    assert decision.budget_bytes_per_second == budget
    assert decision.pause_payload == (budget == 0)
    assert decision.recommended_add_nodes == 0


@pytest.mark.parametrize('changes', [dict(headroom_fraction=.09), dict(headroom_fraction=.41),
    dict(headroom_fraction=float('nan')), dict(capacity_bytes_per_second=float('inf')),
    dict(max_nodes=True), dict(max_nodes=1), dict(initial_nodes='2'), dict(min_active_nodes=9),
    dict(mode='automatic'), dict(safe_fallback_bytes_per_second=2e9)])
def test_invalid_or_actuating_policy_rejected(changes):
    with pytest.raises(ValidationError):
        policy(**changes)


@pytest.mark.parametrize('changes,expected', [({'quota_node_limit': 1},1), ({'runtime_node_limit': 2},2),
                                            ({'runnable_batches':1},1)])
def test_limits_override_minimum_and_bandwidth_demand(changes, expected):
    result = evaluate(policy(100), snapshot(**changes))
    assert result.desired_nodes == expected
    assert result.recommended_add_nodes == 0


def test_hourly_and_remaining_cost_envelopes():
    assert evaluate(policy(100, worker_budget_per_hour=5.9), snapshot()).desired_nodes == 5
    assert evaluate(policy(100, worker_budget_remaining=.5), snapshot()).desired_nodes == 2
    assert evaluate(policy(100, worker_budget_remaining=0), snapshot()).desired_nodes == 0
    result = evaluate(policy(worker_cost_per_hour=None), snapshot(ready_nodes=0))
    assert result.recommended_add_nodes == 0 and 'provisioning_inputs_unknown' in result.reasons


@pytest.mark.parametrize('obs', [None, observation(3500), observation(3610),
    observation(path_id='wrong'), observation(sample_interval_seconds=300),
    observation(attribution_coverage=.90), observation(window_start_seconds=3590),
    observation(total_bytes_per_second=0, migration_bytes_per_second=1e9)])
def test_missing_stale_future_incomplete_metrics_fail_closed(obs):
    result = evaluate(policy(), snapshot(observation=obs))
    assert result.pause_payload and result.budget_bytes_per_second == 0
    assert result.recommended_add_nodes == result.recommended_drain_nodes == 0
    assert result.state.recovering


def test_fallback_is_one_path_budget_without_growth():
    result = evaluate(policy(safe_fallback_bytes_per_second=100_000_000), snapshot(observation=None))
    assert result.budget_bytes_per_second == 100_000_000 and not result.pause_payload
    assert result.recommended_add_nodes == 0 and 'static_reservation' in result.reasons


def test_cancel_and_route_safety_override_fallback():
    p = policy(safe_fallback_bytes_per_second=100_000_000)
    for changes in ({'control':'cancelled'}, {'route_validated':False}):
        decision = evaluate(p, snapshot(**changes))
        assert decision.pause_payload and decision.budget_bytes_per_second == 0
        assert decision.recommended_add_nodes == 0


@pytest.mark.parametrize('changes', [dict(control='paused'), dict(control='draining'),
    dict(control='unbound'), dict(control='complete'), dict(run_limit_reached=True), dict(hold_capacity=True),
    dict(bottleneck='backend'), dict(bottleneck='ledger'), dict(bottleneck='verification'), dict(bottleneck='no_gain')])
def test_controls_and_bottlenecks_prevent_growth(changes):
    p = policy()
    state = None
    for now in (3600, 3660, 3720, 3780, 3840):
        result = evaluate(p, snapshot(now, **changes), state)
        state = result.state
        assert result.recommended_add_nodes == result.recommended_drain_nodes == 0


def test_three_distinct_windows_and_readiness_cooldown_required():
    p = policy()
    first = evaluate(p, snapshot())
    repeated = evaluate(p, snapshot(3605, observation=observation()), first.state)
    assert repeated.state.low_windows == 1
    state = first.state
    for now in (3660, 3720):
        decision = evaluate(p, snapshot(now), state)
        state = decision.state
        assert decision.recommended_add_nodes == 0
    decision = evaluate(p, snapshot(3780), state)
    assert decision.recommended_add_nodes == 1 and decision.state.low_windows == 0


def test_fresh_recovery_windows_cannot_be_replayed():
    p = policy()
    state = evaluate(p, snapshot(observation=None)).state
    first = evaluate(p, snapshot(3660), state)
    repeated = evaluate(p, snapshot(3665, observation=observation(3660)), first.state)
    assert repeated.state.recovery_windows == 1 and repeated.state.recovering
    second = evaluate(p, snapshot(3720), repeated.state)
    assert second.state.recovering
    third = evaluate(p, snapshot(3780), second.state)
    assert not third.state.recovering


def test_pending_draining_terminating_and_unavailable_count_against_cap():
    p = policy()
    pending = evaluate(p, snapshot(pending_nodes=1))
    assert pending.desired_nodes == 3 and pending.recommended_add_nodes == 0
    for field in ('draining_nodes','terminating_nodes','unavailable_nodes'):
        state = None
        for now in (3600,3660,3720,3780):
            result = evaluate(p, snapshot(now, **{field:6}), state)
            state = result.state
        assert result.recommended_add_nodes == 0 and 'physical_capacity_reserved' in result.reasons


def test_queue_tail_suppresses_launches_and_empty_queue_recommends_drain():
    result = evaluate(policy(), snapshot(ready_nodes=0, remaining_unique_bytes=10**9))
    assert result.recommended_add_nodes == 0 and 'queue_tail' in result.reasons
    result = evaluate(policy(), snapshot(runnable_batches=0, remaining_unique_bytes=0))
    assert result.desired_nodes == 0 and result.recommended_drain_nodes == 2
    held = evaluate(policy(), snapshot(runnable_batches=0, remaining_unique_bytes=0, hold_capacity=True))
    assert held.recommended_drain_nodes == 0


def test_normal_scale_down_is_slow_and_bounded():
    p = policy()
    state = None
    for now in range(3600,4500,60):
        result = evaluate(p, snapshot(now, ready_nodes=6), state)
        state = result.state
        assert result.recommended_drain_nodes == 0
    result = evaluate(p, snapshot(4500, ready_nodes=6), state)
    assert result.recommended_drain_nodes == 1
    assert evaluate(p, snapshot(4560, ready_nodes=6), result.state).recommended_drain_nodes == 0


def test_congestion_reduces_rate_before_capacity():
    result = evaluate(policy(), snapshot(observation=observation(total_bytes_per_second=1.2e9,
        migration_bytes_per_second=.8e9, external_p95_bytes_per_second=.4e9)))
    assert result.budget_bytes_per_second == .64e9
    assert result.recommended_add_nodes == 0 and 'congestion_backoff' in result.reasons


def test_policy_change_resets_hysteresis_and_time_cannot_reverse():
    p = policy()
    result = evaluate(p, snapshot())
    with pytest.raises(ValueError, match='backwards'):
        evaluate(p, snapshot(3599), result.state)
    changed = evaluate(policy(headroom_fraction=.2), snapshot(3660), result.state)
    assert changed.state.low_windows == 1
    assert changed.policy_digest != result.policy_digest


def test_hundred_gbps_ramp_with_delayed_provisioning_is_bounded_and_settles():
    p = policy(100)
    ready, requests, state = 4, [], None
    for now in range(60,3661,30):
        ready += sum(count for due,count in requests if due <= now)
        requests = [(due,count) for due,count in requests if due > now]
        pending = sum(count for _,count in requests)
        rate = min(ready*400e6, p.migration_cap_bytes_per_second)
        frame = snapshot(now, ready_nodes=ready, pending_nodes=pending,
                         observation=observation(now,total_bytes_per_second=rate,migration_bytes_per_second=rate))
        result = evaluate(p, frame, state)
        state = result.state
        assert result.recommended_add_nodes <= 4
        assert ready + pending + result.recommended_add_nodes <= 32
        if pending:
            assert result.recommended_add_nodes == 0
        if result.recommended_add_nodes:
            requests.append((now+120,result.recommended_add_nodes))
        if ready == 27:
            assert result.recommended_add_nodes == 0
            assert rate >= .9 * result.budget_bytes_per_second
            break
    else:
        pytest.fail('Ramp failed to reach 27 ready nodes inside 60 minutes')


def samples(**changes):
    result = []
    for i in range(13):
        result.append(PathCounterSample(**(dict(path_id='test-path',meter_id='meter-1',boot_id='boot-1',
            sequence=i,timestamp_seconds=3540+i*5,total_wire_bytes=i*5*500_000_000,
            migration_wire_bytes=i*5*400_000_000) | changes)))
    return result


def test_counter_aggregation_keeps_attribution_and_byte_units():
    obs = aggregate_path(policy(), samples(), now_seconds=3600)
    assert obs.valid and obs.total_bytes_per_second == 500_000_000
    assert obs.migration_bytes_per_second == 400_000_000
    assert obs.external_p95_bytes_per_second == 100_000_000
    result = evaluate(policy(), snapshot(observation=obs))
    assert result.budget_bytes_per_second == 962_500_000


@pytest.mark.parametrize('field,value,reason', [('boot_id','new','meter_changed'),
    ('meter_id','new','meter_changed'), ('path_id','other','meter_changed'),
    ('sequence',0,'out_of_order'),('timestamp_seconds',3540.0,'out_of_order'),
    ('total_wire_bytes',0,'counter_reset'),('migration_wire_bytes',10**12,'attribution_invalid')])
def test_bad_counters_do_not_look_like_spare_bandwidth(field,value,reason):
    points = samples()
    points[6] = points[6].model_copy(update={field:value})
    obs = aggregate_path(policy(), points, now_seconds=3600)
    assert not obs.valid and obs.invalid_reason == reason
    assert evaluate(policy(), snapshot(observation=obs)).pause_payload


def test_insufficient_slow_stale_or_future_counter_windows():
    assert not aggregate_path(policy(), [], now_seconds=3600).valid
    assert not aggregate_path(policy(), samples()[1:], now_seconds=3600).valid
    assert not aggregate_path(policy(), samples()[::3], now_seconds=3600).valid
    assert aggregate_path(policy(), samples(), now_seconds=3620).invalid_reason == 'stale'
    assert aggregate_path(policy(), samples(), now_seconds=3599).invalid_reason == 'future_sample'


def test_background_p95_is_computed_from_aligned_deltas():
    points = samples()
    # A five-second background spike is >5% of the 60s window, so p95 captures it.
    points[-1] = points[-1].model_copy(update={'total_wire_bytes':points[-1].total_wire_bytes+5*500_000_000})
    obs = aggregate_path(policy(), points, now_seconds=3600)
    assert obs.external_p95_bytes_per_second == 600_000_000


def test_retry_counter_is_a_subset_of_source_reads():
    with pytest.raises(ValidationError):
        PhaseCounters(node_id='w1',boot_id='b1',sequence=1,timestamp_seconds=3600,
            source_read_bytes=1,source_retry_bytes=2,destination_read_bytes=0,destination_write_bytes=0)


def test_seeded_safety_invariants():
    rng = random.Random(20260916)
    for i in range(2000):
        p = policy(rng.choice([10,100]), worker_budget_per_hour=float(rng.randint(0,32)))
        background = rng.uniform(0,p.capacity_bytes_per_second)
        frame = snapshot(3600+i*60, ready_nodes=rng.randint(0,32), pending_nodes=rng.randint(0,4),
                         unavailable_nodes=rng.randint(0,4), quota_node_limit=rng.randint(0,32),
                         runnable_batches=rng.randint(0,100),
                         observation=observation(3600+i*60, external_p95_bytes_per_second=background))
        decision = evaluate(p,frame)
        assert 0 <= decision.budget_bytes_per_second <= p.migration_cap_bytes_per_second
        assert 0 <= decision.desired_nodes <= decision.effective_node_limit
        assert decision.recommended_add_nodes <= 4
        if decision.recommended_add_nodes:
            assert frame.physical_nodes + decision.recommended_add_nodes <= decision.effective_node_limit
        assert not decision.actuation_enabled


def test_replay_is_deterministic_and_uses_production_evaluator(tmp_path):
    p = policy()
    data = b''.join((snapshot(t).model_dump_json()+'\n').encode() for t in (3600,3660,3720,3780))
    first = list(replay(p, BytesIO(data)))
    assert first == list(replay(p, BytesIO(data)))
    assert first[-1].recommended_add_nodes == 1
    policy_path, trace, output = (tmp_path / f for f in ('policy.json','trace.jsonl','result.jsonl'))
    policy_path.write_text(p.model_dump_json())
    trace.write_bytes(data)
    assert main(['--policy',str(policy_path),'--input',str(trace),'--output',str(output)]) == 0
    assert [json.loads(line) for line in output.read_text().splitlines()] == [x.model_dump(mode='json') for x in first]
    assert main(['--policy',str(policy_path),'--input',str(trace),'--output',str(output)]) == 1


def test_replay_rejects_invalid_oversize_and_time_reversal():
    with pytest.raises(ValueError):
        list(replay(policy(), BytesIO(b'x'*65537)))
    with pytest.raises(ValueError):
        list(replay(policy(), BytesIO(b'{"unexpected":true}\n')))
    frames = (snapshot(t).model_dump_json() for t in (3600,3599))
    with pytest.raises(ValueError, match='backwards'):
        list(replay(policy(),BytesIO(('\n'.join(frames)+'\n').encode())))
