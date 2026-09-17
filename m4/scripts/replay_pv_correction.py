"""Replay saved forward PV evaluations with labels restricted to closed quarters."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from m4.settings.pv_correction import correct_forecast, timestamp, POLICY


def metrics(rows, key):
    errors = [r[key]-r['actual_kw'] for r in rows]
    return dict(count=len(errors), mae_kw=sum(abs(e) for e in errors)/len(errors) if errors else None,
        mean_overprediction_kw=sum(max(0, e) for e in errors)/len(errors) if errors else None)


def replay(reports):
    # Build one clock grid per day; select the newest available batch per tick.
    # A validated operational batch is generated before forecast_start. Using
    # that start as availability is conservative when exact publication is absent.
    days = {}
    for report in reports:
        start = timestamp(report['forecast_start'])
        day = start.date().isoformat()
        if day not in days or start < timestamp(days[day]['forecast_start']):
            days[day] = report
    rows, daily = [], []
    for day, report in sorted(days.items()):
        pairs = report['pairs']
        index = {timestamp(p['target_time']): p for p in pairs}
        day_rows = []
        for now in sorted(index):
            if not 7 <= now.hour < 18 or now.date().isoformat() != day:
                continue
            available = [r for r in reports if timestamp(r['forecast_start']) <= now
                < timestamp(r['forecast_end'])]
            selected = max(available, key=lambda r: timestamp(r['forecast_start']))
            current = {timestamp(p['target_time']): p for p in selected['pairs']}
            history = [dict(timestamp=t, forecast_kw=p['forecast_kw'], actual_kw=p['actual_kw'],
                valid_minutes=p['valid_minutes']) for t, p in current.items() if t+timedelta(minutes=15) <= now]
            # Match the rolling service: first target is the NEXT quarter.
            future = [dict(timestamp=now+timedelta(minutes=15*i),
                forecast_kw=current[now+timedelta(minutes=15*i)]['forecast_kw']) for i in range(1, 9)
                if now+timedelta(minutes=15*i) in current]
            result = correct_forecast(history, future, now)
            for point in result['points']:
                t = timestamp(point['timestamp']); actual = current[t]['actual_kw']
                if actual is None or point['original_kw'] < 20 or t.hour >= 18:
                    continue
                day_rows.append(dict(as_of=now.isoformat(), target_time=t.isoformat(),
                    run_id=selected['run_id'],
                    actual_kw=actual, original_kw=point['original_kw'], corrected_kw=point['corrected_kw'],
                    solver_kw=point['solver_kw'],
                    status=result['status']))
        rows.extend(day_rows)
        if day_rows:
            daily.append(dict(date=day, run_ids=sorted({r['run_id'] for r in day_rows}), original=metrics(day_rows, 'original_kw'),
                corrected=metrics(day_rows, 'corrected_kw'), solver=metrics(day_rows, 'solver_kw')))
    original, corrected = metrics(rows, 'original_kw'), metrics(rows, 'corrected_kw')
    solver = metrics(rows, 'solver_kw')
    return dict(policy=POLICY, evaluation='retrospective_event_time_replay',
        limitation='Telemetry ingestion timestamps unavailable; historical arrival delay cannot be proven.',
        original=original, corrected=corrected, solver=solver, daily=daily,
        acceptance=dict(minimum_days=5, passed=len(daily) >= 5 and bool(rows)
            and solver['mae_kw'] < original['mae_kw']
            and all(d['solver']['mean_overprediction_kw'] <= d['original']['mean_overprediction_kw']
                    for d in daily)), rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = [json.loads(p.read_text()) for p in sorted(args.reports.glob('*.json'))]
    result = replay([r for r in reports if r.get('policy') == 'pv-forward-accuracy-v1'])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
