"""Read-only reconciliation preview for NocoBase t_model CRUD.

Operations are a change set, not a safe execution order. Standard CRUD alone
does not establish atomic EMS activation or one-day expiry of recurring rows.
"""
from datetime import datetime, timedelta
import json
import math

from .ems_model_update import EMSModelUpdateAdapter, ModelUpdateError, ZONE, _stamp, validate_dispatch_safety
from .dispatch_power import dispatch_points

POLICY = 'ems-remaining-plan-preview-v1'


def matches_body(row, body):
    """Compare date-only fields with NocoBase's optional midnight encoding."""
    for key, expected in body.items():
        actual = row.get(key)
        if key == 'm4_plan_date' and isinstance(actual, str) and actual != expected:
            try:
                at = datetime.fromisoformat(actual.replace('Z', '+00:00'))
                if at.time() == datetime.min.time():
                    actual = at.date().isoformat()
            except ValueError:
                pass
        if actual != expected:
            return False
    return True


def _clock(value, day, *, end=False):
    if value == '24:00:00' and end:
        return datetime.combine(day+timedelta(days=1), datetime.min.time(), ZONE)
    try:
        if not isinstance(value, str) or len(value) != 8:
            raise ValueError
        at = datetime.strptime(value, '%H:%M:%S').time()
        return datetime.combine(day, at, ZONE)
    except (TypeError, ValueError):
        raise ModelUpdateError('现有计划的时间格式无效。') from None


def _explanation(point, source, request, *, startup=False):
    """Business purpose from the exact forecast used for this dispatched plan."""
    if startup:
        return '凌晨谷价时段充电，补充储能电量'
    load, pv = source['load_forecast_kw'], source['pv_forecast_kw']
    if point['mode'] == 'charge':
        if pv-load >= point['target_power_kw']-1e-6:
            return '利用预计光伏余电充电，供后续用电'
        if source['tariff_period'] == 'gu':
            return '谷价时段充电，补充后续用电所需电量'
        return '补充储能电量，供后续用电'
    limits = [request.get('constraints', {}).get(k)
              for k in ('demand_limit_kw', 'grid_import_limit_kw')]
    limits = [v for v in limits if type(v) in (int, float) and math.isfinite(v)]
    if limits and load-pv > min(limits)+1e-6:
        return '放电补充负荷供电，降低预计购电高峰'
    if source['tariff_period'] in ('feng', 'jian'):
        return '高价时段放电供负荷，减少高价购电'
    return '储能供应部分负荷，减少本时段电网购电'


def _mutation(action, row, station, *, body=None):
    # Array equality on es_sn does not match this NocoBase collection. Ownership
    # is checked before each POST; id + updatedAt guard the checked row remotely.
    predicate = {'$and': [{'id': {'$eq': row['id']}},
        {'updatedAt': {'$eq': row['updatedAt']}}]}
    if action == 'destroy' or action == 'update' and body and body.get('repeat') == '已过期':
        plan_id = row.get('m4_run_id')
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ModelUpdateError('现有计划没有计划 ID，保留该记录并暂停写入。')
        predicate['$and'].append({'m4_run_id': {'$eq': plan_id}})
    result = dict(method='POST', path='t_model:'+action,
        query=dict(filterByTk=row['id'], filter=json.dumps(predicate, ensure_ascii=False, separators=(',', ':'))))
    if body is not None:
        result['body'] = body
    return result


class EMSRemainingPlanAdapter:
    def __init__(self, reader, *, fixed_cabinet_power=False):
        self.reader = reader
        self.fixed_cabinet_power = fixed_cabinet_power
        self.single = EMSModelUpdateAdapter(reader)

    def preview(self, station_id, payload, configuration, *, now=None, fixed_cabinet_power=None):
        station, observed, start, _, run_id, points, request = self.single._context(
            station_id, payload, configuration, now=now)
        midnight = datetime.combine(observed.date()+timedelta(days=1), datetime.min.time(), ZONE)
        replacement_end = midnight
        startup = request.get('source_versions', {}).get('startup_policy')
        if startup:
            from .startup_admission import request_window
            try:
                replacement_end = request_window(request, observed)
            except (ValueError, KeyError, TypeError):
                raise ModelUpdateError('凌晨谷电保底窗口校验失败。') from None
            if (payload.get('source') != 'ems' or payload.get('end_at') != replacement_end.isoformat()
                    or any(p.get('mode') not in ('charge', 'idle') for p in points)):
                raise ModelUpdateError('凌晨保底只允许当前谷段充电或待机。')
        if len(points) != int((replacement_end-start).total_seconds()/900):
            raise ModelUpdateError('计划未完整覆盖本轮允许的替换窗口。')
        validate_dispatch_safety(request, points)
        points = dispatch_points(points)
        validate_dispatch_safety(request, points)
        if fixed_cabinet_power is None:
            fixed_cabinet_power = self.fixed_cabinet_power
        segments = []
        for index, point in enumerate(points):
            at = start+timedelta(minutes=15*index)
            if _stamp(point.get('timestamp')) != at:
                raise ModelUpdateError('剩余日计划时间不连续。')
            mode, power = point.get('mode'), point.get('target_power_kw')
            if (mode not in ('charge', 'discharge', 'idle') or type(power) not in (int, float)
                    or not math.isfinite(power) or power < 0 or mode == 'idle' and power != 0):
                raise ModelUpdateError('剩余日计划动作或功率无效。')
            if mode == 'idle' or power == 0:
                continue
            cap = request.get('capability', {}).get('max_'+mode+'_kw')
            if (type(cap) not in (int, float) or not math.isfinite(cap)
                    or power > min(cap, getattr(configuration.parameters, 'max_'+mode+'_kw'))+1e-7):
                raise ModelUpdateError('剩余日计划功率超过本站配置上限。')
            end = at+timedelta(minutes=15)
            kw = power/len(station.cabinet_sns)
            if fixed_cabinet_power:
                # Map the EMS setpoint using this slot's tariff; keep solver advice intact.
                kw = (station.m4['ems_discharge_kw'][request['points'][index]['tariff_period']]
                    if mode == 'discharge' else station.m4['ems_charge_kw'])
            reason = _explanation(point, request['points'][index], request, startup=bool(startup))
            if segments and segments[-1]['end'] == at and segments[-1]['mode'] == mode and segments[-1]['kw'] == kw:
                segments[-1]['end'] = end
                if reason not in segments[-1]['reasons']:
                    segments[-1]['reasons'].append(reason)
            else:
                segments.append(dict(start=at, end=end, mode=mode, kw=kw, reasons=[reason]))

        # Export control is independent of storage and uses its project setpoint.
        exports = []
        for index, point in enumerate(points):
            export = point.get('grid_export_kw', 0)
            if type(export) not in (int, float) or not math.isfinite(export) or export < 0:
                raise ModelUpdateError('预计上网功率无效。')
            if export <= .01 or startup:
                continue
            at = start+timedelta(minutes=15*index)
            reason = ('光伏发电满足负荷及储能充电后，剩余电量上网'
                      if point['mode'] == 'charge' else '光伏发电超过负荷需求，余电上网')
            if exports and exports[-1]['end'] == at:
                exports[-1]['end'] = at+timedelta(minutes=15)
                if reason not in exports[-1]['reasons']:
                    exports[-1]['reasons'].append(reason)
            else:
                exports.append(dict(start=at, end=at+timedelta(minutes=15),
                    mode='pv_surplus_export', kw=station.m4['ems_export_kw'],
                    reasons=[reason]))
        segments.extend(exports)
        lane = lambda mode: 'export' if mode == 'pv_surplus_export' else 'storage'
        rows = self.reader._read_table('t_model')
        future, preserved, identifiers = {}, [], set()
        carried, cutovers, windows = {}, {}, []
        for row in rows:
            owners = row.get('es_sn')
            if not isinstance(owners, list):
                raise ModelUpdateError('现有计划的电站归属格式无效。')
            if station.source_code not in owners:
                continue
            identifier = row.get('id')
            if owners != [station.source_code] or type(identifier) is not int or identifier <= 0 or identifier in identifiers:
                raise ModelUpdateError('现有计划存在共享电站、重复或无效记录。')
            identifiers.add(identifier)
            if row.get('repeat') == '已过期':
                preserved.append(identifier)
                continue
            if row.get('repeat') not in ('每天重复', '今日有效') or row.get('type') not in ('charge', 'discharge', 'pv_surplus_export'):
                raise ModelUpdateError('现有计划动作或重复规则无法解释。')
            _stamp(row.get('updatedAt'))
            if row.get('m4_run_id') and row.get('m4_plan_date'):
                try:
                    plan_date = datetime.fromisoformat(row['m4_plan_date'].replace('Z', '+00:00'))
                    if plan_date.time() != datetime.min.time():
                        raise ValueError
                except (KeyError, AttributeError, TypeError, ValueError):
                    raise ModelUpdateError('旧计划日期不明确，暂停计划替换。') from None
                if plan_date.date() < observed.date():
                    raise ModelUpdateError('往日计划尚未标记过期，需先完成有效期处理。')
            begin, end = _clock(row.get('start_time'), observed.date()), _clock(row.get('end_time'), observed.date(), end=True)
            if end == begin:
                raise ModelUpdateError('现有计划开始与结束时间相同。')
            if end < begin:
                if row['end_time'] != '00:00:00':
                    raise ModelUpdateError('现有跨日计划需先明确当天替换范围。')
                end = midnight
            if end <= start or begin >= replacement_end:
                preserved.append(identifier)
                continue
            if startup and end > replacement_end:
                raise ModelUpdateError('现有计划跨越凌晨保底边界，保留原计划并暂停本轮写入。')
            windows.append((identifier, begin, end, lane(row['type'])))
            if begin < start:
                first = next((s for s in segments if lane(s['mode']) == lane(row['type'])), None)
                try:
                    kw = float(row.get('kw'))
                except (ValueError, TypeError):
                    kw = float('nan')
                if isinstance(row.get('kw'), bool) or not math.isfinite(kw) or kw < 0:
                    raise ModelUpdateError('现有计划功率无效，无法确认连续切换方式。')
                if (not first or first['start'] != start or first['end'] != end
                        or first['mode'] != row['type'] or isinstance(row.get('kw'), bool)
                        or not math.isfinite(kw) or first['kw'] != kw):
                    cutovers[identifier] = row
                    continue
                carried[identifier] = row
                # Match only the remaining portion while preserving the actual
                # running record's original boundaries and power.
                begin = start
            if (begin, end, lane(row['type'])) in future:
                raise ModelUpdateError('现有未来计划时段重复。')
            future[begin, end, lane(row['type'])] = row

        for identifier, begin, end, category in windows:
            if identifier in (carried.keys() | cutovers.keys()) and any(other != identifier and other_category == category and b < end and e > begin
                                              for other, b, e, other_category in windows):
                raise ModelUpdateError('承接中的计划与其他时段重叠，需先确认连续切换方式。')

        operations = dict(create=[], update=[], destroy=[])
        # Apply these updates LAST, after all future records are read back.
        # Leave the elapsed portion and its original version ownership intact.
        for row in cutovers.values():
            operations['update'].append(_mutation('update', row, station,
                body={'end_time': start.strftime('%H:%M:%S'), 'repeat': '今日有效'}))
        unchanged, schedule = [], []
        for segment in segments:
            begin, end = segment['start'], segment['end']
            body = dict(start_time=begin.strftime('%H:%M:%S'), end_time=end.strftime('%H:%M:%S'),
                type=segment['mode'], kw=segment['kw'], repeat='今日有效', es_sn=[station.source_code],
                m4_run_id=run_id, m4_plan_date=start.date().isoformat(),
                explain='；'.join(segment['reasons'])+'。')
            row = future.pop((begin, end, lane(segment['mode'])), None)
            carrying = row is not None and row['id'] in carried
            if carrying:
                body.update(start_time=row['start_time'], end_time=row['end_time'])
                body['kw'] = row['kw']
            schedule.append(dict(record_id=row['id'] if row else None,
                start_at=_clock(body['start_time'], start.date()).isoformat(),
                end_at=end.isoformat(), **body))
            if row is None:
                operations['create'].append(dict(method='POST', path='t_model:create', query={}, body=body))
            elif matches_body(row, body):
                unchanged.append(row['id'])
            else:
                update = {k: body[k] for k in ('m4_run_id', 'm4_plan_date', 'repeat', 'explain')} if carrying else body
                operations['update'].append(_mutation('update', row, station, body=update))
        # Includes old future charge/discharge rows in newly idle gaps. Omitting
        # idle creates must not leave an old conflicting instruction in place.
        for row in future.values():
            operations['destroy'].append(_mutation('destroy', row, station))
        if now is None and datetime.now(ZONE) >= start:
            raise ModelUpdateError('读取期间已错过方案生效时刻，不生成旧计划变更。')
        return dict(policy_version=POLICY, status='preview', station_id=station_id, run_id=run_id,
            effective_at=start.isoformat(), end_at=replacement_end.isoformat(), power_scope='cabinet',
            configuration_version=configuration.version, schedule=schedule, operations=operations,
            preserved_record_ids=preserved, unchanged_record_ids=unchanged,
            carried_record_ids=list(carried),
            cutover_record_ids=list(cutovers),
            dispatch_status='not_dispatched', network_write_performed=False, execution_ready=False,
            activation_requirements=['atomic_plan_activation_unverified', 'ems_today_valid_expiry_unverified'],
            execution_order=None)
