"""Immutable, station-isolated allocation snapshots under the persisted M4 root."""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from .cabinet_allocation import ALGORITHM_VERSION, SHANGHAI, allocate_cabinets, timestamp

SCHEMA = 'm4-cabinet-allocation-v1'


class AllocationError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def encode(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(encode(data)).hexdigest()


def identity(station, value=None):
    if station not in ('station-1', 'station-2'):
        raise AllocationError(404, '未知电站')
    if value is not None:
        try:
            parsed = UUID(value)
            if parsed.version != 4 or str(parsed) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AllocationError(400, '分配或计划编号无效') from None


def plan_input(job, snapshot, now):
    request = job['request']
    comparison = job['result']['record']['daily_comparison']
    station = job['station_id']
    if comparison['status'] not in ('ems', 'optimized') or comparison['recommended_source'] != comparison['status']:
        raise AllocationError(409, '暂无可用的已校验站级计划')
    if comparison['date'] != now.astimezone(SHANGHAI).date().isoformat():
        raise AllocationError(409, '站级计划已跨日，请重新生成')
    points = [dict(power=0.0, load=p['load_forecast_kw'], pv=p['pv_forecast_kw']) for p in request['points']]
    if len(points) != 96 or request['station_id'] != station:
        raise AllocationError(409, '站级计划数据不完整')
    if comparison['status'] == 'ems':
        occupied = set()
        schedule = job['baseline']['schedule']
        if not schedule:
            raise AllocationError(409, 'EMS原始时段配置不可用')
        for item in schedule:
            def slot(value):
                parts = value.split(':')
                h, m = int(parts[0]), int(parts[1])
                if not (len(parts) in (2, 3) and (len(parts) == 2 or parts[2] == '00')
                        and 0 <= h <= 24 and m in (0,15,30,45) and (h < 24 or m == 0)):
                    raise ValueError('EMS时段格式无效')
                return h*4+m//15
            a, b = slot(item['start_time']), slot(item['end_time'])
            if a == 96 or a == b or item['repeat'] != 'daily' or item['mode'] not in ('charge','discharge'):
                raise ValueError('EMS时段无效')
            for offset in range(b-a if b>a else 96-a+b):
                i = (a+offset)%96
                if i in occupied:
                    raise ValueError('EMS时段重叠')
                occupied.add(i)
                points[i]['power'] = item['power_kw']*(-1 if item['mode']=='charge' else 1)
    else:
        chosen = comparison['recommended']['plan']
        if len(chosen) != 96:
            raise AllocationError(409, '优化计划不完整')
        for i,p in enumerate(chosen):
            points[i]['power'] = -p['target_power_kw'] if p['mode']=='charge' else p['target_power_kw'] if p['mode']=='discharge' else 0
    reserve = request.get('peak_reserve_policy') if comparison['status']=='optimized' else None
    reserve_start = None
    if reserve and reserve['version']=='peak-reserve-v2':
        for i,p in enumerate(request['points']):
            if p['tariff_period']=='feng' and (i==0 or request['points'][i-1]['tariff_period']!='feng'):
                reserve_start = i
        if reserve_start is None:
            raise ValueError('晚峰时段缺失')
    bounds = request['constraints']
    return dict(station_id=station,date=comparison['date'],now=now.isoformat(),
        configuration_version=request['source_versions']['configuration'], controls_version=request['source_versions']['controls'],
        snapshot=deepcopy(snapshot), points=points, bounds=deepcopy(bounds),
        charge_efficiency=request['capability']['charge_efficiency'],discharge_efficiency=request['capability']['discharge_efficiency'],
        max_age_seconds=request['max_input_age_seconds'],grid_import_limit_kw=min(bounds['demand_limit_kw'],bounds.get('grid_import_limit_kw') if bounds.get('grid_import_limit_kw') is not None else bounds['demand_limit_kw']),
        reserve_start_index=reserve_start,terminal_soc_min_pct=reserve['terminal_soc_min_pct'] if reserve else None)


class AllocationRecords:
    def __init__(self, root, current_plan, configuration, fetch_inputs, *, clock=None):
        self.root = Path(root)
        self.current_plan, self.configuration, self.fetch_inputs = current_plan, configuration, fetch_inputs
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _directory(self, station, create=False):
        identity(station)
        directory = self.root/station
        if self.root.is_symlink() or directory.is_symlink():
            raise AllocationError(503, '分配记录目录无效')
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def get(self, station, allocation_id):
        identity(station, allocation_id)
        path = self._directory(station)/(allocation_id+'.json')
        if path.is_symlink():
            raise AllocationError(503, '分配记录路径无效')
        if not path.exists():
            raise AllocationError(404, '本站没有这条分配记录')
        try:
            record = json.loads(path.read_text())
            sha = record.pop('sha256')
            if sha != digest(record) or record['schema_version'] != SCHEMA or record['algorithm_version'] != ALGORITHM_VERSION:
                raise ValueError()
            if record['station_id'] != station or record['allocation_id'] != allocation_id or record['dispatch_status'] != 'not_dispatched' or record['usage'] != 'historical_preview_only':
                raise ValueError()
            identity(station, record['plan_run_id'])
            if record['inputs']['station_id'] != station or record['created_at'] != record['inputs']['now'] or record['input_sha256'] != digest(record['inputs']):
                raise ValueError()
            if allocate_cabinets(record['inputs']) != record['result']:
                raise ValueError()
            record['sha256'] = sha
            return record
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise AllocationError(503, '分配记录校验失败，未显示无效结果') from None

    def history(self, station, *, run_id=None, limit=10, offset=0):
        identity(station)
        if run_id is not None:
            identity(station, run_id)
        if type(limit) is not int or not 1<=limit<=20 or type(offset) is not int or not 0<=offset<=100000:
            raise AllocationError(400, '分页参数无效')
        directory = self._directory(station)
        items = []
        paths = sorted(directory.glob('*.json'), key=lambda p:(p.stat().st_mtime_ns,p.name), reverse=True) if directory.exists() else []
        for path in paths:
            try:
                r = self.get(station,path.stem)
                if run_id and r['plan_run_id'] != run_id:
                    continue
                slots = r['result']['slots']
                items.append(dict(allocation_id=r['allocation_id'],station_id=station,plan_run_id=r['plan_run_id'],
                    created_at=r['created_at'],date=r['inputs']['date'],source=r['source'],status='saved',
                    max_shortfall_kw=max(s['shortfallKw'] for s in slots),
                    unallocated_kwh=sum(s['shortfallKw']*s['durationHours'] for s in slots)))
            except AllocationError:
                if not run_id:
                    items.append(dict(allocation_id=path.stem,station_id=station,status='unreadable'))
        return dict(schema_version='m4-allocation-history-v1',station_id=station,items=items[offset:offset+limit],
                    next_offset=offset+limit if offset+limit<len(items) else None)

    def create(self, station, request_id, plan_run_id):
        identity(station,request_id);identity(station,plan_run_id)
        directory = self._directory(station,True)
        lockpath = directory/'.save.lock'
        if lockpath.is_symlink():
            raise AllocationError(503, '分配记录锁无效')
        with lockpath.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path = directory/(request_id+'.json')
            if path.exists() or path.is_symlink():
                existing = self.get(station,request_id)
                if existing['plan_run_id'] != plan_run_id:
                    raise AllocationError(409, '请求编号已用于另一份站级计划')
                return existing
            job = self.current_plan(station)
            if job.get('status') != 'completed' or job.get('run_id') != plan_run_id or job.get('station_id') != station:
                raise AllocationError(409, '当前站级计划已变化或尚未生成，请刷新')
            cfg = self.configuration(station)
            if cfg.version != job['request']['source_versions']['configuration']:
                raise AllocationError(409, '参数已变化，请重新生成站级计划')
            fresh = self.fetch_inputs(cfg)
            if fresh.get('station_id') != station or fresh.get('configuration_version') != cfg.version:
                raise AllocationError(409, '柜级取数来源发生变化，请刷新')
            now = self.clock()
            try:
                inputs = plan_input(job,fresh['sources']['realtime'],now)
                result = allocate_cabinets(inputs)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                # Only known validation messages; never expose arbitrary upstream exceptions.
                detail = str(error) if isinstance(error,ValueError) else '柜级输入不完整'
                raise AllocationError(409,detail) from None
            if not any(r['eligible'] for r in result['slots'][0]['rows']):
                raise AllocationError(409, '没有可参与的有效柜状态，请刷新后重试')
            current = self.current_plan(station)
            if self.configuration(station).version != cfg.version or current.get('status') != 'completed' or current.get('run_id') != plan_run_id or current.get('request') != job['request']:
                raise AllocationError(409, '计算期间站级计划或参数变化，未保存分配')
            comparison = job['result']['record']['daily_comparison']
            record = dict(schema_version=SCHEMA,algorithm_version=ALGORITHM_VERSION,allocation_id=request_id,station_id=station,
                plan_run_id=plan_run_id,plan_version=comparison['recommended']['plan_version'],source=comparison['status'],
                source_input_sha256=comparison['input_sha256'],created_at=now.isoformat(),usage='historical_preview_only',
                dispatch_status='not_dispatched',inputs=inputs,input_sha256=digest(inputs),result=result)
            record['sha256'] = digest(record)
            temporary = directory/('.'+str(uuid4())+'.tmp')
            try:
                fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as out:
                    out.write(encode(record));out.flush();os.fsync(out.fileno())
                os.replace(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.get(station,request_id)
