"""Gate-free archived input snapshots for compatibility component tests only.

These stubs do NOT prove a current M3 task can produce future 96-point inputs.
Current-task integration and MAPE rejection are covered without these stubs in
 test_m4_current_window.py and test_m4_load_accuracy.py. Production is unmodified.
Historical evidence predating the MAPE gate remains independently revalidated.
"""
import copy
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
from m4.settings import live_inputs


def archived_forecast(client, station_id, *, plan_start_at, now):
    if 'current_load_result' in client.failures:
        raise ValueError('archived input unavailable')
    if not client.runs:
        raise ValueError('archived task missing')
    values_by_time={p['target_time']:p['forecast_value'] for p in client.forecasts}
    values=[values_by_time.get((plan_start_at+timedelta(minutes=15*i)).isoformat()) for i in range(96)]
    digest=hashlib.sha256(json.dumps(values).encode()).hexdigest()
    return dict(values=values,coverage_points=sum(v is not None for v in values),
                horizon_points=96,version='archived-load-'+digest,
                source_kind='archived_component_fixture',issues=[])


def install_archived_inputs(case):
    # Only the upstream boundary is isolated. Timestamp/point/cabinet/configuration
    # checks and optimizer/selection/record-integrity checks remain real.
    original_request=live_inputs.request_from_inputs
    original_gate=live_inputs.require_gate
    def gate(saved, station, now):
        if saved is not None:return original_gate(saved,station,now)
    def request(configuration,bundle,profiles,**kwargs):
        kwargs.setdefault('require_accuracy_gate',False)
        return original_request(configuration,bundle,profiles,**kwargs)
    for target,replacement in [
        ('m4.settings.live_inputs.load_forecast',archived_forecast),
        ('m4.settings.live_inputs.require_gate',gate),
        ('m4.settings.live_inputs.request_from_inputs',request),
        ('m4.settings.candidates.request_from_inputs',request),
        ('m4.settings.selection.request_from_inputs',request),
    ]:
        handle=patch(target,replacement);handle.start();case.addCleanup(handle.stop)
