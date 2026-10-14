"""Produce a reproducible allowlisted source handoff; excludes identities and secrets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile


def build(root):
    names = ['pyproject.toml', 'requirements.lock', 'requirements-cloud.lock', 'requirements-build.lock',
             'alembic.ini', 'Containerfile', 'Containerfile.cloud', '.containerignore',
             'deploy/install-portable.sh', 'deploy/fetch_helper.py', 'deploy/runtime/database-roles.sql',
             'docs/PORTABLE_INSTALLER.md', 'docs/S3_TAG_METADATA_HANDOFF.md',
             'docs/PROPERTY_MAPPING_AND_MANIFEST.md', 'docs/benchmarks/portable-installer-20261014.json',
             'docs/PORTABLE_UI_AND_RENEWAL.md', 'docs/benchmarks/portable-ui-renewal-20261014.json']
    # Ship the reconciled operator guides with this handoff's node installer.
    # The complete repository remains the developer/test distribution.
    names += [p.relative_to(root).as_posix() for p in (root / 'docs').glob('*.md')]
    names += ['DISTRIBUTION_NOTICE.md', 'examples/README.md', 'packages/README.md',
              'scripts/generate_pilot_fixture.py']
    names += [p.relative_to(root).as_posix() for p in (root / 'examples').glob('*.example.json')]
    for directory in ('src', 'alembic'):
        for path in (root / directory).rglob('*'):
            if (path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in path.relative_to(root).parts[:-1])
                    and (path.suffix in {'.py', '.html', '.js', '.css', '.md', '.json', '.tf', '.yaml'}
                         or path.name == '.terraform.lock.hcl' or path.name.endswith('.tftest.hcl'))):
                names.append(path.relative_to(root).as_posix())
    files = {}
    for name in sorted(set(names)):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Required source artifact is missing or is a symlink.')
        files[name] = path.read_bytes()
    # Cloud image recipe references legacy lab helper sources. Include their
    # source only; no lab inputs, keys, certificates, state or generated bundles.
    for name in ('scripts/lab_pki.py', 'scripts/assemble_runtime_secret.py', 'scripts/enroll_cloud_worker.py'):
        files[name] = (root / name).read_bytes()
    manifest = {'format': 'migration-installer-source/v1', 'release': 'portable-installer-preview-2',
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                'includes_credentials': False, 'fresh_tenancy_acceptance': 'not_performed'}
    files['SOURCE_MANIFEST.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    output = io.BytesIO()
    # Uncompressed tar avoids platform-dependent gzip timestamps and is small.
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo('migration-installer/' + name)
            info.size = len(data); info.mtime = 0; info.uid = info.gid = 0
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            bundle.addfile(info, io.BytesIO(data))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = build(Path(__file__).resolve().parents[1])
    with args.output.open('xb') as stream:
        stream.write(value)
    print(json.dumps({'artifact': str(args.output), 'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}))


if __name__ == '__main__':
    main()
