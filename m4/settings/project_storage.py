"""Bind persistence to one project; never relabel another project's evidence."""
import fcntl
import json
from pathlib import Path
from shared.project import get_project


def bind_database(connection):
    """Called before reading/creating settings tables, serialized in SQLite."""
    project_id = get_project().id
    connection.execute('BEGIN IMMEDIATE')
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        bound = (connection.execute('SELECT project_id FROM m4_project_identity WHERE singleton=1').fetchone()
                 if 'm4_project_identity' in tables else None)
        if bound is None and project_id != 'vifa':
            for table in ('m4_station_settings', 'm4_selection_policies'):
                # Table names are fixed internal identifiers, never user input.
                if table in tables and connection.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
                    raise ValueError('已有未绑定的历史数据库；新项目必须使用独立存储')
        connection.execute('CREATE TABLE IF NOT EXISTS m4_project_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), project_id TEXT NOT NULL)')
        row = connection.execute('SELECT project_id FROM m4_project_identity WHERE singleton=1').fetchone()
        if row is not None and row[0] != project_id:
            raise ValueError('数据库属于其他项目，拒绝读取或写入')
        if row is None:
            connection.execute('INSERT INTO m4_project_identity VALUES (1, ?)', (project_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def bind_root(root):
    """Protect daily/rolling and solver evidence roots, including reused IDs.

    Legacy unmarked evidence is VIFA-only. Project fingerprints are deliberately
    not the root identity: configuration updates must not erase project history.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError('不支持符号链接历史目录')
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / '.project.lock'
    if lock_path.is_symlink():
        raise ValueError('invalid project lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = root / '.project.json'
        if marker.is_symlink():
            raise ValueError('invalid project identity')
        expected = {'schema_version': 1, 'project_id': get_project().id}
        if marker.exists():
            if json.loads(marker.read_text()) != expected:
                raise ValueError('历史目录属于其他项目，拒绝读取或写入')
            return
        existing = [p for p in root.iterdir() if p.name != '.project.lock']
        if existing and get_project().id != 'vifa':
            raise ValueError('已有未绑定历史；新项目必须使用独立历史目录')
        # Publish under the project lock so concurrent initializers cannot race.
        with marker.open('x') as output:
            json.dump(expected, output)
