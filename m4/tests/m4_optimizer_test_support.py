from datetime import datetime, timedelta

from m4.optimizer.contracts import (
    CandidateMetrics,
    CandidateResult,
    CapabilitySnapshot,
    ForecastPoint,
    ObjectiveLayer,
    ObjectiveProfile,
    OptimizationConstraints,
    OptimizationRequest,
    OptimizationResult,
    PlanPoint,
)


def layer(name: str, terms: dict[str, float]) -> ObjectiveLayer:
    return ObjectiveLayer(
        name=name,
        terms=terms,
        absolute_tolerance=1e-6,
        relative_tolerance=0.0,
    )


def make_profiles() -> list[ObjectiveProfile]:
    orders = {
        "balanced": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("balanced-value", {"energy_cost": 0.01, "pv_unused": 0.001}),
            layer("throughput", {"throughput": 1.0}),
        ],
        "cost": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("energy-cost", {"energy_cost": 1.0}),
            layer("pv-unused", {"pv_unused": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("throughput", {"throughput": 1.0}),
        ],
        "pv": [
            layer("demand-peak", {"demand_peak": 1.0}),
            layer("demand-duration", {"demand_duration": 1.0}),
            layer("pv-unused", {"pv_unused": 1.0}),
            layer("energy-cost", {"energy_cost": 1.0}),
            layer("soc-reserve", {"soc_preferred_deviation": 1.0}),
            layer("throughput", {"throughput": 1.0}),
        ],
    }
    return [
        ObjectiveProfile(
            profile_id=profile_id,
            profile_version=f"test-{profile_id}-v1",
            objective_order=orders[profile_id],
        )
        for profile_id in ("balanced", "cost", "pv")
    ]


def make_request(**overrides: object) -> OptimizationRequest:
    plan_start_at = datetime.fromisoformat("2026-09-04T00:00:00+08:00")
    available = overrides.pop("available", True)
    load_kw = overrides.pop("load_kw", 100.0)
    values: dict[str, object] = {
        "request_id": "request-test-001",
        "station_id": "station-test-001",
        "plan_start_at": plan_start_at,
        "input_observed_at": plan_start_at - timedelta(minutes=5),
        "max_input_age_seconds": 1800,
        "interval_minutes": 15,
        "horizon_points": 96,
        "source_versions": {"forecast": "test-v1", "capability": "test-v1"},
        "points": [
            ForecastPoint(
                timestamp=plan_start_at + timedelta(minutes=15 * index),
                load_forecast_kw=load_kw,
                pv_forecast_kw=20.0,
                buy_price_per_kwh=0.8,
                sell_price_per_kwh=0.0,
            )
            for index in range(96)
        ],
        "capability": CapabilitySnapshot(
            available=available,
            initial_soc_pct=50.0,
            energy_capacity_kwh=200.0,
            max_charge_kw=100.0,
            max_discharge_kw=100.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
        ),
        "constraints": OptimizationConstraints(
            soc_min_pct=10.0,
            soc_max_pct=90.0,
            preferred_soc_min_pct=20.0,
            preferred_soc_max_pct=80.0,
            terminal_soc_tolerance_pct=5.0,
            demand_limit_kw=max(120.0, load_kw + 20.0),
            grid_import_limit_kw=max(150.0, load_kw + 50.0),
            grid_export_enabled=False,
            grid_export_limit_kw=0.0,
            cycle_cost_per_kwh=0.01,
        ),
        "profiles": make_profiles(),
        "solver_time_limit_seconds": 30.0,
        "solver_mip_rel_gap": 0.01,
    }
    values.update(overrides)
    return OptimizationRequest(**values)


def make_price_arbitrage_request() -> OptimizationRequest:
    request = make_request()
    points = [
        point.model_copy(
            update={
                "load_forecast_kw": 80.0,
                "pv_forecast_kw": 0.0,
                "buy_price_per_kwh": (
                    0.2 if index < 24 else 1.6 if index < 72 else 0.8
                ),
                "sell_price_per_kwh": 0.0,
            }
        )
        for index, point in enumerate(request.points)
    ]
    constraints = request.constraints.model_copy(
        update={
            "terminal_soc_tolerance_pct": 0.0,
            "demand_limit_kw": 300.0,
            "grid_import_limit_kw": 400.0,
        }
    )
    return request.model_copy(update={"points": points, "constraints": constraints})


def make_midday_pv_request(
    *,
    grid_export_enabled: bool = True,
    grid_export_limit_kw: float = 20.0,
    sell_price_per_kwh: float = 0.2,
) -> OptimizationRequest:
    request = make_request()
    points = [
        point.model_copy(
            update={
                "load_forecast_kw": 40.0,
                "pv_forecast_kw": 140.0 if 40 <= index < 56 else 0.0,
                "buy_price_per_kwh": 0.8,
                "sell_price_per_kwh": sell_price_per_kwh,
            }
        )
        for index, point in enumerate(request.points)
    ]
    constraints = request.constraints.model_copy(
        update={
            "demand_limit_kw": 300.0,
            "grid_import_limit_kw": 400.0,
            "grid_export_enabled": grid_export_enabled,
            "grid_export_limit_kw": grid_export_limit_kw,
        }
    )
    return request.model_copy(update={"points": points, "constraints": constraints})


def make_demand_peak_request(max_discharge_kw: float) -> OptimizationRequest:
    request = make_request()
    points = [
        point.model_copy(
            update={
                "load_forecast_kw": 180.0 if 32 <= index < 36 else 60.0,
                "pv_forecast_kw": 0.0,
                "buy_price_per_kwh": 0.8,
                "sell_price_per_kwh": 0.0,
            }
        )
        for index, point in enumerate(request.points)
    ]
    capability = request.capability.model_copy(
        update={
            "initial_soc_pct": 50.0,
            "energy_capacity_kwh": 300.0,
            "max_charge_kw": max_discharge_kw,
            "max_discharge_kw": max_discharge_kw,
        }
    )
    constraints = request.constraints.model_copy(
        update={
            "terminal_soc_tolerance_pct": 0.0,
            "demand_limit_kw": 100.0,
            "grid_import_limit_kw": 250.0,
        }
    )
    return request.model_copy(
        update={
            "points": points,
            "capability": capability,
            "constraints": constraints,
        }
    )


def make_zero_pv_export_request(
    *,
    load_kw: float = 0.0,
    buy_price_per_kwh: float = 0.8,
    sell_price_per_kwh: float = 0.0,
) -> OptimizationRequest:
    request = make_request(load_kw=load_kw)
    payload = request.model_dump()
    payload["points"] = [
        {
            **point.model_dump(),
            "load_forecast_kw": load_kw,
            "pv_forecast_kw": 0.0,
            "buy_price_per_kwh": buy_price_per_kwh,
            "sell_price_per_kwh": sell_price_per_kwh,
        }
        for point in request.points
    ]
    payload["constraints"] = {
        **request.constraints.model_dump(),
        "terminal_soc_tolerance_pct": 0.0,
        "demand_limit_kw": 300.0,
        "grid_import_limit_kw": 400.0,
        "grid_export_enabled": True,
        "grid_export_limit_kw": 100.0,
    }
    return OptimizationRequest.model_validate(payload)


def make_battery_load_with_pv_export_request() -> OptimizationRequest:
    request = make_zero_pv_export_request(
        load_kw=100.0,
        buy_price_per_kwh=0.1,
        sell_price_per_kwh=0.0,
    )
    payload = request.model_dump()
    payload["points"][40]["pv_forecast_kw"] = 100.0
    payload["points"][40]["sell_price_per_kwh"] = 10.0
    payload["constraints"]["grid_export_limit_kw"] = 20.0
    return OptimizationRequest.model_validate(payload)


def candidate_by_id(result: OptimizationResult, profile_id: str) -> CandidateResult:
    return next(
        candidate
        for candidate in result.candidates
        if candidate.profile_id == profile_id
    )


def sum_power(
    candidate: CandidateResult,
    mode: str,
    point_indexes: range,
) -> float:
    return sum(
        candidate.plan[index].target_power_kw
        for index in point_indexes
        if candidate.plan[index].mode == mode
    )


def no_storage_unused_pv_energy(request: OptimizationRequest) -> float:
    return sum(
        max(point.pv_forecast_kw - point.load_forecast_kw, 0.0) * 0.25
        for point in request.points
    )


def make_candidate(
    request: OptimizationRequest,
    plan: list[PlanPoint],
    metrics: CandidateMetrics,
) -> CandidateResult:
    return CandidateResult(
        profile_id="balanced",
        profile_version="test-balanced-v1",
        plan_version=f"{request.request_id}/test/balanced/test-balanced-v1",
        status="optimal",
        solver_message="test solution",
        solve_seconds=0.0,
        plan=plan,
        metrics=metrics,
        layers=[],
        risk_codes=[],
        risk_messages=[],
    )


def make_candidate_from_optimizer(request: OptimizationRequest) -> CandidateResult:
    from m4.optimizer.metrics import calculate_metrics, materialize_plan
    from m4.optimizer.model import build_model
    from m4.optimizer.solver import solve_milp

    built = build_model(request)
    solved = solve_milp(
        built.problem,
        built.objectives["throughput"],
        locks=(),
        time_limit_seconds=2.0,
        mip_rel_gap=0.0,
    )
    if solved.x is None:
        raise AssertionError(f"test fixture did not solve: {solved.message}")
    plan = materialize_plan(request, built, solved.x)
    return make_candidate(request, plan, calculate_metrics(request, plan))
