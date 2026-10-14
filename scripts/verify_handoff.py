"""Verify a handoff inventory and scan for selected credential/file hazards offline.

Checksums detect changes, not publisher authenticity. This is not a security audit.
"""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {'.git', '.venv', '.pytest_cache', '__pycache__', 'build', 'dist'}
BLOCKED_PARTS = {'.secrets', '.aws', '.oci', '.terraform', 'node_modules'}
BLOCKED_NAMES = {'worker-token', 'operator-token', 'host-plan.json', 'nodes.json',
                 'reader-identity.json', 'features.json', 'installation-profile.json',
                 'deployment-profile.json', 'binding.json', 'credential-policy.json',
                 'runtime-image.tar', 'host-release.zip', 'reference.json'}
PATTERNS = {
    'private key material': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{24,}'),
    'AWS access key identifier': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
}


def safe_name(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name
            or any(part in BLOCKED_PARTS for part in path.parts)
            or path.name in BLOCKED_NAMES
            or path.name == '.env' or path.name.startswith('.env.')
            or path.suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.tfplan', '.tfvars'}
            or '.tfstate' in path.name or path.name.endswith('.tfvars.json')):
        raise ValueError('Forbidden distribution path: ' + name)


def scan(name, data, depth=0):
    safe_name(name)
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            raise ValueError(label + ' detected in ' + name)
    if depth > 3 or len(data) > 25_000_000:
        raise ValueError('Archive scan bound exceeded: ' + name)
    if name.endswith('.tar'):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            seen = set()
            total = 0
            for member in archive:
                if not member.isfile() or member.name in seen:
                    raise ValueError('Unsupported or duplicate TAR member')
                seen.add(member.name)
                total += member.size
                if total > 25_000_000:
                    raise ValueError('TAR scan bound exceeded')
                scan(member.name, archive.extractfile(member).read(), depth + 1)
    elif name.endswith(('.zip', '.whl')):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if sum(e.file_size for e in entries) > 25_000_000 or len({e.filename for e in entries}) != len(entries):
                raise ValueError('ZIP scan bound or duplicate member')
            for entry in entries:
                if entry.is_dir():
                    continue
                scan(entry.filename, archive.read(entry), depth + 1)


def distribution_files(root=ROOT):
    result = {}
    for file in sorted(root.rglob('*')):
        relative = file.relative_to(root)
        if any(part in IGNORED for part in relative.parts) or file.suffix == '.pyc':
            continue
        if file.is_symlink():
            raise ValueError('Symlink in handoff: ' + relative.as_posix())
        if not file.is_file() or relative.as_posix() == 'HANDOFF_MANIFEST.json':
            continue
        data = file.read_bytes()
        scan(relative.as_posix(), data)
        result[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    return result


def main(root=ROOT):
    manifest = json.loads((root / 'HANDOFF_MANIFEST.json').read_text(encoding='utf-8'))
    if manifest['format'] != 'migration-github-handoff/v1':
        raise ValueError('Unknown handoff format')
    actual = distribution_files(root)
    if actual != manifest['files']:
        changed = sorted(set(actual) ^ set(manifest['files']) | {
            key for key in actual.keys() & manifest['files'].keys() if actual[key] != manifest['files'][key]})
        raise ValueError('Missing, added or changed distribution files: ' + ', '.join(changed))
    print(json.dumps({'verified': True, 'files': len(actual), 'selected_secret_scan': 'passed',
                      'cloud_contacted': False, 'publisher_signature_checked': False}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
