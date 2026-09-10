"""Embed validated B1 artifacts in the single-file console (run from repo root)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from m4.optimizer.contracts import OptimizationRequest
from m4.orchestrator.contracts import M4OrchestrationResult


def main():
    folder = ROOT / 'm4/mock/orchestration'
    result = M4OrchestrationResult.model_validate_json((folder / 'orchestration-result.json').read_text())
    inputs = [OptimizationRequest.model_validate_json((folder / f'station-{i}.json').read_text()) for i in (1, 2)]
    for request in inputs:
        station = next(s for s in result.stations if s.station_id == request.station_id)
        assert station.request_id == request.request_id
        assert station.input_summary.source_versions == request.source_versions
        assert station.input_summary.plan_start_at == request.plan_start_at
    bundle = {'result': result.model_dump(mode='json'), 'inputs': [r.model_dump(mode='json') for r in inputs]}
    payload = json.dumps(bundle, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    target = ROOT / 'm4/web/M4优化调度控制台-线上版.html'
    html = target.read_text()
    start = '<script type="application/json" id="m4-offline-bundle">'
    before, rest = html.split(start, 1)
    _, after = rest.split('</script>', 1)
    rendered = before + start + payload + '</script>' + after
    if '--check' in sys.argv:
        if rendered != html:
            raise SystemExit('Embedded B1 artifacts are stale; rerun m4/scripts/embed_offline_result.py')
        print('Embedded B1 artifacts match validated source JSON.')
    else:
        target.write_text(rendered)
        print('Embedded B1 result and two matching input snapshots.')


if __name__ == '__main__':
    main()
