#!/usr/bin/env python3
"""Build immutable training inputs from long PV history plus a refreshed tail."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import hashlib
from urllib.parse import urlencode
from urllib.request import Request

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from m3.worker.domain import pv_training, pv_operational


def load_script(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),Path(__file__).with_name(name))
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


archive=load_script('fetch-open-meteo-history.py')
history=archive.history


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge_pv(old, delta, since, until):
    since,until=pv_operational.timestamp(since),pv_operational.timestamp(until)
    if since > until:
        raise ValueError('invalid PV refresh bounds')
    for group in (old,delta):
        if group: pv_training.prepare_pv(group)
    if any(not since <= pv_operational.timestamp(r['timestamp']) <= until for r in delta):
        raise ValueError('PV delta outside fixed query bounds')
    if any(pv_operational.timestamp(r['timestamp']) > until for r in old):
        raise ValueError('previous PV snapshot is ahead of refresh cutoff')
    kept=[r for r in old if pv_operational.timestamp(r['timestamp']) < since]
    return sorted([*kept,*delta],key=lambda r:pv_operational.timestamp(r['timestamp']))


class PVSource:
    def __init__(self,key,page_size=2000):
        self.key=key
        self.page_size=page_size
        self.client=history.deployment.base.NocoBaseSchemaClient(history.deployment.schema.BASE_URL,key,
            timeout_seconds=30,max_response_bytes=4*1024*1024)

    def request_page(self,since,until,page):
        query={'filter':json.dumps({'es_sn':{'$eq':'ES02'},'timestamp':{'$gte':since,'$lte':until}}),
            'fields':'timestamp,es_sn,ac_solar_power','sort':'timestamp','page':page,'pageSize':self.page_size}
        return self.client._request(Request(history.deployment.schema.BASE_URL+'/api/t_es_data:list?'+urlencode(query),
            headers={'Authorization':'Bearer '+self.key,'Accept':'application/json'}))

    def fetch(self,since,until):
        since,until=pv_operational.timestamp(since).isoformat(),pv_operational.timestamp(until).isoformat()
        rows=[];count=None
        for page in range(1,102):
            response=self.request_page(since,until,page)
            meta=response.get('meta',{}); chunk=response.get('data')
            size=meta.get('count')
            if (not isinstance(chunk,list) or len(chunk)>self.page_size or meta.get('page')!=page
                    or meta.get('pageSize',self.page_size)!=self.page_size):
                raise ValueError('PV query count/page changed or invalid')
            if size is not None:
                if type(size) is not int or not 0<=size<=200000 or count is not None and count!=size:
                    raise ValueError('PV query count/page changed or invalid')
                count=size
                if len(chunk)!=min(self.page_size,count-(page-1)*self.page_size):
                    raise ValueError('PV query returned an incomplete page')
            elif count is not None:
                raise ValueError('PV query pagination mode changed')
            rows.extend(chunk)
            if 'hasNext' in meta:
                more=meta['hasNext']
                if type(more) is not bool or more and len(chunk)!=self.page_size:
                    raise ValueError('PV query hasNext/page length invalid')
                if count is not None and more!=(len(rows)<count):
                    raise ValueError('PV query hasNext/count disagree')
            elif count is not None:
                more=len(rows)<count
            else:
                raise ValueError('PV query lacks pagination completion marker')
            if not more:
                if not rows:
                    raise ValueError('PV refresh returned no observations')
                merge_pv([],rows,since,until)
                return [{k:r[k] for k in ('timestamp','es_sn','ac_solar_power')} for r in rows]
        raise ValueError('PV query exceeded page bound')


def save_source(directory,rows,weather,provenance):
    directory.mkdir(parents=True,exist_ok=False)
    (directory/'pv_snapshot.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n' for r in rows))
    history.save_report(directory/'historical_weather_rows.json',weather)
    history.save_report(directory/'manifest.json',{'format':'pv-training-source-v1','es_sn':'ES02',
        'provenance':provenance,'files_sha256':{p.name:sha(p) for p in directory.iterdir() if p.is_file()}})


def load_source(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    if manifest.get('format')!='pv-training-source-v1' or manifest.get('es_sn')!='ES02':
        raise ValueError('invalid PV training source manifest')
    for name in ('pv_snapshot.jsonl','historical_weather_rows.json'):
        if sha(directory/name)!=manifest['files_sha256'][name]:
            raise ValueError('training source SHA mismatch: '+name)
    rows=[json.loads(line) for line in (directory/'pv_snapshot.jsonl').read_text().splitlines() if line.strip()]
    weather=json.loads((directory/'historical_weather_rows.json').read_text())
    observed=pv_operational.timestamp(manifest['provenance']['observed_at'])
    for row in weather:
        if row['es_sn']!='ES02' or row['source_kind']!='historical_reanalysis':
            raise ValueError('training weather station/source mismatch')
        if pv_operational.timestamp(row['weather_time'])>observed:
            raise ValueError('training weather contains future hours')
        if row['fetched_at'] is not None and pv_operational.timestamp(row['fetched_at'])>observed:
            raise ValueError('training weather unavailable at source cutoff')
    if any(pv_operational.timestamp(row['timestamp'])>observed for row in rows):
        raise ValueError('PV snapshot contains future records')
    frame=pd.DataFrame(weather)
    frame.index=pd.DatetimeIndex([pv_operational.timestamp(t) for t in frame.weather_time])
    aligned=pv_training.align_weather(pv_training.prepare_pv(rows),frame)
    train=aligned.loc[aligned.eligible]
    used=sorted(set(train.weather_hour_start)|set(train.weather_radiation_time))
    by_time={pv_operational.timestamp(r['weather_time']):r for r in weather}
    lineage=[{k:by_time[t][k] for k in ('weather_time','source_batch_id','content_hash')} for t in used]
    return {'training':train,'raw':rows,'weather':weather,'manifest':manifest,
        'weather_batches':sorted({r['source_batch_id'] for r in weather}),'lineage':lineage,
        'sources':{'pv':directory/'pv_snapshot.jsonl','historical_weather':directory/'historical_weather_rows.json',
                   'training_source_manifest':directory/'manifest.json'}}


def original_inputs():
    directory=ROOT/'outputs/m3/backtests/pv_backtest_es02_20260909'
    manifest=json.loads((directory/'manifest.json').read_text())
    pv=directory/'inputs/pv_snapshot.jsonl'
    if sha(pv)!=manifest['files_sha256']['inputs/pv_snapshot.jsonl']:
        raise ValueError('original PV snapshot checksum mismatch')
    csv=ROOT/'m3/data/历史天气数据.csv'
    if sha(csv)!=manifest['weather_sha256']:
        raise ValueError('original historical CSV checksum mismatch')
    weather,summary=history.prepare(csv)
    if summary['source_batch_id']!=manifest['weather_batch_id']:
        raise ValueError('original historical weather batch mismatch')
    return [json.loads(line) for line in pv.read_text().splitlines() if line.strip()],weather


def refresh(archive_directory,output,previous=None, *, require_import=True):
    if output.exists():
        raise ValueError('training output already exists')
    if previous:
        prior=load_source(previous);old=prior['raw']
        base_weather=prior['weather']
    else:
        old,base_weather=original_inputs()
    raw=(archive_directory/'response.json').read_bytes()
    envelope=archive.shared.load_json((archive_directory/'envelope.json').read_bytes())
    recent,summary=archive.prepare_snapshot(envelope,raw)
    if require_import:
        verified=json.loads((archive_directory/'import_verification.json').read_text())
        if verified['status']!='completed' or verified['source_batch_id']!=summary['source_batch_id']:
            raise ValueError('archive batch lacks successful import verification')
    # Reanalysis may arrive late. Keep previously valid hours when the new
    # response has a gap, while replacing revised valid hours by timestamp.
    weather_by_time={pv_operational.timestamp(r['weather_time']):r for r in base_weather}
    for row in recent:
        at=pv_operational.timestamp(row['weather_time'])
        if row['quality_status']=='valid' or at not in weather_by_time:
            weather_by_time[at]=row
    until=pd.Timestamp.now(tz='Asia/Shanghai').floor('ms')
    last=max(pv_operational.timestamp(r['timestamp']) for r in old)
    since=last-pd.Timedelta(hours=48)
    delta=PVSource(history.credential()).fetch(since,until)
    merged=merge_pv(old,delta,since,until)
    provenance={'observed_at':pd.Timestamp.now(tz='Asia/Shanghai').ceil('ms').isoformat(),
        'pv_query_since':since.isoformat(),'pv_query_until':until.isoformat(),'delta_rows':len(delta),
        'history_policy':'preserve_earlier_snapshot_replace_from_previous_latest_minus_48h',
        'archive_directory':str(archive_directory.resolve()),'archive_response_sha256':sha(archive_directory/'response.json'),
        'previous_source':str(previous.resolve()) if previous else 'first_verified_pv_snapshot',
        'archive_publication':'verified' if require_import else 'local_snapshot_only'}
    save_source(output,merged,[weather_by_time[t] for t in sorted(weather_by_time)],provenance)
    data=load_source(output)
    if data['training'].empty or (previous and data['training'].index.max()<prior['training'].index.max()):
        raise ValueError('refreshed training is empty or regresses')
    result={'status':'completed','training_source':str(output.resolve()),'pv_rows':len(merged),'delta_rows':len(delta),
        'training_rows':len(data['training']),'training_end':(data['training'].index.max()+pd.Timedelta(minutes=15)).isoformat(),
        'weather_batches':data['weather_batches']}
    history.save_report(output/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive-snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--previous-source',type=Path)
    args=p.parse_args()
    try: refresh(args.archive_snapshot,args.output,args.previous_source)
    except Exception as error:
        print(json.dumps({'status':'failed','error':str(error) if isinstance(error,ValueError) else type(error).__name__}))
        sys.exit(1)
