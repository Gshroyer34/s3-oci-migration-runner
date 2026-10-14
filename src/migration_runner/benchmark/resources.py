"""Bounded Linux host samples; these include non-benchmark host activity."""
from pathlib import Path
import threading
import time


def host_sample():
    cpu=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    network={}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name,values=line.split(':',1);values=values.split();name=name.strip()
        if name!='lo':network[name]={'rx_bytes':int(values[0]),'tx_bytes':int(values[8])}
    return {'monotonic_seconds':time.monotonic(),'cpu_total_ticks':sum(cpu),'cpu_idle_ticks':cpu[3]+cpu[4],
            'memory_used_bytes':memory['MemTotal']-memory['MemAvailable'],'network':network}


class HostSampler:
    def __init__(self):
        self.stop=threading.Event();self.samples=[];self.errors=0
    def sample(self):
        try:
            if len(self.samples)<725:self.samples.append(host_sample())
        except (OSError,ValueError,KeyError,IndexError):self.errors+=1
    def loop(self):
        while not self.stop.wait(5):self.sample()
    def start(self):
        self.sample();self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def finish(self):
        self.stop.set();self.thread.join(timeout=6);self.sample()
        result={'scope':'whole Linux guest; not path utilization','sample_errors':self.errors,'samples':self.samples}
        if len(self.samples)<2:return result
        first,last=self.samples[0],self.samples[-1];total=last['cpu_total_ticks']-first['cpu_total_ticks']
        idle=last['cpu_idle_ticks']-first['cpu_idle_ticks'];seconds=last['monotonic_seconds']-first['monotonic_seconds']
        result['cpu_busy_fraction']=(total-idle)/total if total>0 else None
        result['peak_host_memory_used_bytes']=max(x['memory_used_bytes'] for x in self.samples)
        result['network_bytes_per_second']={name:{key:(value-first['network'][name][key])/seconds for key,value in data.items()}
            for name,data in last['network'].items() if name in first['network'] and seconds>0}
        return result
