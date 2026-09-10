#!/usr/bin/env python3
"""Run one ES02 hourly refresh; the Codex local automation invokes this CLI."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pandas as pd
from m3.worker.services.pv_hourly import atomic_json,execute_hour


def load(name):
    spec=importlib.util.spec_from_file_location('hourly_'+name.replace('-','_'),Path(__file__).with_name(name))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


archive=load('fetch-open-meteo-history.py')
weather=load('fetch-open-meteo-weather.py')
refresh=load('refresh-pv-training.py')
generator=load('generate-pv-forecast.py')
publisher=load('publish-pv-forecast.py')


def perform(directory,state,seed_archive=None,seed_training=None):
    progress_path=directory/'progress.json'
    progress=json.loads(progress_path.read_text()) if progress_path.exists() else {}

    def record(**values):
        progress.update({k:str(v.resolve()) if isinstance(v,Path) else v for k,v in values.items()})
        atomic_json(progress_path,progress)

    def finish(bundle_path):
        publisher.execute(bundle_path,bundle_path/'publication_verification.json')
        result=json.loads((bundle_path/'publication_verification.json').read_text())
        return {**result,'forecast_bundle':str(bundle_path.resolve()),
            'archive_snapshot':progress['archive_snapshot'],'training_source':progress['training_source'],
            'weather_snapshot':progress['weather_snapshot']}

    # Recover uncertain POSTs from the same immutable bundle before taking any new snapshot.
    if progress.get('forecast_bundle'):
        candidate=Path(progress['forecast_bundle'])
        run,points=publisher.load_bundle(candidate)
        repository=publisher.ForecastRepository(publisher.history.credential())
        actual=repository.read_run(run['run_id'])
        if actual is not None:
            publisher.pv_publication.compare_run(actual,run)
        if actual is not None and actual['status']=='completed' or pd.Timestamp.now(tz='Asia/Shanghai')<pd.Timestamp(run['forecast_start']):
            return finish(candidate)
        record(superseded_bundle=str(candidate),forecast_bundle=None)

    archive_path=Path(progress.get('archive_snapshot') or state.get('archive_snapshot') or seed_archive or directory/'archive')
    today=pd.Timestamp.now(tz='Asia/Shanghai').date()
    if (archive_path/'envelope.json').exists():
        old=archive.shared.load_json((archive_path/'envelope.json').read_bytes())
        if archive.shared.timestamp(old['request_started_at'],explicit=True).date()!=today:
            archive_path=directory/'archive'
    if not (archive_path/'envelope.json').exists():
        historical,summary=archive.fetch(archive_path)
    else:
        historical,summary=archive.prepare_snapshot(archive.shared.load_json((archive_path/'envelope.json').read_bytes()),
            (archive_path/'response.json').read_bytes())
    proof=archive_path/'import_verification.json'
    if not proof.exists() or json.loads(proof.read_text()).get('status')!='completed':
        archive.history.execute(historical,summary,proof)
    elif json.loads(proof.read_text()).get('source_batch_id')!=summary['source_batch_id']:
        raise ValueError('archive import proof mismatch')
    record(archive_snapshot=archive_path)

    training_path=Path(progress.get('training_source') or (seed_training if not state else directory/'training')) if (progress.get('training_source') or seed_training or state) else directory/'training'
    if (training_path/'manifest.json').exists():
        data=refresh.load_source(training_path)
        if summary['source_batch_id'] not in data['weather_batches']:
            raise ValueError('cached training source does not reference current archive batch')
    else:
        previous=Path(state['training_source']) if state.get('training_source') else None
        refresh.refresh(archive_path,training_path,previous)
    record(training_source=training_path)

    weather_path=Path(progress.get('weather_snapshot') or directory/'weather')
    if not (weather_path/'envelope.json').exists():
        envelope,raw=weather.fetch(weather_path)
    else:
        envelope=weather.load_json((weather_path/'envelope.json').read_bytes());raw=(weather_path/'response.json').read_bytes()
    rows,weather_summary=weather.prepare_snapshot(envelope,raw)
    if not (weather_path/'weather_rows.json').exists():
        weather.history.save_report(weather_path/'weather_rows.json',rows)
        weather.history.save_report(weather_path/'summary.json',weather_summary)
    proof=weather_path/'import_verification.json'
    if not proof.exists() or json.loads(proof.read_text()).get('status')!='completed':
        weather.history.execute(rows,weather_summary,proof)
    record(weather_snapshot=weather_path)

    bundle_path=directory/('forecast-'+uuid.uuid4().hex[:12])
    generator.generate(None,weather_path,bundle_path,training_source=training_path)
    record(forecast_bundle=bundle_path)
    return finish(bundle_path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,default=ROOT/'run/pv-hourly')
    p.add_argument('--credential-file',type=Path,default=Path.home()/'.config/vifa/pv-nocobase-api.token')
    p.add_argument('--seed-archive',type=Path);p.add_argument('--seed-training',type=Path)
    args=p.parse_args()
    if os.environ.get('PV_NOCOBASE_IMPORT_API_KEY','').strip():
        raise ValueError('hourly task uses a private credential file only')
    os.environ['PV_NOCOBASE_IMPORT_API_KEY_FILE']=str(args.credential_file.resolve())
    # Keep success output compact; detailed phase reports live next to immutable inputs.
    captured=io.StringIO()
    with contextlib.redirect_stdout(captured):
        result=execute_hour(args.state_dir,pd.Timestamp.now(tz='Asia/Shanghai'),
            lambda directory,state:perform(directory,state,args.seed_archive,args.seed_training))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except BlockingIOError:
        print(json.dumps({'status':'already_running'}))
    except Exception as error:
        print(json.dumps({'status':'incomplete','error_type':type(error).__name__,
            'safe_error':str(error) if isinstance(error,(ValueError,archive.history.deployment.base.DeploymentError)) else 'hourly update failed',
            'automatic_post_retry':False},ensure_ascii=False))
        sys.exit(1)
