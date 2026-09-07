"""Local parameter storage and read-only upstream control configuration."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import SaveSettings, StationConfiguration
from .store import SettingsConflict, SettingsStore
from .control_sources import ControlSourceError, ControlSourceReader
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]


def upstream_token() -> str:
    """Reuse the authorized local configuration without executing it or exposing it."""
    configured = os.environ.get('M4_NOCOBASE_TOKEN')
    if configured is not None:
        return configured.strip()
    try:
        tree = ast.parse((ROOT / 'energy_efficiency_local_config.py').read_text())
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


def create_app(settings_path: Path | None = None, *, control_reader=None, input_service=None, candidate_service=None) -> FastAPI:
    path = settings_path or Path(os.environ.get('M4_SETTINGS_DB', str(ROOT / 'm4/run/settings.sqlite3')))
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
    from .candidates import CandidateError, CandidateService
    candidates = candidate_service if candidate_service is not None else CandidateService(
        store=store, fetch_inputs=input_service.fetch, read_controls=read_controls)
    app = FastAPI(title='M4 调度参数', docs_url=None, redoc_url=None)
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

    @app.get('/m4', include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(ROOT / 'm4/M4优化调度控制台-线上版.html', media_type='text/html')

    return app
