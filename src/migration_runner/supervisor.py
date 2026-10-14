"""Single-coordinator local process supervision; leases remain authoritative."""
import os
import subprocess
import sys
import threading


class LocalSupervisor:
    def __init__(self):
        self.children = {}
        self.lock = threading.Lock()

    def launch(self, job, count):
        with self.lock:
            self.children = {j:[p for p in ps if p.poll() is None] for j,ps in self.children.items()}
            live = self.children.setdefault(str(job), [])
            available = max(0, 4-sum(map(len,self.children.values())))
            environment = dict(os.environ)
            environment['MIGRATION_OPERATOR_ENABLED']='false'
            environment['MIGRATION_OPERATOR_TOKEN']=''
            for _ in range(min(available, max(0,count-len(live)))):
                live.append(subprocess.Popen([sys.executable,'-m','migration_runner.worker','--run-loop','--job',str(job)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment))
            return len(live)

    def close(self):
        with self.lock:
            for children in self.children.values():
                for p in children:
                    if p.poll() is None:
                        p.terminate()
                        try:
                            p.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            p.kill()
                            p.wait()
