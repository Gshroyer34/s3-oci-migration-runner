"""Linux exec shim: kill Rclone if its Python worker dies, without threaded preexec_fn."""
import ctypes
import os
import signal
import sys


def main():
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected_parent:
        raise SystemExit(125)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == '__main__':
    main()
