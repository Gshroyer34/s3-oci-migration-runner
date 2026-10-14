"""Release integrity checks; all inputs are generated in disposable directories."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_handoff import main, scan
from check_documentation import check


def test_checksum_tampering_fails(tmp_path):
    payload = b'reviewed release\n'
    (tmp_path / 'README.md').write_bytes(payload)
    (tmp_path / 'HANDOFF_MANIFEST.json').write_text(json.dumps({
        'format': 'migration-github-handoff/v1',
        'files': {'README.md': hashlib.sha256(payload).hexdigest()}}))
    main(tmp_path)
    (tmp_path / 'README.md').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed distribution files'):
        main(tmp_path)


def test_private_key_material_rejected():
    payload = b'-----BEGIN ' + b'PRIVATE KEY-----\n' + b'A' * 50
    with pytest.raises(ValueError, match='private key material'):
        scan('unexpected.txt', payload)


def test_nested_archive_secret_path_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('.aws/credentials', b'synthetic')
    with pytest.raises(ValueError, match='Forbidden distribution path'):
        scan('release.zip', buffer.getvalue())


def test_tar_symlink_rejected():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        member = tarfile.TarInfo('link')
        member.type = tarfile.SYMTYPE
        member.linkname = '/outside'
        archive.addfile(member)
    with pytest.raises(ValueError, match='Unsupported'):
        scan('release.tar', buffer.getvalue())


def test_document_links_checked(tmp_path):
    (tmp_path / 'README.md').write_text('[missing](docs/missing.md)')
    with pytest.raises(ValueError, match='Broken'):
        check(tmp_path)
