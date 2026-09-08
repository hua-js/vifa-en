"""Isolated browser-test host: temporary DB, mock upstream, real solver/API."""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timedelta
import socket
import tempfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import uvicorn

from test_m4_live_inputs import Client, Controls
from test_m4_realtime import configuration
from m4_settings.api import create_app
from m4_settings.candidates import CandidateService
from m4_settings.live_inputs import LiveInputService
from m4_settings.selection import LiveSelectionService, PolicyStore
from m4_settings.store import SettingsStore


def main():
    now=datetime.now(ZoneInfo('Asia/Shanghai')).replace(microsecond=0)
    start=(now+timedelta(minutes=15)).replace(minute=((now.minute//15+1)*15)%60,second=0)
    with tempfile.TemporaryDirectory(prefix='m4-selection-browser-') as directory:
        os.environ['M4_DECISION_RESULTS_DIR'] = str(Path(directory) / 'decision-results')
        path=Path(directory)/'settings.db';store=SettingsStore(path);policies=PolicyStore(path)
        clients={};controls={}
        for station in ['station-1','station-2']:
            store.save(station,configuration().parameters.model_copy(update={'max_input_age_seconds':1800}),expected_revision=0)
            clients[station]=Client(station_id=station,now=now,start=start)
            controls[station]=Controls(station)
        def fetch(config):
            return LiveInputService(clients[config.station_id],controls[config.station_id]).fetch(config,now=now)
        def control(station):return controls[station].fetch(station)
        candidates=CandidateService(store=store,fetch_inputs=fetch,read_controls=control,clock=lambda:now)
        selection=LiveSelectionService(store=store,policies=policies,candidates=candidates,fetch_inputs=fetch,clock=lambda:now)
        app=create_app(path,control_reader=SimpleNamespace(fetch=control),input_service=SimpleNamespace(fetch=fetch),
                       candidate_service=candidates,selection_service=selection)
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
        print(f'PORT={sock.getsockname()[1]} CLOCK={int(now.timestamp()*1000)}',flush=True)
        uvicorn.run(app,fd=sock.fileno(),log_level='warning')


if __name__=='__main__':main()
