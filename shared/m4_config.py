"""Credential-free M4 project policy, separate from editable solver parameters."""
from copy import deepcopy
import math


DEFAULTS = {
    'max_load_mape_pct': 30.0,
    'minimum_net_savings_yuan': 100.0,
    'minimum_dispatch_power_kw': 10.0,
    'idle_soc_tolerance_pct': 0.05,
    'night_max_input_age_seconds': 900,
    'auto_interval_seconds': 900,
    'auto_retry_seconds': 60,
    'auto_max_attempts': 3,
    'export_min_display_revenue_yuan': 10.0,
    'cycle_cost_per_kwh': 0.0,
    'max_input_age_seconds': 86400,
}
STATION_DEFAULTS = {
    'telemetry_max_charge_kw': None,
    'telemetry_soc_upper_exclusive_pct': None,
    'ems_charge_kw': 100.0,
    'ems_discharge_kw': {'gu': 90.0, 'ping': 90.0, 'feng': 90.0, 'jian': 90.0},
    'ems_export_kw': 540.0,
    'runtime_overrides': {},
}


def _number(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        raise ValueError(f'Invalid M4 configuration: {name}')


def project_policy(raw):
    if type(raw) is not dict or set(raw) - set(DEFAULTS):
        raise ValueError('M4 project policy: unknown fields')
    values = {**DEFAULTS, **raw}
    for name, value in values.items():
        _number(value, name, positive=name in ('auto_interval_seconds', 'auto_retry_seconds',
            'auto_max_attempts', 'night_max_input_age_seconds', 'max_input_age_seconds'))
        if name.endswith('_seconds') or name == 'auto_max_attempts':
            if type(value) is not int:
                raise ValueError(f'M4 {name} must be an integer')
    if values['auto_interval_seconds'] != 900:
        raise ValueError('M4 automatic interval must match the 15-minute plan grid')
    if not values['night_max_input_age_seconds'] <= 900 or values['max_input_age_seconds'] > 86400:
        raise ValueError('M4 input freshness exceeds the supported limit')
    if values['idle_soc_tolerance_pct'] > 1:
        raise ValueError('M4 idle SOC tolerance exceeds one percentage point')
    return values


def station_policy(raw, *, legacy_station2=False):
    if type(raw) is not dict or set(raw) - set(STATION_DEFAULTS):
        raise ValueError('M4 station policy: unknown fields')
    defaults = deepcopy(STATION_DEFAULTS)
    # Existing externally mounted project files must retain their previous EMS
    # commands until operators explicitly add the new station policy fields.
    if legacy_station2:
        defaults.update(ems_charge_kw=600.0,
            ems_discharge_kw={'gu': 600.0, 'ping': 540.0, 'feng': 600.0, 'jian': 600.0})
    values = {**defaults, **raw}
    for name in ('telemetry_max_charge_kw', 'telemetry_soc_upper_exclusive_pct'):
        if values[name] is not None:
            _number(values[name], name, positive=True)
    upper = values['telemetry_soc_upper_exclusive_pct']
    if upper is not None and upper > 100:
        raise ValueError('M4 telemetry SOC upper limit exceeds 100%')
    for name in ('ems_charge_kw', 'ems_export_kw'):
        _number(values[name], name, positive=True)
    if values['telemetry_max_charge_kw'] is not None and values['telemetry_max_charge_kw'] < values['ems_charge_kw']:
        raise ValueError('M4 telemetry charging limit is below the EMS charging setpoint')
    discharge = values['ems_discharge_kw']
    if type(discharge) is not dict or set(discharge) != {'gu', 'ping', 'feng', 'jian'}:
        raise ValueError('M4 discharge setpoints require all tariff periods')
    for value in discharge.values():
        _number(value, 'ems_discharge_kw', positive=True)
    overrides = values['runtime_overrides']
    if type(overrides) is not dict or set(overrides) - {'cycle_cost_per_kwh', 'max_input_age_seconds'}:
        raise ValueError('M4 runtime overrides: unknown fields')
    project_policy(overrides)
    return values
