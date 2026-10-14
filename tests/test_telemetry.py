from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from migration_runner.worker.telemetry import WorkerMeter
from migration_runner.autoscaling.worker_metrics import WorkerTelemetry, summarize_attempts
from migration_runner.autoscaling.monitor import MonitorConfig, NetworkReader, CapacityMonitor
from migration_runner.autoscaling.contracts import CapacityPolicy, CapacitySnapshot
from migration_runner.autoscaling.policy import evaluate


def policy():
    return CapacityPolicy(path_id="test-path", capacity_bytes_per_second=1_250_000_000,
                          migration_cap_bytes_per_second=1_062_500_000)


def test_meter_phases_retry_subsets_highwater_and_bounds():
    clock = [0.0]
    meter = WorkerMeter(retry=True, clock=lambda:clock[0])
    for phase in ("source_hash", "source_recheck", "destination_verify"):
        with meter.phase(phase):
            meter.read(100)
    meter.start_copy(retry=False)
    for value in (40,40,30,100,True,-1,"100"):
        meter.copy_stats(value)
    meter.end_copy()
    meter.start_copy(retry=True)
    meter.copy_stats(20)
    meter.end_copy(complete=False)
    for i in range(1,40):
        clock[0] = float(i)
        sample = meter.sample()
    data = WorkerTelemetry.model_validate(sample)
    assert data.source_read_bytes == 320
    assert data.counters.source_retry_bytes == 320
    assert data.counters.copy_retry_bytes == data.counters.copy_bytes == 120
    assert data.counters.destination_verify_bytes == 100
    assert len(data.samples) == 25 and data.phase == "idle"
    assert data.missing_copy_stats == 1
    with pytest.raises(ValidationError):
        WorkerTelemetry.model_validate(sample | {"source_read_bytes":999})


def measured_row(now, age=0, missing=False):
    clock = [0.0]
    meter = WorkerMeter(clock=lambda:clock[0])
    with meter.phase("source_hash"):
        meter.read(100)
    clock[0] = 10.0
    raw = meter.sample()
    raw["observed_at"] = datetime.fromtimestamp(now-age,timezone.utc).isoformat()
    return {"worker_id":"worker-1", "state":"running", "summary":{} if missing else {"telemetry":raw}}


def test_summaries_do_not_turn_missing_or_stale_data_into_zero_traffic():
    now = datetime.fromtimestamp(1000,timezone.utc)
    row = measured_row(1000)
    value = summarize_attempts([row],now=now)
    assert value["active_source_bytes_per_second"] == 10
    assert value["source_read_bytes"] == 100 and not value["wire_bytes_available"]
    assert summarize_attempts([measured_row(1000,age=16)],now=now)["active_source_bytes_per_second"] is None
    mixed = summarize_attempts([row,measured_row(1000,missing=True)],now=now)
    assert mixed["active_source_bytes_per_second"] is None and mixed["coverage"] == "partial"
    assert summarize_attempts([],now=now)["coverage"] == "unavailable"
    row["state"] = "abandoned"
    assert summarize_attempts([row],now=now)["coverage"] == "partial"
    assert summarize_attempts([measured_row(1000,age=-1)],now=now)["coverage"] == "unavailable"


def feed():
    return {"samples":[{"path_id":"test-path", "meter_id":"edge-1", "boot_id":"boot-1",
        "sequence":i, "timestamp_seconds":940.0+5*i,
        "total_wire_bytes":i*5000, "migration_wire_bytes":i*3000} for i in range(13)]}


def test_network_file_valid_reset_stale_and_bounded(tmp_path):
    path = tmp_path / "counter.json"
    cfg = MonitorConfig(policy=policy(),kind="file",counter_file=str(path),route_validated=True)
    reader = NetworkReader(cfg)
    assert reader.read(1000)["status"] == "unavailable"
    data = feed(); path.write_text(json.dumps(data))
    value = reader.read(1000)
    assert value["status"] == "fresh"
    assert value["total_bytes_per_second"] == 1000
    assert value["migration_bytes_per_second"] == 600
    assert value["external_bytes_per_second"] == 400
    assert reader.read(1016)["status"] == "stale"
    data["samples"][-1]["attribution_coverage"] = 0.5
    path.write_text(json.dumps(data))
    assert reader.read(1000)["status"] == "insufficient_attribution"
    data["samples"][-1]["attribution_coverage"] = 1.0
    data["samples"][-1]["total_wire_bytes"] = 0
    path.write_text(json.dumps(data))
    assert reader.read(1000)["status"] == "counter_reset"
    path.write_text("x"*131073)
    assert reader.read(1000)["status"] == "unavailable"


def test_fastconnect_display_only_converts_bits_filters_resource_and_caches():
    pytest.importorskip("oci")
    cfg = MonitorConfig(policy=policy(),kind="oci_fastconnect",
        compartment_id="ocid1.compartment.oc1..test",resource_id="ocid1.virtualcircuit.oc1.iad.test")
    calls = []
    def query(compartment, request, **kwargs):
        calls.append(request)
        assert compartment == cfg.compartment_id
        def stream(resource,value):
            return NS(name="BitRateReceived",dimensions={"resourceId":resource},aggregated_datapoints=[
                NS(timestamp=datetime.fromtimestamp(1000,timezone.utc),value=value)])
        return NS(data=[stream(cfg.resource_id,8_000_000),stream("unrelated",999_999_999)])
    reader = NetworkReader(cfg,client=NS(summarize_metrics_data=query))
    value = reader.read(1010)
    assert value["status"] == "slow_unattributed"
    assert value["total_bytes_per_second"] == 1_000_000
    assert value["observation"] is None and value["migration_bytes_per_second"] is None
    assert reader.read(1011)["age_seconds"] == 11 and len(calls) == 1
    assert 'BitRateReceived[1m]' in calls[0].query
    assert reader.read(1200)["status"] == "stale" and len(calls) == 2


def test_monitor_keeps_state_and_bounded_session_history(tmp_path):
    path = tmp_path / "missing"
    cfg = MonitorConfig(policy=policy(),kind="file",counter_file=str(path))
    clock = [1000.0]
    monitor = CapacityMonitor(cfg,clock=lambda:clock[0])
    previous_values = []
    def preview(job,request,previous=None):
        previous_values.append(previous)
        snapshot = CapacitySnapshot(now_seconds=clock[0])
        decision = evaluate(request.policy,snapshot,previous)
        return {"decision":decision.model_dump(mode="json"),"snapshot":snapshot.model_dump(mode="json")}
    for _ in range(125):
        result = monitor.status("a",preview)
        clock[0] += 30
    assert len(result["history"]) == 120
    assert previous_values[0] is None and previous_values[1] is not None
    assert result["actuation_enabled"] is False
    assert result["recommendation"]["recommended_add_nodes"] == 0
    for i in range(101):
        monitor.status(str(i),preview)
    assert len(monitor.jobs) == 100 and "a" not in monitor.jobs
    assert CapacityMonitor().status("a",lambda *args:pytest.fail("No preview without config"))["recommendation"] is None
