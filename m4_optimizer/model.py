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
    index = _build_variable_index()
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


def _build_variable_index() -> VariableIndex:
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
        objectives["pv_unused"][index.grid_export.start + t] = INTERVAL_HOURS
        objectives["pv_unused"][index.pv_unabsorbed.start + t] = INTERVAL_HOURS
    return objectives
