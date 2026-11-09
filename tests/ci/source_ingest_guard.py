"""Verbatim OFFLINE_GUARD from LU 5aba2adb:tests/test_source_ingest_entrypoints.py.

The product import guard is shared as test input, without copying its test module.
"""

OFFLINE_GUARD = """
import os
import sys
from pathlib import Path

private_tmp = Path(os.environ['TMPDIR']).resolve()

def private_path(path, dir_fd=None):
    # Resolve symlinks and '..'. On Linux, resolve directory descriptors too:
    # tempfile cleanup uses unlink/rmdir relative to an open directory fd.
    # Missing descriptor telemetry fails closed on other platforms.
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    target = Path(os.fsdecode(path))
    if dir_fd not in (None, -1):
        try:
            directory = Path('/proc/self/fd/' + str(dir_fd)).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if not directory.is_relative_to(private_tmp):
            return False
        target = directory / target
    return target.resolve().is_relative_to(private_tmp)

def offline(event, args):
    if event in {'socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.sendmsg'}:
        raise RuntimeError('IMPORT_SMOKE_BLOCKED_NETWORK: ' + event)
    # Shared root resolvers use this local Git plumbing command at import time.
    # Permit it without permitting shell commands or a network-capable Git verb.
    if event == 'subprocess.Popen' and Path(args[0]).name == 'git':
        argv = args[1]
        if isinstance(argv, (list, tuple)):
            command = list(argv[1:])
            if len(command) >= 2 and command[0] == '-C':
                command = command[2:]
            if command and command[0] == 'rev-parse':
                return
    if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'}:
        raise RuntimeError('IMPORT_SMOKE_BLOCKED_SUBPROCESS: ' + event)
    # Only the fresh private probe directory is writable. This also permits
    # tempfile.gettempdir() to validate it without hiding subsequent imports.
    if event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        # The open audit event omits dir_fd: only absolute paths are safe to
        # authorize, since a relative name may target another directory fd.
        if not private_path(args[0]) or not os.path.isabs(args[0]):
            raise RuntimeError('IMPORT_SMOKE_BLOCKED_WRITE: ' + event)
    if event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.link',
                 'os.symlink', 'os.truncate', 'os.chmod', 'os.chown', 'os.utime'}:
        path_fds = {
            'os.mkdir': ((0, 2),), 'os.remove': ((0, 1),), 'os.rename': ((0, 2), (1, 3)),
            'os.rmdir': ((0, 1),), 'os.link': ((0, 2), (1, 3)), 'os.symlink': ((0, None), (1, 2)),
            'os.truncate': ((0, None),), 'os.chmod': ((0, 2),), 'os.chown': ((0, 3),),
            'os.utime': ((0, 3),),
        }[event]
        if not all(private_path(args[p], args[fd] if fd is not None else None) for p, fd in path_fds):
            raise RuntimeError('IMPORT_SMOKE_BLOCKED_WRITE: ' + event)
    if event == 'sqlite3.connect' and args[0] != ':memory:' and 'mode=ro' not in str(args[0]):
        if not private_path(args[0]):
            raise RuntimeError('IMPORT_SMOKE_BLOCKED_WRITE: ' + event)

sys.addaudithook(offline)
"""
