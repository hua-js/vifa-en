"""Read and independently verify historical solver decision evidence."""
from hashlib import sha256
import json
from pathlib import Path

from m4_optimizer.metrics import calculate_metrics
from m4_selection import select_candidate
from m4_selection.decision_chain import (EVIDENCE_NAMES, FINAL_STATUSES, SELECTOR_VERSION,
    candidate_snapshot, read_bytes, read_json, parse_json, timestamp, validate_identity,
    verify_precheck_inputs, verify_request_inputs, verify_selection)
from .models import StationConfiguration
from .peak_preparation import summarize_peak_preparation
from .daily_comparison import compare_daily_plan, unavailable_comparison
from .selection import LiveSelectionResult, StationPolicy


METRICS = ('peak_demand_exceed_kw', 'demand_exceed_energy_kwh', 'energy_cost',
           'preferred_soc_deviation', 'pv_unabsorbed_energy_kwh', 'max_grid_import_kw')


class DecisionResultsReader:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def _view(station_id):
        return dict(schema_version='m4-decision-results-v1', station_id=station_id,
            usage='historical_preview_only', dispatch_status='not_dispatched', status='empty', record=None)

    def _paths(self, station_id):
        validate_identity(station_id)
        directory = self.root / station_id
        if directory.is_symlink():
            raise ValueError('symlink evidence directory is not supported')
        if not directory.exists():
            return []
        paths = [child / 'report.json' for child in directory.iterdir()
                 if child.is_dir() and not child.is_symlink() and (child / 'report.json').exists()]
        return sorted(paths, key=lambda item: (item.stat().st_mtime_ns, item.parent.name), reverse=True)

    def latest(self, station_id):
        paths = self._paths(station_id)
        return self._read(station_id, paths[0]) if paths else self._view(station_id)

    def by_run(self, station_id, run_id):
        validate_identity(station_id, run_id)
        matches = []
        for path in self._paths(station_id):
            if path.parent.name == run_id:
                matches.append(path)
                continue
            # CLI outputs may use a human-readable directory. Their report
            # identity still has to be a canonical UUID and pass full checks.
            try:
                if read_json(path).get('run_id') == run_id:
                    matches.append(path)
            except (ValueError, TypeError, AttributeError, OSError):
                continue
        if not matches:
            raise FileNotFoundError('decision record not found')
        if len(matches) != 1:
            raise ValueError('ambiguous decision identity')
        view = self._read(station_id, matches[0])
        if view['record']['run_id'] != run_id:
            raise ValueError('requested decision identity mismatch')
        return view

    def history(self, station_id, *, limit=10, offset=0):
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError('invalid history page')
        paths = self._paths(station_id)
        items = []
        for path in paths[offset:offset+limit]:
            try:
                record = self._read(station_id, path)['record']
                chosen = next((c for c in record['candidates']
                               if c['profile_id'] == (record['selected'] or {}).get('profile_id')), None)
                items.append(dict(run_id=record['run_id'], status=record['status'],
                    started_at=record['started_at'], finished_at=record['finished_at'],
                    plan_start_at=record['plan_start_at'], selected=record['selected'],
                    metrics=chosen['metrics'] if chosen else None, issues=record['issues']))
            except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError):
                items.append(dict(run_id=None, status='unreadable', started_at=None, finished_at=None,
                    plan_start_at=None, selected=None, metrics=None, issues=['此条记录读取或独立校验失败']))
        return dict(schema_version='m4-decision-history-v1', station_id=station_id,
            usage='historical_preview_only', dispatch_status='not_dispatched', items=items,
            offset=offset, next_offset=offset+limit if offset+limit < len(paths) else None,
            order='newest_saved_first')

    def _read(self, station_id, path):
        view = self._view(station_id)
        report = read_json(path)
        validate_identity(report['station_id'], report['run_id'])
        if (report['station_id'] != station_id
                or report['schema_version'] != 'm4-decision-run-v1'
                or report['usage'] != 'preview_only' or report['dispatch_status'] != 'not_dispatched'
                or report['status'] not in FINAL_STATUSES
                or timestamp(report['finished_at']) < timestamp(report['started_at'])):
            raise ValueError('invalid decision report')
        stage_order = ('configuration', 'inputs', 'model_solver', 'selection')
        completed = report['status'] == 'completed'
        expected_stages = (stage_order if completed else
            stage_order[:stage_order.index(report['status'].removeprefix('blocked_')) + 1])
        if (not isinstance(report['issues'], list)
                or any(not isinstance(issue, str) for issue in report['issues'])
                or completed and report['issues']
                or not isinstance(report['stages'], list)
                or [item.get('stage') for item in report['stages']] != list(expected_stages)
                or any(item.get('status') != ('completed' if completed or index < len(expected_stages) - 1 else 'blocked')
                       for index, item in enumerate(report['stages']))):
            raise ValueError('decision stages disagree with final status')
        evidence = {}
        for name, expected_hash in report['evidence_sha256'].items():
            if name not in EVIDENCE_NAMES:
                raise ValueError('unknown evidence file')
            raw = read_bytes(path.parent / name)
            if sha256(raw).hexdigest() != expected_hash:
                raise ValueError('decision evidence changed')
            evidence[name] = parse_json(raw)
        if any((path.parent / name).exists() and name not in evidence for name in EVIDENCE_NAMES):
            raise ValueError('unbound decision evidence')
        record = dict(run_id=report['run_id'], station_id=station_id, status=report['status'],
            started_at=report['started_at'], finished_at=report['finished_at'],
            solve_seconds=None, solver_name=None, solver_version=None, model_version=None,
            selector_version=None, candidate_run_id=report.get('candidate_run_id'),
            selected=None, reason='', checked_at=None, expires_at=None, input_sha256=None,
            comparison=[], candidates=[], policy=None, plan_start_at=None, peak_preparation=None, input_summary=None,
            daily_comparison=unavailable_comparison(station_id),
            issues=report['issues'], stages=report['stages'])
        configuration = policy = None
        if 'configuration.json' in evidence:
            saved = evidence['configuration.json']
            configuration = StationConfiguration.model_validate_json(json.dumps(saved['settings']))
            policy = StationPolicy.model_validate_json(json.dumps(saved['selection_policy']))
            if configuration.station_id != station_id or policy.station_id != station_id:
                raise ValueError('configuration station binding changed')
            record['policy'] = policy.policy.model_dump(mode='json') if policy.policy else None
        if 'candidates.json' in evidence:
            if configuration is None or policy is None or 'inputs.json' not in evidence:
                raise ValueError('missing candidate configuration')
            envelope = evidence['candidates.json']
            request, result = candidate_snapshot(envelope, station_id, require_result=False)
            verify_precheck_inputs(evidence['inputs.json'], configuration, request.profiles)
            verify_request_inputs(envelope, configuration, request)
            if (configuration.version != envelope['configuration_version']
                    or envelope['result']['run_id'] != report.get('candidate_run_id')):
                raise ValueError('candidate report binding changed')
            if result is None:
                if (report['status'] != 'blocked_model_solver' or report.get('selected') is not None
                        or 'selection.json' in evidence):
                    raise ValueError('missing optimization result')
                view.update(status='available', record=record)
                return view
            record['input_summary'] = dict(configuration_version=configuration.version,
                horizon_points=request.horizon_points,
                source_versions=dict(request.source_versions),
                initial_soc_pct=request.capability.initial_soc_pct,
                energy_capacity_kwh=request.capability.energy_capacity_kwh,
                max_charge_kw=request.capability.max_charge_kw,
                max_discharge_kw=request.capability.max_discharge_kw,
                demand_limit_kw=request.constraints.demand_limit_kw,
                input_observed_at=request.input_observed_at.isoformat())
            preview = select_candidate(request, result, policy.policy)
            record.update(plan_start_at=request.plan_start_at.isoformat(),
                solve_seconds=sum(candidate.solve_seconds for candidate in result.candidates),
                solver_name=result.solver_name, solver_version=result.solver_version,
                model_version=result.model_version, input_sha256=preview.input_sha256,
                comparison=[step.model_dump(mode='json') for step in preview.steps])
            excluded = {item.profile_id for item in preview.excluded}
            for candidate in result.candidates:
                usable = candidate.profile_id not in excluded and candidate.status in {'optimal', 'feasible'}
                metrics = calculate_metrics(request, candidate.plan) if usable else None
                record['candidates'].append(dict(profile_id=candidate.profile_id, status=candidate.status,
                    plan_version=candidate.plan_version,
                    metrics={key: getattr(metrics, key) for key in METRICS} if metrics else None,
                    plan=[point.model_dump(mode='json') for point in candidate.plan] if usable else []))
        if 'selection.json' in evidence:
            if 'candidates.json' not in evidence:
                raise ValueError('missing candidate evidence')
            live = LiveSelectionResult.model_validate_json(json.dumps(evidence['selection.json']))
            verify_selection(evidence['candidates.json'], live, policy, configuration)
            record.update(selector_version=live.selection.selector_version,
                checked_at=live.checked_at.isoformat(), expires_at=live.expires_at.isoformat())
        if report['status'] == 'completed':
            if set(evidence) != set(EVIDENCE_NAMES):
                raise ValueError('completed report requires all evidence')
            if (live.selection.status != 'selected' or report['selector_version'] != SELECTOR_VERSION
                    or report['selected'] != live.selection.selected.model_dump(mode='json')
                    or report['reason'] != live.selection.reason
                    or report['input_sha256'] != live.selection.input_sha256
                    or timestamp(report['checked_at']) != live.checked_at
                    or timestamp(report['expires_at']) != live.expires_at):
                raise ValueError('completed report disagrees with final decision')
            record.update(selected=report['selected'], reason=report['reason'])
            chosen = next(candidate for candidate in result.candidates
                          if candidate.profile_id == live.selection.selected.profile_id)
            record['peak_preparation'] = summarize_peak_preparation(request, chosen)
            saved_inputs = evidence['candidates.json']['inputs']
            record['daily_comparison'] = compare_daily_plan(request, chosen,
                baseline=saved_inputs.get('daily_baseline'),
                controls_version=saved_inputs.get('sources', {}).get('controls', {}).get('version'))
        elif report.get('selected') is not None:
            raise ValueError('blocked decision cannot select a plan')
        view.update(status='available', record=record)
        return view
