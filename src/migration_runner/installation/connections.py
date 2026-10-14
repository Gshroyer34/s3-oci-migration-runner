"""Bounded, read-only checks using credentials selected by the local operator.

No credential values, arbitrary endpoints, shell commands or filesystem paths are
accepted from HTTP. This stage tests the setup host, never worker readiness.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal

from pydantic import Field, model_validator

from .profile import InputModel, Source, Destination


class ConnectionPath(InputModel):
    payload_route: Literal['internet', 'private']
    s3_endpoint_id: str | None = Field(default=None, pattern=r'^vpce-[0-9a-f]{8,32}$')

    @model_validator(mode='after')
    def route(self):
        if (self.payload_route == 'private') != bool(self.s3_endpoint_id):
            raise ValueError('Private transport requires an endpoint ID.')
        return self


class ConnectionSettings(InputModel):
    source: Source
    destination: Destination
    network: ConnectionPath

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()


def check(name, status, detail):
    return {'name': name, 'status': status, 'detail': detail}


class ProbeError(Exception):
    """Only allowlisted, nonsecret text is exposed to the browser."""


def safe_error(raw):
    value = str(raw).lower()
    if any(x in value for x in ('timeout', 'timed out', 'endpointconnection', 'could not connect', 'name resolution', 'proxyconnection')):
        return 'Connection timed out or endpoint unreachable. Check network, DNS and proxy settings, then retry.'
    if any(x in value for x in ('expired', 'invalidclienttoken', 'invalidsecuritytoken', 'notauthenticated', 'unable to locate credentials', 'security token', 'refresh', 'login')):
        return 'Cloud login is missing or expired. Renew the selected local cloud profile and retry.'
    if any(x in value for x in ('accessdenied', 'forbidden', 'notauthorized', '403')):
        return 'Access denied. Check the selected identity, resource ownership and scoped permissions.'
    if any(x in value for x in ('nosuchbucket', 'notfound', '404')):
        return 'Resource unavailable. Check its name, region and permissions.'
    return 'Cloud check failed. Check the local credential profile, region, resource scope and permissions.'


class LocalConnectionTester:
    def __init__(self, aws_profile, oci_profile, *, aws_cli=None, defaults=None):
        self.aws_profile = aws_profile
        self.oci_profile = oci_profile
        self.aws_cli = aws_cli or shutil.which('aws')
        self.defaults = defaults
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def context(self):
        return {'enabled': True, 'execution_location': 'Local setup host',
                'credential_mode': 'Existing local administrator profiles; not worker identities',
                'aws_profile': self.aws_profile, 'oci_profile': self.oci_profile,
                'defaults': self.defaults.model_dump() if self.defaults else None}

    def run(self, settings):
        if not self.lock.acquire(blocking=False):
            raise ProbeError('A connection test is already running. Wait for its results.')
        try:
            if time.monotonic() < self.next_allowed:
                raise ProbeError('Please wait ten seconds between connection tests.')
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=2) as pool:
                aws = pool.submit(self.aws_checks, settings)
                oci = pool.submit(self.oci_checks, settings)
                checks = aws.result() + oci.result()
            read_ok = all(c['status'] == 'passed' for c in checks)
            checks += [
                check('Destination write and multipart upload', 'not_tested',
                      'Read-only test: no destination objects were written. A controlled write and cleanup test is required before migration.'),
                check('Worker credentials and network path', 'not_tested',
                      'Run preflight from the deployed OCI workers using their workload identities. Local administrator access does not prove worker access.'),
                check('Private interconnect' if settings.network.payload_route == 'private' else 'Migration network capacity',
                      'not_tested', 'These requests use public cloud service endpoints from this setup host. Routes, private DNS, endpoint enforcement and bandwidth are not verified.'),
            ]
            return {'checked_at': datetime.now(timezone.utc).isoformat(),
                    'connection_revision': settings.revision,
                    'elapsed_seconds': round(time.monotonic() - started, 1),
                    'execution_location': 'Local setup host',
                    'credential_mode': 'Local administrator profiles, not worker identities',
                    'read_checks_passed': read_ok, 'migration_ready': False,
                    'status': 'read_checks_passed' if read_ok else 'attention_required',
                    'checks': checks}
        finally:
            self.next_allowed = time.monotonic() + 10
            self.lock.release()

    def aws_checks(self, settings):
        source = settings.source
        names = ['AWS identity', 'Source bucket owner and region', 'Source prefix listing', 'Source sample read']
        result = []
        deadline = time.monotonic() + 60

        def call(service, operation, *args):
            if not self.aws_cli:
                raise ProbeError('AWS CLI is not installed on the setup host.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError('Connection test timed out. Check network access and retry.')
            endpoint = f'https://{ "sts" if service == "sts" else "s3"}.{source.region}.amazonaws.com'
            env = dict(os.environ, AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off',
                       AWS_MAX_ATTEMPTS='1', AWS_EC2_METADATA_DISABLED='true')
            # Explicit profile and endpoint take priority over ambient credentials/endpoints.
            for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN'):
                env.pop(key, None)
            try:
                process = subprocess.run([self.aws_cli, '--profile', self.aws_profile, '--region', source.region,
                    '--endpoint-url', endpoint, '--cli-connect-timeout', '5', '--cli-read-timeout', '8',
                    '--output', 'json', '--no-cli-pager', '--no-paginate', service, operation, *args],
                    capture_output=True, text=True, timeout=min(15, remaining), env=env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if process.returncode:
                    raise ProbeError(safe_error(process.stderr))
                return json.loads(process.stdout)
            except subprocess.TimeoutExpired:
                raise ProbeError('Connection test timed out. Check network access and retry.') from None
            except (OSError, ValueError):
                raise ProbeError('AWS CLI could not complete the check. Verify its installation and selected local profile.') from None

        try:
            identity = call('sts', 'get-caller-identity')
            if identity['Account'] != source.account_id:
                raise ProbeError('The signed-in AWS account differs from the source account. Select the intended local profile.')
            result.append(check(names[0], 'passed', 'Authenticated to the expected AWS account using the configured local profile.'))
            bucket = ['--bucket', source.bucket, '--expected-bucket-owner', source.account_id]
            location = call('s3api', 'get-bucket-location', *bucket).get('LocationConstraint') or 'us-east-1'
            if location == 'EU':
                location = 'eu-west-1'
            if location != source.region:
                raise ProbeError('The source bucket region differs from the configured AWS region.')
            result.append(check(names[1], 'passed', 'Bucket ownership and region match the entered source settings.'))
            objects = call('s3api', 'list-objects-v2', *bucket, '--prefix', source.prefix, '--max-keys', '1').get('Contents', [])
            result.append(check(names[2], 'passed', 'Listed the approved prefix (at most one object; not a full inventory).'))
            if not objects:
                result.append(check(names[3], 'not_tested', 'The prefix is empty. Add an approved test object to verify source reads.'))
            elif objects[0].get('Size', 0) == 0:
                result.append(check(names[3], 'not_tested', 'The first object is empty. A nonempty sample is needed to verify payload reads.'))
            else:
                item = objects[0]
                if not item['Key'].startswith(source.prefix):
                    raise ProbeError('The returned sample is outside the approved prefix. No object was read.')
                # Pin the listed ETag to avoid reading a changed sample. Never expose its name or payload.
                with tempfile.TemporaryDirectory(prefix='migration-read-check-') as directory:
                    target = Path(directory) / 'sample'
                    metadata = call('s3api', 'get-object', *bucket, '--key', item['Key'],
                                    '--range', 'bytes=0-0', '--if-match', item['ETag'], str(target))
                    if target.stat().st_size != 1 or metadata.get('ContentLength') != 1:
                        raise ProbeError('Unexpected sample response. Source read was not verified.')
                result.append(check(names[3], 'passed', 'Read one byte from one source object and removed the local sample. This does not verify every object or KMS key.'))
        except ProbeError as error:
            result.append(check(names[len(result)], 'failed', str(error)))
        except Exception:
            result.append(check(names[len(result)], 'failed', 'Source check could not complete. Verify the source settings and local authentication.'))
        for name in names[len(result):]:
            result.append(check(name, 'not_tested', 'An earlier source check failed. Correct it and retry.'))
        return result

    def oci_checks(self, settings):
        try:
            process = subprocess.run([sys.executable, '-m', 'migration_runner.installation.oci_probe'],
                input=json.dumps({'profile': self.oci_profile, 'destination': settings.destination.model_dump()}),
                capture_output=True, text=True, timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode:
                raise ValueError()
            return json.loads(process.stdout)
        except subprocess.TimeoutExpired:
            return [check('OCI connection', 'failed', 'OCI check timed out. Check network access and retry; destination checks are incomplete.')]
        except (OSError, ValueError):
            return [check('OCI connection', 'failed', 'OCI check could not start. Install the cloud dependencies and configure the selected local OCI API-key profile.')]
