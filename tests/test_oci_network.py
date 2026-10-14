from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
from pydantic import ValidationError
from migration_runner.autoscaling.oci_network import WorkerNetworkConfig, WorkerNetworkReader
from migration_runner.autoscaling.monitor import CapacityMonitor


def config():
    return WorkerNetworkConfig(compartment_id='ocid1.compartment.oc1..lab',vnics=[
        {'node_id':'worker-1','vnic_id':'ocid1.vnic.oc1.iad.first'},
        {'node_id':'worker-2','vnic_id':'ocid1.vnic.oc1.iad.second'}])


def fake_reader(*,missing=False,stale=False,duplicate=False,negative=False,error=False):
    pytest.importorskip('oci')
    calls=[]
    def query(compartment,request,**kwargs):
        calls.append(request)
        assert compartment==config().compartment_id
        assert request.namespace=='oci_vcn' and request.resolution=='1m' and request.query.endswith('.sum()')
        metric=request.query.split('[')[0]
        resource=request.query.split('"')[1]
        if error: raise RuntimeError('private-provider-error')
        if missing and resource.endswith('second'): return NS(data=[])
        value=-1 if negative else 6000 if metric=='VnicFromNetworkBytes' else 12000
        point=NS(timestamp=datetime.fromtimestamp(600 if stale else 900,timezone.utc),value=value)
        points=[point,point] if duplicate else [point]
        # Incomplete provider window must not be selected.
        points=points+[NS(timestamp=datetime.fromtimestamp(990,timezone.utc),value=999999)]
        stream=NS(name=metric,dimensions={'resourceId':resource},aggregated_datapoints=points)
        other=NS(name=metric,dimensions={'resourceId':'unrelated'},aggregated_datapoints=[point])
        return NS(data=[stream,other])
    return WorkerNetworkReader(config(),client=NS(summarize_metrics_data=query)),calls


def test_network_uses_complete_aligned_byte_windows_and_directional_totals():
    reader,calls=fake_reader()
    value=reader.read(1000)
    assert value['status']=='worker_network' and value['scope']=='worker_vnics'
    assert value['receive_bytes_per_second']==200
    assert value['send_bytes_per_second']==400
    assert value['age_seconds']==40 and value['sample_end_seconds']==960
    assert value['measured_vnics']==2 and len(value['workers'])==2
    assert value['observation'] is None and value['utilization_fraction'] is None
    assert value['total_bytes_per_second'] is None and value['capacity_bytes_per_second'] is None
    assert reader.read(1005)['age_seconds']==45 and len(calls)==4
    monitor=CapacityMonitor(worker_network_reader=reader,clock=lambda:1005)
    result=monitor.status('job',lambda *a:pytest.fail('VNIC data must not enter path evaluator'))
    assert result['recommendation'] is None and result['actuation_enabled'] is False


@pytest.mark.parametrize('flag,status',[('missing','partial'),('stale','stale'),
    ('duplicate','unavailable'),('negative','unavailable'),('error','unavailable')])
def test_invalid_or_partial_network_data_never_becomes_zero_traffic(flag,status):
    reader,_=fake_reader(**{flag:True})
    value=reader.read(1000)
    assert value['status']==status
    assert value['receive_bytes_per_second'] is None and value['send_bytes_per_second'] is None
    assert 'private-provider-error' not in str(value)
    assert value['observation'] is None


def test_network_target_validation_and_no_double_counting():
    data=config().model_dump()
    data['vnics'][1]['vnic_id']=data['vnics'][0]['vnic_id']
    with pytest.raises(ValidationError): WorkerNetworkConfig.model_validate(data)
    data=config().model_dump();data['vnics'][0]['vnic_id']='x"}.sum()'
    with pytest.raises(ValidationError): WorkerNetworkConfig.model_validate(data)


def test_network_refresh_failure_drops_previous_values():
    reader,_=fake_reader()
    assert reader.read(1000)['receive_bytes_per_second']==200
    def fail(*args): raise RuntimeError('private-error')
    reader._collect=fail
    result=reader.read(1060)
    assert result['status']=='unavailable' and result['receive_bytes_per_second'] is None
