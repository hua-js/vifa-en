"""Loopback-only deployment target for the read-only PV forecast query API."""
from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from m3_worker.services.pv_query import latest_forecast
from m3_worker.domain.pv_operational import timestamp
from m3_worker.pv_credentials import read_token_file


class Point(BaseModel):
    model_config=ConfigDict(extra='ignore')
    target_time:str
    horizon_step:int=Field(ge=1,le=96)
    forecast_kw:float=Field(ge=0,allow_inf_nan=False)
    raw_forecast_kw:float|None
    baseline_kw:float|None
    lower_kw:float|None
    upper_kw:float|None
    is_clipped:bool


class Forecast(BaseModel):
    model_config=ConfigDict(extra='ignore')
    run_id:str
    run_pk:int
    es_sn:Literal['ES02']
    as_of:str
    generated_at:str
    updated_at:str
    queried_at:str
    forecast_start:str
    forecast_end:str
    interval_minutes:Literal[15]
    expected_points:Literal[96]
    model_name:str
    model_version:str
    training_end:str
    training_rows:int
    weather_batch_id:str
    weather_fetched_at:str
    forecast_energy_kwh:float=Field(ge=0,allow_inf_nan=False)
    peak_kw:float=Field(ge=0,allow_inf_nan=False)
    peak_time:str
    freshness:Literal['fresh','stale','expired']
    age_seconds:float
    remaining_full_points:int=Field(ge=0,le=96)
    points:list[Point]=Field(min_length=96,max_length=96)


class Envelope(BaseModel):
    status:Literal['ok','empty']
    data:Forecast|None


def create_app(reader_factory):
    @asynccontextmanager
    async def lifespan(app):
        app.state.reader=reader_factory()
        yield
    app=FastAPI(title='VIFA PV Query',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

    @app.get('/health')
    def health()->dict[str,str]:return {'status':'ok','service':'pv-query'}

    @app.get('/api/pv/ES02/latest',response_model=Envelope)
    async def latest(request:Request):
        headers={'Cache-Control':'no-store'}
        if request.query_params or await request.body():
            return JSONResponse(status_code=400,content={'status':'error','code':'invalid_request'},headers=headers)
        try:
            value=await run_in_threadpool(request.app.state.reader)
            result=Envelope.model_validate(value)
            return JSONResponse(result.model_dump(),headers=headers)
        except Exception:
            return JSONResponse(status_code=502,content={'status':'error','code':'forecast_unavailable'},headers=headers)
    return app


def build_reader():
    path=Path(__file__).resolve().parents[1]/'m3/deploy/publish-pv-forecast.py'
    spec=importlib.util.spec_from_file_location('pv_query_repository',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    key_file=os.environ.get('PV_NOCOBASE_QUERY_API_KEY_FILE',str(Path.home()/'.config/vifa/pv-nocobase-api.token'))
    key=read_token_file(key_file)
    class ReadOnlyRepository(module.ForecastRepository):
        def request(self,table,action,**kwargs):
            if action!='list':raise ValueError('PV query is read-only')
            return super().request(table,action,**kwargs)
    repository=ReadOnlyRepository(key)
    # A separate repository per request avoids sharing urllib opener state across worker threads.
    def read():
        import pandas as pd
        return latest_forecast(ReadOnlyRepository(key),pd.Timestamp.now(tz='Asia/Shanghai'))
    return read


app=create_app(build_reader)
