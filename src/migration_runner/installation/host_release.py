"""Small, checksummed host configuration release; runtime images travel separately."""
import io
import json
from pathlib import Path
import zipfile

from .enrollment import trust
from .host_files import InstallError, encoded, sha
from .host_plan import HostPlan, SCHEMA
from .package import archive

RELEASE_FILES = {'plan.json', 'tls-ca.crt', 'roles-ca.crt', 'schema.sql', 'database-roles.sql'}


def build_release(plan, tls_ca, roles_ca, source_root):
    from alembic import command
    from alembic.config import Config
    trust(plan, tls_ca, roles_ca)
    output = io.StringIO()
    config = Config(str(source_root / 'alembic.ini'), output_buffer=output)
    config.set_main_option('script_location', str(source_root / 'alembic'))
    command.upgrade(config, SCHEMA, sql=True)
    files = {'plan.json': plan.canonical(), 'tls-ca.crt': tls_ca, 'roles-ca.crt': roles_ca,
             'schema.sql': output.getvalue().encode(),
             'database-roles.sql': (source_root / 'deploy/runtime/database-roles.sql').read_bytes()}
    files['manifest.json'] = encoded({'format': 'migration-host-release/v1', 'plan_revision': plan.revision,
        'ledger_revision': SCHEMA, 'files': {name: sha(data) for name, data in files.items()}})
    return archive(files)


def verify_release(content, expected_sha):
    # The out-of-band digest authenticates the reviewed archive, including SQL.
    if len(content) > 2_000_000 or sha(content) != expected_sha:
        raise InstallError('Release checksum differs from the independently reviewed value.')
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != RELEASE_FILES | {'manifest.json'}:
            raise InstallError('Unexpected or duplicate release files.')
        if any(i.file_size > 500_000 or i.flag_bits & 1 for i in bundle.infolist()):
            raise InstallError('Release member exceeds the supported envelope.')
        files = {name: bundle.read(name) for name in names}
    manifest = json.loads(files.pop('manifest.json'))
    if (set(manifest) != {'format', 'plan_revision', 'ledger_revision', 'files'}
            or manifest['format'] != 'migration-host-release/v1' or manifest['ledger_revision'] != SCHEMA
            or manifest['files'] != {name: sha(data) for name, data in files.items()}):
        raise InstallError('Release manifest verification failed.')
    plan = HostPlan.model_validate_json(files['plan.json'])
    if manifest['plan_revision'] != plan.revision:
        raise InstallError('Release plan revision differs.')
    trust(plan, files['tls-ca.crt'], files['roles-ca.crt'])
    return plan, files
