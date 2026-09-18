from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
import pyomo.environ as pyo
from pyomo.repn.standard_repn import generate_standard_repn

from m4.optimizer.contracts import ObjectiveName, OptimizationRequest
from m4.optimizer.solver import PyomoProblem


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
    peak_reserve_shortfall: int | None = None
    power_variation: slice | None = None
    discharge_active: slice | None = None
    discharge_start: slice | None = None


@dataclass(frozen=True)
class BuiltModel:
    problem: PyomoProblem
    index: VariableIndex
    objectives: dict[ObjectiveName, NDArray[np.float64]]
    valley_charge_windows: tuple[tuple[int, ...], ...] = ()


def build_model(request: OptimizationRequest, *, terminal_soc_target_pct: float | None = None) -> BuiltModel:
    """Build the station rules as named Pyomo variables and constraints."""
    policies = [request.pv_policy_at(point.timestamp) for point in request.points]
    load_first = any(policy != "legacy" for policy in policies)
    horizon = request.horizon_points
    peak_reserve = request.peak_reserve_policy
    continuity = any("power_variation" in layer.terms for profile in request.profiles
                     for layer in profile.objective_order)
    discharge_starts = any("discharge_starts" in layer.terms for profile in request.profiles
                           for layer in profile.objective_order)
    index = _build_variable_index(load_first=load_first, horizon=horizon,
                                  peak_reserve=peak_reserve is not None, continuity=continuity,
                                  discharge_starts=discharge_starts)
    capability, constraints = request.capability, request.constraints
    charge_max = capability.max_charge_kw if capability.available else 0.0
    discharge_max = capability.max_discharge_kw if capability.available else 0.0
    import_max = (constraints.grid_import_limit_kw
                  if constraints.grid_import_limit_kw is not None
                  else max(p.load_forecast_kw for p in request.points) + charge_max)
    export_max = constraints.grid_export_limit_kw if constraints.grid_export_enabled else 0.0
    capacity = capability.energy_capacity_kwh
    initial_energy = capacity * capability.initial_soc_pct / 100.0
    if terminal_soc_target_pct is not None and not (
        np.isfinite(terminal_soc_target_pct)
        and constraints.soc_min_pct <= terminal_soc_target_pct <= constraints.soc_max_pct
    ):
        raise ValueError('terminal SOC target must be within safety bounds')
    terminal_energy = initial_energy if terminal_soc_target_pct is None else capacity * terminal_soc_target_pct / 100.0
    minimum_energy = capacity * constraints.soc_min_pct / 100.0
    maximum_energy = capacity * constraints.soc_max_pct / 100.0
    terminal_tolerance = capacity * constraints.terminal_soc_tolerance_pct / 100.0
    if peak_reserve is not None:
        if not constraints.preferred_soc_min_pct <= peak_reserve.terminal_soc_min_pct <= constraints.soc_max_pct:
            raise ValueError('peak reserve terminal SOC floor is outside allowed bounds')
        if terminal_soc_target_pct is not None and peak_reserve.terminal_soc_min_pct < terminal_soc_target_pct:
            raise ValueError('peak reserve terminal SOC floor cannot be below baseline target')
        if any(p.tariff_period not in ('gu', 'ping', 'feng', 'jian') for p in request.points):
            raise ValueError('peak reserve policy requires known tariff periods')
        first_peak = next((t for t, point in enumerate(request.points)
                           if point.tariff_period in ('feng', 'jian')), None)
        if first_peak is None and not request.is_remaining_day:
            raise ValueError('peak reserve policy requires at least one peak period')
    surplus = {t: max(p.pv_forecast_kw - p.load_forecast_kw, 0.0)
               for t, p in enumerate(request.points)}
    windows = _overnight_valley_windows(request)

    model = pyo.ConcreteModel(name="station_battery_dispatch")
    model.periods = pyo.RangeSet(0, horizon - 1)
    model.energy_states = pyo.RangeSet(0, horizon)
    model.pv_policy_periods = pyo.Set(initialize=range(horizon) if load_first else (), ordered=True)
    model.pv_surplus_periods = pyo.Set(initialize=[t for t in model.periods if policies[t] != "legacy" and surplus[t] > 0], ordered=True)

    def allowed_charge(t):
        return charge_max if request.ems_schedule_modes is None or request.ems_schedule_modes[t] == "charge" else 0.0

    def charge_bound(m, t):
        if policies[t] == "load_first_export_priority" and surplus[t] > 0:
            return (0.0, 0.0)
        return (0.0, min(allowed_charge(t), surplus[t]) if policies[t] != "legacy" and surplus[t] > 0 else allowed_charge(t))

    def discharge_bound(m, t):
        if request.ems_schedule_modes is not None and request.ems_schedule_modes[t] != "discharge":
            return 0.0, 0.0
        point = request.points[t]
        net_load = max(point.load_forecast_kw - point.pv_forecast_kw, 0.0)
        maximum = min(discharge_max, net_load) if policies[t] != "legacy" or peak_reserve is not None else discharge_max
        if peak_reserve is not None and peak_reserve.version != 'peak-reserve-v3' and point.tariff_period not in ('feng', 'jian'):
            grid_boundary = min(constraints.demand_limit_kw, import_max)
            maximum = min(maximum, max(net_load - grid_boundary, 0.0))
        return 0.0, maximum

    def energy_bound(m, t):
        if t == 0:
            return initial_energy, initial_energy
        if t == horizon:
            if peak_reserve is not None:
                return capacity * peak_reserve.terminal_soc_min_pct / 100.0, maximum_energy
            if terminal_soc_target_pct is not None:
                return terminal_energy, terminal_energy
            return max(minimum_energy, initial_energy - terminal_tolerance), min(maximum_energy, initial_energy + terminal_tolerance)
        return minimum_energy, maximum_energy

    # Component declaration order also matches the stable materialization index.
    model.charge = pyo.Var(model.periods, domain=pyo.NonNegativeReals, bounds=charge_bound)
    model.discharge = pyo.Var(model.periods, domain=pyo.NonNegativeReals, bounds=discharge_bound)
    model.grid_import = pyo.Var(model.periods, domain=pyo.NonNegativeReals,
                                bounds=lambda m, t: (0.0, 0.0 if policies[t] != "legacy" and surplus[t] > 0 else import_max))
    model.grid_export = pyo.Var(model.periods, domain=pyo.NonNegativeReals, bounds=(0.0, export_max))
    model.pv_unabsorbed = pyo.Var(model.periods, domain=pyo.NonNegativeReals,
                                 bounds=lambda m, t: (0.0, request.points[t].pv_forecast_kw))
    model.energy = pyo.Var(model.energy_states, domain=pyo.NonNegativeReals, bounds=energy_bound)
    model.demand_exceed = pyo.Var(model.periods, domain=pyo.NonNegativeReals, bounds=(0.0, import_max))
    model.peak_demand_exceed = pyo.Var(domain=pyo.NonNegativeReals, bounds=(0.0, import_max))
    model.charge_on = pyo.Var(model.periods, domain=pyo.Binary)
    model.discharge_on = pyo.Var(model.periods, domain=pyo.Binary)
    model.grid_import_on = pyo.Var(model.periods, domain=pyo.Binary)
    model.soc_low_deviation = pyo.Var(model.periods, domain=pyo.NonNegativeReals)
    model.soc_high_deviation = pyo.Var(model.periods, domain=pyo.NonNegativeReals)
    model.pv_curtail_on = pyo.Var(model.pv_policy_periods, domain=pyo.Binary,
                                 bounds=lambda m, t: (0, 1 if surplus[t] > 0 else 0))
    model.pv_storage_full = pyo.Var(model.pv_policy_periods, domain=pyo.Binary,
                                   bounds=lambda m, t: (0, 1 if surplus[t] > 0 else 0))
    if peak_reserve is not None:
        model.peak_reserve_energy_shortfall = pyo.Var(domain=pyo.NonNegativeReals)
        if first_peak is None:
            # A remaining-day tail can have no peak left to prepare for.
            model.peak_reserve_preparation = pyo.Constraint(
                expr=model.peak_reserve_energy_shortfall == 0.0)
        else:
            model.peak_reserve_preparation = pyo.Constraint(expr=
                model.peak_reserve_energy_shortfall + model.energy[first_peak]
                >= capacity * constraints.preferred_soc_max_pct / 100.0)
        if peak_reserve.version in ('peak-reserve-v2', 'peak-reserve-v3'):
            # Preserve the terminal reserve from the final contiguous peak block,
            # including its starting energy state and every later state.
            last_peak_start = max((t for t, point in enumerate(request.points)
                                  if point.tariff_period in ('feng', 'jian')), default=0)
            while last_peak_start > 0 and request.points[last_peak_start - 1].tariff_period in ('feng', 'jian'):
                last_peak_start -= 1
            model.terminal_reserve_states = pyo.RangeSet(last_peak_start, horizon)
            model.late_peak_terminal_reserve = pyo.Constraint(
                model.terminal_reserve_states,
                rule=lambda m, t: m.energy[t] >= capacity * peak_reserve.terminal_soc_min_pct / 100.0)

    model.power_balance = pyo.Constraint(model.periods, rule=lambda m, t:
        m.grid_import[t] + m.discharge[t] + request.points[t].pv_forecast_kw
        == request.points[t].load_forecast_kw + m.charge[t] + m.grid_export[t] + m.pv_unabsorbed[t])
    model.energy_transition = pyo.Constraint(model.periods, rule=lambda m, t:
        m.energy[t + 1] == m.energy[t] + capability.charge_efficiency * INTERVAL_HOURS * m.charge[t]
        - INTERVAL_HOURS / capability.discharge_efficiency * m.discharge[t])
    model.demand_exceedance = pyo.Constraint(model.periods, rule=lambda m, t:
        m.demand_exceed[t] >= m.grid_import[t] - constraints.demand_limit_kw)
    model.peak_exceedance = pyo.Constraint(model.periods, rule=lambda m, t:
        m.peak_demand_exceed >= m.demand_exceed[t])
    model.charge_power = pyo.Constraint(model.periods, rule=lambda m, t: m.charge[t] <= charge_max * m.charge_on[t])
    model.discharge_power = pyo.Constraint(model.periods, rule=lambda m, t: m.discharge[t] <= discharge_max * m.discharge_on[t])
    model.storage_exclusivity = pyo.Constraint(model.periods, rule=lambda m, t:
        m.charge_on[t] + m.discharge_on[t] <= (1.0 if capability.available else 0.0))
    model.grid_import_mode = pyo.Constraint(model.periods, rule=lambda m, t: m.grid_import[t] <= import_max * m.grid_import_on[t])
    model.grid_export_mode = pyo.Constraint(model.periods, rule=lambda m, t: m.grid_export[t] <= export_max * (1 - m.grid_import_on[t]))
    model.pv_available = pyo.Constraint(model.periods, rule=lambda m, t:
        m.grid_export[t] + m.pv_unabsorbed[t] <= request.points[t].pv_forecast_kw)
    model.pv_load_first = pyo.Constraint(model.pv_policy_periods, rule=lambda m, t:
        m.grid_export[t] + m.pv_unabsorbed[t] <= surplus[t]
        if policies[t] != "legacy" else pyo.Constraint.Skip)
    # Curtailment requires saturated export and either saturated charging power
    # (storage_full=0) or a full battery at the next SOC state (storage_full=1).
    model.pv_curtailment_gate = pyo.Constraint(model.pv_surplus_periods, rule=lambda m, t:
        m.pv_unabsorbed[t] <= surplus[t] * m.pv_curtail_on[t])
    model.pv_export_before_curtailment = pyo.Constraint(model.pv_surplus_periods, rule=lambda m, t:
        m.grid_export[t] >= min(surplus[t], export_max) * m.pv_curtail_on[t])
    model.pv_charge_before_curtailment = pyo.Constraint(model.pv_surplus_periods, rule=lambda m, t:
        m.charge[t] >= min(surplus[t], allowed_charge(t)) * (m.pv_curtail_on[t] - m.pv_storage_full[t]))
    model.pv_full_before_curtailment = pyo.Constraint(model.pv_surplus_periods, rule=lambda m, t:
        m.energy[t + 1] >= minimum_energy + (maximum_energy - minimum_energy) * m.pv_storage_full[t])
    # Customer preference is a physical allocation rule, independent of price.
    # In surplus periods the battery cannot discharge or import from the grid.
    # Filling the battery and hitting the charge bound are the two ways charging
    # can saturate; the existing storage_full binary encodes the former.
    model.pv_export_priority = pyo.Constraint(model.pv_surplus_periods,
        rule=lambda m, t: m.grid_export[t] == surplus[t]
        if policies[t] == 'load_first_export_priority' else pyo.Constraint.Skip)
    model.pv_priority_charge = pyo.Constraint(model.pv_surplus_periods,
        rule=lambda m, t: m.charge[t] >= min(allowed_charge(t), surplus[t])
            * (1-m.pv_storage_full[t])
        if policies[t] == 'load_first_storage_priority' else pyo.Constraint.Skip)

    model.preferred_soc_low = pyo.Constraint(model.periods, rule=lambda m, t:
        m.soc_low_deviation[t] + 100.0 / capacity * m.energy[t + 1] >= constraints.preferred_soc_min_pct)
    model.preferred_soc_high = pyo.Constraint(model.periods, rule=lambda m, t:
        m.soc_high_deviation[t] - 100.0 / capacity * m.energy[t + 1] >= -constraints.preferred_soc_max_pct)

    model.demand_peak = pyo.Expression(expr=model.peak_demand_exceed)
    model.demand_duration = pyo.Expression(expr=INTERVAL_HOURS * pyo.quicksum(model.demand_exceed.values()))
    model.soc_preferred_deviation = pyo.Expression(expr=INTERVAL_HOURS * pyo.quicksum(
        model.soc_low_deviation[t] + model.soc_high_deviation[t] for t in model.periods))
    model.energy_cost = pyo.Expression(expr=INTERVAL_HOURS * pyo.quicksum(
        request.points[t].buy_price_per_kwh * model.grid_import[t]
        - request.points[t].sell_price_per_kwh * model.grid_export[t] for t in model.periods))
    model.pv_unused = pyo.Expression(expr=INTERVAL_HOURS * pyo.quicksum(
        model.pv_unabsorbed[t] + (model.grid_export[t] if policies[t] == "legacy" else 0.0) for t in model.periods))
    model.throughput = pyo.Expression(expr=INTERVAL_HOURS * pyo.quicksum(
        model.charge[t] + model.discharge[t] for t in model.periods))
    model.valley_charge_delay = pyo.Expression(expr=pyo.quicksum(
        elapsed * INTERVAL_HOURS * INTERVAL_HOURS * model.charge[t]
        for window in windows for elapsed, t in enumerate(window)))
    if peak_reserve is not None:
        model.peak_reserve_shortfall = pyo.Expression(expr=model.peak_reserve_energy_shortfall)

    if discharge_starts:
        # Classify effective discharge at 0.01 kW without imposing a new physical
        # minimum power. Unlike discharge_on, this flag cannot bridge idle slots.
        threshold = 0.01
        model.discharge_active = pyo.Var(model.periods, domain=pyo.Binary)
        model.discharge_start = pyo.Var(model.periods, domain=pyo.Binary)
        model.discharge_active_upper = pyo.Constraint(model.periods, rule=lambda m, t:
            m.discharge[t] <= threshold + discharge_max * m.discharge_active[t])
        model.discharge_active_lower = pyo.Constraint(model.periods, rule=lambda m, t:
            m.discharge[t] >= threshold * m.discharge_active[t])
        model.discharge_start_lower = pyo.Constraint(model.periods, rule=lambda m, t:
            m.discharge_start[t] >= m.discharge_active[t]
                - (m.discharge_active[t-1] if t > 0 else 0))
        model.discharge_start_active = pyo.Constraint(model.periods, rule=lambda m, t:
            m.discharge_start[t] <= m.discharge_active[t])
        model.discharge_start_previous = pyo.Constraint(model.periods, rule=lambda m, t:
            m.discharge_start[t] <= 1 - (m.discharge_active[t-1] if t > 0 else 0))
        model.discharge_starts = pyo.Expression(expr=pyo.quicksum(model.discharge_start.values()))

    if continuity:
        model.power_edges = pyo.RangeSet(0, horizon)
        model.power_change = pyo.Var(model.power_edges, domain=pyo.NonNegativeReals)

        def power_delta(m, t):
            current = m.discharge[t] - m.charge[t] if t < horizon else 0.0
            previous = m.discharge[t-1] - m.charge[t-1] if t > 0 else 0.0
            return current - previous

        model.power_change_positive = pyo.Constraint(model.power_edges,
            rule=lambda m, t: m.power_change[t] >= power_delta(m, t))
        model.power_change_negative = pyo.Constraint(model.power_edges,
            rule=lambda m, t: m.power_change[t] >= -power_delta(m, t))
        model.power_variation = pyo.Expression(expr=pyo.quicksum(model.power_change.values()))

    variables = tuple(model.component_data_objects(pyo.Var))
    if index.size != len(variables):
        raise ValueError("model variable index does not match native solver columns")
    problem = PyomoProblem(model, variables)
    # Vectors are an export of the native expressions for existing layer locks,
    # materialization and independent validation; they do not define the model.
    objectives = _objective_vectors(model, variables)
    return BuiltModel(problem, index, objectives, windows)


def _objective_vectors(model, variables) -> dict[ObjectiveName, NDArray[np.float64]]:
    columns = {id(var): i for i, var in enumerate(variables)}
    objectives = {}
    for expression in model.component_objects(pyo.Expression):
        representation = generate_standard_repn(expression.expr)
        vector = np.zeros(len(variables), dtype=float)
        for var, coefficient in zip(representation.linear_vars, representation.linear_coefs):
            vector[columns[id(var)]] = float(coefficient)
        objectives[expression.local_name] = vector
    return objectives


def _overnight_valley_windows(
    request: OptimizationRequest,
) -> tuple[tuple[int, ...], ...]:
    """Use declared daily tariff periods, keeping dates and price blocks separate.

    Reconstruct the known midnight-connected tariff block. With 95 points a
    missing clock slot outside that block is harmless; a gap before its known
    end disables the preference rather than inferring the missing tariff.
    """
    if any(point.tariff_period is None or point.timestamp.minute % 15
           or point.timestamp.second or point.timestamp.microsecond
           for point in request.points):
        return ()
    by_slot = {point.timestamp.hour * 4 + point.timestamp.minute // 15: point
               for point in request.points}
    valley_end = next((slot for slot in range(HORIZON_POINTS)
                       if slot not in by_slot or by_slot[slot].tariff_period != "gu"), HORIZON_POINTS)
    if valley_end in (0, HORIZON_POINTS) or valley_end not in by_slot:
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


def _build_variable_index(*, load_first: bool = False, horizon: int = HORIZON_POINTS,
                          peak_reserve: bool = False, continuity: bool = False,
                          discharge_starts: bool = False) -> VariableIndex:
    cursor = 0

    def allocate(size: int) -> slice:
        nonlocal cursor
        result = slice(cursor, cursor + size)
        cursor += size
        return result

    charge = allocate(horizon)
    discharge = allocate(horizon)
    grid_import = allocate(horizon)
    grid_export = allocate(horizon)
    pv_unabsorbed = allocate(horizon)
    energy = allocate(horizon + 1)
    demand_exceed = allocate(horizon)
    peak_demand_exceed = cursor
    cursor += 1
    charge_on = allocate(horizon)
    discharge_on = allocate(horizon)
    grid_import_on = allocate(horizon)
    soc_low_deviation = allocate(horizon)
    soc_high_deviation = allocate(horizon)
    pv_curtail_on = allocate(horizon if load_first else 0)
    pv_storage_full = allocate(horizon if load_first else 0)
    peak_reserve_shortfall = cursor if peak_reserve else None
    if peak_reserve:
        cursor += 1
    discharge_active = allocate(horizon) if discharge_starts else None
    discharge_start = allocate(horizon) if discharge_starts else None
    power_variation = allocate(horizon + 1) if continuity else None
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
        peak_reserve_shortfall=peak_reserve_shortfall,
        power_variation=power_variation,
        discharge_active=discharge_active,
        discharge_start=discharge_start,
    )
