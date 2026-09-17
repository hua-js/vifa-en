"""Offline expanding-window PV refits against archived operational weather.

This is a retrospective sensitivity study: archive revisions and telemetry
arrival times were not available historically. No API or publication calls.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from m3.worker.domain import pv_operational
from m4.scripts.replay_pv_correction import replay


def refit(training, run):
    as_of = pv_operational.timestamp(run['as_of'])
    # Match daily refresh: archive requested only through yesterday's 23:00
    # anchor. Radiation is end-labelled; its last eligible interval ends then.
    cutoff = as_of.normalize()-pd.Timedelta(hours=1)
    train = training.loc[training.index+pd.Timedelta(minutes=15) <= cutoff]
    result = pv_operational.generate(train, run['source_manifest']['forecast_weather_rows'], as_of)
    if result['training'].index.max()+pd.Timedelta(minutes=15) > cutoff:
        raise ValueError('future label leaked into replay')
    return result, cutoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training-source', type=Path, required=True)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--evaluations', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; use a new evidence directory')
    spec = importlib.util.spec_from_file_location('refresh_pv_training', ROOT/'m3/scripts/refresh-pv-training.py')
    refresh = importlib.util.module_from_spec(spec); spec.loader.exec_module(refresh)
    training = refresh.load_source(args.training_source)['training']
    args.output.mkdir(parents=True)
    original, revised, fits = [], [], []
    for path in sorted(args.runs.glob('*.json')):
        run = json.loads(path.read_text())
        report = json.loads((args.evaluations/path.name).read_text())
        result, cutoff = refit(training, run)
        forecast = result['points']['forecast_kw']
        updated = deepcopy(report)
        for point in updated['pairs']:
            point['forecast_kw'] = float(forecast.loc[pv_operational.timestamp(point['target_time'])])
        original.append(report); revised.append(updated)
        fit = dict(run_id=run['run_id'], as_of=run['as_of'], cutoff=cutoff.isoformat(),
            training_rows=len(result['training']),
            training_end=(result['training'].index.max()+pd.Timedelta(minutes=15)).isoformat(),
            model=result['model'])
        fits.append(fit)
        (args.output/path.name).write_text(json.dumps(dict(fit=fit, pairs=updated['pairs']), indent=2))
    before, after = replay(original), replay(revised)
    def cohort(result):
        return [(r['as_of'], r['target_time'], r['run_id'], r['actual_kw']) for r in result['rows']]
    if cohort(before) != cohort(after):
        raise ValueError('replay cohorts differ; compare a fixed shared evaluation grid before drawing conclusions')
    summary = dict(evaluation='retrospective_expanding_training_sensitivity',
        limitations=['Historical reanalysis was fetched after the replay dates and may be revised.',
            'Telemetry ingestion times unavailable; only event-time label cutoff verified.',
            'Not an independent holdout or online acceptance; no forecast published.'],
        training_source_sha256=hashlib.sha256((args.training_source/'manifest.json').read_bytes()).hexdigest(),
        joint_comparison=dict(
            mae_change_kw=after['solver']['mae_kw']-before['solver']['mae_kw'],
            overprediction_change_kw=after['solver']['mean_overprediction_kw']-before['solver']['mean_overprediction_kw'],
            no_regression_vs_fixed_with_correction=(
                after['solver']['mae_kw'] <= before['solver']['mae_kw']
                and after['solver']['mean_overprediction_kw'] <= before['solver']['mean_overprediction_kw']),
            online_acceptance=False),
        fits=fits, fixed_training={k:v for k,v in before.items() if k!='rows'},
        expanding_training={k:v for k,v in after.items() if k!='rows'})
    (args.output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    (args.output/'replay.json').write_text(json.dumps(after, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(dict(original=after['original'], corrected=after['corrected'],
        solver=after['solver'], acceptance=after['acceptance'], fits=[{k:v for k,v in f.items() if k!='model'} for f in fits]),indent=2))


if __name__ == '__main__':
    main()
