"""Root-owned host state, exclusive locks, bounded input and atomic checkpoints."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4


class InstallError(ValueError):
    pass


def read(path, limit=131072):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise InstallError('Input is missing, unsafe or too large.')
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise InstallError('Input exceeds its size limit.')
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def protected(path, directory=False):
    path = Path(path)
    if not path.is_absolute():
        raise InstallError('An absolute protected path is required.')
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise InstallError('Host state must be root-owned without symlinks or writable parents.')
    if directory and not path.is_dir():
        raise InstallError('A protected directory is required.')
    return path


def root_only():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise InstallError('Run this host operation as root on Linux.')
    os.umask(0o077)


def mkdir(path):
    path = Path(path)
    if not path.exists():
        protected(path.parent, directory=True)
        path.mkdir(mode=0o700)
    return protected(path, directory=True)


def sync(path):
    if sys.platform == 'linux':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise InstallError('Refusing a symbolic link.')
    temp = path.with_name('.' + path.name + '-' + uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        sync(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def exact(path, content, mode=0o600):
    path = Path(path)
    if path.exists() or path.is_symlink():
        protected(path)
        if read(path, max(len(content), 131072)) != content:
            raise InstallError('Existing installation file differs; automatic overwrite is refused.')
        return
    atomic(path, content, mode)


@contextmanager
def lock(directory, name='operation.lock'):
    import fcntl
    path = Path(directory) / name
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('Another installation or enrollment operation is active.') from None
        yield
    finally:
        os.close(descriptor)
