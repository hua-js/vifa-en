"""Run the local input, scheduling solver and final decision preview pipeline."""
import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from m4.optimizer.contracts import OptimizationRequest
from m4.orchestrator.contracts import M4OrchestrationResult
from m4.selection import select_candidate
from m4.selection.__main__ import _reject_constant, _unique_pairs
from m4.settings.models import StationConfiguration
from m4.settings.selection import LiveSelectionResult, StationPolicy


ROOT = Path(__file__).resolve().parents[2]
SELECTOR_VERSION = 'pyomo-highs-selection-v2'
STATIONS = ('station-1', 'station-2')
EVIDENCE_NAMES = ('configuration.json', 'inputs.json', 'candidates.json', 'selection.json')
FINAL_STATUSES = {'completed', 'blocked_configuration', 'blocked_inputs',
                  'blocked_model_solver', 'blocked_selection'}


def parse_json(value):
    return json.loads(value, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def read_bytes(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('symlink evidence is not supported')
    with path.open('rb') as source:
        raw = source.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('evidence too large')
    return raw


def read_json(path):
    return parse_json(read_bytes(path))


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)
    return sha256(raw).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp requires an offset')
    return parsed


def validate_identity(station_id, run_id=None):
    if station_id not in STATIONS:
        raise ValueError('unknown station')
    if run_id is not None and (str(UUID(run_id)) != run_id or UUID(run_id).version != 4):
        raise ValueError('run_id must be a canonical UUID4')


def candidate_snapshot(envelope, station_id, *, require_result=True):
    """Validate immutable candidate bindings without claiming current freshness."""
    if (envelope['station_id'] != station_id or envelope['usage'] != 'preview_only'
            or envelope['stale'] is not False):
        raise ValueError('candidate envelope is not a valid preview snapshot')
    request = OptimizationRequest.model_validate_json(json.dumps(envelope['request']))
    orchestration = M4OrchestrationResult.model_validate_json(json.dumps(envelope['result']))
    if len(orchestration.stations) != 1 or orchestration.stations[0].station_id != station_id:
        raise ValueError('candidate station binding changed')
    result = orchestration.stations[0].optimization_result
    if request.station_id != station_id or require_result and result is None:
        raise ValueError('missing station optimization result')
    return request, result


def verify_request_inputs(envelope, configuration, request):
    """Rebuild historical request inputs with their original validation clock."""
    from m4.settings.live_inputs import request_from_inputs
    from m4.settings.selection import _decision_content
    generated_at = timestamp(envelope['generated_at'])
    original = request_from_inputs(configuration, envelope['inputs'], request.profiles, now=generated_at)
    expires_at = min(request.plan_start_at,
                     request.input_observed_at + timedelta(seconds=request.max_input_age_seconds))
    if (_decision_content(original) != _decision_content(request)
            or timestamp(envelope['expires_at']) != expires_at or generated_at >= expires_at):
        raise ValueError('saved inputs and configuration do not match the optimization request')


def verify_precheck_inputs(inputs, configuration, profiles):
    """Validate the initial read on its own clock, before the solver resample."""
    from m4.settings.live_inputs import request_from_inputs
    request_from_inputs(configuration, inputs, profiles, now=timestamp(inputs['fetched_at']))


def verify_selection(envelope, live, policy, configuration):
    """Independently verify the mathematical selector's answer and provenance."""
    request, result = candidate_snapshot(envelope, configuration.station_id)
    verify_request_inputs(envelope, configuration, request)
    expected = select_candidate(request, result, policy.policy)
    supplied = live.selection.model_dump()
    supplied['selector_version'] = expected.selector_version
    if (live.selection.selector_version != SELECTOR_VERSION or supplied != expected.model_dump()
            or live.station_id != configuration.station_id
            or policy.station_id != configuration.station_id
            or live.policy_revision != policy.revision
            or live.candidate_run_id != envelope['result']['run_id']
            or live.configuration_version != configuration.version
            or live.configuration_version != envelope['configuration_version']
            or live.expires_at != timestamp(envelope['expires_at'])
            or live.checked_at.tzinfo is None or live.checked_at >= live.expires_at
            or live.checked_at < timestamp(envelope['generated_at'])):
        raise ValueError('solver selection does not match independently verified evidence')
    return expected


class LocalApi:
    def __init__(self, base_url):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError('M4 API must be a loopback HTTP origin')
        self.base_url = base_url.rstrip('/')

    def request(self, method, path, data=None):
        with httpx.Client(timeout=180, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, self.base_url + path, json=data)
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise ValueError('local API response too large')
        return parse_json(response.content)


def run_chain(api, output, *, station_id='station-1', run_id=None, progress=None):
    """Persist a solver-only preview; the selection endpoint owns live rechecks."""
    run_id = run_id or str(uuid4())
    validate_identity(station_id, run_id)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in (*EVIDENCE_NAMES, 'report.json')):
        raise ValueError('output already contains a run; use a new directory')
    base = f'/m4-api/stations/{station_id}/'
    report = dict(schema_version='m4-decision-run-v1', run_id=run_id, station_id=station_id,
        usage='preview_only', dispatch_status='not_dispatched',
        started_at=datetime.now(timezone.utc).isoformat(), status='started',
        selected=None, reason='', issues=[], stages=[], evidence_sha256={})
    stage = 'configuration'

    def enter(name, message):
        nonlocal stage
        stage = name
        if progress is not None:
            progress(name, message)

    def save(name, value):
        report['evidence_sha256'][name] = write_json(output / name, value)

    try:
        enter('configuration', '正在读取调度参数与经营偏好')
        settings = api.request('GET', base + 'settings')
        preferences = api.request('GET', base + 'selection-policy')
        save('configuration.json', dict(settings=settings, selection_policy=preferences))
        configuration = StationConfiguration.model_validate_json(json.dumps(settings))
        policy = StationPolicy.model_validate_json(json.dumps(preferences))
        if configuration.station_id != station_id or policy.station_id != station_id:
            raise ValueError('configuration station mismatch')
        if configuration.parameters is None or not configuration.version or policy.policy is None:
            report.update(status='blocked_configuration', issues=['本站调度参数或经营偏好尚未配置。'])
            return report
        report['stages'].append(dict(stage=stage, status='completed'))
        enter('inputs', '正在获取并校验真实输入')
        inputs = api.request('GET', base + 'inputs')
        save('inputs.json', inputs)
        if inputs.get('status') != 'ready' or inputs.get('issues'):
            report.update(status='blocked_inputs', issues=inputs.get('issues') or ['输入尚未就绪。'])
            return report
        report['stages'].append(dict(stage=stage, status='completed'))
        enter('model_solver', '正在建模并求解调度候选')
        envelope = api.request('POST', base + 'candidates', dict(configuration_version=configuration.version))
        save('candidates.json', envelope)
        report['candidate_run_id'] = envelope['result']['run_id']
        request, result = candidate_snapshot(envelope, station_id, require_result=False)
        verify_precheck_inputs(inputs, configuration, request.profiles)
        verify_request_inputs(envelope, configuration, request)
        if envelope['configuration_version'] != configuration.version:
            raise ValueError('configuration changed during optimization')
        if result is None:
            report.update(status='blocked_model_solver', issues=['调度求解未生成可用候选。'])
            return report
        # Physical validation is independent of the new modeling framework.
        preview = select_candidate(request, result, policy.policy)
        report.update(candidate_run_id=envelope['result']['run_id'], solver_name=result.solver_name,
            solver_version=result.solver_version, model_version=result.model_version)
        if preview.status != 'selected':
            report.update(status='blocked_model_solver', issues=[preview.reason])
            return report
        report['stages'].append(dict(stage=stage, status='completed', candidate_count=len(result.candidates)))
        enter('selection', '正在求解最终方案并复核实时状态')
        answer = api.request('POST', base + 'selection', dict(candidate_run_id=report['candidate_run_id'],
                             policy_revision=policy.revision))
        save('selection.json', answer)
        live = LiveSelectionResult.model_validate_json(json.dumps(answer))
        verified = verify_selection(envelope, live, policy, configuration)
        if verified.status != 'selected':
            report.update(status='blocked_selection', issues=[verified.reason])
            return report
        report['stages'].append(dict(stage=stage, status='completed'))
        report.update(status='completed', selected=live.selection.selected.model_dump(mode='json'),
            reason=live.selection.reason, selector_version=live.selection.selector_version,
            checked_at=live.checked_at.isoformat(), expires_at=live.expires_at.isoformat(),
            input_sha256=live.selection.input_sha256)
        return report
    except Exception:
        # API handlers already sanitize upstream failures; do not persist URLs,
        # headers or arbitrary upstream messages as public decision diagnostics.
        messages = {'configuration': '调度参数或偏好读取失败。', 'inputs': '真实输入读取或校验失败。',
                    'model_solver': '调度求解或候选独立复验未通过。',
                    'selection': '最终决策、实时状态或版本复核未通过，请重新计算。'}
        report.update(status='blocked_' + stage, issues=[messages[stage]])
        return report
    finally:
        if report['status'] != 'completed':
            report['stages'].append(dict(stage=stage, status='blocked'))
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'report.json', report)


def main(argv=None):
    parser = argparse.ArgumentParser(description='M4 求解器决策预览，不下发设备指令')
    parser.add_argument('--api-base', default='http://127.0.0.1:8844')
    parser.add_argument('--station', choices=STATIONS, default='station-1')
    parser.add_argument('--output', type=Path, help='本轮独立输出目录')
    args = parser.parse_args(argv)
    run_id = str(uuid4())
    root = Path(os.environ.get('M4_DECISION_RESULTS_DIR', str(ROOT / 'outputs/m4/solver-decisions')))
    output = args.output or root / args.station / run_id
    try:
        report = run_chain(LocalApi(args.api_base), output, station_id=args.station, run_id=run_id)
    except (ValueError, OSError) as error:
        parser.exit(2, f'无法启动本地决策预览：{error}\n')
    print(json.dumps(dict(status=report['status'], selected=report['selected'],
                         issues=report['issues'], output=str(output.resolve())), ensure_ascii=False))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
