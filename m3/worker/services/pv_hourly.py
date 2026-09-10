"""Local hour-level exclusion and atomic success records for PV refresh."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile

from m3.worker.domain.pv_operational import timestamp


def atomic_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False,allow_nan=False,indent=2)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def execute_hour(root, at, operation):
    root.mkdir(parents=True,exist_ok=True)
    hour=timestamp(at).floor('h')
    with (root/'refresh.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
        if state.get('last_success_hour') and timestamp(state['last_success_hour'])>=hour:
            return {'status':'already_completed','hour':hour.isoformat(),'run_id':state.get('run_id')}
        directory=root/hour.strftime('%Y%m%d-%H')
        directory.mkdir(exist_ok=True)
        try:
            result=operation(directory,state)
            if result.get('status')!='completed':
                raise ValueError('hourly operation did not finish publication')
            final={**result,'last_success_hour':hour.isoformat()}
            atomic_json(directory/'result.json',final)
            atomic_json(root/'state.json',final)
            return final
        except Exception as error:
            atomic_json(directory/'failure.json',{'status':'incomplete','hour':hour.isoformat(),'error_type':type(error).__name__})
            raise
