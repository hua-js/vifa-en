"""Load the server-only secret, prepare the volume, and start one M4 worker."""
import os
from pathlib import Path
import stat
import sys


RUNTIME_UID = 10001
RUNTIME_GID = 10001
DATA_ROOT = Path('/data')
RESULTS_ROOT = DATA_ROOT / 'solver-decisions'
SECRET_LIMIT_BYTES = 65536


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


def main():
    try:
        # Never print the token, a secret-bearing environment, or exception text.
        token = read_token()
    except (OSError, UnicodeError, ValueError):
        print('M4 startup failed: provide a readable, non-empty NocoBase secret file.', file=sys.stderr)
        return 1
    try:
        prepare_data()
        drop_privileges()
        assert_data_access()
    except (OSError, ValueError, RuntimeError):
        print('M4 startup failed: data volume must be writable by UID/GID 10001; check migration ownership.', file=sys.stderr)
        return 1
    os.environ['M4_NOCOBASE_TOKEN'] = token
    os.execv(sys.executable, [
        sys.executable, '-m', 'uvicorn', 'm4_settings.api:create_app',
        '--factory', '--host', '0.0.0.0', '--port', '8844', '--workers', '1',
        '--no-proxy-headers',
    ])


if __name__ == '__main__':
    raise SystemExit(main())
