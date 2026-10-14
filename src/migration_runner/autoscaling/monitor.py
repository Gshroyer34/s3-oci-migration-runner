"""Read-only network feeds and bounded, process-local shadow history."""
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
import math
import stat
from pathlib import Path
import threading
import time
from typing import Literal

from pydantic import Field, model_validator

from .contracts import CapacityPolicy, CapacityPreviewRequest, Contract, ControllerState
from .telemetry import PathCounterSample, aggregate_path


class MonitorConfig(Contract):
    policy: CapacityPolicy
    kind: Literal['file', 'oci_fastconnect']
    route_validated: bool = False
    counter_file: str | None = None
    region: str = Field(default='us-ashburn-1', pattern=r'^[a-z0-9-]{1,40}$')
    compartment_id: str | None = Field(default=None, pattern=r'^ocid1\.compartment\.[A-Za-z0-9_.-]+$')
    resource_id: str | None = Field(default=None, pattern=r'^ocid1\.(virtualcircuit|crossconnectgroup)\.[A-Za-z0-9_.-]+$')
    worker_source_bytes_per_second: float | None = Field(default=None, ge=1, le=10**15)
    worker_profile_seconds: float = Field(default=0, ge=0, le=10**9)

    @model_validator(mode='after')
    def source(self):
        if self.kind == 'file' and (not self.counter_file or not Path(self.counter_file).is_absolute()):
            raise ValueError('Absolute counter-file path required')
        if self.kind == 'oci_fastconnect' and (not self.compartment_id or not self.resource_id):
            raise ValueError('Explicit monitored compartment and circuit required')
        return self


class CounterBatch(Contract):
    samples: list[PathCounterSample] = Field(min_length=2, max_length=256)


class NetworkReader:
    def __init__(self, config, client=None):
        self.config, self.client = config, client
        self.cached = None
        self.last_read = float('-inf')

    def read(self, now):
        cfg = self.config
        result = {'source':cfg.kind, 'status':'unavailable', 'observation':None,
                  'capacity_bytes_per_second':cfg.policy.capacity_bytes_per_second,
                  'headroom_fraction':cfg.policy.headroom_fraction, 'total_bytes_per_second':None,
                  'utilization_fraction':None, 'age_seconds':None, 'route_validated':cfg.route_validated,
                  'migration_bytes_per_second':None, 'external_bytes_per_second':None}
        try:
            if cfg.kind == 'file':
                path = Path(cfg.counter_file)
                if any(p.is_symlink() for p in (path, *path.parents)) or not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError('Counter-file symlinks are not supported')
                with path.open('rb') as stream:
                    raw = stream.read(131073)
                if len(raw) > 131072:
                    raise ValueError('Counter feed exceeds limit')
                samples = CounterBatch.model_validate_json(raw).samples
                obs = aggregate_path(cfg.policy, samples, now_seconds=now)
                age = max(0, now - samples[-1].timestamp_seconds)
                valid = obs.valid and obs.attribution_coverage >= 0.95
                status = 'fresh' if valid else obs.invalid_reason if not obs.valid else 'insufficient_attribution'
                result.update(status=status, age_seconds=age)
                if valid:
                    result.update(observation=obs, total_bytes_per_second=obs.total_bytes_per_second,
                                  utilization_fraction=obs.total_bytes_per_second/cfg.policy.capacity_bytes_per_second,
                                  migration_bytes_per_second=obs.migration_bytes_per_second,
                                  external_bytes_per_second=obs.external_p95_bytes_per_second)
                return result
            # Provider aggregation is display-only; it lacks migration attribution
            # and does not meet the fast-control sample interval.
            if now - self.last_read >= 60:
                self.last_read = now
                self.cached = None
                self.cached = self._read_fastconnect(now)
            if self.cached:
                stamp, rate = self.cached
                age = now - stamp
                if 0 <= age <= 180:
                    result.update(status='slow_unattributed', age_seconds=age,
                                  total_bytes_per_second=rate,
                                  utilization_fraction=rate/cfg.policy.capacity_bytes_per_second)
                else:
                    result.update(status='stale', age_seconds=max(0,age))
            return result
        except Exception:
            # Never expose provider error text, local paths, or credential discovery.
            return result

    def _read_fastconnect(self, now):
        import oci
        cfg = self.config
        if self.client is None:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self.client = oci.monitoring.MonitoringClient({'region':cfg.region}, signer=signer,
                timeout=(3,5), retry_strategy=oci.retry.NoneRetryStrategy())
        end = datetime.fromtimestamp(now, timezone.utc)
        request = oci.monitoring.models.SummarizeMetricsDataDetails(namespace='oci_fastconnect',
            query='BitRateReceived[1m]{resourceId = "' + cfg.resource_id + '"}.mean()',
            start_time=end-timedelta(minutes=10), end_time=end, resolution='1m')
        response = self.client.summarize_metrics_data(cfg.compartment_id, request,
                                                     retry_strategy=oci.retry.NoneRetryStrategy())
        points = []
        for stream in response.data:
            if stream.name != 'BitRateReceived' or stream.dimensions.get('resourceId') != cfg.resource_id:
                continue
            for point in stream.aggregated_datapoints:
                value = point.value
                if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0:
                    points.append((point.timestamp.timestamp(), value/8))
        return max(points, default=None)


class CapacityMonitor:
    def __init__(self, config=None, reader=None, clock=time.time, worker_network_reader=None):
        self.config = config
        self.worker_network_reader = worker_network_reader
        self.reader = reader or (NetworkReader(config) if config else None)
        self.clock = clock
        self.lock = threading.Lock()
        self.jobs = OrderedDict()

    def status(self, job, preview):
        if not self.config:
            if self.worker_network_reader:
                network = self.worker_network_reader.read(self.clock())
                network.pop('observation', None)
                return {'network':network, 'recommendation':None, 'history':[],
                    'reason':'Worker network traffic is measured. A validated path meter and worker calibration are required for capacity recommendations.',
                    'actuation_enabled':False, 'history_persistence':'process_lifetime'}
            return {'network':{'status':'not_configured','utilization_fraction':None,
                    'total_bytes_per_second':None,'age_seconds':None}, 'recommendation':None,
                    'history':[], 'reason':'Connect network monitoring to calculate worker recommendations.',
                    'actuation_enabled':False, 'history_persistence':'process_lifetime'}
        with self.lock:
            cfg = self.config
            network = self.reader.read(self.clock())
            obs = network.pop('observation')
            if self.worker_network_reader:
                worker_network = self.worker_network_reader.read(self.clock())
                worker_network.pop('observation', None)
                network['worker_network'] = worker_network
            state, history = self.jobs.pop(str(job), (None, deque(maxlen=120)))
            request = CapacityPreviewRequest(policy=cfg.policy, observation=obs,
                route_validated=cfg.route_validated,
                worker_source_bytes_per_second=cfg.worker_source_bytes_per_second,
                worker_profile_seconds=cfg.worker_profile_seconds)
            value = preview(job, request, previous=state)
            decision = value['decision']
            state = ControllerState.model_validate(decision['state'])
            stamp = value['snapshot']['now_seconds']
            # Sample history at most every 30s, not on every browser refresh.
            if not history or stamp-history[-1]['at_seconds'] >= 30:
                history.append({'at_seconds':stamp, 'desired_nodes':decision['desired_nodes'],
                                'budget_bytes_per_second':decision['budget_bytes_per_second'],
                                'reasons':decision['reasons']})
            self.jobs[str(job)] = (state,history)
            while len(self.jobs) > 100:
                self.jobs.popitem(last=False)
            return {'network':network, 'recommendation':decision, 'history':list(history),
                    'reason':'Recommendations only; worker deployment is unchanged.',
                    'actuation_enabled':False, 'history_persistence':'process_lifetime'}
