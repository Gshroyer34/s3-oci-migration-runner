"""Read-only, checkpointed S3 discovery. Never creates a migration or reads payloads."""
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from .profile import InputModel, Source
from .connections import safe_error


class DiscoveryError(ValueError):
    pass


class DiscoverySettings(InputModel):
    account_id: str = Field(pattern=r'^\d{12}$')
    region: str = Field(pattern=r'^(us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+$', max_length=40)
    bucket: str = Field(pattern=r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
    scope: Literal['prefix', 'bucket'] = 'prefix'
    prefix: str = ''
    object_limit: int = Field(default=10000, strict=True, ge=1, le=1000000)
    metadata_samples: int = Field(default=20, strict=True, ge=0, le=100)

    @field_validator('bucket')
    @classmethod
    def bucket_name(cls, value):
        Source.bucket_name(value)
        if value.endswith(('--x-s3', '-s3alias', '--ol-s3', '.mrap')):
            raise ValueError('Only general-purpose buckets are supported')
        return value

    @model_validator(mode='after')
    def source_scope(self):
        if (self.scope == 'bucket') != (self.prefix == ''):
            raise ValueError('Explicitly select whole bucket or provide a prefix')
        if '\x00' in self.prefix or len(self.prefix.encode('utf-8')) > 1024:
            raise ValueError('Invalid prefix')
        return self


def now():
    return datetime.now(timezone.utc).isoformat()


def size_band(size):
    for upper, label in ((1, '0 B'), (128*1024, '1 B–128 KiB'), (1024**2, '128 KiB–1 MiB'),
                         (100_000_001, '1 MiB–100 MB'), (1024**3, '>100 MB–1 GiB'),
                         (10_000_000_001, '1 GiB–10 GB')):
        if size < upper:
            return label
    return '>10 GB'


def phase(tier):
    return {'STANDARD': 'standard_first', 'GLACIER_IR': 'later_instant_retrieval',
            'DEEP_ARCHIVE': 'excluded_deep_archive'}.get(tier, 'review_other_tier')


def supported_key(key):
    return bool(re.fullmatch(r'[A-Za-z0-9_./-]+', key) and
                all(p not in ('', '.', '..') for p in key.split('/')))


class AWSDiscoveryClient:
    """AWS CLI supports the operator's existing interactive login profile."""
    def __init__(self, profile, executable=None):
        self.profile = profile
        self.executable = executable or shutil.which('aws')

    def call(self, settings, operation, **values):
        if operation not in ('get-bucket-location', 'list-objects-v2', 'head-object'):
            raise DiscoveryError('Discovery operation is not read-only')
        if not self.executable:
            raise DiscoveryError('AWS CLI is not installed on this setup host.')
        env = dict(os.environ, AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off', AWS_MAX_ATTEMPTS='1',
                   AWS_EC2_METADATA_DISABLED='true', PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
                   AWS_CLI_OUTPUT_ENCODING='UTF-8', AWS_CLI_FILE_ENCODING='UTF-8')
        for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN'):
            env.pop(k, None)
        args = [self.executable, '--profile', self.profile, '--region', settings.region,
                '--endpoint-url', f'https://s3.{settings.region}.amazonaws.com',
                '--cli-connect-timeout', '5', '--cli-read-timeout', '15', '--output', 'json',
                '--no-cli-pager', '--no-paginate', 's3api', operation,
                '--bucket', settings.bucket, '--expected-bucket-owner', settings.account_id]
        for k, v in values.items():
            # Equals form keeps keys/prefixes beginning with '-' literal.
            args.append('--' + k.replace('_', '-') + '=' + str(v))
        try:
            response = subprocess.run(args, capture_output=True, timeout=25, env=env,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if response.returncode:
                raise DiscoveryError(safe_error(response.stderr.decode('utf-8', errors='replace')))
            if len(response.stdout) > 4*1024*1024:
                raise DiscoveryError('Discovery response exceeded the page limit.')
            return json.loads(response.stdout.decode('utf-8'))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, DiscoveryError):
                raise
            raise DiscoveryError('Discovery request failed or timed out. Check the selected profile and source settings.') from None


class DiscoveryService:
    def __init__(self, directory, client):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise DiscoveryError('Use a private discovery state directory')
        self.path = directory / 'discovery.sqlite3'
        if self.path.is_symlink():
            raise DiscoveryError('Discovery database must not be a link')
        self.client = client
        self.lock = threading.Lock()
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY, settings TEXT NOT NULL, state TEXT NOT NULL,
                cursor TEXT, started TEXT NOT NULL, updated TEXT NOT NULL,
                pages INTEGER NOT NULL DEFAULT 0, head_requests INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS objects (
                scan TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL, tier TEXT NOT NULL,
                phase TEXT NOT NULL, band TEXT NOT NULL, key_supported INTEGER NOT NULL,
                etag TEXT, modified TEXT, metadata_state TEXT NOT NULL, observations TEXT,
                PRIMARY KEY(scan,key)); CREATE INDEX IF NOT EXISTS objects_scan ON objects(scan);
                CREATE TABLE IF NOT EXISTS cursors(scan TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(scan,token));''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def context(self):
        return {'enabled': True, 'execution_location': 'Local setup host',
                'credential_mode': 'Selected local AWS profile; not worker credentials',
                'aws_profile': self.client.profile, 'payload_reads': False,
                'maximum_objects': 1000000, 'maximum_metadata_samples': 100}

    def create(self, settings):
        scan = uuid4().hex
        with self.db() as c:
            if c.execute('SELECT count(*) FROM scans').fetchone()[0] >= 100:
                raise DiscoveryError('This state directory has 100 scans. Archive it before starting another.')
            c.execute('INSERT INTO scans(id,settings,state,started,updated) VALUES(?,?,?,?,?)',
                      (scan, settings.model_dump_json(), 'partial', now(), now()))
        return self.report(scan)

    def recent(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT id,state,started,updated FROM scans ORDER BY started DESC LIMIT 20')]

    def advance(self, scan):
        if not self.lock.acquire(blocking=False):
            raise DiscoveryError('A discovery page is already running. Wait and retry.')
        try:
            return self._advance(scan)
        finally:
            self.lock.release()

    def _advance(self, scan):
        with self.db() as c:
            run = c.execute('SELECT * FROM scans WHERE id=?', (scan,)).fetchone()
            if run is None:
                raise DiscoveryError('Unknown discovery scan')
            settings = DiscoverySettings.model_validate_json(run['settings'])
            count = c.execute('SELECT count(*) FROM objects WHERE scan=?', (scan,)).fetchone()[0]
        if run['state'] == 'complete' or count >= settings.object_limit:
            return self.report(scan)
        limit = min(1000, settings.object_limit-count)
        args = {'prefix': settings.prefix, 'max_keys': limit, 'encoding_type': 'url'}
        if run['cursor']:
            args['continuation_token'] = run['cursor']
        try:
            deadline = time.monotonic() + 45
            if run['pages'] == 0:
                location = self.client.call(settings, 'get-bucket-location').get('LocationConstraint')
                region = 'us-east-1' if location is None else 'eu-west-1' if location == 'EU' else location
                if region != settings.region:
                    raise DiscoveryError('Bucket region differs from the selected source region. Correct settings before discovery.')
            page = self.client.call(settings, 'list-objects-v2', **args)
            rows, more = page.get('Contents', []), page.get('IsTruncated')
            if (not isinstance(rows, list) or len(rows) > limit or type(more) is not bool
                    or page.get('EncodingType') != 'url'):
                raise DiscoveryError('Invalid discovery page; checkpoint was not advanced.')
            cursor = page.get('NextContinuationToken') if more else None
            if more and (not isinstance(cursor, str) or not cursor or cursor == run['cursor'] or len(cursor)>16384):
                raise DiscoveryError('Missing or repeated discovery cursor; checkpoint was not advanced.')
            if cursor:
                with self.db() as c:
                    if c.execute('SELECT 1 FROM cursors WHERE scan=? AND token=?', (scan, cursor)).fetchone():
                        raise DiscoveryError('Repeated discovery cursor; checkpoint was not advanced.')
            records, heads = [], run['head_requests']
            for item in rows:
                key = unquote(item['Key'], encoding='utf-8', errors='strict')
                size, tier = item['Size'], item.get('StorageClass', 'UNKNOWN')
                if (not key or len(key.encode('utf-8')) > 1024 or
                        not key.startswith(settings.prefix) or type(size) is not int or size < 0 or size > 2**63-1 or
                        not isinstance(tier, str) or not re.fullmatch(r'[A-Z0-9_]{1,40}', tier)):
                    raise DiscoveryError('Invalid or out-of-scope object; checkpoint was not advanced.')
                records.append([scan, key, size, tier, phase(tier), size_band(size), int(supported_key(key)),
                                str(item.get('ETag', ''))[:256], str(item.get('LastModified', ''))[:64],
                                'not_checked', None])
            # Metadata observations are deliberately limited to STANDARD. No
            # GetObject, RestoreObject, tag mutation, or archive HEAD is allowed.
            for row in records:
                if (row[3] != 'STANDARD' or not row[6] or heads >= settings.metadata_samples or
                        heads-run['head_requests'] >= 5 or time.monotonic() >= deadline):
                    continue
                heads += 1
                try:
                    h = self.client.call(settings, 'head-object', key=row[1], if_match=row[7])
                    encryption = h.get('ServerSideEncryption', 'UNKNOWN')
                    changed = h.get('ContentLength') != row[2] or h.get('ETag') != row[7] or h.get('StorageClass', 'STANDARD') != row[3]
                    retention = bool(h.get('ObjectLockMode') or h.get('ObjectLockRetainUntilDate') or h.get('ObjectLockLegalHoldStatus') == 'ON')
                    status = ('source_changed' if changed else 'retention_review' if retention else
                              'encryption_review' if encryption != 'AES256' else 'observed_sse_s3')
                    row[9:11] = [status, json.dumps({'encryption': encryption, 'versioned': bool(h.get('VersionId')),
                                                    'retention_observed': retention,
                                                    'metadata_field_count': len(h.get('Metadata', {}))})]
                except DiscoveryError:
                    row[9] = 'unavailable'
            with self.db() as c:
                for row in records:
                    c.execute('''INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scan,key)
                        DO UPDATE SET size=excluded.size,tier=excluded.tier,phase=excluded.phase,
                        band=excluded.band,key_supported=excluded.key_supported,etag=excluded.etag,
                        modified=excluded.modified,metadata_state=excluded.metadata_state,observations=excluded.observations''', row)
                total = c.execute('SELECT count(*) FROM objects WHERE scan=?', (scan,)).fetchone()[0]
                if cursor:
                    c.execute('INSERT INTO cursors VALUES(?,?)', (scan, cursor))
                state = 'complete' if not more else 'limit_reached' if total >= settings.object_limit else 'partial'
                c.execute('UPDATE scans SET state=?,cursor=?,updated=?,pages=pages+1,head_requests=?,error=NULL WHERE id=?',
                          (state, cursor, now(), heads, scan))
        except (DiscoveryError, KeyError, TypeError, ValueError) as error:
            message = str(error) if isinstance(error, DiscoveryError) else 'Invalid discovery response; checkpoint was not advanced.'
            with self.db() as c:
                c.execute('UPDATE scans SET state=?,error=?,updated=? WHERE id=?', ('failed', message, now(), scan))
        return self.report(scan)

    def report(self, scan):
        with self.db() as c:
            run = c.execute('SELECT * FROM scans WHERE id=?', (scan,)).fetchone()
            if run is None:
                raise DiscoveryError('Unknown discovery scan')
            settings = json.loads(run['settings'])
            def grouped(column):
                # Column names come only from calls below, never HTTP input.
                return [dict(r) for r in c.execute(f'SELECT {column} AS name,count(*) AS objects,coalesce(sum(size),0) AS bytes FROM objects WHERE scan=? GROUP BY {column} ORDER BY {column}', (scan,))]
            totals = dict(c.execute('SELECT count(*) AS objects,coalesce(sum(size),0) AS bytes FROM objects WHERE scan=?', (scan,)).fetchone())
            bounds = dict(c.execute('SELECT min(size) AS smallest_bytes,max(size) AS largest_bytes FROM objects WHERE scan=?', (scan,)).fetchone())
            blockers = dict(c.execute('''SELECT coalesce(sum(size>100000000),0) AS exceeds_current_object_limit,
                coalesce(sum(key_supported=0),0) AS unsupported_keys,
                coalesce(sum(metadata_state IN ('source_changed','retention_review','encryption_review','unavailable')),0) AS metadata_attention
                FROM objects WHERE scan=? AND phase='standard_first' ''', (scan,)).fetchone())
            return {'schema_version': 'migration-discovery/v1', 'id': scan, 'state': run['state'],
                    'settings': settings, 'started_at': run['started'], 'updated_at': run['updated'],
                    'listing_complete': run['state']=='complete', 'atomic_snapshot': False,
                    'migration_ready': False, 'execution_location': 'Local setup host',
                    'credential_mode': 'Selected local AWS profile; not worker credentials',
                    'totals': totals, 'size_range': bounds, 'tiers': grouped('tier'), 'size_bands': grouped('band'),
                    'phases': grouped('phase'), 'metadata_observations': grouped('metadata_state'),
                    'standard_phase_blockers': blockers, 'pages_completed': run['pages'],
                    'head_requests': run['head_requests'], 'error': run['error'],
                    'attention_examples': [dict(r) for r in c.execute('''SELECT key,size,
                        size>100000000 AS exceeds_object_limit,key_supported=0 AS unsupported_key,metadata_state
                        FROM objects WHERE scan=? AND phase='standard_first' AND
                        (size>100000000 OR key_supported=0 OR metadata_state IN
                        ('source_changed','retention_review','encryption_review','unavailable')) ORDER BY key LIMIT 20''', (scan,))],
                    'limitations': [
                        'Counts describe observed current objects only. A multi-page listing is not a snapshot; changes between pages may be missed.',
                        'Standard is selected for the first phase. Instant Retrieval is deferred; Deep Archive is excluded. Other tiers need review.',
                        'Metadata checks cover at most five Standard objects per page within the time and sample budgets, not a representative sample or every object. Tags, policies, versions and retention are not fully qualified.',
                        'Whole-bucket discovery does not enable whole-bucket transfer. The portable pilot still requires a dedicated prefix, at most 100 objects, 100 MB per object and 10 GB total.',
                        'No payload was read, restored or copied. No migration was created. Listing and metadata API request charges may apply.',
                        'Pause/resume uses a local checkpoint. Keep the state directory private; it contains object keys. Large-scale S3 Inventory import remains separate work.']}
