"""Service-owned scheduling values; never editable through the dashboard."""
import hashlib
import json
from typing import Annotated

from pydantic import Field
from m4_optimizer.contracts import StrictModel
from .roster import STATION_CABINETS


class RuntimeSettings(StrictModel):
    cycle_cost_per_kwh: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    max_input_age_seconds: Annotated[int, Field(strict=True, gt=0, le=86400)]


# Preserve the values currently configured for both stations. This is not a
# production freshness recommendation; operators maintain these values here.
GLOBAL_RUNTIME_DEFAULTS = {
    'cycle_cost_per_kwh': 0.0,
    'max_input_age_seconds': 86400,
}
# Optional partial overrides, e.g. {'station-2': {'max_input_age_seconds': 300}}.
STATION_RUNTIME_OVERRIDES: dict[str, dict] = {}
RUNTIME_POLICY = 'm4-runtime-v2-pv-priority'


def runtime_parameters(station_id):
    if station_id not in STATION_CABINETS:
        raise ValueError('未知电站')
    values = RuntimeSettings.model_validate({
        **GLOBAL_RUNTIME_DEFAULTS, **STATION_RUNTIME_OVERRIDES.get(station_id, {}),
    })
    # Retired manual grid limit must not constrain a newly built request.
    # The actual hard limit is resolved from t_need.need_kw with each input.
    return {**values.model_dump(), 'grid_import_limit_kw': None}


def effective_configuration(configuration):
    if configuration.parameters is None:
        return configuration
    values = {**runtime_parameters(configuration.station_id),
              'pv_export_priority': configuration.parameters.pv_export_priority if configuration.parameters.pv_export_priority is not None else False}
    parameters = type(configuration.parameters).model_validate({
        **configuration.parameters.model_dump(), **values,
    })
    digest = hashlib.sha256(json.dumps({
        'policy': RUNTIME_POLICY, 'station_id': configuration.station_id,
        'parameters': values,
    }, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
    return configuration.model_copy(update={
        'parameters': parameters,
        'version': f'{configuration.version}/service/{digest}',
    })
