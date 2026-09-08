"""Run station-1's live read/model/solver/AI preview chain, without dispatch."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from uuid import uuid4

from pydantic import Field, model_validator
from m4_optimizer.contracts import OptimizationRequest, OptimizationResult, ProfileId, StrictModel
from m4_optimizer.metrics import calculate_metrics
from m4_selection import SelectionResult, select_candidate
from m4_selection.__main__ import _unique_pairs, _reject_constant
from m4_settings.control_sources import _NoRedirect
from m4_selection.ai_config import DEFAULT_CONFIG_PATH, load_model_config, resolve_model_config

ROOT = Path(__file__).resolve().parents[1]
BASE = '/m4-api/stations/station-1/'


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


class AiAnswer(StrictModel):
    snapshot_id: str
    selected_candidate_id: ProfileId
    reason: str = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.reason.strip():raise ValueError('reason is blank')
        return self


def make_ai_payload(envelope, preview, model_config=None):
    model_config=model_config or load_model_config()
    if not model_config.enabled:raise ValueError('AI is disabled')
    request=OptimizationRequest.model_validate_json(json.dumps(envelope['request']))
    result=OptimizationResult.model_validate_json(json.dumps(envelope['result']['stations'][0]['optimization_result']))
    verified=select_candidate(request,result,preview.policy)
    if verified != preview or verified.status!='selected':
        raise ValueError('candidate snapshot does not match verified selection')
    excluded={item.profile_id for item in verified.excluded}
    candidates=[]
    for candidate in result.candidates:
        if candidate.profile_id in excluded:continue
        metrics=calculate_metrics(request,candidate.plan)
        candidates.append(dict(candidate_id=candidate.profile_id,solver_status=candidate.status,
            **{name:getattr(metrics,name) for name in ['peak_demand_exceed_kw','demand_exceed_energy_kwh',
                'energy_cost','preferred_soc_deviation','pv_unabsorbed_energy_kwh']}))
    context=dict(snapshot_id=preview.input_sha256[:16],station_id='station-1',usage='preview_only',
        policy=preview.policy.model_dump(mode='json'),candidates=candidates,
        comparison=[item.model_dump(mode='json') for item in preview.steps],
        excluded_ids=sorted(excluded))
    schema=AiAnswer.model_json_schema()
    schema['properties']['snapshot_id']['enum']=[context['snapshot_id']]
    schema['properties']['selected_candidate_id']['enum']=[item['candidate_id'] for item in candidates]
    system=('你是M4候选选择助手，只选择已求解和复验的候选，不计算功率或修改计划。'
        '先按comparison的需量峰值层和累计超限层保留候选。'
        '若policy.metric为profile_priority，在剩余候选中按policy.tie_order从前往后选第一个；'
        'balanced就是均衡方案名称，不是SOC指标。其他metric先限定comparison第三层remaining_ids，再按tie_order选。'
        '不要根据候选列表位置或optimal/feasible标签改选。'
        '只输出符合schema的JSON，原样返回snapshot_id；reason写一句完整简短中文，说明规则，不声称已下发或实测收益。')
    payload=dict(model=model_config.model,messages=[dict(role='system',content=system),
        dict(role='user',content=json.dumps(context,ensure_ascii=False,separators=(',',':')))],
        format=schema,stream=True,keep_alive=0,options=model_config.generation.model_dump())
    if model_config.think is not None:payload['think']=model_config.think
    return payload


def assess_answer(raw,context,preview):
    audit=dict(status='invalid_response',raw_answer=raw,proposal=None)
    try:
        data=json.loads(raw,object_pairs_hook=_unique_pairs,parse_constant=_reject_constant)
        answer=AiAnswer.model_validate(data)
    except (ValueError,TypeError):return audit
    audit['proposal']=answer.model_dump()
    if answer.snapshot_id!=context['snapshot_id']:
        audit['status']='snapshot_mismatch'
    elif answer.selected_candidate_id!=preview.selected.profile_id:
        audit['status']='policy_mismatch'
    else:
        audit['status']='accepted'
    return audit


class LocalApi:
    def __init__(self,base_url):
        parts=urlsplit(base_url)
        if (parts.scheme!='http' or parts.hostname not in ('127.0.0.1','localhost','::1')
                or parts.username or parts.password or parts.path not in ('','/') or parts.query or parts.fragment):
            raise ValueError('M4 API must be a loopback HTTP origin')
        self.base_url=base_url.rstrip('/');self.opener=build_opener(_NoRedirect())

    def request(self,method,path,data=None):
        request=Request(self.base_url+path,method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'})
        with self.opener.open(request,timeout=180) as response:
            body=response.read(8*1024*1024+1)
            if len(body)>8*1024*1024:raise ValueError('local API response too large')
        return json.loads(body,object_pairs_hook=_unique_pairs,parse_constant=_reject_constant)


def run_chain(api,model,output,model_config=None):
    model_config=model_config or load_model_config()
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'report.json').exists():raise ValueError('output already contains a run; use a new directory')
    report=dict(station_id='station-1',usage='preview_only',dispatch_status='not_dispatched',
        started_at=datetime.now(timezone.utc).isoformat(),status='started',selected=None,ai=None,stages=[],
        model_config_sha256=model_config.digest())
    write(output/'model-config.json',model_config.model_dump(mode='json'))
    stage='configuration'
    try:
        config=api.request('GET',BASE+'settings');policy=api.request('GET',BASE+'selection-policy')
        write(output/'configuration.json',dict(settings=config,selection_policy=policy))
        if not config.get('parameters') or not policy.get('policy'):
            report['status']='blocked_configuration';return report
        report['stages'].append(dict(stage=stage,status='completed'))
        stage='inputs';inputs=api.request('GET',BASE+'inputs');write(output/'inputs.json',inputs)
        if inputs.get('status')!='ready' or inputs.get('issues'):
            report.update(status='blocked_inputs',issues=inputs.get('issues',[]))
            report['stages'].append(dict(stage=stage,status='blocked'));return report
        report['stages'].append(dict(stage=stage,status='completed'))
        stage='model_solver'
        envelope=api.request('POST',BASE+'candidates',dict(configuration_version=config['version']))
        write(output/'candidates.json',envelope)
        report['candidate_run_id']=envelope['result']['run_id']
        report['stages'].append(dict(stage=stage,status='completed',candidate_count=len(envelope['result']['stations'][0]['optimization_result']['candidates'])))
        stage='pre_ai_validation'
        selection_request=dict(candidate_run_id=report['candidate_run_id'],policy_revision=policy['revision'])
        before=api.request('POST',BASE+'selection',selection_request);write(output/'pre-ai-check.json',before)
        preview=SelectionResult.model_validate_json(json.dumps(before['selection']))
        if preview.status!='selected':
            report.update(status='blocked_pre_ai_validation',issues=[preview.reason]);return report
        report['stages'].append(dict(stage=stage,status='completed'))
        if model is None or not model_config.enabled:report['status']='blocked_ai_configuration';return report
        payload=make_ai_payload(envelope,preview,model_config);write(output/'ai-request.json',payload)
        stage='ai';raw=model(payload,output)
        context=json.loads(payload['messages'][1]['content'])
        report['ai']=assess_answer(raw,context,preview)
        write(output/'ai-assessment.json',report['ai'])
        report['stages'].append(dict(stage=stage,status=report['ai']['status']))
        if report['ai']['status']!='accepted':report['status']='ai_rejected';return report
        stage='post_ai_validation'
        after=api.request('POST',BASE+'selection',selection_request);write(output/'post-ai-check.json',after)
        if (after['candidate_run_id']!=before['candidate_run_id'] or after['policy_revision']!=before['policy_revision']
                or after['configuration_version']!=before['configuration_version'] or after['selection']!=before['selection']):
            raise ValueError('snapshot or policy changed during AI selection')
        report['stages'].append(dict(stage=stage,status='completed'))
        report.update(status='completed',selected=preview.selected.model_dump(mode='json'),
            checked_at=after['checked_at'],expires_at=after['expires_at'])
        return report
    except Exception as error:
        report.update(status='blocked_'+stage,error=dict(type=type(error).__name__,message=str(error)[:400]))
        report['stages'].append(dict(stage=stage,status='blocked'));return report
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat();write(output/'report.json',report)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-base',default='http://127.0.0.1:8844')
    parser.add_argument('--ai-config',type=Path,help='Model JSON file; overrides M4_AI_CONFIG')
    parser.add_argument('--ollama-url',help='Override the configured Ollama origin for this run')
    parser.add_argument('--model',help='Override the configured model name for this run')
    parser.add_argument('--no-ai',action='store_true',help='Disable AI for this run without changing the file')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    try:
        config=resolve_model_config(args.ai_config or os.environ.get('M4_AI_CONFIG') or DEFAULT_CONFIG_PATH,
            base_url=args.ollama_url,model=args.model,disable=args.no_ai)
    except ValueError:
        parser.error('AI 配置无效，请核对配置文件、模型名称和地址；未使用旧配置回退')
    output=args.output or ROOT/'m4/run/station1-ai-chain'/str(uuid4())
    model=None
    if config.enabled:
        from m4_selection.ollama import OllamaSelector
        model=OllamaSelector.from_config(config)
    report=run_chain(LocalApi(args.api_base),model,output,config)
    print(json.dumps(dict(status=report['status'],selected=report['selected'],issues=report.get('issues'),output=str(output.resolve())),ensure_ascii=False))
    return 0 if report['status']=='completed' else 1


if __name__=='__main__':raise SystemExit(main())
