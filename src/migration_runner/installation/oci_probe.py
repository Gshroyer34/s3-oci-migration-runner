"""Read-only OCI subprocess. The parent enforces a hard wall-clock deadline."""
import json
import sys

from .connections import check, safe_error, ProbeError
from .profile import Destination


def probe(destination, profile, *, sdk=None):
    names = ['OCI identity and namespace', 'Destination bucket and compartment', 'Destination prefix listing']
    result = []
    try:
        if sdk is None:
            import oci as sdk
        config = sdk.config.from_file(profile_name=profile)
        if config['tenancy'] != destination.tenancy_ocid:
            raise ProbeError('The local OCI profile tenancy differs from the entered destination tenancy.')
        config['region'] = destination.region
        client = sdk.object_storage.ObjectStorageClient(config,
            service_endpoint=f'https://objectstorage.{destination.region}.oraclecloud.com',
            timeout=(5, 8), retry_strategy=sdk.retry.NoneRetryStrategy())
        if client.get_namespace(compartment_id=destination.tenancy_ocid).data != destination.namespace:
            raise ProbeError('The authenticated tenancy namespace differs from the entered namespace.')
        result.append(check(names[0], 'passed', 'Authenticated to the expected tenancy and verified its Object Storage namespace.'))
        bucket = client.get_bucket(destination.namespace, destination.bucket).data
        if bucket.compartment_id != destination.compartment_ocid:
            raise ProbeError('The destination bucket is in a different compartment from the entered installation compartment.')
        result.append(check(names[1], 'passed', 'Destination bucket exists in the selected OCI region and compartment.'))
        client.list_objects(destination.namespace, destination.bucket, prefix=destination.prefix, limit=1)
        result.append(check(names[2], 'passed', 'Destination prefix listing is permitted. An empty prefix is valid; write permission has not been tested.'))
    except ProbeError as error:
        result.append(check(names[len(result)], 'failed', str(error)))
    except ImportError:
        result.append(check(names[len(result)], 'failed', 'Install the cloud dependencies on the setup host to enable OCI checks.'))
    except Exception as error:
        result.append(check(names[len(result)], 'failed', safe_error(error)))
    for name in names[len(result):]:
        result.append(check(name, 'not_tested', 'An earlier destination check failed. Correct it and retry.'))
    return result


if __name__ == '__main__':
    request = json.loads(sys.stdin.read(16384))
    print(json.dumps(probe(Destination.model_validate(request['destination']), request['profile'])))
