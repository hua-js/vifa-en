"""Read-only validation of the confirmed station SOC and participation rules.

The configured station capacity is shared by the fixed storage cabinet roster.
Unavailable observations never change that roster or increase the remaining
cabinet capacities; dispatch SOC is normalized over the participating subset. A displayable SOC is not, by itself, permission to schedule.
"""
from shared.project import get_project
from datetime import datetime, timezone
import hashlib
import json
import math

from .models import StationConfiguration
from .roster import CABINET_ISOLATION_POLICY, STATION_CABINETS


SOC_METHOD = 'configured_equal_capacity_weighted'
OPERATING_STATES = frozenset(('wait', 'standby', 'charge', 'discharge'))
PCS_OPERATING_STATES = OPERATING_STATES | {'work'}
STATUS_FIELDS = ('emu_status', 'bcu1_status', 'bcu2_status', 'pcs1_status', 'pcs2_status')
SOURCE_FIELDS = ('f_es_sn', 'emu_sn', 'latest_soc', 'last_time_iso', 'alert_status', *STATUS_FIELDS)
ALARM_POLICY = 'emu11-alert-advisory-v1'


def telemetry_soc_allowed(configuration, soc):
    """Observation admission is separate from the solver's charging target."""
    upper = get_project().station(configuration.station_id).m4['telemetry_soc_upper_exclusive_pct']
    return (configuration.parameters.soc_min_pct <= soc
        and (soc < upper if upper is not None else soc <= configuration.parameters.soc_max_pct))


def alarm_is_advisory(station_id, emu_sn, policy):
    """User-authorized exception for emu11; never applies to other cabinets."""
    return policy == ALARM_POLICY and emu_sn in get_project().station(station_id).alarm_advisory_cabinets


def _soc(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 100 else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _source_version(configuration, grouped_rows, participating_cabinet_ids):
    # The same readings may yield a different scheduling scope as they expire.
    # Hash that scope, but not refresh time when participation has not changed.
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)

    rows = {
        emu_sn: sorted(
            (canonical({key: row[key] for key in SOURCE_FIELDS if key in row}) for row in records)
        )
        for emu_sn, records in grouped_rows.items()
    }
    document = {
        'station_id': configuration.station_id,
        'configuration_version': configuration.version,
        'parameters': configuration.parameters.model_dump() if configuration.parameters else None,
        'soc_method': SOC_METHOD,
        'participation_policy': CABINET_ISOLATION_POLICY,
        'alarm_policy': ALARM_POLICY,
        'participating_cabinet_ids': participating_cabinet_ids,
        'rows': rows,
    }
    digest = hashlib.sha256(canonical(document).encode('utf-8')).hexdigest()
    return f'realtime-v2-{digest[:24]}'


def build_realtime_snapshot(
    configuration: StationConfiguration, rows: list[dict], *, now: datetime,
    plan_start_at: datetime | None = None,
) -> dict:
    """Return cabinet eligibility and the remaining configured station capability.

    The current mapping accepts ``alert_status: null`` as no reported alert.
    emu11's alarm flag is advisory by user instruction. Other cabinets retain
    strict alarm checks. All operating-state, SOC and freshness checks remain.
    Nothing here writes an EMS plan.
    """
    configuration = StationConfiguration.model_validate(configuration.model_dump())
    if now.utcoffset() is None:
        raise ValueError('实时校验参考时间必须携带时区')
    if plan_start_at is not None and plan_start_at.utcoffset() is None:
        raise ValueError('计划起点必须携带时区')
    source_station_id, roster = STATION_CABINETS[configuration.station_id]
    parameters = configuration.parameters
    capacity = parameters.energy_capacity_kwh if parameters else None
    cabinet_capacity = capacity / len(roster) if capacity is not None else None
    grouped_rows = {emu_sn: [] for emu_sn in roster}
    for row in rows:
        if (isinstance(row, dict) and row.get('f_es_sn') == source_station_id
                and row.get('emu_sn') in roster):
            grouped_rows[row['emu_sn']].append(row)

    station_issues = []
    if parameters is None:
        station_issues.append('调度参数尚未配置，不能计算容量加权 SOC 或生成新候选')
    elif not configuration.version:
        station_issues.append('调度参数缺少保存版本，不能生成新候选')

    cabinets = []
    station_warnings = []
    for emu_sn, records in grouped_rows.items():
        issues = []
        advisories = []
        advisory = alarm_is_advisory(configuration.station_id, emu_sn, ALARM_POLICY)
        row = records[0] if len(records) == 1 else {}
        observed_at = None
        soc_pct = None
        if not records:
            issues.append('缺少本站储能柜记录')
        elif len(records) != 1:
            issues.append('同一储能柜存在重复记录，无法确定当前状态')
        else:
            soc_pct = _soc(row.get('latest_soc'))
            if soc_pct is None:
                issues.append('latest_soc 必须为 0–100 的有限数值')
            elif parameters and not telemetry_soc_allowed(configuration, soc_pct):
                issues.append('SOC 超出本站配置的安全范围')

            observed_at = _timestamp(row.get('last_time_iso'))
            if observed_at is None:
                issues.append('last_time_iso 缺失或不是带时区的有效采样时间')
            elif observed_at > now:
                issues.append('采样时间晚于当前时间')
            else:
                if parameters and (now - observed_at).total_seconds() > parameters.max_input_age_seconds:
                    issues.append('实时采样已过期')
                if (parameters and plan_start_at is not None
                        and (plan_start_at - observed_at).total_seconds() > parameters.max_input_age_seconds):
                    issues.append('实时采样在计划起点已过期')
            if observed_at is not None and plan_start_at is not None and observed_at > plan_start_at:
                issues.append('采样时间晚于计划起点')

            for field in STATUS_FIELDS:
                value = row.get(field)
                allowed = PCS_OPERATING_STATES if field in ('pcs1_status', 'pcs2_status') else OPERATING_STATES
                if not isinstance(value, str) or value not in allowed:
                    issues.append(f'{field} 缺失、未知或不允许参与调度')
            alarm_issue = ('alert_status 缺失，无法确认无活动告警' if 'alert_status' not in row
                           else 'alert_status 存在活动告警或未知告警状态' if row['alert_status'] is not None
                           else None)
            if alarm_issue:
                if advisory:
                    advisories.append(alarm_issue + '；仅提示，不影响输入准入')
                else:
                    issues.append(alarm_issue)

        cabinets.append({
            'emu_sn': emu_sn,
            'soc_pct': soc_pct,
            'capacity_kwh': cabinet_capacity,
            'status': row.get('emu_status') if isinstance(row.get('emu_status'), str) else None,
            'alert_status': row.get('alert_status') if isinstance(row.get('alert_status'), str) else None,
            'alert_validation': 'advisory' if advisory else 'strict',
            'advisories': advisories,
            'observed_at': observed_at.isoformat() if observed_at else None,
            'available': bool(parameters and configuration.version and not issues),
            'issues': issues,
        })
        station_warnings.extend(f'{emu_sn}：{issue}' for issue in [*issues, *advisories])

    participating_cabinets = [item for item in cabinets if item['available']]
    participating_cabinet_ids = [item['emu_sn'] for item in participating_cabinets]
    excluded_cabinet_ids = [item['emu_sn'] for item in cabinets if not item['available']]
    if not participating_cabinets:
        station_issues.append('本站没有通过参与校验的储能柜，不能生成新候选')

    def weighted_soc(items):
        # Every cabinet keeps its original equal capacity allocation. Normalize
        # that allocation over the participating subset for its dispatch SOC.
        # Multiplying by 1/N first avoids intermediate energy overflow.
        weight = 1.0 / len(items)
        return math.fsum(item['soc_pct'] * weight for item in items)

    # A complete raw station SOC is display-only. It must never substitute for
    # the dispatch SOC of the healthy subset, even when excluded SOCs are valid.
    full_station_soc_pct = None
    if parameters and all(item['soc_pct'] is not None for item in cabinets):
        full_station_soc_pct = weighted_soc(cabinets)
    initial_soc_pct = weighted_soc(participating_cabinets) if participating_cabinets else None
    observed_times = [_timestamp(item['observed_at']) for item in participating_cabinets]
    available_fraction = len(participating_cabinets) / len(roster)
    participation_status = ('full' if len(participating_cabinets) == len(roster)
                            else 'partial' if participating_cabinets else 'none')

    return {
        'station_id': configuration.station_id,
        'source_station_id': source_station_id,
        'available': bool(participating_cabinets) and not station_issues,
        'initial_soc_pct': initial_soc_pct,
        'full_station_soc_pct': full_station_soc_pct,
        'observed_at': min(observed_times).isoformat() if observed_times else None,
        'source_version': _source_version(configuration, grouped_rows, participating_cabinet_ids),
        'configuration_version': configuration.version,
        'soc_method': SOC_METHOD,
        'participation_policy': CABINET_ISOLATION_POLICY,
        'alarm_policy': ALARM_POLICY,
        'participation_status': participation_status,
        'participating_cabinet_ids': participating_cabinet_ids,
        'excluded_cabinet_ids': excluded_cabinet_ids,
        'energy_capacity_kwh': capacity,
        'capacity_per_cabinet_kwh': cabinet_capacity,
        'available_energy_capacity_kwh': capacity * available_fraction if parameters else None,
        'available_max_charge_kw': parameters.max_charge_kw * available_fraction if parameters else None,
        'available_max_discharge_kw': parameters.max_discharge_kw * available_fraction if parameters else None,
        'cabinets': cabinets,
        'issues': station_issues,
        'warnings': station_warnings,
    }
