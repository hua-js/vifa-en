"""Read-only reconciliation preview for NocoBase t_model CRUD.

Operations are a change set, not a safe execution order. Standard CRUD alone
does not establish atomic EMS activation or one-day expiry of recurring rows.
"""
from datetime import datetime, timedelta
import json
import math

from .ems_model_update import EMSModelUpdateAdapter, ModelUpdateError, ZONE, _stamp, validate_dispatch_safety

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


def _mutation(action, row, station, *, body=None):
    # Array equality on es_sn does not match this NocoBase collection. Ownership
    # is checked before each POST; id + updatedAt guard the checked row remotely.
    predicate = {'$and': [{'id': {'$eq': row['id']}},
        {'updatedAt': {'$eq': row['updatedAt']}}]}
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
        if len(points) != int((midnight-start).total_seconds()/900):
            raise ModelUpdateError('剩余日计划未完整覆盖至当天结束。')
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
            kw = (100 if mode == 'charge' else 90) if fixed_cabinet_power else power/len(station.cabinet_sns)
            if fixed_cabinet_power and station_id == 'station-2':
                # t_model stores per-cabinet kW; commissioning uses 600 kW total in either mode.
                kw = 600 / len(station.cabinet_sns)
            if segments and segments[-1]['end'] == at and segments[-1]['mode'] == mode and segments[-1]['kw'] == kw:
                segments[-1]['end'] = end
            else:
                segments.append(dict(start=at, end=end, mode=mode, kw=kw))

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
            if row.get('repeat') != '每天重复' or row.get('type') not in ('charge', 'discharge'):
                raise ModelUpdateError('现有计划动作或重复规则无法解释。')
            _stamp(row.get('updatedAt'))
            begin, end = _clock(row.get('start_time'), observed.date()), _clock(row.get('end_time'), observed.date(), end=True)
            if end == begin:
                raise ModelUpdateError('现有计划开始与结束时间相同。')
            if end < begin:
                if row['end_time'] != '00:00:00':
                    raise ModelUpdateError('现有跨日计划需先明确当天替换范围。')
                end = midnight
            if end <= start:
                preserved.append(identifier)
                continue
            windows.append((identifier, begin, end))
            if begin < start:
                first = segments[0] if segments else None
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
            if (begin, end) in future:
                raise ModelUpdateError('现有未来计划时段重复。')
            future[begin, end] = row

        for identifier, begin, end in windows:
            if identifier in (carried.keys() | cutovers.keys()) and any(other != identifier and b < end and e > begin
                                              for other, b, e in windows):
                raise ModelUpdateError('承接中的计划与其他时段重叠，需先确认连续切换方式。')

        operations = dict(create=[], update=[], destroy=[])
        # Apply these updates LAST, after all future records are read back.
        # Leave the elapsed portion and its original version ownership intact.
        for row in cutovers.values():
            operations['update'].append(_mutation('update', row, station,
                body={'end_time': start.strftime('%H:%M:%S')}))
        unchanged, schedule = [], []
        for segment in segments:
            begin, end = segment['start'], segment['end']
            body = dict(start_time=begin.strftime('%H:%M:%S'), end_time=end.strftime('%H:%M:%S'),
                type=segment['mode'], kw=segment['kw'], repeat='每天重复', es_sn=[station.source_code],
                m4_run_id=run_id, m4_plan_date=start.date().isoformat())
            row = future.pop((begin, end), None)
            carrying = row is not None and row['id'] in carried
            if carrying:
                body.update(start_time=row['start_time'], end_time=row['end_time'], kw=row['kw'])
            schedule.append(dict(start_at=begin.isoformat(), end_at=end.isoformat(), **body))
            if row is None:
                operations['create'].append(dict(method='POST', path='t_model:create', query={}, body=body))
            elif matches_body(row, body):
                unchanged.append(row['id'])
            else:
                update = {k: body[k] for k in ('m4_run_id', 'm4_plan_date')} if carrying else body
                operations['update'].append(_mutation('update', row, station, body=update))
        # Includes old future charge/discharge rows in newly idle gaps. Omitting
        # idle creates must not leave an old conflicting instruction in place.
        for row in future.values():
            operations['destroy'].append(_mutation('destroy', row, station))
        if now is None and datetime.now(ZONE) >= start:
            raise ModelUpdateError('读取期间已错过方案生效时刻，不生成旧计划变更。')
        return dict(policy_version=POLICY, status='preview', station_id=station_id, run_id=run_id,
            effective_at=start.isoformat(), end_at=midnight.isoformat(), power_scope='cabinet',
            configuration_version=configuration.version, schedule=schedule, operations=operations,
            preserved_record_ids=preserved, unchanged_record_ids=unchanged,
            carried_record_ids=list(carried),
            cutover_record_ids=list(cutovers),
            dispatch_status='not_dispatched', network_write_performed=False, execution_ready=False,
            activation_requirements=['atomic_plan_activation_unverified', 'daily_repeat_expiry_unverified'],
            execution_order=None)
