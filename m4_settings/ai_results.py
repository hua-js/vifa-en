"""Read completed local AI evidence for display, never for live acceptance."""
from datetime import datetime
import json
import math
from pathlib import Path

from m4_optimizer.contracts import OptimizationRequest, OptimizationResult
from m4_optimizer.metrics import calculate_metrics
from m4_selection import select_candidate
from m4_selection.ai_config import ModelConfig
from m4_selection.live_chain import assess_answer
from m4_selection.__main__ import _unique_pairs, _reject_constant
from .selection import LiveSelectionResult

METRICS = ('peak_demand_exceed_kw', 'demand_exceed_energy_kwh', 'energy_cost',
           'preferred_soc_deviation', 'pv_unabsorbed_energy_kwh')
STATUSES = {'completed', 'ai_rejected', 'blocked_configuration', 'blocked_inputs',
            'blocked_model_solver', 'blocked_pre_ai_validation', 'blocked_ai_configuration',
            'blocked_ai', 'blocked_post_ai_validation'}


def read_text(path):
    if path.is_symlink():
        raise ValueError('symlink evidence is not supported')
    with path.open('rb') as source:
        raw = source.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('evidence too large')
    return raw.decode('utf-8')


def read_json(path):
    return json.loads(read_text(path), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def date(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('missing timezone')
    return parsed


class AiResultsReader:
    def __init__(self, root: Path):
        self.root = Path(root)

    def latest(self, station_id: str) -> dict:
        view = dict(schema_version='m4-ai-results-v1', station_id=station_id,
                    usage='historical_preview', dispatch_status='not_dispatched',
                    status='empty', record=None)
        # The existing CLI produces station-1 evidence only. Never reuse it for station 2.
        if station_id != 'station-1' or not self.root.exists():
            return view
        paths = [p / 'report.json' for p in self.root.iterdir()
                 if p.is_dir() and not p.is_symlink() and (p / 'report.json').exists()]
        if not paths:
            return view
        # report.json is the final artifact. In-flight directories are not completed runs.
        path = max(paths, key=lambda p: (p.stat().st_mtime_ns, p.parent.name))
        report = read_json(path)
        if (report['station_id'] != station_id or report['usage'] != 'preview_only'
                or report['dispatch_status'] != 'not_dispatched' or report['status'] not in STATUSES
                or date(report['finished_at']) < date(report['started_at'])):
            raise ValueError('invalid report')
        config = ModelConfig.model_validate(read_json(path.parent / 'model-config.json'))
        if config.digest() != report['model_config_sha256']:
            raise ValueError('model configuration changed')
        record = dict(run_id=path.parent.name, status=report['status'], model=config.model,
                      provider=config.provider, started_at=report['started_at'],
                      finished_at=report['finished_at'], wall_seconds=None,
                      candidate_run_id=report.get('candidate_run_id'),
                      ai_status='not_called', proposal=None, selected=None,
                      plan_start_at=None, expires_at=None, policy=None,
                      comparison=[], candidates=[], input_sha256=None)
        transport_file = path.parent / 'ai-transport.json'
        transport = None
        if transport_file.exists():
            transport = read_json(transport_file)
            seconds = transport.get('wall_seconds')
            if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0 or transport.get('model') != config.model:
                raise ValueError('invalid transport record')
            record['wall_seconds'] = seconds
        if report.get('ai') is not None:
            if (not transport or transport.get('completed') is not True or transport.get('done_reason') != 'stop'
                    or read_text(path.parent / 'ai-answer.txt') != report['ai']['raw_answer']):
                raise ValueError('AI response is missing or not known complete')
        before_file = path.parent / 'pre-ai-check.json'
        if before_file.exists():
            before = LiveSelectionResult.model_validate_json(json.dumps(read_json(before_file)))
            envelope = read_json(path.parent / 'candidates.json')
            request = OptimizationRequest.model_validate_json(json.dumps(envelope['request']))
            result = OptimizationResult.model_validate_json(json.dumps(envelope['result']['stations'][0]['optimization_result']))
            preview = select_candidate(request, result, before.selection.policy)
            if (before.station_id != station_id or request.station_id != station_id
                    or before.candidate_run_id != report.get('candidate_run_id')
                    or envelope['result']['run_id'] != before.candidate_run_id
                    or envelope['configuration_version'] != before.configuration_version
                    or preview != before.selection):
                raise ValueError('candidate evidence does not match')
            record.update(plan_start_at=request.plan_start_at.isoformat(),
                          expires_at=before.expires_at.isoformat(),
                          policy=preview.policy.model_dump(mode='json') if preview.policy else None,
                          comparison=[step.model_dump(mode='json') for step in preview.steps],
                          input_sha256=preview.input_sha256)
            excluded = {item.profile_id for item in preview.excluded}
            for candidate in result.candidates:
                usable = candidate.profile_id not in excluded and candidate.status in ('optimal', 'feasible')
                metrics = calculate_metrics(request, candidate.plan) if usable else None
                record['candidates'].append(dict(profile_id=candidate.profile_id,
                    status=candidate.status, plan_version=candidate.plan_version,
                    metrics={key: getattr(metrics, key) for key in METRICS} if metrics else None,
                    plan=[point.model_dump(mode='json') for point in candidate.plan] if usable else []))
            if report.get('ai') is not None:
                if preview.status != 'selected':
                    raise ValueError('AI requires a verified candidate snapshot')
                assessment = assess_answer(report['ai']['raw_answer'],
                    {'snapshot_id': preview.input_sha256[:16]}, preview)
                if assessment != report['ai']:
                    raise ValueError('AI assessment changed')
                record.update(ai_status=assessment['status'], proposal=assessment['proposal'])
        elif report.get('ai') is not None:
            raise ValueError('missing AI input evidence')
        if report['status'] == 'completed':
            after = LiveSelectionResult.model_validate_json(json.dumps(read_json(path.parent / 'post-ai-check.json')))
            if (record['ai_status'] != 'accepted' or after.selection != before.selection
                    or after.candidate_run_id != before.candidate_run_id
                    or after.policy_revision != before.policy_revision
                    or after.configuration_version != before.configuration_version
                    or report['selected'] != preview.selected.model_dump(mode='json')):
                raise ValueError('completed report does not match final check')
            record['selected'] = report['selected']
        elif report.get('selected') is not None:
            raise ValueError('blocked report cannot select a plan')
        view.update(status='available', record=record)
        return view
