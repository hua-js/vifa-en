"""Local parameter storage and read-only upstream control configuration."""
import ast
from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import SaveSettings, StationConfiguration
from .store import SettingsConflict, SettingsStore
from .control_sources import ControlSourceError, ControlSourceReader
from pydantic import BaseModel, ConfigDict, Field, UUID4

ROOT = Path(__file__).resolve().parents[2]


def upstream_token() -> str:
    """Reuse the authorized local configuration without executing it or exposing it."""
    configured = os.environ.get('M4_NOCOBASE_TOKEN')
    if configured is not None:
        return configured.strip()
    try:
        tree = ast.parse((ROOT / '.local/energy_efficiency_local_config.py').read_text())
        for item in tree.body:
            if isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'EMU_TOKEN' for target in item.targets
            ) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                return item.value.value.strip()
    except (OSError, SyntaxError):
        pass
    return ''


class CalculateCandidates(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    configuration_version: str = Field(min_length=1, max_length=200)


class StartCandidateJob(CalculateCandidates):
    request_id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


class StartDecisionRun(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


def create_app(settings_path: Path | None = None, *, control_reader=None, input_service=None, candidate_service=None, selection_service=None, billing_service=None, daily_input_service=None) -> FastAPI:
    path = settings_path or Path(os.environ.get('M4_SETTINGS_DB', str(ROOT / 'runtime/m4/settings.sqlite3')))
    store = SettingsStore(path)
    reader = control_reader
    token = upstream_token() if reader is None else ''
    def read_controls(station_id):
        source = reader if reader is not None else ControlSourceReader(token)
        return source.fetch(station_id)

    if input_service is None:
        from .live_inputs import LiveInputService
        from .upstream import NocoBaseClient
        input_service = LiveInputService(NocoBaseClient(token), SimpleNamespace(fetch=read_controls))
    from .daily_inputs import DailyInputService
    daily_inputs = daily_input_service if daily_input_service is not None else DailyInputService(input_service)
    from .candidates import CandidateError, CandidateService
    candidates = candidate_service if candidate_service is not None else CandidateService(
        store=store, fetch_inputs=input_service.fetch, read_controls=read_controls)
    from .selection import PolicyStore, SavePolicy, SelectCandidate, StationPolicy, LiveSelectionResult, LiveSelectionService
    policies = PolicyStore(path)
    selections = selection_service if selection_service is not None else LiveSelectionService(
        store=store, policies=policies, candidates=candidates, fetch_inputs=input_service.fetch)
    @asynccontextmanager
    async def lifespan(app):
        automatic.start()
        try:
            yield
        finally:
            automatic.stop()

    app = FastAPI(title='M4 调度参数', docs_url=None, redoc_url=None, lifespan=lifespan)
    from .billing import BillingService, MONTH_PATTERN
    from .upstream import NocoBaseClient
    billing = billing_service if billing_service is not None else BillingService(NocoBaseClient(token))
    from .decision_results import DecisionResultsReader
    decision_results = DecisionResultsReader(Path(os.environ.get(
        'M4_DECISION_RESULTS_DIR', str(ROOT / 'outputs/m4/solver-decisions'))))
    from .daily_plans import DailyPlanService
    daily_plans = DailyPlanService(store, daily_inputs, decision_results.root.parent / 'daily-comparisons')
    from .auto_plans import AutomaticPlans
    from .rolling_plans import RollingPlanService, PlanningCoordinator
    rolling_plans = RollingPlanService(daily_plans)
    coordinator = PlanningCoordinator(daily_plans, rolling_plans)
    automatic = AutomaticPlans(coordinator, daily_plans.root,
        enabled=os.environ.get('M4_AUTO_PLAN_ENABLED', '1').lower() not in ('0', 'false', 'off'))
    app.state.automatic_plans = automatic
    # This factory serves a local workstation. Production must use the platform's authenticated adapter.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def same_origin_writes(request: Request, call_next):
        from fastapi.responses import JSONResponse
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail':'只允许同源保存参数或启动计算'},status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def station_exists(station_id):
        if station_id not in ('station-1', 'station-2'):
            raise HTTPException(404, '未知电站')

    @app.get('/m4-api/stations/{station_id}/bills')
    def get_bills(station_id: str, month: Annotated[str, Query(pattern='^' + MONTH_PATTERN + '$')]) -> dict:
        station_exists(station_id)
        try:
            return billing.fetch(station_id, month)
        except Exception:
            raise HTTPException(502, '账单统计读取失败，请稍后重试') from None

    @app.get('/m4-api/stations/{station_id}/settings')
    def get_settings(station_id: str) -> StationConfiguration:
        station_exists(station_id)
        try:
            return store.get(station_id)
        except Exception:
            raise HTTPException(503, '无法读取参数存储') from None

    @app.put('/m4-api/stations/{station_id}/settings')
    def save_settings(station_id: str, body: SaveSettings) -> StationConfiguration:
        station_exists(station_id)
        try:
            return store.save(station_id, body.parameters, expected_revision=body.expected_revision)
        except SettingsConflict as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            raise HTTPException(503, '无法保存参数，原配置未被替换') from None

    @app.get('/m4-api/stations/{station_id}/control-sources')
    def get_control_sources(station_id: str) -> dict:
        station_exists(station_id)
        try:
            return read_controls(station_id)
        except ControlSourceError as error:
            raise HTTPException(502, str(error)) from None
        except Exception:
            raise HTTPException(502, '控制配置接口读取失败，请重新读取') from None

    @app.get('/m4-api/stations/{station_id}/inputs')
    def get_inputs(station_id: str) -> dict:
        station_exists(station_id)
        try:
            configuration = store.get(station_id)
        except Exception:
            raise HTTPException(503, '无法读取本站参数') from None
        try:
            result = input_service.fetch(configuration)
        except Exception:
            raise HTTPException(502, '真实输入读取失败，请重新读取') from None
        try:
            current = store.get(station_id)
        except Exception:
            raise HTTPException(503, '无法复核本站参数版本，请刷新输入后重试') from None
        if current.version != configuration.version or current.revision != configuration.revision:
            raise HTTPException(409, '调度参数在取数期间已更新，请刷新输入以使用新参数')
        return result

    @app.get('/m4-api/stations/{station_id}/daily-inputs')
    def get_daily_inputs(station_id: str) -> dict:
        station_exists(station_id)
        try:
            configuration = store.get(station_id)
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            result = daily_inputs.fetch(configuration, now.date(), now=now)
            if store.get(station_id).version != configuration.version:
                raise HTTPException(409, '参数在全天取数期间变化，请重新读取。')
            return result
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, '全天输入读取失败，请重新读取。') from None

    @app.get('/m4-api/stations/{station_id}/daily-plan')
    def get_daily_plan(station_id: str) -> dict:
        station_exists(station_id)
        try:
            daily = daily_plans.latest(station_id)
            try:
                rolling = rolling_plans.latest(station_id)
            except Exception:
                rolling = dict(station_id=station_id, status='failed', result=None,
                    message='滚动建议读取失败，等待更新。')
            return {**daily, 'automation': automatic.metadata(), 'rolling': rolling}
        except Exception:
            raise HTTPException(503, '全天计划读取或校验失败，请重新计算。') from None

    @app.post('/m4-api/stations/{station_id}/daily-plan', status_code=202)
    def start_daily_plan(station_id: str) -> dict:
        station_exists(station_id)
        return daily_plans.start(station_id)

    from .candidate_jobs import CandidateJobs
    candidate_jobs = CandidateJobs(candidates)

    @app.post('/m4-api/stations/{station_id}/candidate-jobs', status_code=202)
    def start_candidate_job(station_id: str, body: StartCandidateJob):
        station_exists(station_id)
        try:
            return candidate_jobs.start(station_id, body.request_id, body.configuration_version)
        except CandidateError as error:
            raise HTTPException(error.status_code, error.detail) from None

    @app.get('/m4-api/stations/{station_id}/candidate-jobs')
    def get_candidate_job(station_id: str, request_id: str):
        station_exists(station_id)
        job = candidate_jobs.get(station_id, request_id)
        if job is None:
            raise HTTPException(404, '候选任务不存在或服务已重启，请重新读取输入；未自动重试')
        return job

    @app.get('/m4-api/stations/{station_id}/candidates')
    def get_candidates(station_id: str) -> dict | None:
        station_exists(station_id)
        try:
            return candidates.latest(station_id)
        except CandidateError as error:
            raise HTTPException(error.status_code, error.detail) from None
        except Exception:
            raise HTTPException(503, '无法读取本站候选结果，请重试') from None

    @app.post('/m4-api/stations/{station_id}/candidates')
    def calculate_candidates(station_id: str, body: CalculateCandidates) -> dict:
        station_exists(station_id)
        try:
            return candidates.calculate(station_id, body.configuration_version)
        except CandidateError as error:
            raise HTTPException(error.status_code, error.detail) from None
        except Exception:
            raise HTTPException(503, '候选计算失败，请检查输入后重试') from None

    @app.get('/m4-api/stations/{station_id}/selection-policy')
    def get_selection_policy(station_id: str) -> StationPolicy:
        station_exists(station_id)
        try:
            return policies.get(station_id)
        except Exception:
            raise HTTPException(503, '无法读取本站选择偏好') from None

    @app.put('/m4-api/stations/{station_id}/selection-policy')
    def save_selection_policy(station_id: str, body: SavePolicy) -> StationPolicy:
        station_exists(station_id)
        try:
            return policies.save(station_id, body.preferences, expected_revision=body.expected_revision)
        except SettingsConflict as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            raise HTTPException(503, '无法保存选择偏好，原配置未被替换') from None

    @app.post('/m4-api/stations/{station_id}/selection')
    def select_live_candidate(station_id: str, body: SelectCandidate) -> LiveSelectionResult:
        station_exists(station_id)
        try:
            return selections.select(station_id, body.candidate_run_id, body.policy_revision)
        except CandidateError as error:
            raise HTTPException(error.status_code, error.detail) from None
        except Exception:
            raise HTTPException(503, '选择预览失败，请重新核对输入') from None

    # Reuse the same service handlers and their version checks without a self-HTTP request.
    def chain_request(method, path, data=None):
        prefix = '/m4-api/stations/'
        if not path.startswith(prefix):
            raise ValueError('unsupported decision API path')
        station_id, route = path.removeprefix(prefix).split('/', 1)
        station_exists(station_id)
        routes = {
            ('GET', 'settings'): lambda: get_settings(station_id),
            ('GET', 'selection-policy'): lambda: get_selection_policy(station_id),
            ('GET', 'inputs'): lambda: prepared_inputs(store.get(station_id)),
            ('POST', 'candidates'): lambda: calculate_candidates(station_id, CalculateCandidates.model_validate(data)),
            ('POST', 'selection'): lambda: select_live_candidate(station_id, SelectCandidate.model_validate(data)),
        }
        result = routes[(method, route)]()
        return result.model_dump(mode='json') if isinstance(result, BaseModel) else result

    from .decision_runs import DecisionRunError, DecisionRunManager
    decision_runs = DecisionRunManager(decision_results.root, SimpleNamespace(request=chain_request))

    @app.get('/m4-api/stations/{station_id}/decision-result')
    def get_decision_result(station_id: str) -> dict:
        station_exists(station_id)
        try:
            return decision_results.latest(station_id)
        except Exception:
            raise HTTPException(503, '决策记录读取或独立复验失败，请检查本地记录后刷新') from None

    @app.get('/m4-api/stations/{station_id}/decision-history')
    def get_decision_history(station_id: str,
            limit: Annotated[int, Query(ge=1, le=20)] = 10,
            offset: Annotated[int, Query(ge=0, le=100000)] = 0) -> dict:
        station_exists(station_id)
        try:
            return decision_results.history(station_id, limit=limit, offset=offset)
        except Exception:
            raise HTTPException(503, '历史记录读取失败，请刷新后重试') from None

    @app.get('/m4-api/stations/{station_id}/decision-results/{run_id}')
    def get_historical_decision(station_id: str, run_id: UUID4) -> dict:
        station_exists(station_id)
        try:
            return decision_results.by_run(station_id, str(run_id))
        except FileNotFoundError:
            raise HTTPException(404, '本站没有这条历史记录') from None
        except Exception:
            raise HTTPException(503, '历史记录读取或独立复验失败，未显示其他记录') from None

    @app.get('/m4-api/stations/{station_id}/decision-runs')
    def get_decision_run(station_id: str) -> dict:
        station_exists(station_id)
        try:
            return decision_runs.latest(station_id)
        except Exception:
            raise HTTPException(503, '无法读取决策运行状态，请刷新后再操作') from None

    @app.post('/m4-api/stations/{station_id}/decision-runs', status_code=202)
    def start_decision_run(station_id: str, body: StartDecisionRun) -> dict:
        station_exists(station_id)
        try:
            job = decision_runs.start(station_id, str(UUID(body.request_id)))
            return dict(station_id=station_id, usage='preview_only', dispatch_status='not_dispatched', job=job)
        except DecisionRunError as error:
            raise HTTPException(error.status_code, error.detail) from None
        except Exception:
            raise HTTPException(503, '无法启动求解器决策，请先刷新运行状态；未自动重试') from None

    @app.get('/m4', include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(ROOT / 'm4/web/M4优化调度控制台-线上版.html', media_type='text/html')

    return app
