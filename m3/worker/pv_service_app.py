"""Private Unix-socket PV service with manual and daily forecast submission."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from m3.worker.pv_query_app import build_reader, create_app as create_query_app
from m3.worker.services.pv_manual_jobs import ManualJobs
from m3.worker.pv_credentials import read_token_file


def reply(status, data=None, *, http_status=200, code=None):
    value = {'status': status, 'data': data}
    if code:
        value['code'] = code
    return JSONResponse(value, status_code=http_status, headers={'Cache-Control': 'no-store'})


def create_app(reader_factory, manager_factory, token_factory):
    app = create_query_app(reader_factory)
    query_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with query_lifespan(application):
            token = token_factory()
            if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
                raise ValueError('invalid private PV service token')
            application.state.service_token = token
            application.state.jobs = manager_factory()
            try:
                yield
            finally:
                await run_in_threadpool(application.state.jobs.close)

    app.router.lifespan_context = lifespan
    app.title = 'VIFA PV Manual Service'

    @app.middleware('http')
    async def authorize(request, call_next):
        if request.url.path == '/health':
            return reply('ok', {'service': 'pv-manual'})
        provided = request.headers.get('authorization', '')
        expected = 'Bearer '+request.app.state.service_token
        if len(provided) > 4096 or not hmac.compare_digest(provided.encode(), expected.encode()):
            return reply('error', http_status=401, code='unauthorized')
        return await call_next(request)

    async def submit(request, kind):
        if request.query_params or await request.body():
            return reply('error', http_status=400, code='invalid_request')
        try:
            accepted, value = await run_in_threadpool(request.app.state.jobs.submit, kind)
        except Exception:
            return reply('error', http_status=503, code='job_unavailable')
        return reply('accepted' if accepted else 'busy', value, http_status=202 if accepted else 409)

    @app.post('/api/pv/ES02/weather')
    async def weather(request: Request):
        return await submit(request, 'weather')

    @app.post('/api/pv/ES02/runs')
    async def forecast(request: Request):
        return await submit(request, 'forecast')

    @app.get('/api/pv/ES02/job')
    async def job(request: Request):
        if request.query_params or await request.body():
            return reply('error', http_status=400, code='invalid_request')
        value = request.app.state.jobs.latest()
        return reply('ok' if value else 'empty', value)

    return app


def build_manager():
    root = Path(os.environ.get('PV_MANUAL_STATE_DIR', '/run/vifa-pv/manual'))
    training = Path(os.environ.get('PV_TRAINING_SOURCE', '/app/pv-training'))
    project = Path(__file__).resolve().parents[2]

    def operate(kind, directory):
        subprocess.run([sys.executable, str(project/'m3/scripts/run-pv-manual.py'), '--operation', kind,
            '--job-dir', str(directory), '--training-source', str(training)], cwd=project,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=600)
        return json.loads((directory/'result.json').read_text())

    enabled = os.environ.get('PV_DAILY_SCHEDULE_ENABLED', '0')
    if enabled not in ('0', '1'):
        raise ValueError('PV_DAILY_SCHEDULE_ENABLED must be 0 or 1')
    manager = ManualJobs(root, operate)
    if enabled == '1':
        manager.start_daily_schedule()
    return manager


def service_token():
    return read_token_file(os.environ['PV_SERVICE_TOKEN_FILE'])


app = create_app(build_reader, build_manager, service_token)
