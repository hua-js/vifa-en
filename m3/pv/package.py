#!/usr/bin/env python3
"""Create a credential-free production bundle containing the verified ES02 training source."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--training-source',type=Path,default=ROOT/'outputs/m3/training/pv_training_es02_20260909_refresh')
    args=parser.parse_args()
    if args.output.exists() or args.output.with_suffix('.tar.gz').exists():
        raise ValueError('package output already exists')
    spec=importlib.util.spec_from_file_location('package_pv_training',ROOT/'m3/scripts/refresh-pv-training.py')
    refresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(refresh)
    training=refresh.load_source(args.training_source)
    sources=[ROOT/'m3/__init__.py', *list((ROOT/'m3/worker').rglob('*.py'))]
    scripts=('run-pv-manual.py','fetch-open-meteo-weather.py','fetch-open-meteo-history.py',
        'import-weather-history.py','generate-pv-forecast.py','publish-pv-forecast.py','refresh-pv-training.py',
        'backtest-pv-history.py','create-pv-forecast-collections.py','create-custom-forecast-collections.py')
    sources.extend(ROOT/'m3/scripts'/name for name in scripts)
    sources.extend([ROOT/'m3/contracts/pv_forecast_schema.py',ROOT/'m3/contracts/nocobase_collections.json'])
    sources.extend(ROOT/'m3/pv'/name for name in ('Dockerfile','entrypoint.sh','pv_manual_production_flow.json'))
    sources.append(ROOT/'docs/m3/pv/README.md')
    files={str(path.relative_to(ROOT)):path.read_bytes() for path in sources}
    for name in ('manifest.json','pv_snapshot.jsonl','historical_weather_rows.json','summary.json'):
        files['pv-training/'+name]=(args.training_source/name).read_bytes()
    files['m3/pv/compose.yaml']=(ROOT/'m3/pv/compose.yaml').read_bytes()
    files['.dockerignore']=b'run\n*.token\n*.env\n**/__pycache__\n*.tar.gz\n'
    # Allow paths/env variable names; never package token files or plaintext JWTs/private keys.
    sensitive=re.compile(rb'eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    if any(sensitive.search(data) for data in files.values()):
        raise ValueError('credential-like content detected; package not created')
    args.output.mkdir(parents=True)
    for name,data in files.items():
        path=args.output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    manifest={'format':'pv-manual-production-v1','es_sn':'ES02','schedule_enabled':False,
        'training_rows':len(training['training']),
        'training_end':(training['training'].index.max()+refresh.pd.Timedelta(minutes=15)).isoformat(),
        'files_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}}
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    archive=args.output.with_suffix('.tar.gz')
    with tarfile.open(archive,'w:gz') as tar:
        tar.add(args.output,arcname='vifa-pv-manual',filter=lambda info: _private_metadata(info))
    print(json.dumps({'status':'packaged','directory':str(args.output.resolve()),'archive':str(archive.resolve()),
        'files':len(files)+1,'training_rows':manifest['training_rows'],'training_end':manifest['training_end'],
        'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},ensure_ascii=False,indent=2))


def _private_metadata(info):
    info.uid=0;info.gid=0;info.uname='';info.gname=''
    info.mode=0o755 if info.isdir() else 0o644
    return info


if __name__=='__main__':main()
