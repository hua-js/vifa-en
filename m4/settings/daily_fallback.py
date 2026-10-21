"""Versioned, independently checked EMS fallback; the fee baseline stays fixed."""
from shared.project import get_project
from m4.optimizer.contracts import CandidateResult, PlanPoint
from m4.optimizer.metrics import calculate_metrics
from m4.optimizer.soc_policy import allows_initial_soc_excess
from m4.optimizer.validation import validate_candidate
from .dispatch_power import dispatch_points
from .ems_model_update import validate_dispatch_safety
from .ems_simulation import simulate_ems_day
from .terminal_policy import enabled as operating_floor


POLICY = 'ems-operating-floor-daily-fallback-v1'


def _filtered_plan(request, points):
    """Rebuild reported energy/flows after filtering; never keep raw SOC labels."""
    cap, bounds = request.capability, request.constraints
    soc = cap.initial_soc_pct
    plan = []
    for source, point in zip(request.points, dispatch_points(points), strict=True):
        charge = point['target_power_kw'] if point['mode'] == 'charge' else 0.0
        discharge = point['target_power_kw'] if point['mode'] == 'discharge' else 0.0
        soc += (charge*cap.charge_efficiency-discharge/cap.discharge_efficiency)*0.25/cap.energy_capacity_kwh*100
        net = source.load_forecast_kw-source.pv_forecast_kw+charge-discharge
        surplus = max(-net, 0.0)
        export = surplus if bounds.grid_export_enabled else 0.0
        if bounds.grid_export_limit_kw is not None:
            export = min(export, bounds.grid_export_limit_kw)
        plan.append(PlanPoint(timestamp=source.timestamp, mode=point['mode'],
            target_power_kw=point['target_power_kw'], expected_soc_pct=soc,
            grid_import_kw=max(net, 0.0), grid_export_kw=export,
            pv_unabsorbed_kw=surplus-export,
            demand_exceed_kw=max(net-bounds.demand_limit_kw, 0.0)))
    return plan


def versions(station_id):
    if get_project().id == 'vifa' and station_id == 'station-2':
        return {'daily_fallback_policy': POLICY}
    return {}


def enabled(request):
    return (bool(versions(request.station_id)) and operating_floor(request)
        and request.source_versions.get('daily_fallback_policy') == POLICY
        and not request.source_versions.get('startup_policy'))


def build_fallback(request, baseline):
    """Replay only a schedule copy, then verify physical and filtered advice."""
    floor = request.peak_reserve_policy.terminal_soc_min_pct
    schedule = [dict(row, soc_min_pct=max(
        row.get('soc_min_pct', request.constraints.soc_min_pct), floor))
        for row in baseline['schedule']]
    plan, simulation = simulate_ems_day(request.capability, request.constraints,
        request.points, schedule, pv_dispatch_policy=request.pv_dispatch_policy,
        allow_initial_soc_excess=allows_initial_soc_excess(request.source_versions))
    metrics = calculate_metrics(request, plan)
    candidate = CandidateResult(profile_id='balanced', profile_version=POLICY,
        plan_version='ems-fallback/'+POLICY+'/'+baseline['input_sha256'],
        status='feasible', solver_message='Conservative EMS day replay',
        solve_seconds=0.0, plan=plan, metrics=metrics, layers=[],
        risk_codes=[], risk_messages=[])
    # EMS boundary slots are time-averaged actions. Check their physics without
    # the optimizer-only minimum-action condition; the actual plan is filtered.
    validation_request = request.model_copy(deep=True)
    validation_request.source_versions.pop('dispatch_power_policy', None)
    validation_request.source_versions.pop('minimum_dispatch_power_kw', None)
    validate_candidate(validation_request, candidate, tolerance=1e-5,
        terminal_soc_target_pct=floor)
    points = [point.model_dump(mode='json') for point in plan]
    raw = request.model_dump(mode='json')
    validate_dispatch_safety(raw, points)
    validate_dispatch_safety(raw, dispatch_points(points))
    candidate.plan = _filtered_plan(request, points)
    candidate.metrics = calculate_metrics(request, candidate.plan)
    validate_candidate(request, candidate, tolerance=1e-5,
        terminal_soc_target_pct=floor)
    points = [point.model_dump(mode='json') for point in candidate.plan]
    return dict(profile_id='ems_fallback', plan_version=candidate.plan_version,
        plan=points, metrics=candidate.metrics.model_dump(mode='json'),
        raw_simulation=simulation, fallback_policy_version=POLICY)
