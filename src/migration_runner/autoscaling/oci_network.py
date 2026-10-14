"""OCI VNIC measurements for display. Never an attributed interconnect observation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import threading

from pydantic import Field, model_validator
from .contracts import Contract, Identifier


class WorkerVnic(Contract):
    node_id: Identifier
    vnic_id: str = Field(pattern=r'^ocid1\.vnic\.[A-Za-z0-9_.-]+$')


class WorkerNetworkConfig(Contract):
    region: str = Field(default='us-ashburn-1',pattern=r'^[a-z0-9-]{1,40}$')
    compartment_id: str = Field(pattern=r'^ocid1\.compartment\.[A-Za-z0-9_.-]+$')
    vnics: list[WorkerVnic] = Field(min_length=1,max_length=4)

    @model_validator(mode='after')
    def unique(self):
        if len({v.vnic_id for v in self.vnics})!=len(self.vnics):
            raise ValueError('Each VNIC may be counted only once')
        return self


class WorkerNetworkReader:
    def __init__(self,config,client=None):
        self.config,self.client=config,client
        self.cached=None
        self.last_read=float('-inf')
        self.lock=threading.Lock()

    def _query(self,target,metric,now):
        import oci
        end=datetime.fromtimestamp(now,timezone.utc)
        request=oci.monitoring.models.SummarizeMetricsDataDetails(namespace='oci_vcn',
            query=metric+'[1m]{resourceId = "'+target.vnic_id+'"}.sum()',
            start_time=end-timedelta(minutes=10),end_time=end,resolution='1m')
        response=self.client.summarize_metrics_data(self.config.compartment_id,request,
            retry_strategy=oci.retry.NoneRetryStrategy())
        points={}
        streams=[s for s in response.data if s.name==metric and (s.dimensions or {}).get('resourceId')==target.vnic_id]
        # Ambiguous duplicate streams are not silently added together.
        if len(streams)!=1:
            return points
        for point in streams[0].aggregated_datapoints or []:
            stamp=point.timestamp.timestamp()
            value=point.value
            if stamp in points: return {}
            if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 and stamp+60<=now:
                points[stamp]=float(value)/60.0
        return points

    def _collect(self,now):
        import oci
        if self.client is None:
            signer=oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self.client=oci.monitoring.MonitoringClient({'region':self.config.region},signer=signer,
                timeout=(3,5),retry_strategy=oci.retry.NoneRetryStrategy())
        tasks=[(v,m) for v in self.config.vnics for m in ('VnicFromNetworkBytes','VnicToNetworkBytes')]
        values={}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(self._query,v,m,now) for v,m in tasks]
            for (v,m),future in zip(tasks,futures):
                try: values[(v.vnic_id,m)]=future.result()
                except Exception: values[(v.vnic_id,m)]={}
        return values

    def read(self,now):
        with self.lock:
            if now-self.last_read>=60:
                self.last_read=now
                self.cached=None
                try: self.cached=self._collect(now)
                except Exception: pass
            result={'source':'oci_vcn','scope':'worker_vnics','status':'unavailable',
                'observation':None,'capacity_bytes_per_second':None,'utilization_fraction':None,
                'headroom_fraction':None,'route_validated':False,'total_bytes_per_second':None,
                'migration_bytes_per_second':None,'external_bytes_per_second':None,
                'receive_bytes_per_second':None,'send_bytes_per_second':None,'age_seconds':None,
                'sample_interval_seconds':60,'expected_vnics':len(self.config.vnics),'measured_vnics':0,
                'workers':[],'aggregation':'sum of distinct worker VNICs, separately by direction'}
            values=self.cached
            if not values: return result
            common=None
            for v in self.config.vnics:
                rx=values[(v.vnic_id,'VnicFromNetworkBytes')]
                tx=values[(v.vnic_id,'VnicToNetworkBytes')]
                aligned=set(rx)&set(tx)
                common=aligned if common is None else common&aligned
                stamp=max(aligned,default=None)
                age=now-(stamp+60) if stamp is not None else None
                fresh=age is not None and 0<=age<=180
                result['workers'].append({'node_id':v.node_id,'status':'fresh' if fresh else 'stale' if stamp is not None else 'missing',
                    'sample_age_seconds':age,'sample_end_seconds':stamp+60 if stamp is not None else None,
                    'receive_bytes_per_second':rx[stamp] if fresh else None,'send_bytes_per_second':tx[stamp] if fresh else None})
                result['measured_vnics']+=int(fresh)
            stamp=max(common,default=None)
            age=now-(stamp+60) if stamp is not None else None
            result['age_seconds']=age
            if age is not None and 0<=age<=180:
                result.update(status='worker_network',sample_end_seconds=stamp+60,
                    receive_bytes_per_second=sum(values[(v.vnic_id,'VnicFromNetworkBytes')][stamp] for v in self.config.vnics),
                    send_bytes_per_second=sum(values[(v.vnic_id,'VnicToNetworkBytes')][stamp] for v in self.config.vnics))
            else:
                result['status']='partial' if result['measured_vnics'] else 'stale' if any(w['status']=='stale' for w in result['workers']) else 'unavailable'
            return result
