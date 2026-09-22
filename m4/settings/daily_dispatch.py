"""Day-plan dispatch contracts; solver advice and EMS setpoints stay separate."""
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID

from shared.project import get_project
from m4.optimizer.contracts import GRID_CHARGING_POLICY
from .daily_policy import matches_current_daily_policy
from .dispatch_power import dispatch_points
from .ems_model_update import ModelUpdateError, ZONE, _stamp, validate_dispatch_safety
from .ems_remaining_plan import EMSRemainingPlanAdapter, _mutation
from .night_charging import POLICY as NIGHT_POLICY, validate_freshness

SCHEMA = 'm4-daily-dispatch-v1'


def daily_payload(daily, configuration, now=None):
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    comparison = (daily.get('result') or {}).get('record', {}).get('daily_comparison', {})
    recommended = comparison.get('recommended')
    if (daily.get('status') != 'completed' or comparison.get('date') != now.date().isoformat()
            or not recommended or daily.get('request', {}).get('source_versions', {}).get('startup_policy')):
        raise ModelUpdateError('当天完整日计划尚未就绪。')
    request = deepcopy(daily['request'])
    full_request = deepcopy(request)
    if (not matches_current_daily_policy(configuration.station_id, request)
            or request['source_versions'].get('configuration') != configuration.version):
        raise ModelUpdateError('日计划配置已变化，请重新生成。')
    points = deepcopy(recommended['plan'])
    if len(points) != 96 or len(request['points']) != 96:
        raise ModelUpdateError('日计划未完整覆盖当天。')
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for i, (point, source) in enumerate(zip(points, request['points'], strict=True)):
        stamp = midnight + timedelta(minutes=15*i)
        if _stamp(point.get('timestamp')) != stamp or _stamp(source.get('timestamp')) != stamp:
            raise ModelUpdateError('日计划时段不连续。')
    validate_dispatch_safety(request, points)
    # Validate the thresholded advice, not a fictional constant-power EMS replay.
    validate_dispatch_safety(request, dispatch_points(points))
    start = now.replace(minute=now.minute//15*15, second=0, microsecond=0)+timedelta(minutes=15)
    index = int((start-midnight).total_seconds()/900)
    if index >= 96:
        raise ModelUpdateError('当天已无完整的未来时段。')
    # This remains the daily forecast trajectory, NOT a new measured-SOC forecast.
    request['capability']['initial_soc_pct'] = points[index-1]['expected_soc_pct']
    request['points'] = request['points'][index:]
    request.update(plan_start_at=start.isoformat(), horizon_points=96-index)
    return dict(schema_version=SCHEMA, usage='daily_schedule_table_only', dispatch_status='not_dispatched',
        kind='daily', station_id=configuration.station_id, date=now.date().isoformat(),
        run_id=daily['run_id'], daily_run_id=daily['run_id'], configuration_version=configuration.version,
        effective_at=start.isoformat(), valid_until=(start+timedelta(minutes=15)).isoformat(),
        end_at=(midnight+timedelta(days=1)).isoformat(), finished_at=now.isoformat(),
        source=comparison.get('recommended_source'), request=request, full_request=full_request, plan=points[index:],
        prediction_basis='daily_solver_advice',
        ems_setpoints_kw=dict(charge=600, discharge=dict(jian=600, feng=600, ping=540, gu=600)),
        device_execution_status='unverified')


def night_payload(payload):
    payload = deepcopy(payload)
    payload.update(schema_version=SCHEMA, usage='daily_schedule_table_only', kind='night_fallback',
        reason='日计划未就绪，凌晨谷电保底充电。', ems_setpoint_kw=600)
    return payload


class DailyDispatchContext:
    def _context(self, station_id, payload, configuration, *, now=None):
        now = (now or datetime.now(ZONE)).astimezone(ZONE)
        station = get_project().station(station_id)
        request = payload.get('request') or {}
        versions = request.get('source_versions', {})
        if (station.ems_model_record_id is None or not configuration.parameters
                or configuration.station_id != station_id or payload.get('station_id') != station_id
                or payload.get('schema_version') != SCHEMA
                or payload.get('usage') != 'daily_schedule_table_only'
                or payload.get('dispatch_status') != 'not_dispatched'
                or payload.get('configuration_version') != configuration.version
                or versions.get('configuration') != configuration.version
                or versions.get('project_configuration') != get_project().fingerprint
                or versions.get('grid_charging_policy') != GRID_CHARGING_POLICY
                or request.get('station_id') != station_id
                or request.get('capability', {}).get('available') is not True):
            raise ModelUpdateError('日计划下发来源或配置不一致。')
        kind = payload.get('kind')
        if kind == 'daily':
            if (payload.get('daily_run_id') != payload.get('run_id')
                    or versions.get('startup_policy')
                    or not matches_current_daily_policy(station_id, payload.get('full_request'))
                    or payload['full_request']['source_versions'] != versions):
                raise ModelUpdateError('日计划版本已失效。')
        elif kind == 'night_fallback':
            if versions.get('night_charging_policy') != NIGHT_POLICY:
                raise ModelUpdateError('凌晨保底来源无效。')
            validate_freshness(payload, configuration, now)
        else:
            raise ModelUpdateError('不支持此计划下发类型。')
        start = _stamp(payload.get('effective_at'))
        next_slot = now.replace(minute=now.minute//15*15, second=0, microsecond=0)+timedelta(minutes=15)
        end = start+timedelta(minutes=15)
        points = payload.get('plan')
        if (start != next_slot or start.date() != now.date() or payload.get('date') != now.date().isoformat()
                or _stamp(payload.get('valid_until')) != end or _stamp(payload.get('finished_at')) > now
                or not isinstance(points, list) or not points or _stamp(points[0].get('timestamp')) != start):
            raise ModelUpdateError('日计划下发窗口已变化，请重新读取。')
        try:
            run_id = str(UUID(payload['run_id']))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ModelUpdateError('日计划下发版本无效。') from None
        return station, now, start, end, run_id, points, request


class DailyScheduleAdapter(EMSRemainingPlanAdapter):
    def __init__(self, reader):
        super().__init__(reader, fixed_cabinet_power=True)
        self.single = DailyDispatchContext()

    def preview(self, station_id, payload, configuration, *, now=None, fixed_cabinet_power=None):
        if payload.get('kind') != 'expire':
            return super().preview(station_id, payload, configuration, now=now, fixed_cabinet_power=True)
        now = (now or datetime.now(ZONE)).astimezone(ZONE)
        if (payload.get('station_id') != station_id or configuration.station_id != station_id
                or payload.get('date') != now.date().isoformat()
                or payload.get('schema_version') != SCHEMA):
            raise ModelUpdateError('旧计划清理日期或电站不一致。')
        UUID(payload['run_id'])
        station = get_project().station(station_id)
        operations = dict(create=[], update=[], destroy=[])
        identifiers = set()
        for row in self.reader._read_table('t_model'):
            owners = row.get('es_sn')
            if not isinstance(owners, list):
                raise ModelUpdateError('旧计划电站归属无效。')
            if station.source_code not in owners:
                continue
            if (owners != [station.source_code] or type(row.get('id')) is not int
                    or row['id'] <= 0 or row['id'] in identifiers):
                raise ModelUpdateError('旧计划记录归属或标识无效。')
            identifiers.add(row['id'])
            _stamp(row.get('updatedAt'))
            if row.get('repeat') not in ('每天重复', '今日有效') or row.get('type') not in ('charge', 'discharge', 'pv_surplus_export'):
                raise ModelUpdateError('旧计划动作或有效期规则无法解释。')
            if not row.get('m4_run_id'):
                if row.get('repeat') == '每天重复':
                    operations['update'].append(_mutation('update', row, station, body={'repeat': '今日有效'}))
                continue
            UUID(row['m4_run_id'])
            day = row.get('m4_plan_date')
            try:
                # NocoBase may serialize a date field as midnight ISO text.
                date_value = datetime.fromisoformat(day.replace('Z', '+00:00'))
                if date_value.time() != datetime.min.time():
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise ModelUpdateError('旧计划日期不明确，暂停自动清理。') from None
            _stamp(row.get('updatedAt'))
            if date_value.date() < now.date():
                operations['destroy'].append(_mutation('destroy', row, station))
            elif date_value.date() == now.date() and row.get('repeat') == '每天重复':
                operations['update'].append(_mutation('update', row, station, body={'repeat': '今日有效'}))
        return dict(status='preview', run_id=payload['run_id'], station_id=station_id,
            effective_at=payload['effective_at'], configuration_version=configuration.version,
            operations=operations, schedule=[], cutover_record_ids=[])
