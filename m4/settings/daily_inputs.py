"""Read a complete natural day and report actual gaps, without running EMS.

These are retrospective comparison inputs, not a dispatch-capability snapshot.
The station-level midnight SOC is a whole-station reference and is never used
as the initial SOC of a partially participating cabinet set.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from .pv_forecast_source import load_pv_forecast, validate_source as validate_pv_source
from .timeseries import InputDataError
from .daily_pv_policy import POLICY as DAILY_PV_POLICY, fill_night_gaps
from .forecast_source import load_forecast, _time
from .live_inputs import _complete, _complete_tariff_periods, _display_number, _display_time
from .runtime_config import runtime_parameters
from .roster import STATION_CABINETS


SHANGHAI = ZoneInfo('Asia/Shanghai')


def _version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


class DailyInputService:
    def __init__(self, live_inputs):
        self.live = live_inputs

    def _pv(self, station_id, start, now):
        if station_id == 'station-1':
            return self.live._pv(station_id, start, start)
        source = load_pv_forecast(self.live.client, plan_start_at=start, now=now)
        covered_start = max(start, _time(source['forecast_start']))
        covered_end = min(start+timedelta(days=1), _time(source['forecast_end']))
        if covered_start >= covered_end or not source['coverage_points']:
            raise InputDataError('M3光伏预测与当天没有重叠时段，未将全天填零')
        validate_pv_source(source, start=covered_start, end=covered_end, now=now)
        values, missing = fill_night_gaps(source['values'], start)
        filled = {**source, 'values': values,
                  'forecast_coverage_points': source['coverage_points'], 'coverage_points': 96,
                  'gap_policy': DAILY_PV_POLICY, 'zero_filled_points': len(missing),
                  'zero_filled_at': [(start+timedelta(minutes=15*i)).isoformat() for i in missing]}
        filled['version'] = 'pv-daily-zero-'+_version(filled)
        return filled

    def _initial_soc(self, station_id, start):
        source_station, roster = STATION_CABINETS[station_id]
        rows = self.live.client.list_rows('t_es_data',
            fields='timestamp,es_sn,emus_soc',
            filters={'$and': [{'es_sn': {'$eq': source_station}},
                {'timestamp': {'$gte': start.isoformat()}},
                {'timestamp': {'$lt': (start+timedelta(seconds=1)).isoformat()}}]},
            sort='timestamp', page_size=10, max_pages=1)
        output = dict(status='incomplete', observed_at=start.isoformat(),
            initial_soc_pct=None, scope='whole_station_reference',
            cabinet_ids=list(roster), version=None, issues=[],
            note='站级历史 SOC；只适用于同一全站储能范围，不能直接用于部分柜方案。')
        exact = []
        for row in rows:
            stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            if row.get('es_sn') != source_station or stamp.utcoffset() is None:
                raise ValueError('invalid midnight SOC identity')
            if stamp == start:
                exact.append(row)
        if len(exact) != 1:
            output['issues'] = ['零点站级 SOC 缺失或重复，未用邻近采样代替。']
            return output
        value = exact[0].get('emus_soc')
        if type(value) not in (int, float, str):
            raise ValueError('invalid midnight SOC')
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('invalid midnight SOC')
        output.update(status='ready', initial_soc_pct=value,
            version='midnight-station-soc-'+_version({'station_id': station_id,
                'timestamp': start.isoformat(), 'soc': value, 'scope': output['scope']}))
        return output

    def _current_soc(self, station_id, now):
        """Optional display sample, excluded from solver inputs and readiness gates."""
        source_station, _ = STATION_CABINETS[station_id]
        rows = self.live.client.list_rows('t_es_data', fields='timestamp,es_sn,emus_soc',
            filters={'es_sn': {'$eq': source_station}}, sort='-timestamp', page_size=1, limit=1)
        output = dict(status='incomplete', station_id=station_id, soc_pct=None,
                      observed_at=None, scope='whole_station_latest', issues=[])
        if not rows or rows[0].get('es_sn') != source_station:
            return output
        row = rows[0]
        observed = _display_time(row.get('timestamp'), now=now,
            max_age=runtime_parameters(station_id)['max_input_age_seconds'])
        value = _display_number(row.get('emus_soc'), percentage=True)
        if observed is not None and value is not None:
            output.update(status='ready', soc_pct=value, observed_at=observed.isoformat())
        return output

    def _current_power(self, station_id, now=None):
        """Read station AC power from its complete cabinet roster; display only."""
        source_station, roster = STATION_CABINETS[station_id]
        rows = self.live.client.list_rows('t_emu',
            fields='f_es_sn,emu_sn,latest_power,last_time_iso',
            filters={'$and': [{'f_es_sn': {'$eq': source_station}},
                {'emu_sn': {'$in': list(roster)}}]}, sort='emu_sn', page_size=100)
        output = dict(status='incomplete', station_id=station_id, power_kw=None,
                      observed_at=None, observed_until=None, scope='whole_station_latest')
        if len(rows) != len(roster) or {r.get('emu_sn') for r in rows} != set(roster):
            return output
        now = now or datetime.now(SHANGHAI)
        values, times = [], []
        for row in rows:
            at = _display_time(row.get('last_time_iso'), now=now,
                max_age=runtime_parameters(station_id)['max_input_age_seconds'])
            value = _display_number(row.get('latest_power'))
            if row.get('f_es_sn') != source_station or at is None or value is None:
                return output
            values.append(value)
            times.append(at)
        output.update(status='ready', power_kw=sum(values),
                      observed_at=min(times).isoformat(), observed_until=max(times).isoformat())
        return output

    def fetch(self, configuration, day: date, *, now=None):
        now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        if not now.date()-timedelta(days=31) <= day <= now.date():
            raise ValueError('只支持今天及前31天的自然日输入')
        start = datetime.combine(day, time(), SHANGHAI)
        station_id = configuration.station_id
        jobs = {
            'load': lambda: load_forecast(self.live.client, station_id,
                plan_start_at=start, now=now, require_full_day=True),
            'pv': lambda: self._pv(station_id, start, now),
            'tariff': lambda: self.live._tariff(start),
            'controls': lambda: self.live._controls(station_id),
            'initial_soc': lambda: self._initial_soc(station_id, start),
            'current_soc': lambda: self._current_soc(station_id, now),
            'current_power': lambda: self._current_power(station_id),
        }
        labels = {'load': '全天负荷预测', 'pv': 'M3光伏预测',
                  'tariff': '全天分时电价', 'controls': 'EMS 时段配置', 'initial_soc': '零点站级 SOC'}
        sources = {}
        with ThreadPoolExecutor(max_workers=7) as pool:
            pending = {key: pool.submit(job) for key, job in jobs.items()}
            for key, future in pending.items():
                try:
                    result = future.result()
                    if key in ('load', 'pv', 'tariff'):
                        complete = _complete(result.get('values'), 96) and not result.get('issues')
                        if key == 'tariff':
                            complete = complete and _complete_tariff_periods(result.get('period_types'), 96)
                        result = {**result, 'status': 'ready' if complete else 'incomplete'}
                    sources[key] = result
                except InputDataError as error:
                    sources[key] = dict(status='incomplete', issues=[str(error)])
                except Exception:
                    sources[key] = dict(status='error', issues=[labels.get(key, '当前实测数据')+'读取或校验失败，请重新读取。'])
        load = sources.get('load', {})
        actual_values = load.get('actual_values', [None] * 96)
        sources['actual_load'] = dict(station_id=station_id,
            source='M3 station_total_load', run_id=load.get('run_id'),
            values=actual_values, read_at=load.get('actual_read_at'),
            status='ready' if any(v is not None for v in actual_values) else 'unavailable')
        checks = []
        for key, label in labels.items():
            source = sources[key]
            ready = source.get('status') == 'ready'
            if key == 'controls':
                ready = ready and source.get('source_health', {}).get('schedule') == 'ready' and bool(source.get('schedule'))
            if key == 'initial_soc' and ready:
                detail = f"{source['initial_soc_pct']:g}% · 全站参考"
            elif key in ('load', 'pv', 'tariff'):
                detail = '96 / 96 点' if ready else '；'.join(source.get('issues', [])) or '完整96点尚未就绪'
            else:
                detail = '已读取' if ready else '；'.join(source.get('issues', [])) or '尚未读取到有效时段'
            if key == 'load' and ready:
                detail += ' · MAPE ' + str(source['accuracy_gate']['mape_percent']) + '%（门槛30%）'
            checks.append(dict(key=key, label=label, status='ready' if ready else 'missing', detail=detail))
        capacity = sources.get('controls', {}).get('storage_capacity', {}).get('energy_capacity_kwh')
        capacity_ready = type(capacity) in (int, float) and math.isfinite(capacity) and capacity > 0
        checks.append(dict(key='station_capacity', label='电站容量（接口）',
            status='ready' if capacity_ready else 'missing',
            detail=f'{capacity:g} kWh · t_es.es_power_storage' if capacity_ready else '电站容量接口尚未就绪'))
        points = []
        if all(sources[key]['status'] == 'ready' for key in ('load', 'pv', 'tariff')):
            points = [dict(timestamp=(start+timedelta(minutes=15*i)).isoformat(),
                load_forecast_kw=float(sources['load']['values'][i]),
                pv_forecast_kw=float(sources['pv']['values'][i]),
                buy_price_per_kwh=float(sources['tariff']['values'][i]),
                tariff_period=sources['tariff']['period_types'][i], sell_price_per_kwh=0.0)
                for i in range(96)]
        missing = [item['key'] for item in checks if item['status'] != 'ready']
        result = dict(schema_version='m4-daily-inputs-v1', station_id=station_id,
            date=day.isoformat(), plan_start_at=start.isoformat(),
            plan_end_at=(start+timedelta(days=1)).isoformat(),
            configuration_version=configuration.version, fetched_at=now.isoformat(),
            usage='retrospective_comparison_only', dispatch_status='not_dispatched',
            status='blocked' if missing else 'ready', can_compare=False,
            interval_minutes=15, horizon_points=96, sources=sources, points=points,
            checks=checks, missing=missing,
            warnings=['预测可能包含当日零点后生成的批次，仅用于相同输入下的日费用回算；不代表零点已知计划或实测收益。',
                      '历史日期电价采用当前配置按时段展开；未接入历史电价版本。'])
        if sources.get('pv', {}).get('zero_filled_points'):
            result['warnings'].append(f"光伏预测夜间缺测{sources['pv']['zero_filled_points']}个时段按0 kW估算，并非实测零发电。")
        result.update(baseline=None, comparison=None)
        if not missing:
            from .daily_baseline import prepare_ems_day
            try:
                request, baseline, comparison = prepare_ems_day(configuration, result)
                result.update(baseline=baseline, comparison=comparison, can_compare=True,
                              request=request.model_dump(mode='json'))
                result['warnings'].append('EMS 基线按配置时段、需量阈值和 SOC 边界模拟有效运行时长；到限区间按有效时长折算平均功率，未使用现场执行回读。')
                if request.peak_reserve_policy is not None:
                    result['warnings'].append('本站新优化按全天经济性安排充放电，允许非峰段经济性放电，保留需量、SOC及日末电量约束。当前仅完成 EMS 基线准备，不代表已生成或采用优化方案。')
                if baseline['simulation']['summary']['demand_shortfall_points']:
                    result['warnings'].append('当前预测下，基线存在电量或功率不足导致的需量缺口；这不是现场实测超限，新优化仍必须满足需量约束。')
                checks.append(dict(key='ems_baseline', label='EMS 全天模拟基线', status='ready',
                    detail=f"{comparison['baseline_cost_yuan']:.2f} 元"))
            except ValueError as error:
                checks.append(dict(key='ems_baseline', label='EMS 全天模拟基线', status='missing', detail=str(error)))
                result['missing'].append('ems_baseline')
                result['status'] = 'blocked'
        return result
