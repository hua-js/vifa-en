from datetime import datetime, timedelta

from m4_optimizer.contracts import (
    CapabilitySnapshot,
    ForecastPoint,
    ObjectiveLayer,
    ObjectiveProfile,
    OptimizationConstraints,
    OptimizationRequest,
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
            profile_version="test-v1",
            objective_order=orders[profile_id],
        )
        for profile_id in ("balanced", "cost", "pv")
    ]


def make_request(**overrides: object) -> OptimizationRequest:
    plan_start_at = datetime.fromisoformat("2026-09-04T00:00:00+08:00")
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
                load_forecast_kw=100.0,
                pv_forecast_kw=20.0,
                buy_price_per_kwh=0.8,
                sell_price_per_kwh=0.0,
            )
            for index in range(96)
        ],
        "capability": CapabilitySnapshot(
            available=True,
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
            demand_limit_kw=120.0,
            grid_import_limit_kw=150.0,
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
