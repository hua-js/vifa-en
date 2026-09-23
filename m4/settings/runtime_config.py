"""Service-owned scheduling values; never editable through the dashboard."""
from shared.project import get_project
import hashlib
import json
from typing import Annotated

from pydantic import Field
from m4.optimizer.contracts import StrictModel
from .roster import STATION_CABINETS


class RuntimeSettings(StrictModel):
    cycle_cost_per_kwh: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    max_input_age_seconds: Annotated[int, Field(strict=True, gt=0, le=86400)]


# Project policy owns service values; dashboard parameters remain in the store.
GLOBAL_RUNTIME_DEFAULTS = {
    key: get_project().m4[key] for key in ('cycle_cost_per_kwh', 'max_input_age_seconds')
}
STATION_RUNTIME_OVERRIDES = {s.id: s.m4['runtime_overrides'] for s in get_project().stations}
RUNTIME_POLICY = 'm4-runtime-v2-pv-priority'


def runtime_parameters(station_id):
    if station_id not in STATION_CABINETS:
        raise ValueError('未知电站')
    values = RuntimeSettings.model_validate({
        **GLOBAL_RUNTIME_DEFAULTS, **STATION_RUNTIME_OVERRIDES.get(station_id, {}),
    })
    # Physical import ceilings are project constraints, not dashboard inputs.
    return {**values.model_dump(), 'grid_import_limit_kw': get_project().station(station_id).grid_import_limit_kw}


def effective_configuration(configuration):
    if configuration.parameters is None:
        return configuration
    values = {**runtime_parameters(configuration.station_id),
              'pv_export_priority': configuration.parameters.pv_export_priority if configuration.parameters.pv_export_priority is not None else False}
    parameters = type(configuration.parameters).model_validate({
        **configuration.parameters.model_dump(), **values,
    })
    telemetry = get_project().station(configuration.station_id).m4
    upper = telemetry['telemetry_soc_upper_exclusive_pct']
    charge = telemetry['telemetry_max_charge_kw']
    if upper is not None and parameters.soc_max_pct >= upper:
        raise ValueError('计划SOC上限必须低于实时准入上界。')
    if charge is not None and parameters.max_charge_kw > charge:
        raise ValueError('计划充电上限不能超过实时准入上限。')
    digest = hashlib.sha256(json.dumps({
        'policy': RUNTIME_POLICY, 'station_id': configuration.station_id,
        'project_configuration': get_project().fingerprint,
        'parameters': values,
    }, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
    return configuration.model_copy(update={
        'parameters': parameters,
        'version': f'{configuration.version}/service/{digest}',
    })
