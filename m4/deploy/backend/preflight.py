"""Offline runtime/storage check; no upstream requests and no optimization."""
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from entrypoint import DATA_ROOT, RESULTS_ROOT, assert_data_access, drop_privileges


def check_writable(directory):
    # Test real write permission using a temporary probe that is deleted on exit.
    with tempfile.TemporaryFile(prefix='.m4-preflight-', dir=directory) as probe:
        probe.write(b'm4-preflight\n')
        probe.flush()
        os.fsync(probe.fileno())


def latest_status(results_root, station_id):
    directory = results_root / station_id / '.web-jobs'
    if not directory.exists():
        return 'none'
    paths = list(directory.glob('*.json'))
    if not paths:
        return 'none'
    path = max(paths, key=lambda item: (item.stat().st_mtime_ns, item.name))
    with path.open('rb') as source:
        raw = source.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('job state exceeds size limit')
    job = json.loads(raw)
    if not isinstance(job, dict) or job.get('station_id') != station_id:
        raise ValueError('invalid job station')
    status = job.get('status')
    if status not in {'running', 'finished', 'failed', 'interrupted'}:
        raise ValueError('invalid job status')
    return status


def main():
    try:
        drop_privileges()
        # Direct invocation by docker compose exec does not otherwise include /app.
        sys.path.insert(0, '/app')
        for module in ('fastapi', 'httpx', 'highspy', 'numpy', 'pydantic', 'pyomo',
                       'scipy', 'uvicorn', 'm4_settings.api', 'm4_optimizer',
                       'm4_orchestrator', 'm4_selection'):
            importlib.import_module(module)
        from pyomo.contrib.appsi.solvers import Highs
        if not Highs().available():
            raise RuntimeError('HiGHS unavailable')
        ZoneInfo('Asia/Shanghai')
        print(f'Runtime imports OK; Python {sys.version.split()[0]}, Pyomo {version("pyomo")}, HiGHS {version("highspy")}.')
        assert_data_access()
        results_root = Path(os.environ.get('M4_DECISION_RESULTS_DIR', str(RESULTS_ROOT)))
        for directory in (DATA_ROOT, results_root):
            check_writable(directory)
        for station in ('station-1', 'station-2'):
            for directory in (results_root / station, results_root / station / '.web-jobs'):
                if directory.exists():
                    check_writable(directory)
        print('Data volume write checks OK (UID/GID 10001).')
        statuses = {station: latest_status(results_root, station)
                    for station in ('station-1', 'station-2')}
        for station, status in statuses.items():
            print(f'{station}: latest job status={status}')
        if 'running' in statuses.values():
            print('A job is marked running; confirm completion before stopping or upgrading.', file=sys.stderr)
            return 2
        return 0
    except Exception:
        print('M4 preflight failed: check installed dependencies and data volume access; no upstream request or solve was attempted.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
