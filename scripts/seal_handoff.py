"""Seal a reviewed clean source tree and produce a local distribution ZIP only."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from verify_handoff import ROOT, distribution_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, required=True)
    args = parser.parse_args()
    output = args.zip.resolve()
    sidecar = Path(str(output) + '.sha256')
    if output.is_relative_to(ROOT) or output.exists() or sidecar.exists():
        raise ValueError('Choose a new ZIP path outside the repository.')
    # First scan before writing the manifest or archive. No ignored credentials allowed.
    distribution_files()
    packages = sorted(p for p in (ROOT / 'packages').iterdir() if p.suffix in {'.tar', '.whl'})
    (ROOT / 'packages/SHA256SUMS.txt').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in packages), encoding='utf-8', newline='\n')
    manifest = {'format': 'migration-github-handoff/v1', 'release': 'github-handoff-preview-2',
                'application_version': '0.5.0', 'date': '2026-10-14',
                'fresh_tenancy_acceptance': 'not_performed', 'files': distribution_files()}
    (ROOT / 'HANDOFF_MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted([*manifest['files'], 'HANDOFF_MANIFEST.json']):
            data = (ROOT / name).read_bytes()
            if name in manifest['files'] and hashlib.sha256(data).hexdigest() != manifest['files'][name]:
                raise ValueError('Source changed while packaging')
            info = zipfile.ZipInfo('migration-runner/' + name, date_time=(2026, 10, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with sidecar.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(digest + '  ' + output.name + '\n')
    print(json.dumps({'zip': str(output), 'sha256': digest, 'files': len(manifest['files']) + 1,
                      'bytes': output.stat().st_size, 'published': False}))


if __name__ == '__main__':
    main()
