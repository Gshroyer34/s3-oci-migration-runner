"""OCI instance-principal remotes with server-side copying explicitly disabled."""
from migration_runner.worker.cloud import CloudRclone
from migration_runner.worker.rclone import Rclone, Limits, WorkerError
from .contracts import Run


class BenchmarkRclone(CloudRclone):
    def __init__(self, run, executable='rclone'):
        self.binding = Run.model_validate(run)
        self.run = self.binding
        Rclone.__init__(self, executable, Limits(timeout_seconds=self.run.max_seconds, attempts=2))
        for side in ('SOURCE', 'DESTINATION'):
            self.env.update({f'RCLONE_CONFIG_{side}_{key}': value for key, value in {
                'TYPE': 'oracleobjectstorage', 'PROVIDER': 'instance_principal_auth',
                'NAMESPACE': self.run.namespace, 'REGION': self.run.region,
                'NO_CHECK_BUCKET': 'true', 'ENCODING': 'None',
            }.items()})

    def args(self, command, *paths):
        if command not in ('cat', 'copyto', 'lsjson'):
            raise WorkerError('Benchmark command is not allowed')
        return Rclone.args(self, command, *paths) + [
            '--disable', 'Copy,Move', '--use-server-modtime',
            '--oos-upload-cutoff', '16Mi', '--oos-chunk-size', f'{self.run.part_size_mib}Mi',
            '--oos-upload-concurrency', str(self.run.part_concurrency),
            '--oos-attempt-resume-upload=false', '--oos-leave-parts-on-error=false']

    def probe(self, root=None):
        result = super().probe(root)
        return result | {'backend': 'oci-to-oci', 'server_side_copy_enabled': False}

    def validate_copy(self, source, destination):
        self.validate_read(source)
        self.validate_read(destination)
        if (source.side != 'source' or destination.side != 'destination'
                or destination.key != self.run.destination_prefix + source.key[len(self.run.source_prefix):]):
            raise WorkerError('Copy must follow the exact campaign mapping')
