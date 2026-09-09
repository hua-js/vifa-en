"""Load the server-only secret, prepare the volume, and start one M4 worker."""
import os
import errno
import fcntl
from contextlib import contextmanager
from pathlib import Path
import socket
import stat
import sys


RUNTIME_UID = 10001
RUNTIME_GID = 10001
DATA_ROOT = Path('/data')
RESULTS_ROOT = DATA_ROOT / 'solver-decisions'
SECRET_LIMIT_BYTES = 65536
SOCKET_ROOT = Path('/run/vifa-m4')
SOCKET_PATH = SOCKET_ROOT / 'api.sock'


def drop_privileges():
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(RUNTIME_GID)
        os.setuid(RUNTIME_UID)
    if os.geteuid() != RUNTIME_UID or os.getegid() != RUNTIME_GID:
        raise RuntimeError('M4 must run as UID/GID 10001')
    os.umask(0o077)


def read_token():
    secret_path = Path(os.environ.get('M4_NOCOBASE_TOKEN_FILE', '/run/secrets/nocobase_token'))
    with secret_path.open('rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('secret is not a regular file')
        raw = source.read(SECRET_LIMIT_BYTES + 1)
    if len(raw) > SECRET_LIMIT_BYTES:
        raise ValueError('secret exceeds size limit')
    token = raw.decode('utf-8').strip()
    if not token or any(character.isspace() for character in token) or '\x00' in token:
        raise ValueError('secret is empty or malformed')
    return token


def prepare_data():
    # Only these two volume directories are initialized. Existing databases and
    # historical subdirectories are never recursively chowned or rewritten.
    for directory in (DATA_ROOT, RESULTS_ROOT):
        if directory.is_symlink():
            raise ValueError('data directory cannot be a symlink')
        directory.mkdir(parents=False, exist_ok=True)
        if not directory.is_dir():
            raise ValueError('data path is not a directory')
        if os.geteuid() == 0:
            os.chown(directory, RUNTIME_UID, RUNTIME_GID)


def assert_data_access():
    for directory in (DATA_ROOT, RESULTS_ROOT):
        if not os.access(directory, os.W_OK | os.X_OK):
            raise PermissionError('data directory is not writable')
    database = Path(os.environ.get('M4_SETTINGS_DB', str(DATA_ROOT / 'settings.sqlite3')))
    if database.is_symlink() or database.exists() and (
            not database.is_file() or not os.access(database, os.R_OK | os.W_OK)):
        raise PermissionError('existing database is not accessible')


def prepare_socket_directory():
    if SOCKET_ROOT.is_symlink():
        raise ValueError('socket directory cannot be a symlink')
    SOCKET_ROOT.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(SOCKET_ROOT, RUNTIME_UID, RUNTIME_GID)


@contextmanager
def socket_listener():
    # Node-RED may connect as group 10001, but cannot replace the socket file.
    os.chmod(SOCKET_ROOT, 0o2750)
    lock_fd = os.open(SOCKET_ROOT / 'server.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    listener = None
    bound_inode = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(SOCKET_PATH):
            if not stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
                raise ValueError('socket path occupied by another file')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(SOCKET_PATH))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise ValueError('socket already has an active listener')
            SOCKET_PATH.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(SOCKET_PATH))
        bound_inode = SOCKET_PATH.lstat().st_ino
        os.chmod(SOCKET_PATH, 0o660)
        listener.listen(2048)
        yield listener
    finally:
        if listener is not None:
            listener.close()
        try:
            if bound_inode is not None and SOCKET_PATH.lstat().st_ino == bound_inode:
                SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        os.close(lock_fd)


def main():
    try:
        # Never print the token, a secret-bearing environment, or exception text.
        token = read_token()
    except (OSError, UnicodeError, ValueError):
        print('M4 startup failed: provide a readable, non-empty NocoBase secret file.', file=sys.stderr)
        return 1
    try:
        prepare_data()
        prepare_socket_directory()
        drop_privileges()
        assert_data_access()
    except (OSError, ValueError, RuntimeError):
        print('M4 startup failed: data volume must be writable by UID/GID 10001; check migration ownership.', file=sys.stderr)
        return 1
    os.environ['M4_NOCOBASE_TOKEN'] = token
    sys.path.insert(0, '/app')
    import uvicorn
    try:
        with socket_listener() as listener:
            uvicorn.run('m4_settings.api:create_app', factory=True,
                        fd=listener.fileno(), workers=1, proxy_headers=False,
                        access_log=False)
    except (OSError, ValueError):
        print('M4 startup failed: check the dedicated socket directory and ensure only one M4 instance uses it.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
