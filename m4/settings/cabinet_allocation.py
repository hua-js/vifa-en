"""Versioned cabinet projection for archived previews, never device control."""
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo
from .roster import STATION_CABINETS

ALGORITHM_VERSION = 'cabinet-energy-weighted-v1'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None, '采样或计算时间缺少时区')
    return parsed


def allocate_cabinets(data):
    station, snapshot = data['station_id'], data['snapshot']
    require(station in STATION_CABINETS and snapshot.get('station_id') == station, '柜级数据电站不匹配')
    roster = list(STATION_CABINETS[station][1])
    require(data['configuration_version'] and data['controls_version']
        and snapshot.get('configuration_version') == data['configuration_version']
        and snapshot.get('capacity_source_version') == data['controls_version']
        and snapshot.get('power_limits_source_version') == data['controls_version'], '柜级数据与计划配置版本不一致')
    now = timestamp(data['now'])
    start = datetime.fromisoformat(data['date'] + 'T00:00:00+08:00')
    require(start <= now < start + timedelta(days=1), '柜级分配仅支持当前日期')
    points, bounds = data['points'], data['bounds']
    require(isinstance(points, list) and len(points) == 96 and all(number(p.get('power')) for p in points), '站级计划不完整')
    grid_limit = data.get('grid_import_limit_kw')
    if grid_limit is not None:
        require(number(grid_limit) and grid_limit >= 0 and all(number(p.get('load')) and p['load'] >= 0
            and number(p.get('pv')) and p['pv'] >= 0 for p in points), '需量或预测数据无效')
    low, high = bounds['soc_min_pct'], bounds['soc_max_pct']
    ce, de = data['charge_efficiency'], data['discharge_efficiency']
    require(all(number(v) for v in (low, high, ce, de)) and 0 <= low < high <= 100 and 0 < ce <= 1 and 0 < de <= 1, 'SOC或效率无效')
    require(number(data['max_age_seconds']) and data['max_age_seconds'] > 0, '柜级采样时效不可用')
    age_limit = min(300, data['max_age_seconds'])
    limits = snapshot.get('cabinet_power_limits', {})
    require(all(number(limits.get(k)) and limits[k] >= 0 for k in ('max_charge_kw', 'max_discharge_kw'))
        and all(number(snapshot.get(k)) and snapshot[k] >= 0 for k in ('available_max_charge_kw', 'available_max_discharge_kw')), '柜级功率限制无效')
    cabinets = snapshot.get('cabinets')
    require(isinstance(cabinets, list) and len(cabinets) == len(roster)
        and set(c.get('emu_sn') for c in cabinets) == set(roster), '柜级名单缺失或重复')
    reserve_start, reserve_soc = data.get('reserve_start_index'), data.get('terminal_soc_min_pct')
    if reserve_start is not None:
        require(type(reserve_start) is int and 0 <= reserve_start < 96 and number(reserve_soc) and low <= reserve_soc <= high, '晚峰保留参数无效')
    states = []
    for identity in roster:
        c = next(c for c in cabinets if c['emu_sn'] == identity)
        capacity, soc = c.get('capacity_kwh'), c.get('soc_pct')
        require(number(capacity) and capacity > 0, '柜级容量不可用')
        reasons = list(c.get('issues') or [])
        if snapshot.get('available') is not True:
            reasons.append('站级实时状态未通过准入')
        if c.get('available') is not True:
            reasons.append('柜状态未通过准入')
        if not number(soc) or not low <= soc <= high:
            reasons.append('SOC缺失或越界')
        try:
            age = (now - timestamp(c['observed_at'])).total_seconds()
            if not 0 <= age <= age_limit:
                reasons.append('SOC采样已过期或时间异常')
        except (KeyError, TypeError, ValueError, AttributeError):
            reasons.append('SOC采样时间不可用')
        power_limits = {}
        for key in ('max_charge_kw', 'max_discharge_kw'):
            value = c.get(key, limits[key])
            require(number(value) and value >= 0, '单柜功率限制无效')
            power_limits[key] = min(value, limits[key])
        states.append(dict(id=identity, capacity=capacity, energy=capacity*soc/100 if number(soc) else 0,
                           eligible=not reasons, reasons=reasons, **power_limits))
    first = int((now-start).total_seconds() // 900)
    slots = []
    for index in range(first, 96):
        at, end = max(now, start+timedelta(minutes=index*15)), start+timedelta(minutes=(index+1)*15)
        hours = (end-at).total_seconds()/3600
        target = points[index]['power']
        sign = 1 if target > 0 else -1 if target < 0 else 0
        charge = sign < 0
        floor = max(low, reserve_soc) if reserve_start is not None and index >= reserve_start else low
        energy = [max(0, c['capacity']*high/100-c['energy'] if charge else c['energy']-c['capacity']*floor/100)
                  if c['eligible'] else 0 for c in states]
        caps = [min(c['max_charge_kw'] if charge else c['max_discharge_kw'],
                    energy[i]/(ce*hours) if charge else energy[i]*de/hours) if c['eligible'] else 0 for i,c in enumerate(states)]
        net = points[index].get('load', 0)-points[index].get('pv', 0)
        grid_room = math.inf if grid_limit is None else max(0, grid_limit-net if charge else net)
        remaining = min(abs(target), grid_room, snapshot['available_max_charge_kw' if charge else 'available_max_discharge_kw'], sum(caps))
        allocated = [0.0]*len(states)
        for _ in range(len(states)+1):
            active = [i for i in range(len(states)) if caps[i]-allocated[i] > 1e-8 and energy[i] > 0]
            weight = sum(energy[i] for i in active)
            if remaining <= 1e-8 or weight <= 0:
                break
            assigned = 0
            for i in active:
                value = min(caps[i]-allocated[i], remaining*energy[i]/weight)
                allocated[i] += value
                assigned += value
            if assigned < 1e-10:
                break
            remaining = max(0, remaining-assigned)
        rows = []
        for i,c in enumerate(states):
            power = sign*allocated[i]
            start_soc = c['energy']/c['capacity']*100 if c['eligible'] else None
            if c['eligible']:
                c['energy'] += allocated[i]*hours*ce if charge else -allocated[i]*hours/de
            end_soc = c['energy']/c['capacity']*100 if c['eligible'] else None
            require(not c['eligible'] or low-1e-7 <= end_soc <= high+1e-7, '柜级能量校验失败')
            rows.append(dict(id=c['id'], eligible=c['eligible'], powerKw=power, startSocPct=start_soc,
                endSocPct=end_soc, reasons=list(c['reasons']), mode='excluded' if not c['eligible'] else 'charge' if power < -1e-8 else 'discharge' if power > 1e-8 else 'idle'))
        slots.append(dict(index=index, startMs=at.timestamp()*1000, endMs=end.timestamp()*1000, durationHours=hours,
            targetKw=target, allocatedKw=sign*sum(allocated), shortfallKw=max(0, abs(target)-sum(allocated)),
            reserveShortfallKwh=sum(max(0, c['capacity']*floor/100-c['energy']) for c in states if c['eligible'])
                if reserve_start is not None and index >= reserve_start else 0, rows=rows))
    return dict(stationId=station, date=data['date'], startIndex=first, anchorMs=now.timestamp()*1000, slots=slots)
