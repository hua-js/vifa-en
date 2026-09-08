from dataclasses import dataclass
from typing import Mapping

import numpy as np
from numpy.typing import NDArray
from scipy.sparse import csr_matrix

from m4_optimizer.contracts import ObjectiveName, OptimizationRequest
from m4_optimizer.solver import MilpProblem


INTERVAL_HOURS = 0.25
HORIZON_POINTS = 96


@dataclass(frozen=True)
class VariableIndex:
    charge: slice
    discharge: slice
    grid_import: slice
    grid_export: slice
    pv_unabsorbed: slice
    energy: slice
    demand_exceed: slice
    peak_demand_exceed: int
    charge_on: slice
    discharge_on: slice
    grid_import_on: slice
    soc_low_deviation: slice
    soc_high_deviation: slice
    pv_curtail_on: slice
    pv_storage_full: slice
    size: int


@dataclass(frozen=True)
class BuiltModel:
    problem: MilpProblem
    index: VariableIndex
    objectives: dict[ObjectiveName, NDArray[np.float64]]
    valley_charge_windows: tuple[tuple[int, ...], ...] = ()


class RowBuilder:
    def __init__(self, variable_count: int) -> None:
        self.variable_count = variable_count
        self.rows: list[dict[int, float]] = []
        self.lower: list[float] = []
        self.upper: list[float] = []

    def add(
        self,
        coefficients: Mapping[int, float],
        *,
        lower: float = -np.inf,
        upper: float = np.inf,
    ) -> None:
        self.rows.append(dict(coefficients))
        self.lower.append(lower)
        self.upper.append(upper)

    def build(self) -> tuple[csr_matrix, NDArray[np.float64], NDArray[np.float64]]:
        row_ids: list[int] = []
        column_ids: list[int] = []
        values: list[float] = []
        for row_id, row in enumerate(self.rows):
            for column_id, value in row.items():
                row_ids.append(row_id)
                column_ids.append(column_id)
                values.append(value)
        matrix = csr_matrix(
            (values, (row_ids, column_ids)),
            shape=(len(self.rows), self.variable_count),
            dtype=float,
        )
        return matrix, np.asarray(self.lower), np.asarray(self.upper)


def build_model(request: OptimizationRequest) -> BuiltModel:
    """Build the 96-period station battery mixed-integer linear program."""
    load_first = request.pv_dispatch_policy == "load_first_economic"
    index = _build_variable_index(load_first=load_first)
    lower_bounds = np.zeros(index.size, dtype=float)
    upper_bounds = np.full(index.size, np.inf, dtype=float)
    integrality = np.zeros(index.size, dtype=np.uint8)

    capability = request.capability
    constraints = request.constraints
    effective_max_charge_kw = capability.max_charge_kw if capability.available else 0.0
    effective_max_discharge_kw = (
        capability.max_discharge_kw if capability.available else 0.0
    )
    grid_import_big_m = (
        constraints.grid_import_limit_kw
        if constraints.grid_import_limit_kw is not None
        else max(point.load_forecast_kw for point in request.points)
        + effective_max_charge_kw
    )
    grid_export_big_m = (
        constraints.grid_export_limit_kw if constraints.grid_export_enabled else 0.0
    )
    capacity_kwh = capability.energy_capacity_kwh
    initial_energy_kwh = capacity_kwh * capability.initial_soc_pct / 100.0
    minimum_energy_kwh = capacity_kwh * constraints.soc_min_pct / 100.0
    maximum_energy_kwh = capacity_kwh * constraints.soc_max_pct / 100.0
    terminal_tolerance_kwh = (
        capacity_kwh * constraints.terminal_soc_tolerance_pct / 100.0
    )

    upper_bounds[index.charge] = effective_max_charge_kw
    upper_bounds[index.discharge] = effective_max_discharge_kw
    upper_bounds[index.grid_import] = grid_import_big_m
    upper_bounds[index.grid_export] = grid_export_big_m
    upper_bounds[index.pv_unabsorbed] = [
        point.pv_forecast_kw for point in request.points
    ]
    lower_bounds[index.energy] = minimum_energy_kwh
    upper_bounds[index.energy] = maximum_energy_kwh
    lower_bounds[index.energy.start] = initial_energy_kwh
    upper_bounds[index.energy.start] = initial_energy_kwh
    terminal_index = index.energy.stop - 1
    lower_bounds[terminal_index] = max(
        lower_bounds[terminal_index], initial_energy_kwh - terminal_tolerance_kwh
    )
    upper_bounds[terminal_index] = min(
        upper_bounds[terminal_index], initial_energy_kwh + terminal_tolerance_kwh
    )
    upper_bounds[index.demand_exceed] = grid_import_big_m
    upper_bounds[index.peak_demand_exceed] = grid_import_big_m
    upper_bounds[index.charge_on] = 1.0
    upper_bounds[index.discharge_on] = 1.0
    upper_bounds[index.grid_import_on] = 1.0
    integrality[index.charge_on] = 1
    integrality[index.discharge_on] = 1
    integrality[index.grid_import_on] = 1
    # Legacy models allocate no PV-policy binary variables.
    upper_bounds[index.pv_curtail_on] = 0.0
    upper_bounds[index.pv_storage_full] = 0.0
    integrality[index.pv_curtail_on] = 1
    integrality[index.pv_storage_full] = 1

    rows = RowBuilder(index.size)
    for t, point in enumerate(request.points):
        charge_t = index.charge.start + t
        discharge_t = index.discharge.start + t
        grid_import_t = index.grid_import.start + t
        grid_export_t = index.grid_export.start + t
        pv_unabsorbed_t = index.pv_unabsorbed.start + t
        energy_now = index.energy.start + t
        energy_next = energy_now + 1
        demand_exceed_t = index.demand_exceed.start + t
        charge_on_t = index.charge_on.start + t
        discharge_on_t = index.discharge_on.start + t
        grid_import_on_t = index.grid_import_on.start + t
        soc_low_deviation_t = index.soc_low_deviation.start + t
        soc_high_deviation_t = index.soc_high_deviation.start + t

        rows.add(
            {
                grid_import_t: 1.0,
                discharge_t: 1.0,
                charge_t: -1.0,
                grid_export_t: -1.0,
                pv_unabsorbed_t: -1.0,
            },
            lower=point.load_forecast_kw - point.pv_forecast_kw,
            upper=point.load_forecast_kw - point.pv_forecast_kw,
        )
        rows.add(
            {
                energy_next: 1.0,
                energy_now: -1.0,
                charge_t: -capability.charge_efficiency * INTERVAL_HOURS,
                discharge_t: INTERVAL_HOURS / capability.discharge_efficiency,
            },
            lower=0.0,
            upper=0.0,
        )
        rows.add(
            {demand_exceed_t: 1.0, grid_import_t: -1.0},
            lower=-constraints.demand_limit_kw,
        )
        rows.add(
            {index.peak_demand_exceed: 1.0, demand_exceed_t: -1.0},
            lower=0.0,
        )
        rows.add(
            {charge_t: 1.0, charge_on_t: -effective_max_charge_kw}, upper=0.0
        )
        rows.add(
            {discharge_t: 1.0, discharge_on_t: -effective_max_discharge_kw}, upper=0.0
        )
        rows.add(
            {charge_on_t: 1.0, discharge_on_t: 1.0},
            upper=1.0 if capability.available else 0.0,
        )
        rows.add(
            {grid_import_t: 1.0, grid_import_on_t: -grid_import_big_m}, upper=0.0
        )
        rows.add(
            {grid_export_t: 1.0, grid_import_on_t: grid_export_big_m},
            upper=grid_export_big_m,
        )
        rows.add(
            {grid_export_t: 1.0, pv_unabsorbed_t: 1.0},
            upper=point.pv_forecast_kw,
        )
        if load_first:
            surplus = max(point.pv_forecast_kw - point.load_forecast_kw, 0.0)
            upper_bounds[discharge_t] = min(
                effective_max_discharge_kw,
                max(point.load_forecast_kw - point.pv_forecast_kw, 0.0),
            )
            rows.add({grid_export_t: 1.0, pv_unabsorbed_t: 1.0}, upper=surplus)
            if surplus > 0.0:
                charge_limit = min(surplus, effective_max_charge_kw)
                export_limit = min(surplus, grid_export_big_m)
                upper_bounds[grid_import_t] = 0.0
                upper_bounds[charge_t] = charge_limit
                curtail_on = index.pv_curtail_on.start + t
                storage_full = index.pv_storage_full.start + t
                upper_bounds[curtail_on] = 1.0
                upper_bounds[storage_full] = 1.0
                # Curtail only after all permitted physical routes saturate.
                # If y=1, export is at its limit and either charging reaches
                # its power/surplus limit (z=0), or next SOC is at safety max.
                rows.add({pv_unabsorbed_t: 1.0, curtail_on: -surplus}, upper=0.0)
                rows.add({grid_export_t: 1.0, curtail_on: -export_limit}, lower=0.0)
                rows.add({charge_t: 1.0, curtail_on: -charge_limit,
                          storage_full: charge_limit}, lower=0.0)
                rows.add({energy_next: 1.0,
                          storage_full: -(maximum_energy_kwh - minimum_energy_kwh)},
                         lower=minimum_energy_kwh)
        rows.add(
            {
                soc_low_deviation_t: 1.0,
                energy_next: 100.0 / capacity_kwh,
            },
            lower=constraints.preferred_soc_min_pct,
        )
        rows.add(
            {
                soc_high_deviation_t: 1.0,
                energy_next: -100.0 / capacity_kwh,
            },
            lower=-constraints.preferred_soc_max_pct,
        )

    matrix, constraint_lower, constraint_upper = rows.build()
    problem = MilpProblem(
        integrality=integrality,
        lower_bounds=lower_bounds,
        upper_bounds=upper_bounds,
        matrix=matrix,
        constraint_lower=constraint_lower,
        constraint_upper=constraint_upper,
    )
    windows = _overnight_valley_windows(request)
    objectives = _build_objectives(request, index)
    for window in windows:
        for elapsed, t in enumerate(window):
            # Charge energy weighted by hours from this same-price window's start.
            objectives["valley_charge_delay"][index.charge.start + t] = (
                elapsed * INTERVAL_HOURS * INTERVAL_HOURS
            )
    return BuiltModel(problem=problem, index=index, objectives=objectives,
                      valley_charge_windows=windows)


def _overnight_valley_windows(
    request: OptimizationRequest,
) -> tuple[tuple[int, ...], ...]:
    """Use declared daily tariff periods, keeping dates and price blocks separate.

    A rolling 24-hour horizon includes every clock slot once. Reconstruct the
    daily schedule so a horizon starting partway through the overnight valley
    still recognizes its remaining hours. Legacy or incomplete labels are a no-op.
    """
    if any(point.tariff_period is None or point.timestamp.minute % 15
           or point.timestamp.second or point.timestamp.microsecond
           for point in request.points):
        return ()
    by_slot = {point.timestamp.hour * 4 + point.timestamp.minute // 15: point
               for point in request.points}
    if len(by_slot) != HORIZON_POINTS:
        return ()
    valley_end = next((slot for slot in range(HORIZON_POINTS)
                       if by_slot[slot].tariff_period != "gu"), HORIZON_POINTS)
    if valley_end in (0, HORIZON_POINTS):
        return ()
    block_by_slot = {}
    block = 0
    for slot in range(valley_end):
        if slot and by_slot[slot].buy_price_per_kwh != by_slot[slot - 1].buy_price_per_kwh:
            block += 1
        block_by_slot[slot] = block
    windows: dict[tuple, list[int]] = {}
    for t, point in enumerate(request.points):
        slot = point.timestamp.hour * 4 + point.timestamp.minute // 15
        if slot in block_by_slot:
            key = (point.timestamp.date(), block_by_slot[slot])
            windows.setdefault(key, []).append(t)
    return tuple(tuple(window) for window in windows.values())


def _build_variable_index(*, load_first: bool = False) -> VariableIndex:
    cursor = 0

    def allocate(size: int) -> slice:
        nonlocal cursor
        result = slice(cursor, cursor + size)
        cursor += size
        return result

    charge = allocate(HORIZON_POINTS)
    discharge = allocate(HORIZON_POINTS)
    grid_import = allocate(HORIZON_POINTS)
    grid_export = allocate(HORIZON_POINTS)
    pv_unabsorbed = allocate(HORIZON_POINTS)
    energy = allocate(HORIZON_POINTS + 1)
    demand_exceed = allocate(HORIZON_POINTS)
    peak_demand_exceed = cursor
    cursor += 1
    charge_on = allocate(HORIZON_POINTS)
    discharge_on = allocate(HORIZON_POINTS)
    grid_import_on = allocate(HORIZON_POINTS)
    soc_low_deviation = allocate(HORIZON_POINTS)
    soc_high_deviation = allocate(HORIZON_POINTS)
    pv_curtail_on = allocate(HORIZON_POINTS if load_first else 0)
    pv_storage_full = allocate(HORIZON_POINTS if load_first else 0)
    return VariableIndex(
        charge=charge,
        discharge=discharge,
        grid_import=grid_import,
        grid_export=grid_export,
        pv_unabsorbed=pv_unabsorbed,
        energy=energy,
        demand_exceed=demand_exceed,
        peak_demand_exceed=peak_demand_exceed,
        charge_on=charge_on,
        discharge_on=discharge_on,
        grid_import_on=grid_import_on,
        soc_low_deviation=soc_low_deviation,
        soc_high_deviation=soc_high_deviation,
        pv_curtail_on=pv_curtail_on,
        pv_storage_full=pv_storage_full,
        size=cursor,
    )


def _build_objectives(
    request: OptimizationRequest, index: VariableIndex
) -> dict[ObjectiveName, NDArray[np.float64]]:
    objectives: dict[ObjectiveName, NDArray[np.float64]] = {
        "demand_peak": np.zeros(index.size, dtype=float),
        "demand_duration": np.zeros(index.size, dtype=float),
        "soc_preferred_deviation": np.zeros(index.size, dtype=float),
        "energy_cost": np.zeros(index.size, dtype=float),
        "pv_unused": np.zeros(index.size, dtype=float),
        "throughput": np.zeros(index.size, dtype=float),
        "valley_charge_delay": np.zeros(index.size, dtype=float),
    }
    objectives["demand_peak"][index.peak_demand_exceed] = 1.0
    objectives["demand_duration"][index.demand_exceed] = INTERVAL_HOURS
    objectives["soc_preferred_deviation"][index.soc_low_deviation] = INTERVAL_HOURS
    objectives["soc_preferred_deviation"][index.soc_high_deviation] = INTERVAL_HOURS
    objectives["throughput"][index.charge] = INTERVAL_HOURS
    objectives["throughput"][index.discharge] = INTERVAL_HOURS

    for t, point in enumerate(request.points):
        objectives["energy_cost"][index.grid_import.start + t] = (
            point.buy_price_per_kwh * INTERVAL_HOURS
        )
        objectives["energy_cost"][index.grid_export.start + t] = (
            -point.sell_price_per_kwh * INTERVAL_HOURS
        )
        if request.pv_dispatch_policy == "legacy":
            objectives["pv_unused"][index.grid_export.start + t] = INTERVAL_HOURS
        objectives["pv_unused"][index.pv_unabsorbed.start + t] = INTERVAL_HOURS
    return objectives
