"""Single-row t_model integration. Scheduling only prepares previews.

No HTTP write path is connected to the API, scheduler or environment switches.
The guarded transport is reserved for a separately authorized execution phase.
"""
from datetime import datetime, timedelta
import json
import math
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

from shared.project import get_project
from .control_sources import _NoRedirect

ZONE = ZoneInfo('Asia/Shanghai')
POLICY = 'ems-single-model-preview-v1'


class ModelUpdateError(ValueError):
    pass


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(ZONE)
    except (AttributeError, TypeError, ValueError):
        raise ModelUpdateError('计划时间或记录更新时间无效。') from None


def validate_dispatch_safety(request, points):
    """Recompute physical bounds from forecasts and power, never reported grid/SOC."""
    from m4.optimizer.contracts import CapabilitySnapshot, OptimizationConstraints
    try:
        cap = CapabilitySnapshot.model_validate(request['capability'])
        bounds = OptimizationConstraints.model_validate(request['constraints'])
        sources = request['points']
        if len(sources) != len(points) or not cap.available:
            raise ValueError
        soc = cap.initial_soc_pct
        if not bounds.soc_min_pct <= soc <= bounds.soc_max_pct:
            raise ValueError
        for source, point in zip(sources, points, strict=True):
            if _stamp(source['timestamp']) != _stamp(point['timestamp']):
                raise ValueError
            load, pv, power = source['load_forecast_kw'], source['pv_forecast_kw'], point['target_power_kw']
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (load, pv, power)):
                raise ValueError
            mode = point['mode']
            if mode not in ('charge', 'discharge', 'idle') or mode == 'idle' and power != 0:
                raise ValueError
            charge = power if mode == 'charge' else 0.0
            discharge = power if mode == 'discharge' else 0.0
            if charge > cap.max_charge_kw+1e-6 or discharge > min(cap.max_discharge_kw, max(load-pv, 0.0))+1e-6:
                raise ValueError
            grid = max(load-pv+charge-discharge, 0.0)
            ceiling = bounds.demand_limit_kw
            if bounds.grid_import_limit_kw is not None:
                ceiling = min(ceiling, bounds.grid_import_limit_kw)
            if grid > ceiling+1e-6:
                raise ModelUpdateError('计划存在需量或购电上限缺口，不能生成下发预览。')
            soc += (charge*cap.charge_efficiency-discharge/cap.discharge_efficiency)*0.25/cap.energy_capacity_kwh*100
            if not bounds.soc_min_pct-1e-6 <= soc <= bounds.soc_max_pct+1e-6:
                raise ModelUpdateError('计划超出SOC安全范围，不能生成下发预览。')
    except ModelUpdateError:
        raise
    except (KeyError, TypeError, ValueError):
        raise ModelUpdateError('计划安全输入不完整或功率不满足约束。') from None


class EMSModelUpdateAdapter:
    def __init__(self, reader, *, opener=None):
        self.reader = reader
        self.opener = opener or build_opener(_NoRedirect())

    def _row(self, station):
        rows = [row for row in self.reader._read_table('t_model')
                if row.get('id') == station.ems_model_record_id]
        if len(rows) != 1 or rows[0].get('es_sn') != [station.source_code]:
            raise ModelUpdateError('指定计划记录缺失、重复或不属于本站。')
        row = rows[0]
        if row.get('type') not in ('charge', 'discharge') or row.get('repeat') != '每天重复':
            raise ModelUpdateError('现有记录的动作或重复规则无法解释。')
        _stamp(row.get('updatedAt'))
        return row

    def _context(self, station_id, payload, configuration, *, now=None):
        station = get_project().station(station_id)
        if station.ems_model_record_id is None:
            raise ModelUpdateError('本站未绑定充放计划记录。')
        now = (now or datetime.now(ZONE)).astimezone(ZONE)
        if (payload.get('station_id') != station_id or configuration.station_id != station_id
                or not configuration.parameters
                or payload.get('configuration_version') != configuration.version
                or payload.get('schema_version') != 'm4-rolling-plan-v1'
                or payload.get('usage') != 'remaining_day_advice_only'
                or payload.get('dispatch_status') != 'not_dispatched'):
            raise ModelUpdateError('滚动建议与本站当前配置不一致。')
        points = payload.get('plan')
        if not isinstance(points, list) or not points:
            raise ModelUpdateError('没有可适配的滚动时段。')
        start = _stamp(payload.get('effective_at'))
        next_slot = now.replace(minute=now.minute//15*15, second=0, microsecond=0)+timedelta(minutes=15)
        end = start+timedelta(minutes=15)
        if (start != next_slot or start.date() != now.date()
                or payload.get('date') != now.date().isoformat()
                or _stamp(points[0].get('timestamp')) != start
                or _stamp(payload.get('valid_until')) != end
                or _stamp(payload.get('finished_at')) > now):
            raise ModelUpdateError('滚动时段已过期或不是下一刻钟。')
        request = payload.get('request') or {}
        if (request.get('station_id') != station_id
                or request.get('capability', {}).get('available') is not True
                or request.get('source_versions', {}).get('configuration') != configuration.version
                or request.get('source_versions', {}).get('project_configuration') != get_project().fingerprint):
            raise ModelUpdateError('滚动输入电站不匹配。')
        try:
            run_id = str(UUID(payload['run_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ModelUpdateError('滚动建议版本无效。') from None
        mode, power = points[0].get('mode'), points[0].get('target_power_kw')
        if (mode not in ('charge', 'discharge', 'idle')
                or type(power) not in (int, float) or not math.isfinite(power) or power < 0
                or mode == 'idle' and power != 0):
            raise ModelUpdateError('滚动动作或功率无效。')
        if mode != 'idle':
            cap = request.get('capability', {}).get('max_'+mode+'_kw')
            if (type(cap) not in (int, float) or not math.isfinite(cap)
                    or power > min(cap, getattr(configuration.parameters, 'max_'+mode+'_kw'))+1e-7):
                raise ModelUpdateError('建议功率超过本站配置上限。')
        return station, now, start, end, run_id, points, request

    def preview(self, station_id, payload, configuration, *, now=None):
        station, observed, start, end, run_id, points, request = self._context(
            station_id, payload, configuration, now=now)
        mode, power = points[0]['mode'], points[0]['target_power_kw']
        if mode == 'idle':
            return dict(policy_version=POLICY, status='skipped', reason='待机时段不下发。',
                station_id=station_id, record_id=station.ems_model_record_id, run_id=run_id,
                dispatch_status='not_dispatched', network_write_performed=False, request=None)
        validate_dispatch_safety(request, points)
        row = self._row(station)
        # Reusing an old row is allowed; replacing a currently active row is not.
        from .ems_remaining_plan import _clock
        begin = _clock(row.get('start_time'), observed.date())
        finish = _clock(row.get('end_time'), observed.date(), end=True)
        if finish <= begin:
            raise ModelUpdateError('单记录预览不支持跨日或同起止时段。')
        if begin < start and finish > observed:
            raise ModelUpdateError('现有计划仍在执行，不能提前覆盖当前时段。')
        if now is None and datetime.now(ZONE) >= start:
            raise ModelUpdateError('读取计划记录期间已错过生效时间，不生成旧时段报文。')
        # Current t_model contract is per cabinet; never write station total as kw.
        body = dict(start_time=start.strftime('%H:%M:%S'), end_time=end.strftime('%H:%M:%S'),
                    type=mode,
                    kw=power/len(station.cabinet_sns), repeat='每天重复', es_sn=[station.source_code])
        predicate = {'$and': [
            {'id': {'$eq': station.ems_model_record_id}},
            {'es_sn': {'$eq': [station.source_code]}},
            {'updatedAt': {'$eq': row['updatedAt']}},
        ]}
        return dict(policy_version=POLICY, status='preview', dispatch_status='not_dispatched',
            station_id=station_id, record_id=station.ems_model_record_id, run_id=run_id,
            effective_at=start.isoformat(), expires_at=end.isoformat(),
            configuration_version=configuration.version, observed_updated_at=row['updatedAt'],
            power_scope='cabinet', station_power_kw=power, network_write_performed=False,
            request=dict(method='POST', path='t_model:update',
                         query={'filterByTk': station.ems_model_record_id,
                                'filter': json.dumps(predicate, ensure_ascii=False, separators=(',', ':'))},
                         body=body))

    def submit(self, station_id, payload, configuration, *, write_token=None,
               explicitly_enabled=False, now=None):
        """Future transport entry point. Never called by the current application.

        Rebuild from fresh ownership/version reads; never accept a caller's URL,
        filter or arbitrary preview body. A timeout is an uncertain result, not a
        retry instruction. Database readback is not device execution feedback.
        """
        if not explicitly_enabled:
            raise ModelUpdateError('真实计划写入未启用。')
        if not isinstance(write_token, str) or not write_token.strip() or any(c in write_token for c in '\r\n'):
            raise ModelUpdateError('未配置有效的写入凭据。')
        prepared = self.preview(station_id, payload, configuration, now=now)
        if prepared['status'] == 'skipped':
            return prepared
        if _stamp(prepared['effective_at']) <= (now or datetime.now(ZONE)):
            raise ModelUpdateError('已错过指令预定生效时间，不补发旧时段。')
        request = prepared['request']
        url = get_project().sources['m4_base_url'].rstrip('/')+'/'+request['path']+'?'+urlencode(request['query'])
        command = Request(url, data=json.dumps(request['body'], ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer '+write_token.strip(), 'Content-Type': 'application/json',
                     'Accept': 'application/json'}, method='POST')
        outcome = dict(station_id=station_id, record_id=prepared['record_id'], run_id=prepared['run_id'],
                       network_write_performed=True, status='update_unconfirmed', device_execution_status='unverified')
        try:
            # Exactly one POST; redirects and automatic retries are forbidden.
            with self.opener.open(command, timeout=8) as response:
                if response.status != 200:
                    return outcome
                raw = response.read(2*1024*1024+1)
                if len(raw) > 2*1024*1024:
                    return outcome
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get('errors'):
                    return outcome
            after = self._row(get_project().station(station_id))
            if (all(after.get(key) == value for key, value in request['body'].items())
                    and _stamp(after['updatedAt']) > _stamp(prepared['observed_updated_at'])):
                outcome['status'] = 'plan_update_readback_verified'
            return outcome
        except Exception:
            # Do not persist or expose credential-bearing request/exception bodies.
            return outcome
