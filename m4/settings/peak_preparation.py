"""Explain a verified historical plan's peak energy and power limits.

This is arithmetic over a selected plan, not a new optimization, live readiness
check, or charging instruction. Each contiguous positive net-load excess is a
window; no charging can occur within that window while keeping imports below
the demand limit. Energy is measured on the battery side unless named otherwise.
"""
from datetime import timedelta
from itertools import groupby

from m4.optimizer.contracts import CandidateResult, OptimizationRequest
from m4.optimizer.validation import validate_candidate

DISPLAY_TOLERANCE = 1e-4


def summarize_peak_preparation(request: OptimizationRequest, candidate: CandidateResult) -> dict:
    if candidate.status not in {'optimal', 'feasible'}:
        raise ValueError('peak preparation requires a usable plan')
    validate_candidate(request, candidate)
    capability, constraints = request.capability, request.constraints
    capacity = capability.energy_capacity_kwh
    interval_hours = request.interval_minutes / 60
    charge_power = capability.max_charge_kw if capability.available else 0.0
    discharge_power = capability.max_discharge_kw if capability.available else 0.0
    usable_capacity = capacity * (constraints.soc_max_pct - constraints.soc_min_pct) / 100
    excess = [max(0.0, p.load_forecast_kw-p.pv_forecast_kw-constraints.demand_limit_kw)
              for p in request.points]
    windows = []
    for has_peak, indices in groupby(range(len(excess)), lambda i: excess[i] > DISPLAY_TOLERANCE):
        indices = list(indices)
        if not has_peak:
            continue
        start, end = indices[0], indices[-1]+1
        start_soc = (candidate.plan[start-1].expected_soc_pct if start
                     else capability.initial_soc_pct)
        available = max(0.0, (start_soc-constraints.soc_min_pct)*capacity/100)
        required_discharge = sum(excess[i]*interval_hours for i in indices)
        required_battery = required_discharge/capability.discharge_efficiency
        energy_gap = max(0.0, required_battery-available)
        additional_charge = energy_gap/capability.charge_efficiency
        windows.append(dict(start_index=start, end_index=end,
            start_at=request.points[start].timestamp.isoformat(),
            end_at=(request.points[end-1].timestamp+timedelta(minutes=request.interval_minutes)).isoformat(),
            planned_start_soc_pct=start_soc, planned_available_kwh=available,
            required_discharge_kwh=required_discharge, required_battery_kwh=required_battery,
            energy_gap_kwh=energy_gap,
            required_start_soc_pct=constraints.soc_min_pct+required_battery/capacity*100,
            capacity_gap_kwh=max(0.0, required_battery-usable_capacity),
            required_discharge_kw=max(excess[i] for i in indices),
            power_gap_kw=max(0.0, max(excess[i] for i in indices)-discharge_power),
            additional_charge_kwh=additional_charge,
            minimum_charge_minutes=additional_charge/charge_power*60 if charge_power else None,
            predicted_exceed_kw=max(max(0.0, candidate.plan[i].grid_import_kw-constraints.demand_limit_kw)
                                    for i in indices)))
    peak_import = max(p.grid_import_kw for p in candidate.plan)
    exceed = max(0.0, peak_import-constraints.demand_limit_kw)
    attention = exceed > DISPLAY_TOLERANCE or any(
        max(w['energy_gap_kwh'], w['power_gap_kw']) > DISPLAY_TOLERANCE for w in windows)
    return dict(schema_version='m4-peak-preparation-v1', basis='selected_plan_forecast',
        station_id=request.station_id, profile_id=candidate.profile_id, plan_version=candidate.plan_version,
        plan_start_at=request.plan_start_at.isoformat(),
        plan_end_at=(request.plan_start_at+timedelta(minutes=request.interval_minutes*len(request.points))).isoformat(),
        input_observed_at=request.input_observed_at.isoformat(), source_versions=dict(request.source_versions),
        status='attention' if attention else 'covered' if windows else 'clear',
        demand_limit_kw=constraints.demand_limit_kw, predicted_peak_import_kw=peak_import,
        predicted_exceed_kw=exceed, energy_capacity_kwh=capacity, soc_max_pct=constraints.soc_max_pct,
        charge_efficiency=capability.charge_efficiency, discharge_efficiency=capability.discharge_efficiency,
        windows=windows)
