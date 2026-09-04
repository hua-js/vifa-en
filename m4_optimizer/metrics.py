"""Public-plan decoding and independently reproducible optimizer metrics."""

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from m4_optimizer.contracts import CandidateMetrics, OptimizationRequest, PlanPoint
from m4_optimizer.model import BuiltModel


INTERVAL_HOURS = 0.25
NUMERIC_TOLERANCE = 1e-9


def materialize_plan(
    request: OptimizationRequest,
    built: BuiltModel,
    x: NDArray[np.float64],
    *,
    tolerance: float = NUMERIC_TOLERANCE,
) -> list[PlanPoint]:
    """Decode a solver vector into the externally visible, unsigned plan."""
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if len(x) != built.index.size:
        raise ValueError("solution vector length must match the built model")
    if not np.isfinite(x).all():
        raise ValueError("solution vector must contain only finite values")
    if np.any(x < -tolerance):
        raise ValueError("solution vector contains significantly negative values")
    charge_values = x[built.index.charge]
    discharge_values = x[built.index.discharge]
    if np.any((charge_values > tolerance) & (discharge_values > tolerance)):
        raise ValueError("solution vector has simultaneous charge and discharge")

    capacity = request.capability.energy_capacity_kwh
    plan: list[PlanPoint] = []
    for index, source in enumerate(request.points):
        charge_kw = _zero_small(float(x[built.index.charge.start + index]), tolerance)
        discharge_kw = _zero_small(
            float(x[built.index.discharge.start + index]), tolerance
        )
        if charge_kw > tolerance:
            mode = "charge"
            target_power_kw = charge_kw
        elif discharge_kw > tolerance:
            mode = "discharge"
            target_power_kw = discharge_kw
        else:
            mode = "idle"
            target_power_kw = 0.0

        energy_kwh = float(x[built.index.energy.start + index + 1])
        expected_soc_pct = _normalize_boundary(
            energy_kwh / capacity * 100.0, 0.0, 100.0, tolerance
        )
        plan.append(
            PlanPoint(
                timestamp=source.timestamp,
                mode=mode,
                target_power_kw=target_power_kw,
                expected_soc_pct=expected_soc_pct,
                grid_import_kw=_zero_small(
                    float(x[built.index.grid_import.start + index]), tolerance
                ),
                grid_export_kw=_zero_small(
                    float(x[built.index.grid_export.start + index]), tolerance
                ),
                pv_unabsorbed_kw=_zero_small(
                    float(x[built.index.pv_unabsorbed.start + index]), tolerance
                ),
                demand_exceed_kw=max(
                    _zero_small(
                        float(x[built.index.grid_import.start + index]), tolerance
                    )
                    - request.constraints.demand_limit_kw,
                    0.0,
                ),
            )
        )
    return plan


def calculate_metrics(
    request: OptimizationRequest, plan: Sequence[PlanPoint]
) -> CandidateMetrics:
    """Recalculate all candidate metrics solely from request and public plan."""
    if len(plan) != len(request.points):
        raise ValueError("plan length must match request points")
    if not plan:
        raise ValueError("plan must not be empty")

    import_cost = sum(
        point.grid_import_kw * source.buy_price_per_kwh * INTERVAL_HOURS
        for point, source in zip(plan, request.points, strict=True)
    )
    export_revenue = sum(
        point.grid_export_kw * source.sell_price_per_kwh * INTERVAL_HOURS
        for point, source in zip(plan, request.points, strict=True)
    )
    charge_energy = sum(
        point.target_power_kw * INTERVAL_HOURS
        for point in plan
        if point.mode == "charge"
    )
    discharge_energy = sum(
        point.target_power_kw * INTERVAL_HOURS
        for point in plan
        if point.mode == "discharge"
    )
    demand_exceed_energy = sum(
        point.demand_exceed_kw * INTERVAL_HOURS for point in plan
    )
    grid_export_energy = sum(
        point.grid_export_kw * INTERVAL_HOURS for point in plan
    )
    pv_unabsorbed_energy = sum(
        point.pv_unabsorbed_kw * INTERVAL_HOURS for point in plan
    )
    pv_self_use = 0.0
    for index, (point, source) in enumerate(zip(plan, request.points, strict=True)):
        self_use_kw = (
            source.pv_forecast_kw
            - point.grid_export_kw
            - point.pv_unabsorbed_kw
        )
        if self_use_kw < -NUMERIC_TOLERANCE:
            raise ValueError(
                f"point {index}: PV-attributed flows exceed available PV"
            )
        pv_self_use += _normalize_boundary(
            self_use_kw,
            0.0,
            source.pv_forecast_kw,
            NUMERIC_TOLERANCE,
        ) * INTERVAL_HOURS
    total_pv_energy = sum(
        source.pv_forecast_kw * INTERVAL_HOURS for source in request.points
    )
    pv_self_use_rate = (
        1.0
        if total_pv_energy == 0.0
        else _normalize_boundary(
            pv_self_use / total_pv_energy, 0.0, 1.0, NUMERIC_TOLERANCE
        )
    )
    preferred_soc_deviation = sum(
        (
            max(request.constraints.preferred_soc_min_pct - point.expected_soc_pct, 0.0)
            + max(point.expected_soc_pct - request.constraints.preferred_soc_max_pct, 0.0)
        )
        * INTERVAL_HOURS
        for point in plan
    )
    throughput = charge_energy + discharge_energy

    return CandidateMetrics(
        energy_cost=import_cost - export_revenue,
        import_cost=import_cost,
        export_revenue=export_revenue,
        max_grid_import_kw=max(point.grid_import_kw for point in plan),
        peak_demand_exceed_kw=max(point.demand_exceed_kw for point in plan),
        demand_exceed_energy_kwh=demand_exceed_energy,
        pv_self_use_kwh=pv_self_use,
        pv_self_use_rate=pv_self_use_rate,
        grid_export_energy_kwh=grid_export_energy,
        pv_unabsorbed_energy_kwh=pv_unabsorbed_energy,
        charge_energy_kwh=charge_energy,
        discharge_energy_kwh=discharge_energy,
        throughput_energy_kwh=throughput,
        cycle_cost=throughput * request.constraints.cycle_cost_per_kwh,
        min_soc_pct=min(point.expected_soc_pct for point in plan),
        max_soc_pct=max(point.expected_soc_pct for point in plan),
        terminal_soc_pct=plan[-1].expected_soc_pct,
        preferred_soc_deviation=preferred_soc_deviation,
    )


def _zero_small(value: float, tolerance: float) -> float:
    return 0.0 if abs(value) <= tolerance else value


def _normalize_boundary(
    value: float, lower: float, upper: float, tolerance: float
) -> float:
    if abs(value - lower) <= tolerance:
        return lower
    if abs(value - upper) <= tolerance:
        return upper
    return value
