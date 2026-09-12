"""Read and validate real scheduling inputs without running or dispatching plans."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from m4.optimizer.contracts import ForecastPoint
from .adapter import build_request as build_optimizer_request
from .control_sources import ControlSourceError
from .forecast_source import load_forecast
from .load_accuracy import require_gate
from .pv_forecast_source import ForecastRefreshRequired, load_pv_forecast, validate_source as validate_pv_source
from .models import LiveStationState, ResolvedControlLimits, StationConfiguration
from .realtime import ALARM_POLICY, SOURCE_FIELDS, alarm_is_advisory, build_realtime_snapshot
from .roster import CABINET_ISOLATION_POLICY, STATION_CABINETS
from .schedule_power import SCHEDULE_POWER_POLICY, cabinet_power_limits, effective_station_power, station_energy_capacity
from .timeseries import InputDataError, build_pv_reference, build_tariff_points
from .upstream import SourceReadError


SHANGHAI = ZoneInfo('Asia/Shanghai')
HORIZON = 96


def _clock_now():
    return datetime.now(SHANGHAI)


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('数据校验时间必须携带时区')
    return value.astimezone(SHANGHAI)


def _ceil_quarter(value):
    floor = value.replace(minute=value.minute // 15 * 15, second=0, microsecond=0)
    return floor if value == floor else floor + timedelta(minutes=15)


def _complete(values, horizon=HORIZON):
    try:
        return (isinstance(values, list) and len(values) == horizon
                and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value >= 0 for value in values))
    except (ValueError, OverflowError):
        return False


def _complete_tariff_periods(period_types, horizon=HORIZON):
    return (isinstance(period_types, list) and len(period_types) == horizon
            and all(isinstance(kind, str) and kind in ('gu', 'ping', 'feng') for kind in period_types))


def _read_result(future, label):
    try:
        return future.result(), None
    except (InputDataError, SourceReadError, ControlSourceError) as error:
        # These adapters expose only controlled messages, never upstream bodies.
        return None, f'{label}：{error}'
    except Exception:
        return None, f'{label}读取或校验失败，请检查数据源与完整性'


def _display_number(value, *, percentage=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or percentage and not 0 <= number <= 100:
        return None
    return number


def _display_time(value, *, now, max_age):
    try:
        observed = _aware(datetime.fromisoformat(value)) if isinstance(value, str) else None
        if observed is None or max_age is None or not 0 <= (now-observed).total_seconds() <= max_age:
            return None
        return observed
    except (ValueError, OverflowError):
        return None


def _add_display_samples(snapshot, station_rows, device_rows, *, now, max_age):
    snapshot.update(observed_station_soc_pct=None, observed_station_at=None,
                    sampled_grid_power_kw=None, sampled_load_power_kw=None,
                    sampled_pv_power_kw=0.0 if snapshot['station_id'] == 'station-1' else None,
                    sampled_pv_at=None)
    warnings = []
    selected = [row for row in station_rows if row.get('es_sn') == snapshot['source_station_id']]
    station = selected[0] if len(selected) == 1 else {}
    observed = _display_time(station.get('timestamp'), now=now, max_age=max_age)
    if observed:
        snapshot.update(
            observed_station_soc_pct=_display_number(station.get('emus_soc'), percentage=True),
            observed_station_at=observed.isoformat(),
            sampled_grid_power_kw=_display_number(station.get('grid_power')),
            sampled_load_power_kw=_display_number(station.get('load_power')),
        )
    else:
        warnings.append('站级对照采样缺失、过期或尚未配置有效期，未显示为当前实测值')
    if snapshot['station_id'] == 'station-2':
        meters = [row for row in device_rows if row.get('f_es_sn') == 'ES02' and row.get('emu_sn') == 'emu27']
        meter = meters[0] if len(meters) == 1 else {}
        at = _display_time(meter.get('last_time_iso'), now=now, max_age=max_age)
        power = _display_number(meter.get('latest_solar_power')) if at else None
        if power is not None and power >= 0:
            snapshot.update(sampled_pv_power_kw=power, sampled_pv_at=at.isoformat())
        else:
            warnings.append('光伏实时采样缺失、无效或过期，未显示为当前实测值')
    return warnings


class LiveInputService:
    def __init__(self, client, control_reader):
        self.client = client
        self.control_reader = control_reader

    def _tariff(self, plan_start):
        with ThreadPoolExecutor(max_workers=2) as pool:
            periods = pool.submit(self.client.list_rows, 't_peak_diy',
                fields='id,start_time,end_time,period_type,updatedAt', sort='start_time', page_size=100)
            rates = pool.submit(self.client.list_rows, 't_rate',
                fields='id,hprice,fprice,vprice,updatedAt', sort='id', page_size=100)
            return build_tariff_points(periods.result(), rates.result(), plan_start_at=plan_start)

    def _pv(self, station_id, plan_start, history_end):
        history = []
        if station_id == 'station-2':
            history = self.client.list_rows('t_es_data', fields='timestamp,es_sn,ac_solar_power',
                filters={'$and': [{'es_sn': {'$eq': 'ES02'}},
                    {'timestamp': {'$gte': (history_end-timedelta(days=7)).isoformat()}},
                    {'timestamp': {'$lt': history_end.isoformat()}}]},
                sort='timestamp', page_size=2000, max_pages=100)
        return build_pv_reference(station_id, history, plan_start_at=plan_start, history_end_at=history_end)

    def _controls(self, station_id):
        result = self.control_reader.fetch(station_id)
        if (result.get('station_id') != station_id or result.get('status') != 'ready'
                or not isinstance(result.get('version'), str) or not result['version'].strip()
                or not _complete([result.get('demand', {}).get('need_kw')] * HORIZON)):
            raise ValueError('控制来源不完整或不属于本站')
        cabinet_power_limits(result)
        return result

    def fetch(self, configuration: StationConfiguration, *, now: datetime | None = None) -> dict:
        configuration = StationConfiguration.model_validate(configuration.model_dump())
        started_at = _aware(now if now is not None else _clock_now())
        plan_start = _ceil_quarter(started_at)
        history_end = started_at.replace(hour=0, minute=0, second=0, microsecond=0)
        station_id = configuration.station_id
        source_station, roster = STATION_CABINETS[station_id]
        issues, warnings, sources = [], [], {}
        horizon = HORIZON

        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {
                'load': pool.submit(load_forecast, self.client, station_id, plan_start_at=plan_start, now=started_at),
                'pv': (pool.submit(load_pv_forecast, self.client, plan_start_at=plan_start, now=started_at)
                       if station_id == 'station-2' else pool.submit(self._pv, station_id, plan_start, history_end)),
                'tariff': pool.submit(self._tariff, plan_start),
                'controls': pool.submit(self._controls, station_id),
            }
            labels = {'load': 'M3负荷预测', 'pv': '光伏功率预测', 'tariff': '共用电价', 'controls': '需量控制'}
            for key, future in pending.items():
                result, error = _read_result(future, labels[key])
                if error:
                    sources[key] = {'status': 'error', 'issues': [error]}
                    if key == 'pv' and isinstance(future.exception(), ForecastRefreshRequired):
                        sources[key]['refresh_required'] = True
                    issues.append(error)
                    continue
                if key == 'controls':
                    sources[key] = result
                    warnings.extend(result.get('warnings', []))
                    warnings.extend(result.get('issues', []))
                else:
                    if key == 'load' and result.get('horizon_points') == 95:
                        horizon = 95
                    # PV/tariff adapters still validate their full daily source.
                    # Export only the same actual window as the load forecast.
                    if key in ('pv','tariff') and horizon == 95 and isinstance(result.get('values'), list) and len(result['values']) == 96:
                        result = {**result, 'values': result['values'][:horizon],
                                  'coverage_points': sum(v is not None for v in result['values'][:horizon])}
                        if key == 'tariff' and _complete_tariff_periods(result.get('period_types')):
                            result['period_types'] = result['period_types'][:horizon]
                    warnings.extend(result.get('warnings', []))
                    complete = _complete(result.get('values'), horizon)
                    source_issues = result.get('issues', [])
                    if key == 'pv' and station_id == 'station-2':
                        try:
                            validate_pv_source(result, start=plan_start,
                                end=plan_start+timedelta(minutes=15*horizon), now=started_at)
                        except InputDataError as error:
                            source_issues = [*source_issues, str(error)]
                            result['refresh_required'] = isinstance(error, ForecastRefreshRequired)
                    if key == 'tariff' and not _complete_tariff_periods(result.get('period_types'), horizon):
                        complete = False
                        source_issues = [*source_issues, f'共用电价缺少完整有效的 {horizon} 点时段标签']
                    status = 'ready' if complete and not source_issues else 'incomplete'
                    sources[key] = {**result, 'status': status, 'issues': source_issues,
                                    'coverage_points': result.get('coverage_points', horizon if complete else 0)}
                    if status != 'ready':
                        issues.extend(source_issues or [f'{labels[key]}缺少完整有效的 {horizon} 点数据'])

        # Historical/forecast sources can be slower. Read current observations
        # last so freshness is measured after the upstream work has completed.
        with ThreadPoolExecutor(max_workers=2) as pool:
            devices = pool.submit(self.client.list_rows, 't_emu',
                fields=','.join((*SOURCE_FIELDS, 'latest_solar_power', 'latest_grid_power', 'load_power')),
                filters={'$and': [{'f_es_sn': {'$eq': source_station}},
                    {'emu_sn': {'$in': list(roster) + (['emu27'] if station_id == 'station-2' else [])}}]},
                sort='emu_sn', page_size=100)
            station = pool.submit(self.client.list_rows, 't_es_data',
                fields='timestamp,es_sn,emus_soc,grid_power,load_power,ac_solar_power',
                filters={'es_sn': {'$eq': source_station}}, sort='-timestamp', page_size=1, limit=1)
            device_rows, device_error = _read_result(devices, '储能柜实时数据')
            station_rows, station_error = _read_result(station, '站级对照采样')
        finished_at = _aware(now if now is not None else _clock_now())
        if sources.get('load', {}).get('status') == 'ready':
            try:
                require_gate(sources['load'].get('accuracy_gate'), station_id, finished_at)
            except ValueError as error:
                sources['load'].update(status='incomplete', issues=[str(error)])
                issues.append(str(error))
        if station_id == 'station-2' and sources['pv']['status'] == 'ready':
            try:
                validate_pv_source(sources['pv'], start=plan_start,
                    end=plan_start+timedelta(minutes=15*horizon), now=finished_at)
            except InputDataError as error:
                sources['pv'].update(status='incomplete', issues=[str(error)],
                                     refresh_required=isinstance(error, ForecastRefreshRequired))
                issues.append(str(error))
        if _ceil_quarter(finished_at) != plan_start:
            issues.append('读取期间已跨过计划时间边界，请刷新输入后重新校验')
        if station_error:
            warnings.append(station_error)
        if device_error:
            sources['realtime'] = {'status': 'error', 'available': False, 'issues': [device_error]}
            issues.append(device_error)
        else:
            try:
                parameters = configuration.parameters
                control = sources.get('controls', {})
                snapshot_configuration = configuration
                if parameters is not None and control.get('status') == 'ready':
                    capacity_kwh = station_energy_capacity(configuration, control)
                    snapshot_configuration = configuration.model_copy(update={'parameters': parameters.model_copy(
                        update={'energy_capacity_kwh': capacity_kwh})})
                snapshot = build_realtime_snapshot(snapshot_configuration, device_rows, now=finished_at, plan_start_at=plan_start)
                if control.get('storage_capacity') is not None:
                    snapshot['capacity_source_version'] = control['version']
                    snapshot['capacity_source'] = 't_es.es_power_storage'
                if parameters is not None and control.get('status') == 'ready' and control.get('power_scope') == 'cabinet':
                    powers = effective_station_power(configuration, control, len(snapshot['participating_cabinet_ids']))
                    snapshot.update(available_max_charge_kw=powers['max_charge_kw'],
                        available_max_discharge_kw=powers['max_discharge_kw'],
                        cabinet_power_limits=cabinet_power_limits(control), power_limits_source_version=control['version'])
                warnings.extend(_add_display_samples(snapshot, station_rows or [], device_rows,
                    now=finished_at, max_age=parameters.max_input_age_seconds if parameters else None))
                warnings.extend(snapshot.get('warnings', []))
                snapshot['status'] = 'ready' if snapshot['available'] else 'blocked'
                sources['realtime'] = snapshot
                issues.extend(snapshot['issues'])
            except Exception:
                error = '储能柜实时数据校验失败，不能确认本站可参与调度'
                sources['realtime'] = {'status': 'error', 'available': False, 'issues': [error]}
                issues.append(error)

        points = []
        if all(sources[key]['status'] == 'ready' and _complete(sources[key].get('values'), horizon)
               for key in ('load', 'pv', 'tariff')):
            points = [{'timestamp': (plan_start+timedelta(minutes=15*i)).isoformat(),
                'load_forecast_kw': float(sources['load']['values'][i]),
                'pv_forecast_kw': float(sources['pv']['values'][i]),
                'buy_price_per_kwh': float(sources['tariff']['values'][i]),
                'tariff_period': sources['tariff']['period_types'][i],
                'sell_price_per_kwh': 0.0} for i in range(horizon)]
        ready = (not issues and configuration.parameters is not None and bool(configuration.version)
                 and len(points) == horizon and all(source['status'] == 'ready' for source in sources.values()))
        return {'station_id': station_id, 'fetched_at': finished_at.isoformat(),
            'plan_start_at': plan_start.isoformat(), 'plan_end_at': (plan_start+timedelta(minutes=15*horizon)).isoformat(),
            'configuration_version': configuration.version, 'interval_minutes': 15, 'horizon_points': horizon,
            'status': 'ready' if ready else 'blocked', 'issues': list(dict.fromkeys(issues)),
            'warnings': list(dict.fromkeys(warnings)), 'sources': sources, 'points': points}


def _validate_participation(configuration, snapshot, *, start, checked_at, fetched_at, observed_at, controls):
    """Recheck the cabinet scope before it determines optimizer capability."""
    parameters = configuration.parameters
    _, roster = STATION_CABINETS[configuration.station_id]
    cabinets = snapshot.get('cabinets')
    alarm_policy = snapshot.get('alarm_policy')
    if (snapshot.get('participation_policy') != CABINET_ISOLATION_POLICY
            or alarm_policy not in (None, ALARM_POLICY)
            or snapshot.get('issues') or not isinstance(cabinets, list)
            or len(cabinets) != len(roster)
            or any(not isinstance(c, dict) for c in cabinets)
            or [c.get('emu_sn') for c in cabinets] != list(roster)):
        raise ValueError('柜级参与快照或隔离政策不完整，请刷新输入')
    active = [c for c in cabinets if c.get('available') is True]
    participants = [c['emu_sn'] for c in active]
    excluded = [c['emu_sn'] for c in cabinets if c.get('available') is not True]
    if (not participants or snapshot.get('participating_cabinet_ids') != participants
            or snapshot.get('excluded_cabinet_ids') != excluded
            or snapshot.get('participation_status') != ('partial' if excluded else 'full')):
        raise ValueError('参与或排除柜名单与逐柜校验结果不一致')

    def same_number(actual, expected):
        try:
            return (type(actual) in (int, float) and math.isfinite(actual)
                    and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9))
        except (ValueError, OverflowError):
            return False

    ratio = len(active) / len(roster)
    capacity_kwh = station_energy_capacity(configuration, controls)
    expected_fields = {
        'energy_capacity_kwh': capacity_kwh,
        'capacity_per_cabinet_kwh': capacity_kwh / len(roster),
        'available_energy_capacity_kwh': capacity_kwh * ratio,
        'available_max_charge_kw': parameters.max_charge_kw * ratio,
        'available_max_discharge_kw': parameters.max_discharge_kw * ratio,
    }
    if controls.get('storage_capacity') is not None and (
            snapshot.get('capacity_source_version') != controls['version']
            or snapshot.get('capacity_source') != 't_es.es_power_storage'):
        raise ValueError('电站容量与电站表来源版本不一致，请重新读取。')
    if controls.get('power_scope') == 'cabinet':
        powers = effective_station_power(configuration, controls, len(active))
        expected_fields.update(available_max_charge_kw=powers['max_charge_kw'],
                              available_max_discharge_kw=powers['max_discharge_kw'])
        if (snapshot.get('power_limits_source_version') != controls['version']
                or snapshot.get('cabinet_power_limits') != cabinet_power_limits(controls)):
            raise ValueError('柜级功率与充放模式表来源版本不一致，请重新读取。')
    if any(not same_number(snapshot.get(key), value) for key, value in expected_fields.items()):
        raise ValueError('可用容量或功率与参与柜折算结果不一致')
    times = []
    for cabinet in cabinets:
        if not same_number(cabinet.get('capacity_kwh'), expected_fields['capacity_per_cabinet_kwh']):
            raise ValueError('逐柜配置容量与原固定柜数等分结果不一致')
        if cabinet not in active:
            continue
        soc = cabinet.get('soc_pct')
        if (type(soc) not in (int, float)
                or not parameters.soc_min_pct <= soc <= parameters.soc_max_pct or not math.isfinite(soc)
                or cabinet.get('issues') != []
                or (not alarm_is_advisory(configuration.station_id, cabinet['emu_sn'], alarm_policy)
                    and cabinet.get('alert_status', 'unknown') is not None)
                or cabinet.get('status') not in ('wait', 'standby', 'charge', 'discharge')):
            raise ValueError('参与柜的 SOC、告警或运行状态未通过校验')
        try:
            at = _aware(datetime.fromisoformat(cabinet['observed_at']))
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError('参与柜采样时间无效') from None
        if (not at <= fetched_at <= checked_at <= start
                or not 0 <= (checked_at-at).total_seconds() <= parameters.max_input_age_seconds
                or not 0 <= (start-at).total_seconds() <= parameters.max_input_age_seconds):
            raise ValueError('参与柜采样已过期或时间顺序无效，请刷新输入')
        times.append(at)
    weighted_soc = math.fsum(c['soc_pct'] / len(active) for c in active)
    if not same_number(snapshot.get('initial_soc_pct'), weighted_soc) or observed_at != min(times):
        raise ValueError('调度初始 SOC 或采样时间与参与柜不一致')
    return participants


def request_from_inputs(configuration: StationConfiguration, bundle: dict, profiles, *, now: datetime | None = None):
    """Construct a validated request only from a fully available current bundle."""
    configuration = StationConfiguration.model_validate(configuration.model_dump())
    checked_at = _aware(now if now is not None else _clock_now())
    parameters = configuration.parameters
    if parameters is None or not configuration.version:
        raise ValueError('本站调度参数尚未配置或保存')
    if bundle.get('status') != 'ready' or bundle.get('issues'):
        raise ValueError('真实输入尚未就绪，不能生成新调度请求')
    if (bundle.get('station_id') != configuration.station_id
            or bundle.get('configuration_version') != configuration.version):
        raise ValueError('真实输入与当前电站或配置版本不一致，请刷新')
    sources = bundle.get('sources', {})
    if any(sources.get(key, {}).get('status') != 'ready' for key in ('load','pv','tariff','controls','realtime')):
        raise ValueError('仍有必需来源未就绪，不能生成新调度请求')
    require_gate(sources['load'].get('accuracy_gate'), configuration.station_id, checked_at)
    snapshot, controls = sources['realtime'], sources['controls']
    if (snapshot.get('available') is not True or snapshot.get('station_id') != configuration.station_id
            or snapshot.get('configuration_version') != configuration.version
            or controls.get('station_id') != configuration.station_id):
        raise ValueError('实时可用状态或控制配置与本站不一致')
    horizon = bundle.get('horizon_points')
    if type(horizon) is not int or horizon not in (95,96):
        raise ValueError('输入计划必须包含连续95或96点')
    if any(not _complete(sources[key].get('values'), horizon) for key in ('load', 'pv', 'tariff')):
        raise ValueError(f'必需来源必须各自包含完整的 {horizon} 点有限非负数值')
    if not _complete_tariff_periods(sources['tariff'].get('period_types'), horizon):
        raise ValueError(f'共用电价必须包含完整的 {horizon} 点有效时段标签')
    try:
        start = _aware(datetime.fromisoformat(bundle['plan_start_at']))
        end = _aware(datetime.fromisoformat(bundle['plan_end_at']))
        fetched_at = _aware(datetime.fromisoformat(bundle['fetched_at']))
        observed_at = _aware(datetime.fromisoformat(snapshot['observed_at']))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('输入的计划、读取与实时采样时间必须有效且携带时区') from None
    if (bundle.get('interval_minutes') != 15
            or _ceil_quarter(start) != start or end != start+timedelta(minutes=15*horizon)):
        raise ValueError('输入计划必须是对齐15分钟、与实际95或96点一致的时间轴')
    if checked_at > start:
        raise ValueError('当前时间已超过计划起点，请重新读取真实输入')
    if not observed_at <= fetched_at <= checked_at:
        raise ValueError('实时采样、读取与当前时间的顺序无效，请重新读取真实输入')
    if (checked_at-observed_at).total_seconds() > parameters.max_input_age_seconds:
        raise ValueError('实时采样相对当前时间已过期，请重新读取真实输入')
    if (checked_at-fetched_at).total_seconds() > parameters.max_input_age_seconds:
        raise ValueError('输入读取时间已过期，请重新读取真实输入')
    if configuration.station_id == 'station-2':
        validate_pv_source(sources['pv'], start=start, end=end, now=checked_at)
    participants = _validate_participation(configuration, snapshot, start=start, checked_at=checked_at,
        fetched_at=fetched_at, observed_at=observed_at, controls=controls)
    if not isinstance(bundle.get('points'), list) or len(bundle['points']) != horizon:
        raise ValueError(f'输入必须包含完整的 {horizon} 个计划点')
    points = [ForecastPoint(**{**point, 'timestamp': _aware(datetime.fromisoformat(point['timestamp']))})
              for point in bundle['points']]
    for index, point in enumerate(points):
        if (point.timestamp != start+timedelta(minutes=15*index)
                or point.load_forecast_kw != sources['load']['values'][index]
                or point.pv_forecast_kw != sources['pv']['values'][index]
                or point.buy_price_per_kwh != sources['tariff']['values'][index]
                or point.tariff_period != sources['tariff']['period_types'][index]
                or point.sell_price_per_kwh != 0):
            raise ValueError('计划点与来源序列或已确认的零售电收益口径不一致')
    live = LiveStationState(station_id=configuration.station_id, available=True,
        initial_soc_pct=snapshot['initial_soc_pct'], observed_at=observed_at,
        participating_cabinet_ids=participants,
        source_version=snapshot['source_version'])
    versions = {key: sources[key]['version'] for key in ('load','pv','tariff')}
    # Legacy evidence without this field keeps its original strict semantics
    # and request identity; new snapshots carry the explicit alarm policy.
    if snapshot.get('alarm_policy') is not None:
        versions['alarm_policy'] = snapshot['alarm_policy']
    digest = hashlib.sha256(json.dumps({'start': start.isoformat(), 'sources': versions,
        'live': live.source_version}, sort_keys=True).encode()).hexdigest()[:20]
    cabinet_limits = cabinet_power_limits(controls)
    limits = ResolvedControlLimits(station_id=configuration.station_id, source_version=controls['version'],
        station_energy_capacity_kwh=station_energy_capacity(configuration, controls) if controls.get('storage_capacity') is not None else None,
        cabinet_max_charge_kw=cabinet_limits['max_charge_kw'] if cabinet_limits else None,
        cabinet_max_discharge_kw=cabinet_limits['max_discharge_kw'] if cabinet_limits else None,
        demand_limit_kw=controls['demand']['need_kw'],
        grid_import_limit_kw=(parameters.grid_import_limit_kw if parameters.grid_import_limit_kw is not None else
            controls['demand']['need_kw'] if controls.get('control_policy_version') == SCHEDULE_POWER_POLICY else None),
        grid_export_enabled=True,
        # re_flow is inactive. The existing model permits PV export only; its
        # maximum input PV supplies a finite physical bound, not a 40 kW rule.
        grid_export_limit_kw=max((point.pv_forecast_kw for point in points), default=0.0))
    return build_optimizer_request(configuration, live, request_id=f'm4-live-{digest}', plan_start_at=start,
        points=points, source_versions=versions, profiles=profiles, control_limits=limits)
