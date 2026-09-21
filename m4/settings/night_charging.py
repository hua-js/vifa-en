"""Early-valley EMS charging from current telemetry, without forecast admission."""
from datetime import datetime, timedelta
import json
import math

from shared.project import get_project
from m4.optimizer.contracts import CapabilitySnapshot, OptimizationConstraints, GRID_CHARGING_POLICY
from .ems_simulation import _slot
from .frozen_baseline import baseline_configuration, baseline_version, planning_controls
from .realtime import build_realtime_snapshot, SOURCE_FIELDS
from .schedule_power import effective_station_power, station_energy_capacity, station_schedule
from .startup_admission import POLICY as WINDOW_POLICY, ZONE, window
from .dispatch_power import MIN_DISPATCH_POWER_KW

POLICY = 'night-valley-direct-v1'


def configured_window(station, now):
    """Scheduling hint only; actual tariff is independently checked before writing."""
    if get_project().id != 'vifa' or station != 'station-2':
        return False
    baseline = baseline_configuration(station)
    if not baseline:
        return False
    slot = now.astimezone(ZONE).hour*4+now.minute//15
    for row in baseline['schedule']:
        a, b = _slot(row['start_time']), _slot(row['end_time'])
        if row['mode'] == 'charge' and row['power_kw'] > 0 and 0 < b <= 48:
            begin = a if a < b else 0
            if begin <= slot and slot+1 < b:
                return True
    return False


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError('凌晨充电实时功率无效。')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('凌晨充电实时功率无效。')
    return value


def read_window(live, station, now):
    if not configured_window(station, now):
        return None
    midnight = now.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    tariff = live._tariff(midnight)
    return tariff if window(station, tariff.get('period_types', []), now) else None


def build_payload(live, configuration, run_id, *, now=None, tariff=None):
    station = configuration.station_id
    began = (now or datetime.now(ZONE)).astimezone(ZONE)
    if not configuration.parameters or not configured_window(station, began):
        raise ValueError('当前不在凌晨充电窗口。')
    midnight = began.replace(hour=0, minute=0, second=0, microsecond=0)
    tariff = tariff if tariff is not None else live._tariff(midnight)
    periods = tariff.get('period_types', [])
    allowed = window(station, periods, began)
    if not allowed:
        raise ValueError('当前不是已配置的凌晨谷电充电时段。')
    controls = planning_controls(live._controls(station, validate_schedule=False))
    project_station = get_project().station(station)
    # These reads deliberately do not use LiveInputService.fetch/DailyInputService.fetch.
    devices = live.client.list_rows('t_emu', fields=','.join((*SOURCE_FIELDS, 'latest_power')),
        filters={'$and': [{'f_es_sn': {'$eq': project_station.source_code}},
            {'emu_sn': {'$in': list(project_station.cabinet_sns)}}]}, sort='emu_sn', page_size=100)
    stations = live.client.list_rows('t_es_data', fields='timestamp,es_sn,load_power,grid_power',
        filters={'es_sn': {'$eq': project_station.source_code}}, sort='-timestamp', page_size=1, limit=1)
    observed = (now or datetime.now(ZONE)).astimezone(ZONE)
    if window(station, periods, observed) != allowed:
        raise ValueError('读取期间充电窗口已变化，等待下一轮。')
    start, end = (datetime.fromisoformat(allowed[k]) for k in ('effective_at', 'end_at'))
    parameters = configuration.parameters
    max_age = min(parameters.max_input_age_seconds, 900)
    capacity = station_energy_capacity(configuration, controls)
    limits = effective_station_power(configuration, controls)
    snapshot_config = configuration.model_copy(update={'parameters': parameters.model_copy(update={
        'energy_capacity_kwh': capacity, 'max_input_age_seconds': max_age, **limits})})
    snapshot = build_realtime_snapshot(snapshot_config, devices, now=observed)
    if not snapshot['available'] or snapshot['participation_status'] != 'full':
        raise ValueError('储能柜状态或SOC不满足凌晨充电条件。')
    if len(stations) != 1 or stations[0].get('es_sn') != project_station.source_code:
        raise ValueError('本站实时负荷缺失，暂停凌晨充电。')
    sample = stations[0]
    at = datetime.fromisoformat(sample['timestamp'].replace('Z', '+00:00'))
    if at.utcoffset() is None or not 0 <= (observed-at).total_seconds() <= max_age:
        raise ValueError('本站实时负荷已过期，暂停凌晨充电。')
    load, grid = _number(sample['load_power']), _number(sample['grid_power'])
    if load < 0:
        raise ValueError('本站实时负荷无效，暂停凌晨充电。')
    rows = {r['emu_sn']: r for r in devices if r.get('f_es_sn') == project_station.source_code}
    count = len(project_station.cabinet_sns)
    cabinet_capacity = capacity/count
    powers, socs = [], []
    for cabinet in snapshot['cabinets']:
        power = _number(rows[cabinet['emu_sn']]['latest_power'])
        if not -limits['max_charge_kw']/count <= power <= limits['max_discharge_kw']/count:
            raise ValueError('当前储能功率超出配置范围，暂停凌晨充电。')
        age = (start-datetime.fromisoformat(cabinet['observed_at'])).total_seconds()/3600
        delta = -power*parameters.charge_efficiency if power < 0 else -power/parameters.discharge_efficiency
        soc = cabinet['soc_pct']+delta*age/cabinet_capacity*100
        if not parameters.soc_min_pct <= soc <= parameters.soc_max_pct:
            raise ValueError('充电生效时刻SOC不满足安全范围。')
        powers.append(power)
        socs.append(soc)
    # Do not credit future solar generation. This is a live-load hold assumption,
    # explicitly identified below, never a fabricated M3 forecast.
    held_load = max(load, grid+sum(powers), 0.0)
    constraints = OptimizationConstraints(**{k: getattr(parameters, k) for k in (
        'soc_min_pct', 'soc_max_pct', 'preferred_soc_min_pct', 'preferred_soc_max_pct',
        'terminal_soc_tolerance_pct', 'cycle_cost_per_kwh')},
        demand_limit_kw=controls['demand']['need_kw'],
        grid_import_limit_kw=parameters.grid_import_limit_kw,
        grid_export_enabled=False, grid_export_limit_kw=0.0)
    ceiling = min(constraints.demand_limit_kw, constraints.grid_import_limit_kw
        if constraints.grid_import_limit_kw is not None else constraints.demand_limit_kw)
    if held_load > ceiling:
        raise ValueError('当前负荷已达需量限制，暂停凌晨充电。')
    baseline = baseline_configuration(station)
    target = min(parameters.soc_max_pct, baseline['soc_max_pct'])
    schedule = station_schedule(controls)
    capability = CapabilitySnapshot(available=True, initial_soc_pct=sum(socs)/count,
        energy_capacity_kwh=capacity, charge_efficiency=parameters.charge_efficiency,
        discharge_efficiency=parameters.discharge_efficiency, **limits)
    inputs, plan = [], []
    for i in range(int((end-start).total_seconds()/900)):
        stamp = start+timedelta(minutes=15*i)
        slot = stamp.hour*4+stamp.minute//15
        matching = [r for r in schedule if r['mode'] == 'charge' and (
            _slot(r['start_time']) <= slot < _slot(r['end_time']) if _slot(r['start_time']) < _slot(r['end_time'])
            else slot >= _slot(r['start_time']) or slot < _slot(r['end_time']))]
        if len(matching) != 1:
            raise ValueError('凌晨充电时段配置不完整。')
        headroom = max(0.0, target-max(socs))/100*capacity/(.25*parameters.charge_efficiency)
        power = min(matching[0]['power_kw'], limits['max_charge_kw'], ceiling-held_load, headroom)
        power = power if power > MIN_DISPATCH_POWER_KW else 0.0
        socs = [s+power*.25*parameters.charge_efficiency/capacity*100 for s in socs]
        inputs.append(dict(timestamp=stamp.isoformat(), load_forecast_kw=held_load,
            pv_forecast_kw=0.0, tariff_period='gu'))
        plan.append(dict(timestamp=stamp.isoformat(), mode='charge' if power else 'idle',
            target_power_kw=power, expected_soc_pct=sum(socs)/count, grid_import_kw=held_load+power))
    request = dict(station_id=station, plan_start_at=start.isoformat(), points=inputs,
        capability=capability.model_dump(mode='json'), constraints=constraints.model_dump(mode='json'),
        source_versions=dict(configuration=configuration.version, project_configuration=get_project().fingerprint,
            grid_charging_policy=GRID_CHARGING_POLICY,
            ems_baseline=baseline_version(station), controls=controls['version'], tariff=tariff['version'],
            startup_policy=WINDOW_POLICY, startup_window_end=end.isoformat(),
            startup_tariff_periods=json.dumps(periods), night_charging_policy=POLICY,
            planning_basis=POLICY, load_basis='current-load-held-no-pv-credit'))
    return dict(schema_version='m4-rolling-plan-v1', station_id=station, date=start.date().isoformat(),
        run_id=run_id, daily_run_id=None, configuration_version=configuration.version,
        daily_gate_passed=False, daily_net_savings_yuan=None, source='ems',
        effective_at=start.isoformat(), valid_until=(start+timedelta(minutes=15)).isoformat(),
        end_at=end.isoformat(), finished_at=observed.isoformat(),
        reason='凌晨谷电充电，按实时状态更新。', request=request, plan=plan,
        anchor=dict(observed_at=snapshot['observed_at'], load_observed_at=at.isoformat(), measured_soc_pct=snapshot['initial_soc_pct'],
            measured_power_kw=sum(powers), projected_soc_pct=capability.initial_soc_pct),
        dispatch_status='not_dispatched', usage='remaining_day_advice_only')


def validate_freshness(payload, configuration, now):
    """Recheck both telemetry sources after potentially slow planning reads."""
    max_age = min(configuration.parameters.max_input_age_seconds, 900)
    for key in ('observed_at', 'load_observed_at'):
        at = datetime.fromisoformat(payload['anchor'][key])
        if at.utcoffset() is None or not 0 <= (now-at).total_seconds() <= max_age:
            raise ValueError('实时数据已过期，等待下一轮凌晨充电计划。')
